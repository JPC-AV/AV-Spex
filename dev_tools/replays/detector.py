#!/usr/bin/env python3
"""
detector.py: re-run the detectors that read the QCTools report, under the code as it is now,
from the sidecars a run kept, and score the result against the confirmed cases. Minutes, not
the hours a fresh collect takes, and it does not need the tapes.

    avspex-dev replay detector [RUN]                         every replayable detector, every tape
    avspex-dev replay detector --detector bars --truth-only  just the tapes bars has verdicts on
    avspex-dev replay detector --tapes JPC_AV_02678 -v       one tape, listing what changed

Replayable: bars, chroma_phase, audio_dropout, audible_timecode. The replay calls the real
run_qctparse() and merge_head_bars_consensus() on the stored QCTools report. CLAMS is frozen:
its bars and tone spans and its SSIM scores are read back from the run, so a change to the
CLAMS detectors themselves is not replayed. tone_leak, duplicate_frames and dropped_samples
read the video and need a collect.

The report has four parts:

    Reproduces the run?   per detector, how many tapes get the run's own events back. With the
                          code unchanged since the run this must be all of them; that is what
                          makes the rest believable. After a change, it is the tapes it moved.
    Scorecard             the truth rows on the replayed tapes, run -> replay.
    Changed truth rows    each row whose outcome moved, marked better or WORSE.
    Unjudged events       reported events no truth row covers. The scorecard cannot say whether
                          losing one is a fix or a miss: -v lists them, to review.

The replay is kept as a run-like directory, replay-<run>-<code>, under out/replays/ in the
corpus (not tracked by git), so the other tools take it wherever they take a run:

    avspex-dev score replay-<run>-<code> --detector bars
    avspex-dev sheet replay-<run>-<code> --heldout --detector bars    the held-out test form

Held-out tapes are replayed like any other (nothing is looked at by computing), but a tape
reserved for a detector in the ledger is left out of everything this command prints for that
detector. The run's saved config is used, not the one AV Spex has saved now; nothing is saved.
Running again under the same code skips tapes already replayed (--force redoes them).
"""
import argparse
import json
import os
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from multiprocessing import get_context
from pathlib import Path

