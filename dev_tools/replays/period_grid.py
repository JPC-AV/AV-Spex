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
periods must equal the run's `brng_analysis.analysis_periods`. The `harness` column checks
exactly that (match / expected / UNEXPECTED, see harness.py), and the summary reports it.
Trust the grid only while no tape is UNEXPECTED: that means the selection code moved on and
this replay no longer mirrors analyze().
"""
import argparse
import csv
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import find_packages  # noqa: E402
from _selection import (BIN, GT_FILE, covered, harness_status, harness_summary, in_spans,  # noqa: E402
                        load_package, select_periods)

from AV_Spex.utils.log_setup import logger

COUNTS = (1, 2, 3, 4, 5, 6, 8)
DURATIONS = (15, 30, 45, 60, 90, 120, 180)
TOP_K = 5             # top-k recall
# Damage events: runs of ground-truth frames over a threshold, merged across short gaps.
BRNG_EVENT = 0.01     # fraction of active-area pixels out of range (QCTools violation threshold)
TOUT_EVENT = 0.03     # fraction of temporal outliers (dropout/head-switching frames)
EVENT_GAP = 1.0       # seconds; runs closer than this are one event
EVENT_MIN = 0.5       # seconds; shorter runs are ignored


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
    ctx = load_package(pkg)
    name, duration, avoid = ctx.name, ctx.duration, ctx.avoid
    meta = dict(ctx.meta)
    gt_rows = ctx.gt_rows
    effective_start = ctx.effective_start
    bins = content_bins(gt_rows, avoid, effective_start, duration)
    ev_brng = events(gt_rows, 1, BRNG_EVENT, avoid, effective_start)
    ev_tout = events(gt_rows, 2, TOUT_EVENT, avoid, effective_start)
    gt_content = [r for r in gt_rows if r[0] >= effective_start and not in_spans(r[0], avoid)]

    actual = ctx.actual
    run_count = len(actual)
    run_dur = actual[0][1] if actual else None
    meta["sampled_s"] = sum(d for _, d in actual)

    scores = ctx.scores
    rows = []
    for count in counts:
        for dur in durations:
            final = select_periods(ctx, count, dur, scores=scores)
            row = {"package": name, "count": count, "duration": dur,
                   "tape_minutes": round(duration / 60, 1), "periods": ";".join(f"{s:.0f}+{d:.0f}" for s, d in final)}
            row.update(score(final, bins, ev_brng, ev_tout, gt_content))
            if count == run_count and dur == run_dur:
                status = harness_status(ctx, [(round(s), round(d)) for s, d in final])
                row["harness"], row["harness_match"] = status, int(status == "match")
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
    for pkg in find_packages(args.runs, require=GT_FILE):
        try:
            rows, meta = replay_package(pkg, counts, durations)
        except Exception as e:  # keep going; one bad package should not sink the batch
            print(f"{pkg.name}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        all_rows.extend(rows)
        timing.append(meta)
        status = [r["harness"] for r in rows if "harness" in r]
        print(f"{rows[0]['package']}: {meta['duration'] / 60:.0f} min, harness={status[0] if status else 'n/a'}")

    if not all_rows:
        sys.exit("no packages replayed")
    lead = ["package", "tape_minutes", "count", "duration", "sampled_s", "periods"]
    fields = lead + sorted({k for r in all_rows for k in r} - set(lead))
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(all_rows)

    print("\n" + harness_summary({r["package"]: r["harness"] for r in all_rows if "harness" in r}))
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
