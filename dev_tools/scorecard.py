#!/usr/bin/env python3
"""
scorecard.py: score each detector's output in a run against the operator-confirmed cases in
$AVSPEX_CORPUS/truth/.

    scorecard.py RUN                          every detector that has a truth file
    scorecard.py RUN --detector audio_dropout
    scorecard.py RUN --compare RUN_B          truth rows whose outcome differs between two runs
    scorecard.py RUN --unjudged bars          what the detector reported that no truth row covers
    scorecard.py RUN --out scorecard.csv      one row per truth row, with its outcome
    scorecard.py --check                      validate the truth files only

Truth is one CSV per detector, truth/<detector>.csv:

    video_id,tc_in,tc_out,verdict,source,note

    verdict  present   a real event: the detector should report something overlapping the span
             absent    an operator looked and there is nothing to report here (usually a past
                       false alarm): the detector should report nothing overlapping the span
    tc_in / tc_out     the file's own timecode as the NLE shows it (HH:MM:SS:FF, or ;FF for
                       drop-frame), or plain elapsed seconds (e.g. 1750 or 52.8). Both blank
                       means the whole tape: "this tape has no audible timecode at all".
    source             who confirmed it and when, or the note it came from

Outcomes: present -> hit / MISS, absent -> clean / FALSE_ALARM, and for either:
    not_run     the run has no output from that detector for the tape (sidecar missing, the
                step was off, or the detector reported it could not measure). Never counted
                as clean or as a miss: a detector that did not run found nothing either way.
    no_package  the tape is not in this run

Truth is never exhaustive, so an event no truth row covers is not a false alarm: it is
unjudged. `--unjudged DETECTOR` lists those, which is the list to review next. Each one an
operator settles becomes a new truth row, so the scorecard only gets stricter.

A span matches when a reported event overlaps it widened by --tolerance seconds (default 1.0)
on each side. For a hit, start_err / end_err are the reported event's edges minus the truth's.

To add a detector: write a function that takes an aggregate.Package and a Timing and returns
[(start_s, end_s), ...] in elapsed seconds, or None when the detector did not run, add it to
DETECTORS, and create truth/<name>.csv.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from aggregate import Package  # noqa: E402
from corpus import CORPUS, resolve_run  # noqa: E402

TRUTH = CORPUS / "truth"
COLUMNS = ("video_id", "tc_in", "tc_out", "verdict", "source", "note")
VERDICTS = ("present", "absent")
BAD = ("MISS", "FALSE_ALARM")


# -------------------------------------------------------------------- time ---

class Timing:
    """What turns a tape's timecode into elapsed seconds: frame rate, start timecode and
    drop-frame flag. The start timecode comes from labels/timecode.json (filled by
    label_sheet.py --probe and dropout_sheet.py); a tape not listed there starts at zero."""
    _cache = None

    def __init__(self, vid, fps):
        from AV_Spex.checks.qct_parse import _tc_parse_start_timecode
        if Timing._cache is None:
            try:
                Timing._cache = json.loads((CORPUS / "labels" / "timecode.json").read_text())
            except FileNotFoundError:
                Timing._cache = {}
        entry = Timing._cache.get(vid) or {}
        self.fps = fps or entry.get("fps") or 29.97
        self.known = vid in Timing._cache
        self.start_frames, self.drop = _tc_parse_start_timecode(entry.get("start_timecode"),
                                                                max(1, round(self.fps)))

    def seconds(self, text):
        """Elapsed seconds from a timecode (HH:MM:SS:FF / ;FF), a clock time with a decimal
        fraction (HH:MM:SS.mmm, as the older sidecars write) or a plain number of seconds."""
        text = str(text).strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)
        if re.fullmatch(r"\d+:\d\d:\d\d[:;]\d\d", text):
            from label_sheet import tc_to_seconds
            return tc_to_seconds(text, self.fps, self.start_frames, self.drop)
        m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d\d(?:\.\d+)?)", text)
        if m:
            return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        raise ValueError(f"unreadable time {text!r}")


# --------------------------------------------------------------- detectors ---
# Each returns [(start_s, end_s), ...] or None when the detector did not run for the tape.

def _table(path, first_header, third_header=None):
    """Rows of the table inside a sidecar that opens with summary lines: everything after the
    row whose first cell is `first_header` (and third cell `third_header`, where a sidecar
    holds two tables), up to the next blank line. None when the file is missing, [] when the
    sidecar is there but holds no such table (the detector ran and found nothing)."""
    if not path.exists():
        return None
    with open(path, newline="", errors="replace") as fh:
        rows = list(csv.reader(fh))
    for i, row in enumerate(rows):
        if row and row[0].strip() == first_header and (
                third_header is None or (len(row) > 2 and row[2].strip() == third_header)):
            out = []
            for r in rows[i + 1:]:
                if len(r) < 2 or not r[0].strip():
                    break
                out.append(r)
            return out
    return []


def _csv_spans(path, first_header, timing, start_col=0, end_col=1, third_header=None):
    rows = _table(path, first_header, third_header)
    if rows is None:
        return None
    return [(timing.seconds(r[start_col]), timing.seconds(r[end_col])) for r in rows]


def det_audio_dropout(p, t):
    return _csv_spans(p.csvs / "qct-parse_audio_dropout.csv", "Timestamp Start", t)


def det_audible_timecode(p, t):
    """The consensus regions (the table with a Duration column). The sidecar's second table
    lists raw per-method detections, which are not what the report calls detected."""
    return _csv_spans(p.csvs / "qct-parse_audible_timecode.csv", "Start Time", t,
                      third_header="Duration")


def det_tone_leak(p, t):
    return _csv_spans(p.csvs / "qct-parse_tone_leak_events.csv", "Timestamp Start", t)


def det_chroma_phase(p, t):
    return _csv_spans(p.csvs / "qct-parse_chroma_phase_events.csv", "Event", t, 1, 2)


def det_bars(p, t):
    """The consensus bars spans frame analysis excluded. Runs older than bars_regions carry
    only the head-bars end time, read as a span from zero."""
    if not p.fa:
        return None
    if "bars_regions" in p.fa:
        return [(r["start"], r["end"]) for r in p.fa["bars_regions"] or []]
    end = p.fa.get("color_bars_end_time")
    return [(0.0, end)] if end else []


def det_duplicate_frames(p, t):
    d = p.fa.get("duplicate_frame_detection")
    if not isinstance(d, dict) or d.get("verification_available") is False:
        return None
    return [(r["start_time"], r["end_time"]) for r in d.get("runs") or []]


def det_dropped_samples(p, t):
    """Splices and digital-silence gaps. Results from before the decoded-audio scan (spikes
    read off a spectrogram image) are a different detector and count as not run."""
    d = p.fa.get("dropped_sample_detection")
    if not isinstance(d, dict) or d.get("status") == "unknown" or "splice_events" not in d:
        return None
    return ([(e["time_s"], e["time_s"]) for e in d.get("splice_events") or []]
            + [(g["start_s"], g["end_s"]) for g in d.get("silence_gaps") or []])


DETECTORS = {
    "audio_dropout": det_audio_dropout,
    "audible_timecode": det_audible_timecode,
    "tone_leak": det_tone_leak,
    "chroma_phase": det_chroma_phase,
    "bars": det_bars,
    "duplicate_frames": det_duplicate_frames,
    "dropped_samples": det_dropped_samples,
}


# ------------------------------------------------------------------- truth ---

def load_truth(only=None):
    """({detector: [row, ...]}, [problem, ...]). Rows keep their file and line for messages."""
    truth, problems = {}, []
    for path in sorted(TRUTH.glob("*.csv")) if TRUTH.is_dir() else []:
        name = path.stem
        if only and name not in only:
            continue
        if name not in DETECTORS:
            problems.append(f"{path.name}: no detector named {name!r} in scorecard.DETECTORS")
            continue
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                problems.append(f"{path.name}: missing column(s) {', '.join(missing)}")
                continue
            rows = []
            for n, row in enumerate(reader, start=2):
                row = {k: (v or "").strip() for k, v in row.items() if k}
                if not any(row.values()):
                    continue
                where = f"{path.name}:{n}"
                if not row["video_id"]:
                    problems.append(f"{where}: no video_id")
                elif row["verdict"] not in VERDICTS:
                    problems.append(f"{where}: verdict {row['verdict']!r} is not present/absent")
                elif bool(row["tc_in"]) != bool(row["tc_out"]):
                    problems.append(f"{where}: give both tc_in and tc_out, or neither (whole tape)")
                elif row["verdict"] == "present" and not row["tc_in"] and name != "audible_timecode" \
                        and name != "tone_leak":
                    problems.append(f"{where}: a present row needs a span")
                else:
                    row["where"] = where
                    rows.append(row)
            truth[name] = rows
    return truth, problems


# ------------------------------------------------------------------- score ---

def packages(run):
    return {json.loads(pj.read_text())["video_id"]: pj.parent
            for pj in sorted((run / "packages").glob("*/package.json"))}


def score(run, truth, tolerance):
    """One result dict per truth row, plus {(detector, video_id): events} for --unjudged."""
    pkgs, results, reported = packages(run), [], {}
    loaded = {}
    for name, rows in truth.items():
        for row in rows:
            vid = row["video_id"]
            res = {"run": run.name, "detector": name, **{c: row[c] for c in COLUMNS},
                   "outcome": None, "matched": "", "start_err": "", "end_err": "", "where": row["where"]}
            results.append(res)
            if vid not in pkgs:
                res["outcome"] = "no_package"
                continue
            if vid not in loaded:
                pkg = Package(pkgs[vid])
                loaded[vid] = (pkg, Timing(vid, (pkg.info.get("video") or {}).get("fps")))
            pkg, timing = loaded[vid]
            if (name, vid) not in reported:
                try:
                    reported[(name, vid)] = DETECTORS[name](pkg, timing)
                except Exception as e:
                    sys.exit(f"{name} / {vid}: could not read the detector's output ({type(e).__name__}: {e})")
            events = reported[(name, vid)]
            if events is None:
                res["outcome"] = "not_run"
                continue
            try:
                span = ((timing.seconds(row["tc_in"]), timing.seconds(row["tc_out"]))
                        if row["tc_in"] else (float("-inf"), float("inf")))
            except ValueError as e:
                sys.exit(f"{row['where']}: {e}")
            row["_span"] = span
            hits = [e for e in events if e[0] <= span[1] + tolerance and e[1] >= span[0] - tolerance]
            if row["verdict"] == "present":
                res["outcome"] = "hit" if hits else "MISS"
            else:
                res["outcome"] = "FALSE_ALARM" if hits else "clean"
            if hits:
                best = max(hits, key=lambda e: min(e[1], span[1]) - max(e[0], span[0]))
                res["matched"] = f"{best[0]:.2f}-{best[1]:.2f}" + (f" (+{len(hits) - 1})" if len(hits) > 1 else "")
                if row["tc_in"]:
                    res["start_err"], res["end_err"] = round(best[0] - span[0], 2), round(best[1] - span[1], 2)
    return results, reported


def print_report(run, results):
    print(f"Scorecard: {run.name}\n")
    print(f"{'detector':<18} {'present':>9} {'absent':>9} {'not run':>8} {'no pkg':>7}")
    for name in dict.fromkeys(r["detector"] for r in results):
        rows = [r for r in results if r["detector"] == name]
        c = Counter(r["outcome"] for r in rows)
        present = f"{c['hit']}/{c['hit'] + c['MISS']}"
        absent = f"{c['clean']}/{c['clean'] + c['FALSE_ALARM']}"
        print(f"{name:<18} {present:>9} {absent:>9} {c['not_run']:>8} {c['no_package']:>7}")
    print("\n(present = real events found / scored; absent = confirmed-clean spans left alone / scored)")
    bad = [r for r in results if r["outcome"] in BAD]
    if bad:
        print()
    for r in bad:
        span = f"{r['tc_in']} - {r['tc_out']}" if r["tc_in"] else "whole tape"
        got = f"  reported {r['matched']}" if r["matched"] else ""
        print(f"  {r['outcome']:<11} {r['detector']:<17} {r['video_id']}  {span}{got}\n"
              f"              {r['note']}")
    quiet = Counter(r["detector"] for r in results if r["outcome"] == "not_run")
    if quiet:
        print("\nNot run (no output from the detector in this run): "
              + ", ".join(f"{k} x{v}" for k, v in quiet.items()))


def compare(run_a, run_b, truth, tolerance):
    a, _ = score(run_a, truth, tolerance)
    b, _ = score(run_b, truth, tolerance)
    print(f"A = {run_a.name}\nB = {run_b.name}\n")
    changed = [(x, y) for x, y in zip(a, b) if x["outcome"] != y["outcome"]]
    for x, y in changed:
        span = f"{x['tc_in']} - {x['tc_out']}" if x["tc_in"] else "whole tape"
        worse = y["outcome"] in BAD
        better = x["outcome"] in BAD and y["outcome"] in ("hit", "clean")
        mark = "WORSE " if worse else "better" if better else "      "
        print(f"  {mark} {x['detector']:<17} {x['video_id']}  {span}: {x['outcome']} -> {y['outcome']}")
    print(f"\n{len(changed)} of {len(a)} truth rows changed outcome")


def unjudged(run, truth, tolerance, name):
    if name not in DETECTORS:
        sys.exit(f"no detector named {name!r} (have: {', '.join(DETECTORS)})")
    rows = truth.get(name, [])
    spans = {}
    _, reported = score(run, {name: rows}, tolerance)
    for row in rows:
        if "_span" in row:
            spans.setdefault(row["video_id"], []).append(row["_span"])
    from AV_Spex.checks.qct_parse import _tc_format_timecode
    total = 0
    for vid, path in packages(run).items():
        pkg = Package(path)
        timing = Timing(vid, (pkg.info.get("video") or {}).get("fps"))
        events = reported.get((name, vid))
        if events is None and (name, vid) not in reported:
            events = DETECTORS[name](pkg, timing)
        if not events:
            continue
        covered = spans.get(vid, [])
        left = [e for e in sorted(set(events))
                if not any(e[0] <= s[1] + tolerance and e[1] >= s[0] - tolerance for s in covered)]
        for s, e in left:
            tc = lambda x: _tc_format_timecode(x, timing.fps, timing.start_frames, timing.drop)
            print(f"{vid},{tc(s)},{tc(e)},,,")
        total += len(left)
    print(f"{total} unjudged {name} event(s) in {run.name}; copy the rows you settle into "
          f"truth/{name}.csv and fill in verdict, source and note", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?")
    ap.add_argument("--detector", action="append", help="only this detector (repeatable)")
    ap.add_argument("--compare", metavar="RUN_B", help="show truth rows whose outcome differs in RUN_B")
    ap.add_argument("--unjudged", metavar="DETECTOR", help="list reported events no truth row covers")
    ap.add_argument("--tolerance", type=float, default=1.0, help="seconds of slack either side of a span")
    ap.add_argument("--out", help="write one CSV row per truth row")
    ap.add_argument("--check", action="store_true", help="validate the truth files and exit")
    args = ap.parse_args()

    import logging
    from AV_Spex.utils.log_setup import logger
    logger.setLevel(logging.ERROR)

    truth, problems = load_truth(args.detector)
    for p in problems:
        print(f"! {p}", file=sys.stderr)
    if problems:
        sys.exit(f"{len(problems)} problem(s) in {TRUTH}; fix them first")
    if args.check:
        for name, rows in truth.items():
            c = Counter(r["verdict"] for r in rows)
            print(f"{name:<18} {c['present']:>3} present  {c['absent']:>3} absent  "
                  f"{len({r['video_id'] for r in rows}):>3} tape(s)")
        return
    if not args.run:
        ap.error("RUN is required (see corpus.py list)")
    if not truth and not args.unjudged:
        sys.exit(f"no truth files in {TRUTH}")
    run = resolve_run(args.run)

    if args.unjudged:
        unjudged(run, truth, args.tolerance, args.unjudged)
        return
    if args.compare:
        compare(run, resolve_run(args.compare), truth, args.tolerance)
        return
    results, _ = score(run, truth, args.tolerance)
    print_report(run, results)
    if args.out:
        fields = ["run", "detector", *COLUMNS, "outcome", "matched", "start_err", "end_err"]
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(results)
        print(f"\n{len(results)} rows -> {args.out}")


if __name__ == "__main__":
    main()
