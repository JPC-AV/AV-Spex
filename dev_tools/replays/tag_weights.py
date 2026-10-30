#!/usr/bin/env python3
"""
tag_weights.py: score QCTools tags, and period-selection scoring configurations, against
operator labels (label_sheet.py).

    python3 dev_tools/replays/tag_weights.py period-study-2026-10 [--out-dir DIR]

Labels: every labels/sheet_*.csv with its key_*.json, plus labels/seed_labels.csv, keyed by
(video_id, bin_start), so they carry across runs. Unfilled rows are unlabeled and count for
nothing. What counts as *damaged* is --damage (everything else labeled is clean):

  labeled   severity >= 1 with a defect other than none / graphics_slate_only (the default)
  severe    the same at severity 2
  tape      severity >= 1 and origin 'tape'. Rows without an origin (round 1) count when they
            name dropout, sync_loss_roll or signal_loss_freeze
  illegal   severity >= 1 with illegal_levels, whatever its origin: illegal levels in a graphic
            or in the content are still what BRNG analysis is there to measure

Round 1 showed why this matters: on worn VHS, 63% of random bins carry some visible defect at
severity >= 1, so 'labeled' leaves nothing for a tag to beat.

Part 1, per tag (every CANDIDATES column, plus --tags):
  p@1, p@2      share of tapes whose top (top two) bins for the tag are damaged, among labeled ones.
                Compare to `base`, the damaged share of the random controls.
  auc           pooled over labeled bins: does the tag's within-file rank put damaged above clean?
  auc[<defect>] the same for one defect type against clean, i.e. what kind of damage it finds

Part 2, per configuration (CONFIGS: METRICS/FAMILY_WEIGHTS variants, replayed at --count x
--duration through the real selection code):
  dmg_recall    share of damaged labeled bins the periods cover
  prec_est      of the evidence bins the configuration targets (bins in its periods scoring
                >= EVIDENCE_MIN_SCORE), the damaged share, with unlabeled ones valued at the
                control base rate. Unlabeled targets are not free: moving onto bins nobody has
                looked at costs precision until they are labeled
  objective     F1 of dmg_recall and prec_est
  clean_cov     share of clean labeled bins the periods cover (context only: it is biased
                toward configurations whose bins are unlabeled)
  types/tape    distinct defect types covered per tape
  hit_brng/tout period_grid's ground-truth guard (worst active-area bin sampled), must not drop
  moved         tapes whose periods differ from the baseline
  unlabeled     evidence bins the configuration targets that nobody has labeled: a configuration
                is neither credited nor penalized for those, so label them (next round) before
                trusting a configuration that moves many of them

Then a hold-out: the best configuration by objective on each collection group is
scored on the others, and a leave-one-tape-out pick is compared with the baseline. With ~27
tapes, a configuration that only wins in-sample should not be adopted.

The baseline configuration must reproduce the run's own periods at the run's settings
(harness check), or this replay no longer mirrors the app.
"""
import argparse
import csv
import json
import logging
import os
import statistics
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from bin_table import find_packages, package_rows, source_group  # noqa: E402
from label_sheet import CANDIDATES, LEGALITY_CANDIDATES, ranked_bins, selector  # noqa: E402
from _selection import NEG_PREFIX, covered, in_spans, load_package, score, scoring, select_periods  # noqa: E402
import period_grid  # noqa: E402

from AV_Spex.checks import bin_scoring
from AV_Spex.checks.bin_scoring import MetricSpec
from AV_Spex.utils.log_setup import logger

LABELS = Path(os.environ.get("AVSPEX_CORPUS", "~/git/JPC_AV/avspex_corpus")).expanduser() / "labels"
CLEAN_DEFECTS = {"none", "graphics_slate_only"}
TAPE_DEFECTS = {"dropout", "sync_loss_roll", "signal_loss_freeze"}
DAMAGE_MODES = ("labeled", "severe", "tape", "illegal")
DAMAGE = "labeled"      # set from --damage
MIN_DEFECT_N = 3        # per-defect AUC only with at least this many labeled bins of that type

