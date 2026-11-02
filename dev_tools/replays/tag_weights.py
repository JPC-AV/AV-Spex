#!/usr/bin/env python3
"""
tag_weights.py: score QCTools tags, and period-selection scoring configurations, against
operator labels (label_sheet.py).

    python3 dev_tools/replays/tag_weights.py period-study-2026-10 [--out-dir DIR]
    python3 dev_tools/replays/tag_weights.py period-study-2026-10 --damage tape --stake

Labels: read and checked by labels.py (every labels/sheet_*.csv through its key, plus
labels/seed_labels.csv); an error in a sheet stops the run. Unfilled rows are unlabeled and
count for nothing. A bin is *damaged* when any of its findings is, under --damage (everything
else labeled is clean):

  labeled   severity >= 1 with a defect other than none / graphics_slate_only (the default)
  severe    the same at severity 2
  tape      severity >= 1 and origin 'tape'. Findings without an origin (round 1) count when
            they name dropout, sync_loss_roll or signal_loss_freeze
  illegal   severity >= 1 with illegal_levels, whatever its origin: illegal levels in a graphic
            or in the content are still what BRNG analysis is there to measure

Round 1 showed why this matters: on worn VHS, 63% of random bins carry some visible defect at
severity >= 1, so 'labeled' leaves nothing for a tag to beat.

Part 1, per tag (every study.CANDIDATES and LEGALITY_CANDIDATES column, plus --tags):
  p@1, p@2      share of tapes whose top (top two) bins for the tag are damaged, among labeled ones.
                Compare to `base`, the damaged share of the random controls.
  auc           pooled over labeled bins: does the tag's within-file rank put damaged above clean?
  auc[<defect>] the same for one defect type against clean, i.e. what kind of damage it finds

Part 2, per configuration (study.CONFIGS: METRICS/FAMILY_WEIGHTS variants, replayed at --count x
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

Uncertainty (--bootstrap N, default 2000; 0 turns it off): whole tapes are resampled with
replacement, since bins on one tape are not independent, and every headline number is
recomputed on each resample, the control base rate included. Tags get a 95% interval on p@1
and on p@1 - base, with `P(>base)`, the share of resamples where the tag beats the base rate.
Configurations get the same on the objective and on `delta`, the objective minus the
baseline's on the same resample, with `P(>0)`. A configuration whose delta interval includes 0
has not shown it is better, whatever its point estimate.

What more labels could change (--stake, before a labeling round): the evidence bins nobody has
labeled are the only ones that move a configuration's result relative to the baseline. For
each configuration, they fall into three groups: targeted by it only, by the baseline only, or
by both. Each group is set all-damaged or all-clean (8 cases), and the objective delta is
recomputed with those bins as labeled. The spread is how far labeling them could move the
verdict:
  settled-worse   the delta stays below +--margin in every case: labels can't make it a win
  settled-better  the delta stays at or above +--margin in every case
  open            labeling could go either way
The bound is the worst case, so a configuration that moves many tapes onto unlabeled bins is
always open: name the configurations the decision is actually between (`--stake
impulsive_heavy`) to get a sheet that size. The open ones' bins, all three groups, are written
to tag_weights_<MODE>_stake_bins.csv (`group` says which; the config-only and baseline-only
bins move the delta most); pass it to `label_sheet.py --bins` for a sheet of just those. Bins no configuration targets are left
out of the bound: labeling them adds to every configuration's recall denominator alike.

The baseline configuration must reproduce the run's own periods at the run's settings
(harness check, see harness.py), or this replay no longer mirrors the app.
"""
import argparse
import csv
import logging
import random
import statistics
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from corpus import find_packages  # noqa: E402
from labels import DAMAGE_MODES, load as load_labels  # noqa: E402
from bin_table import package_rows, source_group  # noqa: E402
from study import CANDIDATES, CONFIGS, LEGALITY_CANDIDATES  # noqa: E402
from _selection import (covered, harness_replay, harness_status, harness_summary, in_spans,  # noqa: E402
                        load_package, ranked_bins, score, scoring, select_periods, selector)
import period_grid  # noqa: E402

from AV_Spex.checks import bin_scoring
from AV_Spex.utils.log_setup import logger

DAMAGE = "labeled"      # set from --damage
MIN_DEFECT_N = 3        # per-defect AUC only with at least this many labeled bins of that type

def is_damaged(label, mode=None):
    return label.damaged(mode or DAMAGE)


