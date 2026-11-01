#!/usr/bin/env python3
"""
bin_table.py: one row per 10 s bin of every corpus package, carrying every QCTools tag.

The table is the raw material for deciding which tags period selection should score. Each row
has the bin's statistics (`<tag>_{mean,p95,max,min}` over non-black video frames) for every
numeric tag the report carries, whether period selection may use the bin and why not, its
composite score and family scores today, whether today's selection (replayed at --count x
--duration, default the app's 6 x 30) covers it, and the bin's whole-file active-area ground
truth when the package has it.

    python3 dev_tools/replays/bin_table.py RUN [RUN ...] [--out bins.csv]
    python3 dev_tools/replays/bin_table.py period-study-2026-10 --screen [--screen-out screen.csv]

--screen adds the unsupervised screen: for every tag statistic and direction, across the
analyzable bins of every package,

  n_pkgs        packages whose report carries it
  zero_share    median share of bins at exactly the file's minimum (rank 0)
  spread        median p90/p50 of the values (flat signals cannot target anything)
  rho_scored    median within-file |Spearman| against the most similar metric scored today
  rho_yavg      median within-file Spearman against mean luma (a content/brightness proxy)
  rho_ydif      median within-file Spearman against mean YDIF (motion, also content: the reason
                YDIF itself is unscored)
  top_cur_rank  median within-file rank, under today's composite score, of the bin where the tag
                peaks (in that direction). Low = today's scoring ignores where this tag points
  top_covered   share of packages whose peak bin today's periods cover (chance is the coverage
                share printed above the table)

and flags CONSTANT, FLAT, REDUNDANT, CONTENT and SCORED (already in bin_scoring.METRICS). Nothing here
says a tag finds damage. That needs labels (label_sheet.py, then tag_weights.py); the screen
only decides which tags are worth labeling for.
"""
import argparse
import csv
import logging
import statistics
import sys
import warnings
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import resolve_run  # noqa: E402
from _selection import covered, in_spans, load_package, select_periods, window  # noqa: E402

from AV_Spex.checks import bin_scoring
from AV_Spex.checks.qctools_bin_profile import PROFILE_BIN_SIZE
from AV_Spex.utils.log_setup import logger

BIN = PROFILE_BIN_SIZE
FLAT_SPREAD = 1.1        # p90/p50 below this, with few zero bins: no targeting value
REDUNDANT_RHO = 0.8      # |rho| against a scored metric above this: adds nothing new
CONTENT_RHO = 0.6        # |rho| against mean luma above this: tracks brightness, not damage
SCORED_FIELDS = tuple(s.field for s in bin_scoring.METRICS)


def source_group(info):
    """Which collection a package came from, for the hold-out split."""
    src = (info.get("source") or "").lower()
    for group in ("george_blood", "media_burn", "jpc", "lc"):
        if f"/{group}" in src:
            return group
    return "other"


def find_packages(args):
    pkgs = []
    for arg in args:
        p = Path(arg).expanduser()
        if (p / "package.json").exists():
            pkgs.append(p)
            continue
        run = resolve_run(arg)
        pkgs.extend(sorted(pj.parent for pj in (run / "packages").glob("*/package.json")))
    return pkgs


def avoid_reason(ctx, b):
    """Why period selection may not use bin b, or '' when it may."""
    mid = b + BIN / 2
    if mid < ctx.effective_start:
        return "before_content"
    if mid >= ctx.duration:
        return "past_end"
    if in_spans(mid, ctx.bars_regions):
        return "bars"
    if any(s <= mid < e for s, e in ctx.black):
        return "black"
    verdict = ctx.suitability.verdicts.get(b)
    if verdict is not None and not verdict.suitable:
        return "unanalyzable:" + "+".join(verdict.reasons)
    if in_spans(mid, ctx.avoid):
        return "avoid"
    return ""