BASE_METRICS = bin_scoring.METRICS
BASE_WEIGHTS = dict(bin_scoring.FAMILY_WEIGHTS)


def _with(*extra, weights=None):
    return (BASE_METRICS + tuple(extra), dict(BASE_WEIGHTS, **(weights or {})))


# name -> (metrics, family weights). Edit freely; 'baseline' must stay first and unchanged.
CONFIGS = {
    "baseline": (BASE_METRICS, BASE_WEIGHTS),
    "no_instability": (tuple(m for m in BASE_METRICS if m.family != "instability"),
                       {"legality": 0.5, "impulsive": 0.5}),
    "legality_heavy": (BASE_METRICS, {"legality": 0.6, "impulsive": 0.3, "instability": 0.1}),
    "impulsive_heavy": (BASE_METRICS, {"legality": 0.3, "impulsive": 0.6, "instability": 0.1}),
    "+udif_max(imp)": _with(MetricSpec("udif_max", "impulsive")),
    "+vdif_max(imp)": _with(MetricSpec("vdif_max", "impulsive")),
    "+uvdif_max(chroma)": _with(MetricSpec("udif_max", "chroma"), MetricSpec("vdif_max", "chroma"),
                                weights={"chroma": 0.2}),
    "+ydif_max(inst)": _with(MetricSpec("ydif_max", "instability")),
    "+psnr_y_min-(imp)": _with(MetricSpec(NEG_PREFIX + "psnr_psnr_y_min", "impulsive")),
    "+satlow_mean(chroma)": _with(MetricSpec("satlow_mean", "chroma"), weights={"chroma": 0.2}),
    "+tout_max(imp)": _with(MetricSpec("tout_max", "impulsive")),
    "+brng_min(leg)": _with(MetricSpec("brng_min", "legality")),
    # Legal-floor luma (_selection.add_legal_floor_columns): only the part past legal black or
    # white counts, as bin_scoring already floors SATMAX.
    "+under_black(leg)": _with(MetricSpec("under_black", "legality")),
    "+over_white(leg)": _with(MetricSpec("over_white", "legality")),
    "+legal_floor(leg)": _with(MetricSpec("under_black", "legality"), MetricSpec("over_white", "legality")),
}


# --- labels ----------------------------------------------------------------------------------

def load_labels():
    """{(video_id, bin_start): {'defects': set, 'severity': int, 'origin': str|None, 'control': bool}}"""
    labels = {}

    def put(vid, b, defect, severity, control=False, origin=None):
        defects = {d.strip() for d in (defect or "").split("+") if d.strip()}
        if not defects or str(severity).strip() == "":
            return
        labels[(vid, float(b))] = {"defects": defects, "severity": int(float(severity)),
                                   "origin": (origin or "").strip().lower() or None, "control": control}

    seed = LABELS / "seed_labels.csv"
    if seed.exists():
        for row in csv.DictReader(open(seed)):
            put(row["video_id"], row["bin_start"], row["defect"], row["severity"])
    for sheet in sorted(LABELS.glob("sheet_*.csv")):
        key_path = sheet.with_name(sheet.name.replace("sheet_", "key_", 1)).with_suffix(".json")
        if not key_path.exists():
            continue
        key = json.loads(key_path.read_text())
        for row in csv.DictReader(open(sheet)):
            k = key.get(row["blind_id"])
            if k:
                put(k["video_id"], k["bin_start"], row.get("defect"), row.get("severity"),
                    control="control" in k["selectors"], origin=row.get("origin"))
    return labels


def is_damaged(label, mode=None):
    mode = mode or DAMAGE
    defects, severity = label["defects"], label["severity"]
    if mode == "illegal":
        return severity >= 1 and "illegal_levels" in defects
    if mode == "tape":
        if severity < 1:
            return False
        if label.get("origin"):
            return "tape" in label["origin"].split("+")
        return bool(defects & TAPE_DEFECTS)
    floor = 2 if mode == "severe" else 1
    return severity >= floor and not defects <= CLEAN_DEFECTS