def damaged_defects(label):
    return label.damaged_defects(DAMAGE)


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
    """(rows, p1): one row per tag, and {tag: {tape: top bin damaged}} for the bootstrap."""
    controls = [l for l in labels.values() if l.control]
    base = statistics.mean(is_damaged(l) for l in controls) if controls else None
    defect_types = sorted({d for l in labels.values() for d in damaged_defects(l)})
    out, p1_by_tag = [], {}
    for col, d in tags:
        p1, p2, scored = [], [], []
        p1_tapes = p1_by_tag.setdefault(selector(col, d), {})
        for name, (rows, _ctx) in tapes.items():
            rows = [r for r in rows if r["analyzable"]]
            order = ranked_bins(rows, col, d)
            if not order:
                continue
            tops = [labels.get((name, b)) for b in order[:2]]
            if tops[0] is not None:
                p1.append(is_damaged(tops[0]))
                p1_tapes[name] = is_damaged(tops[0])
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
            pos = [r for r, l in scored if t in damaged_defects(l)]
            row[f"auc[{t}]"] = _r(auc(pos, clean)) if len(pos) >= MIN_DEFECT_N else None
        out.append(row)
    return out, p1_by_tag


def _r(x, nd=3):
    return None if x is None else round(x, nd)


# --- part 2: configurations ------------------------------------------------------------------

def evidence_bins(periods, scores):
    return sorted({e.bin_start for p in periods for e in bin_scoring.evidence_within(p[0], p[1], scores)})


def tape_outcome(name, periods, evidence, labels):
    lab = {b: l for (v, b), l in labels.items() if v == name}
    dmg = [b for b, l in lab.items() if is_damaged(l)]
    clean = [b for b, l in lab.items() if not is_damaged(l)]
    types = {d for b in dmg if covered(b, periods) for d in damaged_defects(lab[b])}
    return {"dmg": len(dmg), "dmg_hit": sum(covered(b, periods) for b in dmg),
            "clean": len(clean), "clean_hit": sum(covered(b, periods) for b in clean),
            "types": len(types),
            "ev": len(evidence), "ev_dmg": sum(b in lab and is_damaged(lab[b]) for b in evidence),
            "unlabeled": [b for b in evidence if b not in lab], "evidence_bins": evidence}


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
    harness = {}    # tape -> harness status
    for cname, (metrics, weights) in CONFIGS.items():
        with scoring(metrics, weights):
            for name, (_rows, ctx) in tapes.items():
                scores = score(ctx)
                periods = select_periods(ctx, count, dur, scores=scores)
                if cname == "baseline":
                    baseline_periods[name] = periods
                    harness[name] = harness_status(ctx, harness_replay(ctx, scores=scores))
                o = tape_outcome(name, periods, evidence_bins(periods, scores), labels)
                o.update(gt_guard(ctx, periods))
                o["moved"] = int(periods != baseline_periods.get(name, periods))
                o["periods"] = periods
                per[(cname, name)] = o
    return per, harness


def _recall_precision(os_, base):
    ev = sum(o["ev"] for o in os_)
    unlab = sum(len(o["unlabeled"]) for o in os_)
    prec = (sum(o["ev_dmg"] for o in os_) + base * unlab) / ev if ev else None
    dmg = sum(o["dmg"] for o in os_)
    return (sum(o["dmg_hit"] for o in os_) / dmg if dmg else None), prec


def _f1(r, p):
    return None if r is None or p is None else (2 * p * r / (p + r) if p + r else 0.0)


def objective(per, cname, names, base):
    """The objective, unrounded (summarize() rounds recall and precision first, as reported)."""
    return _f1(*_recall_precision([per[(cname, n)] for n in names], base))


def summarize(per, cname, names, base):
    os_ = [per[(cname, n)] for n in names]
    ev = sum(o["ev"] for o in os_)
    unlab = sum(len(o["unlabeled"]) for o in os_)
    recall, prec = _recall_precision(os_, base)
    clean = sum(o["clean"] for o in os_)
    out = {"config": cname, "tapes": len(os_),
           "dmg_recall": _r(recall),
           "prec_est": _r(prec),
           "clean_cov": _r(sum(o["clean_hit"] for o in os_) / clean) if clean else None,
           "types/tape": _r(statistics.mean(o["types"] for o in os_), 2) if os_ else None,
           "moved": sum(o["moved"] for o in os_),
           "unlabeled": unlab, "evidence": ev}
    for k in ("hit_brng", "hit_tout"):
        vals = [o[k] for o in os_ if o.get(k) is not None]
        out[k] = _r(statistics.mean(vals), 2) if vals else None
    out["objective"] = _r(_f1(out["dmg_recall"], out["prec_est"]))
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