HERE = Path(__file__).resolve().parent
for _p in (HERE, HERE.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _detector  # noqa: E402
import ledger  # noqa: E402
import scorecard as sc  # noqa: E402
from aggregate import Package  # noqa: E402
from corpus import CORPUS, code_state, require_sidecars, resolve_run  # noqa: E402

REPLAYS = CORPUS / "out" / "replays"


def code_tag():
    code = code_state()
    return code["commit"][:7] + (f"-d{code['src_diff_hash']}" if code["src_dirty"] else ""), code


def run_replay(run, dest, tapes, detectors, jobs, force):
    """Replay `tapes` ({video_id: package dir}) into dest/packages. Returns the failures."""
    todo = {}
    for vid, path in tapes.items():
        try:
            done = json.loads((dest / "packages" / path.name / "package.json").read_text()).get("replayed") or []
        except (OSError, ValueError):
            done = []
        if force or not set(detectors) <= set(done):
            todo[vid] = path
    print(f"{len(tapes)} tape(s): {len(tapes) - len(todo)} already replayed under this code, {len(todo)} to do")
    failed, notes = [], []
    if not todo:
        return failed, notes
    # A tape replayed for fewer detectors before is redone for the union, so none is lost
    want = {vid: sorted(set(detectors) | set(_replayed(dest, path))) for vid, path in todo.items()}
    with ProcessPoolExecutor(max_workers=min(jobs, len(todo)), mp_context=get_context("spawn")) as pool:
        futures = [pool.submit(_detector.replay_package, str(path), str(dest / "packages" / path.name),
                               str(run), want[vid]) for vid, path in todo.items()]
        for n, fut in enumerate(as_completed(futures), start=1):
            r = fut.result()
            line = f"  [{n}/{len(todo)}] {r['video_id']}  {r['seconds']:.0f}s"
            if r.get("skipped"):
                line += f"  skipped: {r['skipped']}"
            elif not r["ok"]:
                failed.append(r["video_id"])
                line += f"  FAILED: {r['error']}"
            print(line, flush=True)
            if r["no_evidence"]:
                notes.append((r["video_id"], r["no_evidence"]))
    return failed, notes


def _replayed(dest, path):
    try:
        return json.loads((dest / "packages" / path.name / "package.json").read_text()).get("replayed") or []
    except (OSError, ValueError):
        return []


def _events(name, pkg_dir):
    pkg = Package(pkg_dir)
    timing = sc.Timing.for_package(pkg)
    return sc.DETECTORS[name](pkg, timing), timing


def _same(a, b):
    if a is None or b is None:
        return a is None and b is None
    key = lambda ev: sorted((round(s, 2), round(e, 2)) for s, e in ev)
    return key(a) == key(b)


def _overlaps(ev, others, tol):
    return any(ev[0] <= o[1] + tol and ev[1] >= o[0] - tol for o in others)


def report(run, dest, tapes, detectors, tolerance, verbose):
    from AV_Spex.checks.qct_parse import _tc_format_timecode
    truth, problems = sc.load_truth(detectors)
    for p in problems:
        print(f"! {p}", file=sys.stderr)
    if problems:
        sys.exit(f"{len(problems)} problem(s) in {sc.TRUTH}; fix them first")
    new_pkgs = sc.packages(dest)
    res_run, _ = sc.score(run, truth, tolerance)
    res_new, _ = sc.score(dest, truth, tolerance)
    held = {d: ledger.reserved(d) for d in detectors}
    shown = {d: {v: p for v, p in tapes.items() if v in new_pkgs and v not in held[d]} for d in detectors}

    events, differ = {}, {d: [] for d in detectors}
    for d in detectors:
        for vid, path in shown[d].items():
            old, timing = _events(d, path)
            new, _ = _events(d, new_pkgs[vid])
            events[(d, vid)] = (old, new, timing)
            if not _same(old, new):
                differ[d].append(vid)

    print("\nReproduces the run?")
    for d in detectors:
        left_out = len(held[d] & set(tapes))
        note = f"   ({left_out} held-out tape(s) not shown)" if left_out else ""
        print(f"  {d:<18} {len(shown[d]) - len(differ[d]):>3} of {len(shown[d])} tape(s) match{note}")

    print(f"\nScorecard, truth rows on the replayed tapes       run -> replay")
    print(f"  {'detector':<18} {'present':>18} {'absent':>18}")
    pairs = [(a, b) for a, b in zip(res_run, res_new) if a["video_id"] in shown.get(a["detector"], {})]
    for d in detectors:
        ca = Counter(a["outcome"] for a, _ in pairs if a["detector"] == d)
        cb = Counter(b["outcome"] for _, b in pairs if b["detector"] == d)
        present = f"{ca['hit']}/{ca['hit'] + ca['MISS']} -> {cb['hit']}/{cb['hit'] + cb['MISS']}"
        absent = (f"{ca['clean']}/{ca['clean'] + ca['FALSE_ALARM']} -> "
                  f"{cb['clean']}/{cb['clean'] + cb['FALSE_ALARM']}")
        quiet = f"   not run in the replay: {cb['not_run']}" if cb["not_run"] else ""
        print(f"  {d:<18} {present:>18} {absent:>18}{quiet}")
    print("  (present = real events found / scored; absent = confirmed-clean spans left alone / scored)")

    changed = [(a, b) for a, b in pairs if a["outcome"] != b["outcome"]]
    if changed:
        print("\nChanged truth rows")
    for a, b in changed:
        span = f"{a['tc_in']} - {a['tc_out']}" if a["tc_in"] else "whole tape"
        mark = "WORSE " if b["outcome"] in sc.BAD else "better" if a["outcome"] in sc.BAD else "      "
        print(f"  {mark} {a['detector']:<17} {a['video_id']}  {span}: {a['outcome']} -> {b['outcome']}")

    print("\nUnjudged events (no truth row covers them)        run -> replay")
    listing = []
    for d in detectors:
        spans = {}
        for row in truth.get(d, []):
            if "_span" in row:
                spans.setdefault(row["video_id"], []).append(row["_span"])
        n_old = n_new = n_gone = n_added = 0
        for vid in shown[d]:
            old, new, timing = events[(d, vid)]
            covered = spans.get(vid, [])
            old_u = [e for e in sorted(set(old or [])) if not _overlaps(e, covered, tolerance)]
            new_u = [e for e in sorted(set(new or [])) if not _overlaps(e, covered, tolerance)]
            gone = [e for e in old_u if not _overlaps(e, new or [], tolerance)]
            added = [e for e in new_u if not _overlaps(e, old or [], tolerance)]
            n_old, n_new, n_gone, n_added = n_old + len(old_u), n_new + len(new_u), n_gone + len(gone), n_added + len(added)
            tc = lambda x: _tc_format_timecode(x, timing.fps, timing.start_frames, timing.drop)
            listing += [(d, vid, "gone", tc(s), tc(e), e - s) for s, e in gone]
            listing += [(d, vid, "new ", tc(s), tc(e), e - s) for s, e in added]
        print(f"  {d:<18} {n_old:>4} -> {n_new:<4}  {n_gone} no longer reported, {n_added} new")
    if listing and verbose:
        print()
        for d, vid, what, tc_in, tc_out, secs in listing:
            print(f"  {what} {d:<17} {vid}  {tc_in} - {tc_out}  ({secs:.1f}s)")
    elif listing:
        print("  (-v lists them; an unjudged event that is no longer reported may be a fix or a miss)")
    for d in detectors:
        if differ[d] and verbose:
            print(f"\n  {d}: tapes that differ from the run: {', '.join(sorted(differ[d]))}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run", nargs="?", help="run ID, prefix or 'latest' (default)")
    ap.add_argument("--detector", action="append", choices=sorted(_detector.REPLAYABLE) + list(_detector.NEEDS_VIDEO),
                    metavar="D", help=f"only this detector (repeatable): {', '.join(_detector.REPLAYABLE)}")
    ap.add_argument("--truth-only", action="store_true",
                    help="only the tapes with a verdict for one of the detectors (the fast loop while tuning)")
    ap.add_argument("--tapes", nargs="+", metavar="ID", help="only these tapes")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 2), help="tapes at once")
    ap.add_argument("--force", action="store_true", help="replay again tapes already replayed under this code")
    ap.add_argument("--tolerance", type=float, default=1.0, help="seconds of slack either side of a span")
    ap.add_argument("-v", "--verbose", action="store_true", help="list the events and tapes that changed")
    args = ap.parse_args()

    detectors = args.detector or list(_detector.REPLAYABLE)
    cannot = [d for d in detectors if d in _detector.NEEDS_VIDEO]
    if cannot:
        sys.exit(f"{', '.join(cannot)}: reads the video, not the QCTools report, so it cannot be replayed "
                 f"from sidecars. Change the code and collect again (avspex-dev collect).")

    import logging
    from AV_Spex.utils.log_setup import logger
    logger.setLevel(logging.ERROR)

    run = resolve_run(args.run or "latest")
    require_sidecars(run)
    if (run / "run.json").exists() and json.loads((run / "run.json").read_text()).get("replay_of"):
        sys.exit(f"{run.name} is itself a replay; give the run it was replayed from")
    tapes = sc.packages(run)
    if args.tapes:
        unknown = [t for t in args.tapes if t not in tapes]
        if unknown:
            sys.exit(f"not in {run.name}: {', '.join(unknown)}")
        tapes = {v: tapes[v] for v in args.tapes}
    if args.truth_only:
        truth, _ = sc.load_truth(detectors)
        judged = {r["video_id"] for rows in truth.values() for r in rows}
        tapes = {v: p for v, p in tapes.items() if v in judged}
    if not tapes:
        sys.exit("no tapes to replay")

    tag, code = code_tag()
    dest = REPLAYS / f"replay-{run.name}-{tag}"
    (dest / "packages").mkdir(parents=True, exist_ok=True)
    (dest / "run.json").write_text(json.dumps({
        "run_id": dest.name, "replay_of": run.name, "commit": code["commit"], "branch": code["branch"],
        "src_dirty": code["src_dirty"], "src_diff_hash": code["src_diff_hash"],
        "created": datetime.now().isoformat(timespec="seconds"),
        "labels": [f"replay of {run.name} under {tag}"]}, indent=2))
    was = json.loads((run / "run.json").read_text())
    same_code = was.get("commit") == code["commit"] and not code["src_dirty"] and not was.get("src_dirty")
    print(f"Replay of {run.name} under {tag}" + (" (the code the run was collected with)" if same_code else "")
          + f"\ndetectors: {', '.join(detectors)}")

    failed, notes = run_replay(run, dest, tapes, detectors, max(1, args.jobs), args.force)
    for vid, spans in notes:
        print(f"! {vid}: the bars arbiter had no recorded SSIM scores for "
              f"{', '.join(f'{lo}-{hi}s' for lo, hi in spans)} and the video is not mounted; "
              f"those spans were kept as bars (no evidence). Mount the drive and --force this tape.")
    report(run, dest, tapes, detectors, args.tolerance, args.verbose)
    if failed:
        print(f"\n{len(failed)} tape(s) failed to replay and are not in the figures above: {', '.join(sorted(failed))}")
    print(f"\nKept as {dest.name}  ({dest})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