# --- part 1: tags ----------------------------------------------------------------------------

def auc(pos, neg):
    """Probability a random positive outranks a random negative (ties count half)."""
    if not pos or not neg:
        return None
    pos, neg = np.asarray(pos), np.asarray(neg)
    greater = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * ties) / (len(pos) * len(neg)))


def within_rank(rows, col, direction):
    """{bin_start: rank 0..1 in the suspicious direction} over a tape's analyzable bins."""
    order = ranked_bins(rows, col, direction)
    n = len(order)
    return {b: 1 - i / (n - 1) for i, b in enumerate(order)} if n > 1 else {}


def tag_report(tapes, labels, tags):
    controls = [l for l in labels.values() if l["control"]]
    base = statistics.mean(is_damaged(l) for l in controls) if controls else None
    defect_types = sorted({d for l in labels.values() if is_damaged(l) for d in l["defects"]} - CLEAN_DEFECTS)
    out = []
    for col, d in tags:
        p1, p2, scored = [], [], []
        for name, (rows, _ctx) in tapes.items():
            rows = [r for r in rows if r["analyzable"]]
            order = ranked_bins(rows, col, d)
            if not order:
                continue
            tops = [labels.get((name, b)) for b in order[:2]]
            if tops[0] is not None:
                p1.append(is_damaged(tops[0]))
            top2 = [t for t in tops if t is not None]
            if top2:
                p2.append(statistics.mean(is_damaged(t) for t in top2))
            ranks = within_rank(rows, col, d)
            for b, rank in ranks.items():
                if (name, b) in labels:
                    scored.append((rank, labels[(name, b)]))
        row = {"tag": selector(col, d), "tapes": len(p1) or None,
               "p@1": _r(statistics.mean(p1)) if p1 else None,
               "p@2": _r(statistics.mean(p2)) if p2 else None,
               "base": _r(base) if base is not None else None,
               "n_lab": len(scored),
               "auc": _r(auc([r for r, l in scored if is_damaged(l)],
                             [r for r, l in scored if not is_damaged(l)]))}
        clean = [r for r, l in scored if not is_damaged(l)]
        for t in defect_types:
            pos = [r for r, l in scored if is_damaged(l) and t in l["defects"]]
            row[f"auc[{t}]"] = _r(auc(pos, clean)) if len(pos) >= MIN_DEFECT_N else None
        out.append(row)
    return out


def _r(x, nd=3):
    return None if x is None else round(x, nd)


# --- part 2: configurations ------------------------------------------------------------------

def tape_outcome(ctx, periods, scores, labels):
    lab = {b: l for (v, b), l in labels.items() if v == ctx.name}
    evidence = sorted({e.bin_start for p in periods for e in bin_scoring.evidence_within(p[0], p[1], scores)})
    dmg = [b for b, l in lab.items() if is_damaged(l)]
    clean = [b for b, l in lab.items() if not is_damaged(l)]
    types = {d for b in dmg if covered(b, periods) for d in lab[b]["defects"]} - CLEAN_DEFECTS
    return {"dmg": len(dmg), "dmg_hit": sum(covered(b, periods) for b in dmg),
            "clean": len(clean), "clean_hit": sum(covered(b, periods) for b in clean),
            "types": len(types),
            "ev": len(evidence), "ev_dmg": sum(b in lab and is_damaged(lab[b]) for b in evidence),
            "unlabeled": [b for b in evidence if b not in lab]}


def gt_guard(ctx, periods):
    if ctx.gt_rows is None:
        return {}
    bins = period_grid.content_bins(ctx.gt_rows, ctx.avoid, ctx.effective_start, ctx.duration)
    gt_content = [r for r in ctx.gt_rows if r[0] >= ctx.effective_start and not in_spans(r[0], ctx.avoid)]
    s = period_grid.score(periods, bins, [], [], gt_content)
    return {"hit_brng": s.get("hit_brng"), "hit_tout": s.get("hit_tout")}