# --- uncertainty: resample whole tapes -------------------------------------------------------

def base_rate(labels, names=None):
    """Damaged share of the controls, over `names` (tapes, repeats counted) or every tape."""
    by_tape = {}
    for (v, _b), l in labels.items():
        if l.control:
            by_tape.setdefault(v, []).append(is_damaged(l))
    vals = [d for n in (names if names is not None else list(by_tape)) for d in by_tape.get(n, [])]
    return statistics.mean(vals) if vals else None


def _interval(values):
    """(lo, hi) of the central 95%, or (None, None) without values."""
    if not values:
        return None, None
    v = np.asarray(values, dtype=float)
    return _r(float(np.percentile(v, 2.5))), _r(float(np.percentile(v, 97.5)))


def bootstrap(tags, p1_by_tag, configs, per, labels, names, n, seed):
    """Add 95% intervals to the tag and configuration rows, from n resamples of whole tapes."""
    rng = random.Random(seed)
    draws = [[rng.choice(names) for _ in names] for _ in range(n)]
    bases = [base_rate(labels, d) for d in draws]
    full_base = base_rate(labels)
    for row in tags:
        p1 = p1_by_tag[row["tag"]]
        stats, deltas = [], []
        for d, b in zip(draws, bases):
            vals = [p1[t] for t in d if t in p1]
            if vals and b is not None:
                stats.append(statistics.mean(vals))
                deltas.append(stats[-1] - b)
        row["p@1_lo"], row["p@1_hi"] = _interval(stats)
        row["vs_base_lo"], row["vs_base_hi"] = _interval(deltas)
        row["P(>base)"] = _r(statistics.mean(x > 0 for x in deltas)) if deltas else None
    if full_base is not None:
        lo, hi = _interval([b for b in bases if b is not None])
        for row in tags:
            row["base_lo"], row["base_hi"] = lo, hi
    for row in configs:
        c = row["config"]
        full = [objective(per, c, names, full_base or 0.0), objective(per, "baseline", names, full_base or 0.0)]
        row["delta"] = _r(full[0] - full[1]) if None not in full else None
        objs, deltas = [], []
        for d, b in zip(draws, bases):
            b = b or 0.0
            o, ob = objective(per, c, d, b), objective(per, "baseline", d, b)
            if o is not None and ob is not None:
                objs.append(o)
                deltas.append(o - ob)
        row["obj_lo"], row["obj_hi"] = _interval(objs)
        if c != "baseline":
            row["delta_lo"], row["delta_hi"] = _interval(deltas)
            row["P(>0)"] = _r(statistics.mean(x > 0 for x in deltas)) if deltas else None


# --- stake: what labeling the unlabeled targets could change ---------------------------------

class _Assumed:
    """A stand-in label for an unlabeled bin, assumed damaged or clean."""
    control = False

    def __init__(self, damaged):
        self._damaged = damaged

    def damaged(self, mode):
        return self._damaged

    def damaged_defects(self, mode):
        return set()


def _delta_with(per, cname, names, labels, base, assumed):
    """The objective delta against baseline with `assumed` ({(tape, bin): damaged}) added as labels."""
    hyp = dict(labels)
    hyp.update({k: _Assumed(v) for k, v in assumed.items()})
    redo = {(c, n): tape_outcome(n, per[(c, n)]["periods"], per[(c, n)]["evidence_bins"], hyp)
            for c in (cname, "baseline") for n in names}
    o, ob = objective(redo, cname, names, base), objective(redo, "baseline", names, base)
    return None if o is None or ob is None else o - ob