def package_rows(ctx, count, dur):
    scores = ctx.scores
    periods = select_periods(ctx, count, dur, scores=scores)
    evidence = {s.bin_start for p in periods
                for s in bin_scoring.evidence_within(p[0], p[1], scores)}
    group = source_group(ctx.info)
    rows = []
    for b in sorted(set(ctx.tag_bins) | set(ctx.profiles)):
        reason = avoid_reason(ctx, b)
        sc = scores.get(b)
        row = {"package": ctx.name, "group": group, "bin_start": b,
               "analyzable": int(sc is not None and not reason), "avoid_reason": reason,
               "score": round(sc.score, 4) if sc else None,
               "dominant_family": sc.dominant_family if sc else None,
               "in_periods": int(covered(b, periods)), "evidence": int(b in evidence)}
        for fam in bin_scoring.FAMILY_WEIGHTS:
            row[f"family_{fam}"] = round(sc.family_scores.get(fam, 0.0), 4) if sc else None
        profile = ctx.profiles.get(b)
        for f in SCORED_FIELDS + ("yavg_mean", "ydif_mean"):
            row[f"bp_{f}"] = getattr(profile, f, None) if profile else None
        if ctx.gt_rows is not None:
            gt = window(ctx.gt_rows, b, b + BIN)
            if gt:
                row["gt_brng"] = sum(r[1] for r in gt) / len(gt)
                row["gt_tout"] = sum(r[2] for r in gt) / len(gt)
                row["gt_vrep"] = sum(r[3] for r in gt) / len(gt)
        row.update(ctx.tag_bins.get(b, {}))
        rows.append(row)
    return rows, periods


# --- screen ----------------------------------------------------------------------------------

def _rank0(values):
    """Share of values strictly below each one (bin_scoring's tie-downward rank)."""
    a = np.asarray(values, dtype=float)
    order = np.sort(a)
    return np.searchsorted(order, a, side="left") / max(len(a) - 1, 1)


def _rho(x, y):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        r = spearmanr(x, y).statistic
    return None if r is None or np.isnan(r) else float(r)


def screen(pkg_rows, pkg_periods, tag_columns):
    out = []
    for col in tag_columns:
        per_pkg = []
        for name, rows in pkg_rows.items():
            usable = [r for r in rows if r["analyzable"] and r.get(col) is not None]
            if len(usable) < 5:
                continue
            vals = np.array([r[col] for r in usable], dtype=float)
            scored = {f: np.array([r.get(f"bp_{f}") if r.get(f"bp_{f}") is not None else np.nan
                                   for r in usable], dtype=float) for f in SCORED_FIELDS}
            rhos = {}
            for f, arr in scored.items():
                ok = ~np.isnan(arr)
                if ok.sum() >= 5:
                    r = _rho(vals[ok], arr[ok])
                    if r is not None:
                        rhos[f] = abs(r)
            yavg = np.array([r.get("bp_yavg_mean") or np.nan for r in usable], dtype=float)
            ok = ~np.isnan(yavg)
            ydif = np.array([r.get("bp_ydif_mean") or np.nan for r in usable], dtype=float)
            okd = ~np.isnan(ydif)
            per_pkg.append({
                "vals": vals, "bins": [r["bin_start"] for r in usable], "periods": pkg_periods[name],
                "cur_rank": _rank0([r["score"] or 0.0 for r in usable]),
                "rhos": rhos, "rho_yavg": _rho(vals[ok], yavg[ok]) if ok.sum() >= 5 else None,
                "rho_ydif": _rho(vals[okd], ydif[okd]) if okd.sum() >= 5 else None})
        if not per_pkg:
            continue
        for direction in ("+", "-"):
            zero_share, spread, covered_top, top_rank = [], [], [], []
            for p in per_pkg:
                v = p["vals"] if direction == "+" else -p["vals"]
                zero_share.append(float(np.mean(_rank0(v) == 0)))
                lo, med, hi = np.percentile(np.abs(v), [10, 50, 90])
                if direction == "+":
                    spread.append(hi / med if med > 0 else float("inf"))
                else:   # a low-is-bad signal: how far its bottom decile drops below typical
                    spread.append(med / lo if lo > 0 else float("inf"))
                i = int(np.argmax(v))
                covered_top.append(int(covered(p["bins"][i], p["periods"])))
                top_rank.append(float(p["cur_rank"][i]))
            best = {}
            for p in per_pkg:
                for f, r in p["rhos"].items():
                    best.setdefault(f, []).append(r)
            rho_by_field = {f: statistics.median(v) for f, v in best.items()}
            nearest = max(rho_by_field, key=rho_by_field.get) if rho_by_field else None
            rho_scored = rho_by_field.get(nearest)
            rho_yavg = [p["rho_yavg"] for p in per_pkg if p["rho_yavg"] is not None]
            rho_ydif = [p["rho_ydif"] for p in per_pkg if p["rho_ydif"] is not None]
            row = {
                "tag": col, "dir": direction, "n_pkgs": len(per_pkg),
                "zero_share": round(statistics.median(zero_share), 3),
                "spread": round(statistics.median(spread), 3),
                "nearest_scored": nearest,
                "rho_scored": round(rho_scored, 3) if rho_scored is not None else None,
                "rho_yavg": round(statistics.median(rho_yavg), 3) if rho_yavg else None,
                "rho_ydif": round(statistics.median(rho_ydif), 3) if rho_ydif else None,
                "top_cur_rank": round(statistics.median(top_rank), 3),
                "top_covered": round(statistics.mean(covered_top), 3),
            }
            flags = []
            if row["zero_share"] >= 0.99:
                flags.append("CONSTANT")
            elif row["spread"] < FLAT_SPREAD and row["zero_share"] < 0.5:
                flags.append("FLAT")
            if rho_scored is not None and rho_scored > REDUNDANT_RHO:
                flags.append("REDUNDANT")
            if row["rho_yavg"] is not None and abs(row["rho_yavg"]) > CONTENT_RHO:
                flags.append("CONTENT")
            if direction == "+" and any(_same_stat(col, f) for f in SCORED_FIELDS):
                flags.append("SCORED")
            row["flags"] = " ".join(flags)
            out.append(row)
    return out