def run_configs(tapes, labels, count, dur):
    per = {}        # (config, tape) -> outcome
    baseline_periods = {}
    harness = []
    for cname, (metrics, weights) in CONFIGS.items():
        with scoring(metrics, weights):
            for name, (_rows, ctx) in tapes.items():
                scores = score(ctx)
                periods = select_periods(ctx, count, dur, scores=scores)
                if cname == "baseline":
                    baseline_periods[name] = periods
                    if ctx.actual:
                        run = select_periods(ctx, len(ctx.actual), ctx.actual[0][1], scores=scores)
                        harness.append(int([(round(s), round(d)) for s, d in run] == ctx.actual))
                o = tape_outcome(ctx, periods, scores, labels)
                o.update(gt_guard(ctx, periods))
                o["moved"] = int(periods != baseline_periods.get(name, periods))
                o["periods"] = periods
                per[(cname, name)] = o
    return per, harness


def summarize(per, cname, names, base):
    os_ = [per[(cname, n)] for n in names]
    ev = sum(o["ev"] for o in os_)
    unlab = sum(len(o["unlabeled"]) for o in os_)
    prec = (sum(o["ev_dmg"] for o in os_) + base * unlab) / ev if ev else None
    dmg, clean = sum(o["dmg"] for o in os_), sum(o["clean"] for o in os_)
    out = {"config": cname, "tapes": len(os_),
           "dmg_recall": _r(sum(o["dmg_hit"] for o in os_) / dmg) if dmg else None,
           "prec_est": _r(prec),
           "clean_cov": _r(sum(o["clean_hit"] for o in os_) / clean) if clean else None,
           "types/tape": _r(statistics.mean(o["types"] for o in os_), 2) if os_ else None,
           "moved": sum(o["moved"] for o in os_),
           "unlabeled": unlab, "evidence": ev}
    for k in ("hit_brng", "hit_tout"):
        vals = [o[k] for o in os_ if o.get(k) is not None]
        out[k] = _r(statistics.mean(vals), 2) if vals else None
    r, p = out["dmg_recall"], out["prec_est"]
    out["objective"] = None if r is None or p is None else _r(2 * p * r / (p + r) if p + r else 0.0)
    return out


def holdout(per, groups, base):
    lines = []
    configs = list(CONFIGS)
    for g, names in groups.items():
        rest = [n for gg, ns in groups.items() if gg != g for n in ns]
        if not rest:
            continue
        fit = {c: summarize(per, c, names, base)["objective"] for c in configs}
        fit = {c: v for c, v in fit.items() if v is not None}
        if not fit:
            continue
        best = max(fit, key=fit.get)
        test = {c: summarize(per, c, rest, base)["objective"] for c in configs}
        test = {c: v for c, v in test.items() if v is not None}
        rank = sorted(test, key=test.get, reverse=True).index(best) + 1 if best in test else None
        lines.append(f"  fit on {g} ({len(names)} tapes): best {best} ({fit[best]:+.3f}); on the other "
                     f"{len(rest)} tapes it scores {test.get(best, float('nan')):+.3f} vs baseline "
                     f"{test.get('baseline', float('nan')):+.3f} (rank {rank}/{len(test)})")
    # leave one tape out
    names = [n for ns in groups.values() for n in ns]
    deltas, picks = [], {}
    for held in names:
        rest = [n for n in names if n != held]
        fit = {c: summarize(per, c, rest, base)["objective"] for c in configs}
        fit = {c: v for c, v in fit.items() if v is not None}
        if not fit:
            continue
        best = max(fit, key=fit.get)
        picks[best] = picks.get(best, 0) + 1
        o, ob = per[(best, held)], per[("baseline", held)]
        if o["dmg"]:
            deltas.append((o["dmg_hit"] - ob["dmg_hit"]) / o["dmg"])
    if deltas:
        lines.append(f"  leave-one-tape-out: picks {picks}; on the held-out tape the pick's damage "
                     f"recall beats baseline on {sum(d > 0 for d in deltas)}, loses on "
                     f"{sum(d < 0 for d in deltas)}, ties on {sum(d == 0 for d in deltas)} of "
                     f"{len(deltas)} tapes with damaged labels (mean {statistics.mean(deltas):+.3f})")
    return lines