def stake(per, labels, names, base, margin, configs):
    """(rows, bins): per configuration, the delta range labeling its unlabeled targets could
    give, and {(tape, bin): {config: group}} for the configurations still open."""
    def unlabeled(c):
        return {(n, b) for n in names for b in per[(c, n)]["unlabeled"]}
    base_unl = unlabeled("baseline")
    rows, open_bins = [], {}
    for c in configs:
        mine = unlabeled(c)
        groups = (mine & base_unl, mine - base_unl, base_unl - mine)    # shared, config only, baseline only
        deltas = []
        for setting in range(8):
            assumed = {k: bool(setting >> i & 1) for i, g in enumerate(groups) for k in g}
            d = _delta_with(per, c, names, labels, base, assumed)
            if d is not None:
                deltas.append(d)
        lo, hi = (min(deltas), max(deltas)) if deltas else (None, None)
        now = objective(per, c, names, base), objective(per, "baseline", names, base)
        verdict = ("n/a" if lo is None else "settled-worse" if hi < margin
                   else "settled-better" if lo >= margin else "open")
        rows.append({"config": c, "delta": _r(now[0] - now[1]) if None not in now else None,
                     "delta_min": _r(lo), "delta_max": _r(hi), "verdict": verdict,
                     "shared": len(groups[0]), "config_only": len(groups[1]), "baseline_only": len(groups[2])})
        if verdict == "open":
            for group, keys in zip(("shared", "config_only", "baseline_only"), groups):
                for k in keys:
                    open_bins.setdefault(k, {})[c] = group
    return rows, open_bins


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
    ap.add_argument("--bootstrap", type=int, default=2000, metavar="N",
                    help="tape resamples for the 95%% intervals (0: off; default %(default)s)")
    ap.add_argument("--seed", type=int, default=1, help="bootstrap seed")
    ap.add_argument("--stake", nargs="*", metavar="CONFIG", default=None,
                    help="bound what labeling the unlabeled targets could change, for these "
                         "configurations (none named: all), and list the bins")
    ap.add_argument("--margin", type=float, default=0.02,
                    help="objective gain over baseline worth adopting, for --stake (default %(default)s)")
    args = ap.parse_args()
    logger.setLevel(logging.ERROR)
    global DAMAGE
    DAMAGE = args.damage

    labels = load_labels()
    n_dmg = sum(is_damaged(l) for l in labels.values())
    print(f"damage = {DAMAGE}: {len(labels)} labeled bins ({n_dmg} damaged, {len(labels) - n_dmg} clean, "
          f"{sum(l.control for l in labels.values())} controls)")

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
    tags, p1_by_tag = tag_report(tapes, labels,
                                 list(dict.fromkeys(list(CANDIDATES) + list(LEGALITY_CANDIDATES) + extra)))

    per, harness = run_configs(tapes, labels, args.count, args.duration)
    names = list(tapes)
    base = base_rate(labels)
    if base is None:
        base = n_dmg / len(labels) if labels else 0.0
    configs = [summarize(per, c, names, base) for c in CONFIGS]
    if args.bootstrap:
        bootstrap(tags, p1_by_tag, configs, per, labels, names, args.bootstrap, args.seed)
    ci = f", 95% intervals from {args.bootstrap} tape resamples" if args.bootstrap else ""

    print(f"\n== Tags (labels vs within-file rank{ci}) ==")
    print_table(tags, ["tag", "tapes", "p@1", "p@1_lo", "p@1_hi", "base", "base_lo", "base_hi",
                       "P(>base)", "p@2", "n_lab", "auc"])
    print("\n" + harness_summary(harness))
    print(f"\n== Configurations at {args.count} x {args.duration} s "
          f"(unlabeled targets valued at base rate {base:.3f}{ci}) ==")
    print_table(configs, ["config", "objective", "obj_lo", "obj_hi", "delta", "delta_lo", "delta_hi", "P(>0)",
                          "dmg_recall", "prec_est", "clean_cov", "types/tape", "hit_brng", "hit_tout",
                          "moved", "evidence", "unlabeled"])
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

    if args.stake is not None:
        chosen = args.stake or [c for c in CONFIGS if c != "baseline"]
        unknown = [c for c in chosen if c not in CONFIGS or c == "baseline"]
        if unknown:
            sys.exit(f"--stake: not a configuration to compare: {', '.join(unknown)}")
        rows, open_bins = stake(per, labels, names, base, args.margin, chosen)
        print(f"\n== Stake: objective delta vs baseline if every unlabeled target were labeled "
              f"(adopt margin +{args.margin}) ==")
        print_table(rows)
        path = out / f"tag_weights_{DAMAGE}_stake_bins.csv"
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["video_id", "bin_start", "configs", "group"])
            for (n, b), cs in sorted(open_bins.items()):
                w.writerow([n, f"{b:.0f}", ";".join(cs), ";".join(sorted(set(cs.values())))])
        n_open = sum(r["verdict"] == "open" for r in rows)
        print(f"\n{n_open} configuration(s) open; {len(open_bins)} bins could change a verdict -> {path}"
              + (f"\n  label them: python3 dev_tools/label_sheet.py <run> --bins {path} --name <name>"
                 if open_bins else ""))


if __name__ == "__main__":
    main()
