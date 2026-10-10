#!/usr/bin/env python3
"""
period_grid.py: score frame-analysis period settings (count x duration) offline.

For each corpus package collected with ground truth (collect.sh -g), it replays AV Spex's
real period selection for every grid point:
  - stage 0: suitability check and composite score
  - stage 1: candidates
  - stage 2: placement
  - stage 3: signalstats refinement
It then scores the chosen periods against the whole-file active-area signalstats pass. No
video is decoded: everything comes from the QCTools report and the package's sidecars.

    python3 dev_tools/replays/period_grid.py RUN [RUN ...] [--out grid.csv]
    python3 dev_tools/replays/period_grid.py period-study-2026-10 --counts 3,6,8 --durations 30,60

RUN is a run ID (or unique prefix), a run directory, or a single corpus package directory.
Packages without ground truth are skipped. Results: one CSV row per (package, count, duration),
plus pivot tables of the cross-package medians printed to stdout.

Stage 3 uses the ground truth as the active-area measurement. That is the same ffprobe
signalstats over the same crop the real run used, so at the run's own settings the replayed
periods must equal the run's `brng_analysis.analysis_periods`. The `harness_match` column
checks exactly that, and the summary reports it. Trust the grid only while it holds: a
mismatch means the selection code moved on and this replay no longer mirrors analyze().
"""
import argparse
import bisect
import csv
import gzip
import json
import logging
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from corpus import resolve_run  # noqa: E402

from AV_Spex.utils.log_setup import logger
from AV_Spex.checks import bin_scoring, bin_suitability
from AV_Spex.checks.frame_analysis import (
    EnhancedFrameAnalysis, IntegratedSignalstatsAnalyzer, QCToolsParser,
    content_start_after_bars, merge_avoid_segments,
)

COUNTS = (1, 2, 3, 4, 5, 6, 8)
DURATIONS = (15, 30, 45, 60, 90, 120, 180)
BIN = 10.0            # seconds, same as the QCTools bin profiles
TOP_K = 5             # top-k recall
# Damage events: runs of ground-truth frames over a threshold, merged across short gaps.
BRNG_EVENT = 0.01     # fraction of active-area pixels out of range (QCTools violation threshold)
TOUT_EVENT = 0.03     # fraction of temporal outliers (dropout/head-switching frames)
EVENT_GAP = 1.0       # seconds; runs closer than this are one event
EVENT_MIN = 0.5       # seconds; shorter runs are ignored


GT_FILE = "ground_truth/active_signalstats.txt.gz"


def find_packages(args):
    """Corpus package directories carrying ground truth, from run IDs or paths."""
    pkgs = []
    for arg in args:
        p = Path(arg).expanduser()
        if (p / "package.json").exists():
            pkgs.append(p)
            continue
        run = resolve_run(arg)
        found = sorted(pj.parent for pj in (run / "packages").glob("*/package.json"))
        missing = [d.name for d in found if not (d / GT_FILE).exists()]
        if missing:
            print(f"{run.name}: {len(missing)} package(s) without ground truth skipped "
                  f"({', '.join(missing)})", file=sys.stderr)
        pkgs.extend(d for d in found if (d / GT_FILE).exists())
    return pkgs


def load_ground_truth(path):
    """Per-frame active-area signalstats, sorted by time."""
    rows = []
    with gzip.open(path, "rt") as fh:
        for line in fh:
            rec = {}
            for part in line.strip().split("|"):
                key, _, val = part.partition("=")
                rec[key.rsplit(".", 1)[-1]] = val
            try:
                rows.append((float(rec["pts_time"]), float(rec.get("BRNG", 0) or 0),
                             float(rec.get("TOUT", 0) or 0), float(rec.get("VREP", 0) or 0)))
            except (KeyError, ValueError):
                continue
    rows.sort()
    return rows