def _same_stat(col, field):
    """Whether a generic tag column is the statistic bin_scoring already scores."""
    aliases = {"deflicker_absmax": ("deflicker_relative_change_max",)}
    return col == field or col in aliases.get(field, ())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run IDs, run directories or corpus package directories")
    ap.add_argument("--out", default="bins.csv")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--screen", action="store_true", help="print the unsupervised tag screen")
    ap.add_argument("--screen-out", default=None, help="also write the screen as CSV")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    logger.setLevel(logging.ERROR)

    pkg_rows, pkg_periods = {}, {}
    for pkg in find_packages(args.runs):
        try:
            ctx = load_package(pkg, use_cache=not args.no_cache)
            rows, periods = package_rows(ctx, args.count, args.duration)
        except Exception as e:
            print(f"{pkg.name}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        pkg_rows[ctx.name], pkg_periods[ctx.name] = rows, periods
        n = sum(r["analyzable"] for r in rows)
        print(f"{ctx.name}: {len(rows)} bins, {n} analyzable", file=sys.stderr)
    if not pkg_rows:
        sys.exit("no packages")

    all_rows = [r for rows in pkg_rows.values() for r in rows]
    lead = ["package", "group", "bin_start", "analyzable", "avoid_reason", "score", "dominant_family",
            "in_periods", "evidence"]
    fixed = [k for k in dict.fromkeys(k for r in all_rows for k in r)
             if k.startswith(("family_", "bp_", "gt_"))]
    tag_columns = sorted({k for r in all_rows for k in r} - set(lead) - set(fixed) - {"frames"})
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=lead + fixed + ["frames"] + tag_columns)
        w.writeheader()
        w.writerows(all_rows)
    print(f"Wrote {len(all_rows)} bins x {len(tag_columns)} tag columns to {args.out}", file=sys.stderr)

    if args.screen:
        result = screen(pkg_rows, pkg_periods, tag_columns)
        result.sort(key=lambda r: (-r["n_pkgs"], bool(r["flags"]), r["top_cur_rank"], r["tag"]))
        cols = ["tag", "dir", "n_pkgs", "zero_share", "spread", "nearest_scored", "rho_scored",
                "rho_yavg", "rho_ydif", "top_cur_rank", "top_covered", "flags"]
        share = statistics.median(
            sum(r["in_periods"] for r in rows if r["analyzable"]) / max(1, sum(r["analyzable"] for r in rows))
            for rows in pkg_rows.values())
        print(f"Chance coverage (median share of analyzable bins today's periods cover): {share:.2f}")
        widths = {c: max(len(c), *(len(str(r[c])) for r in result)) for c in cols}
        print("  ".join(c.ljust(widths[c]) for c in cols))
        for r in result:
            print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))
        if args.screen_out:
            with open(args.screen_out, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(result)


if __name__ == "__main__":
    main()