def print_table(rows, cols=None):
    if not rows:
        return
    cols = cols or list(rows[0])
    cols = [c for c in cols if any(r.get(c) is not None for r in rows)]
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows:
        print("  ".join(str("" if r.get(c) is None else r.get(c)).ljust(w[c]) for c in cols))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--tags", default="", help="extra tag columns to score, e.g. 'ydif_mean+,blur_max+'")
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--damage", choices=DAMAGE_MODES, default="labeled",
                    help="what counts as a damaged bin (see the top of this file)")
    args = ap.parse_args()
    logger.setLevel(logging.ERROR)
    global DAMAGE
    DAMAGE = args.damage

    labels = load_labels()
    n_dmg = sum(is_damaged(l) for l in labels.values())
    print(f"damage = {DAMAGE}: {len(labels)} labeled bins ({n_dmg} damaged, {len(labels) - n_dmg} clean, "
          f"{sum(l['control'] for l in labels.values())} controls)")

    tapes, groups = {}, {}
    for pkg in find_packages(args.runs):
        try:
            ctx = load_package(pkg)
            rows, _ = package_rows(ctx, args.count, args.duration)
        except Exception as e:
            print(f"{pkg.name}: skipped ({type(e).__name__}: {e})", file=sys.stderr)
            continue
        tapes[ctx.name] = (rows, ctx)
        groups.setdefault(source_group(ctx.info), []).append(ctx.name)

    extra = [(t[:-1], t[-1]) for t in args.tags.split(",") if t and t[-1] in "+-"]
    tags = tag_report(tapes, labels, list(dict.fromkeys(list(CANDIDATES) + list(LEGALITY_CANDIDATES) + extra)))
    print("\n== Tags (labels vs within-file rank) ==")
    print_table(tags)

    per, harness = run_configs(tapes, labels, args.count, args.duration)
    print(f"\nHarness check: baseline reproduces the run's periods on {sum(harness)}/{len(harness)} tapes")
    names = list(tapes)
    controls = [l for l in labels.values() if l["control"]]
    base = (statistics.mean(is_damaged(l) for l in controls) if controls
            else (n_dmg / len(labels) if labels else 0.0))
    configs = [summarize(per, c, names, base) for c in CONFIGS]
    print(f"\n== Configurations at {args.count} x {args.duration} s "
          f"(unlabeled targets valued at base rate {base:.3f}) ==")
    print_table(configs, ["config", "objective", "dmg_recall", "prec_est", "clean_cov", "types/tape",
                          "hit_brng", "hit_tout", "moved", "evidence", "unlabeled"])
    print("\n== Hold-out (objective = F1 of dmg_recall and prec_est) ==")
    for line in holdout(per, groups, base):
        print(line)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in ((f"tag_weights_{DAMAGE}_tags.csv", tags), (f"tag_weights_{DAMAGE}_configs.csv", configs)):
        with open(out / name, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
            w.writeheader()
            w.writerows(rows)
    with open(out / f"tag_weights_{DAMAGE}_per_tape.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["config", "tape", "dmg", "dmg_hit", "clean", "clean_hit", "types", "hit_brng",
                    "hit_tout", "moved", "periods", "unlabeled_new_picks"])
        for (c, n), o in per.items():
            w.writerow([c, n, o["dmg"], o["dmg_hit"], o["clean"], o["clean_hit"], o["types"],
                        o.get("hit_brng"), o.get("hit_tout"), o["moved"],
                        ";".join(f"{s:.0f}+{d:.0f}" for s, d in o["periods"]),
                        ";".join(f"{b:.0f}" for b in o["unlabeled"])])
    print(f"\nWrote tag_weights_{DAMAGE}_{{tags,configs,per_tape}}.csv to {out.resolve()}")


if __name__ == "__main__":
    main()