def full_frame_brng(xml_path, parser):
    """(time, brng) for every non-black video frame in the QCTools report — what
    parse_brng_period reads per period, collected in one walk."""
    out = []
    opener = gzip.open if str(xml_path).endswith(".gz") else open
    with opener(xml_path, "rt") as fh:
        it = iter(ET.iterparse(fh, events=["start", "end"]))
        _, root = next(it)
        for event, elem in it:
            if event != "end" or elem.tag != "frame":
                continue
            ts = elem.get("pkt_pts_time")
            if ts and not parser._is_black_frame(elem):
                tag = elem.find('.//tag[@key="lavfi.signalstats.BRNG"]')
                if tag is not None and tag.get("value"):
                    out.append((float(ts), float(tag.get("value"))))
            elem.clear()
            root.clear()
    out.sort()
    return out


def window(rows, start, end, inclusive_end=False):
    times = [r[0] for r in rows] if not hasattr(rows, "times") else rows.times
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end) if inclusive_end else bisect.bisect_left(times, end)
    return rows[lo:hi]


class Rows(list):
    def __init__(self, rows):
        super().__init__(rows)
        self.times = [r[0] for r in rows]


def simulate_comparison(periods, ff_rows, gt_rows):
    """comparison_results as analyze_with_signalstats builds them, from the two frame lists."""
    comps = []
    for start, dur in periods:
        comp = {}
        ff = window(ff_rows, start, start + dur, inclusive_end=True)
        gt = window(gt_rows, start, start + dur)
        if ff and gt:
            full = sum(1 for _, b in ff if b > 0) / len(ff) * 100
            active = sum(1 for r in gt if r[1] > 0) / len(gt) * 100
            comp["ffprobe_active_area"] = {"violations_pct": active}
            if full > active + 5 and active < 30:
                comp["diagnosis"] = "border_violations"
            elif active > 10:
                comp["diagnosis"] = "content_violations"
            else:
                comp["diagnosis"] = "minimal_violations"
        comps.append(comp)
    return SimpleNamespace(comparison_results=comps)


def in_spans(t, spans):
    return any(s <= t < e for s, e in spans)


def content_bins(gt_rows, avoid, effective_start, duration):
    """10 s bins of analyzable content with per-bin ground-truth summaries."""
    bins = {}
    for t, brng, tout, vrep in gt_rows:
        if t < effective_start or t >= duration or in_spans(t, avoid):
            continue
        b = bins.setdefault(int(t // BIN) * BIN, [0, 0.0, 0.0, 0.0, 0.0])
        b[0] += 1
        b[1] += brng
        b[2] += tout
        b[3] += vrep
        b[4] = max(b[4], brng)
    return {start: {"brng": v[1] / v[0], "tout": v[2] / v[0], "vrep": v[3] / v[0], "brng_max": v[4]}
            for start, v in bins.items() if v[0] >= 0.5 * BIN * 25}  # mostly-content bins only


def events(gt_rows, idx, threshold, avoid, effective_start):
    runs, cur = [], None
    for r in gt_rows:
        t = r[0]
        if t < effective_start or in_spans(t, avoid) or r[idx] <= threshold:
            continue
        if cur and t - cur[1] <= EVENT_GAP:
            cur[1] = t
        else:
            if cur:
                runs.append(cur)
            cur = [t, t]
    if cur:
        runs.append(cur)
    return [(s, e) for s, e in runs if e - s >= EVENT_MIN]


def covered(bin_start, periods):
    """A bin counts as sampled when a period covers at least half of it."""
    return any(min(bin_start + BIN, s + d) - max(bin_start, s) >= BIN / 2 for s, d in periods)


def touches(span, periods):
    return any(span[0] < s + d and span[1] >= s for s, d in periods)


def score(periods, bins, ev_brng, ev_tout, gt_content):
    out = {}
    for metric in ("brng", "tout", "vrep"):
        ranked = sorted(bins, key=lambda b: -bins[b][metric])
        ranked = [b for b in ranked if bins[b][metric] > 0]
        out[f"hit_{metric}"] = int(covered(ranked[0], periods)) if ranked else None
        top = ranked[:TOP_K]
        out[f"top{TOP_K}_{metric}"] = (sum(covered(b, periods) for b in top) / len(top)) if top else None
    for name, evs in (("brng", ev_brng), ("tout", ev_tout)):
        out[f"events_{name}"] = len(evs)
        out[f"event_recall_{name}"] = (sum(touches(e, periods) for e in evs) / len(evs)) if evs else None
    if gt_content:
        sampled = [r for r in gt_content if any(s <= r[0] < s + d for s, d in periods)]
        for name, idx in (("brng", 1), ("tout", 2)):
            whole = max(r[idx] for r in gt_content)
            got = max((r[idx] for r in sampled), default=0.0)
            out[f"worst_frame_{name}"] = (got / whole) if whole > 0 else None
    out["sampled_s"] = sum(d for _, d in periods)
    return out


def replay_package(pkg, counts, durations):
    info = json.loads((pkg / "package.json").read_text())
    name = info["video_id"]
    qc = pkg / f"{name}_qc_metadata"
    fa = json.load(open(qc / f"{name}_enhanced_frame_analysis.json"))
    meta = dict(info.get("video") or {}, frame_analysis_seconds=info.get("frame_analysis_seconds"))
    if not info.get("qctools_report"):
        raise FileNotFoundError("no QCTools report")
    xml = pkg / info["qctools_report"]

    bars_end = fa.get("color_bars_end_time") or 0
    bars_regions = [(r["start"], r["end"]) for r in fa.get("bars_regions") or []]
    hints = [tuple(h) for h in (fa.get("initial_borders") or {}).get("quality_frame_hints") or []]
    duration = meta["duration"]

    # Stage 0 / 1 inputs, exactly as EnhancedFrameAnalysis.analyze() builds them
    black = QCToolsParser(str(xml)).detect_black_segments(min_duration=2.0)
    avoid = merge_avoid_segments(black, bars_regions)
    parser = QCToolsParser(str(xml))
    violations = parser.parse_for_violations_streaming(
        max_frames=100, skip_color_bars=True, color_bars_end_time=bars_end,
        exclude_regions=bars_regions)
    suitability = bin_suitability.assess_bins(getattr(parser, "bin_profiles", {}),
                                              bit_depth_10=parser.bit_depth_10)
    if suitability.unsuitable_regions:
        avoid = merge_avoid_segments(avoid, suitability.unsuitable_regions)
    scores = bin_scoring.score_bins(getattr(parser, "bin_profiles", {}), suitability.verdicts,
                                    bit_depth_10=parser.bit_depth_10)

    ff_rows = Rows(full_frame_brng(xml, parser))
    gt_rows = Rows(load_ground_truth(pkg / GT_FILE))

    ea = EnhancedFrameAnalysis.__new__(EnhancedFrameAnalysis)
    ss = IntegratedSignalstatsAnalyzer.__new__(IntegratedSignalstatsAnalyzer)
    ss.duration, ss.fps, ss.qctools_report = duration, meta.get("fps"), str(xml)
    ss.last_resort_period_note = None
    ea.signalstats_analyzer = ss

    effective_start = content_start_after_bars(bars_end)
    bins = content_bins(gt_rows, avoid, effective_start, duration)
    ev_brng = events(gt_rows, 1, BRNG_EVENT, avoid, effective_start)
    ev_tout = events(gt_rows, 2, TOUT_EVENT, avoid, effective_start)
    gt_content = [r for r in gt_rows if r[0] >= effective_start and not in_spans(r[0], avoid)]

    actual = [(round(s), round(d)) for s, d in (fa.get("brng_analysis") or {}).get("analysis_periods") or []]
    run_count = len(actual)
    run_dur = actual[0][1] if actual else None
    meta["sampled_s"] = sum(d for _, d in actual)

    rows = []
    for count in counts:
        for dur in durations:
            ss.last_resort_period_note = None
            cands = []
            if violations or scores:
                cands = ea._analyze_qctools_violation_distribution(
                    violations, num_periods=count, period_duration=dur, video_duration=duration,
                    black_segments=avoid, histogram=getattr(parser, "violation_histogram", None),
                    severity=getattr(parser, "violation_severity", None), bin_scores=scores,
                    content_start=content_start_after_bars(bars_end))
            stage2 = ss._find_analysis_periods(0, bars_end, dur, count, hints or None,
                                               qctools_periods=cands, black_segments=avoid)
            final = stage2
            if stage2 and cands:
                final = ea._refine_periods_from_signalstats(
                    stage2, simulate_comparison(stage2, ff_rows, gt_rows), cands, avoid, dur, bars_end)
            final = sorted(final)
            row = {"package": name, "count": count, "duration": dur,
                   "tape_minutes": round(duration / 60, 1), "periods": ";".join(f"{s:.0f}+{d:.0f}" for s, d in final)}
            row.update(score(final, bins, ev_brng, ev_tout, gt_content))
            if count == run_count and dur == run_dur:
                row["harness_match"] = int([(round(s), round(d)) for s, d in final] == actual)
            rows.append(row)
    return rows, meta


def pivot(rows, field, counts, durations):
    lines = [f"\n{field} (median across packages)", "count\\dur " + "".join(f"{d:>7}" for d in durations)]
    for c in counts:
        cells = []
        for d in durations:
            vals = [r[field] for r in rows if r["count"] == c and r["duration"] == d and r.get(field) is not None]
            cells.append(f"{statistics.median(vals):7.2f}" if vals else "      -")
        lines.append(f"{c:>9} " + "".join(cells))
    return "\n".join(lines)


def main():
    global BRNG_EVENT, TOUT_EVENT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run IDs, run directories or corpus package directories")
    ap.add_argument("--out", default="period_grid.csv")
    ap.add_argument("--counts", default=",".join(map(str, COUNTS)))
    ap.add_argument("--durations", default=",".join(map(str, DURATIONS)))
    ap.add_argument("--brng-event", type=float, default=BRNG_EVENT,
                    help="active-area BRNG fraction that marks a damage frame (default %(default)s)")
    ap.add_argument("--tout-event", type=float, default=TOUT_EVENT,
                    help="TOUT fraction that marks a damage frame (default %(default)s)")
    args = ap.parse_args()
    BRNG_EVENT, TOUT_EVENT = args.brng_event, args.tout_event
    counts = [int(x) for x in args.counts.split(",")]
    durations = [int(x) for x in args.durations.split(",")]
    logger.setLevel(logging.ERROR)  # selection code logs every placement decision

    all_rows, timing = [], []
    for pkg in find_packages(args.runs):
        try:
            rows, meta = replay_package(pkg, counts, durations)
        except Exception as e:  # keep going; one bad package should not sink the batch
            print(f"{pkg.name}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        all_rows.extend(rows)
        timing.append(meta)
        match = [r.get("harness_match") for r in rows if "harness_match" in r]
        print(f"{rows[0]['package']}: {meta['duration'] / 60:.0f} min, harness_match={match[0] if match else 'n/a'}")

    if not all_rows:
        sys.exit("no packages replayed")
    lead = ["package", "tape_minutes", "count", "duration", "sampled_s", "periods"]
    fields = lead + sorted({k for r in all_rows for k in r} - set(lead))
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    checks = [r["harness_match"] for r in all_rows if "harness_match" in r]
    print(f"\nHarness check: {sum(checks)}/{len(checks)} packages reproduce the run's periods")
    for r in all_rows:
        r["hit_mean"] = statistics.mean([r[k] for k in ("hit_brng", "hit_tout", "hit_vrep") if r.get(k) is not None] or [0])
    for field in ("hit_mean", f"top{TOP_K}_tout", f"top{TOP_K}_brng", "event_recall_tout", "event_recall_brng",
                  "worst_frame_brng", "worst_frame_tout"):
        print(pivot(all_rows, field, counts, durations))

    # Cost: frame-analysis seconds per sampled period-second, from each run at its own settings
    per_s = [m["frame_analysis_seconds"] / m["sampled_s"] for m in timing
             if m.get("frame_analysis_seconds") and m.get("sampled_s")]
    if per_s:
        print(f"\nFrame analysis ≈ {statistics.median(per_s):.2f} s per sampled period-second "
              f"(median of {len(per_s)} runs at their own settings; includes fixed overhead)")
    print(f"\nWrote {len(all_rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
