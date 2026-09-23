#!/usr/bin/env python3
"""How interesting each analyzable bin is, across every measure a report carries.

Period selection has always ranked bins by BRNG: the count of frames over
`BRNG > 0.01`, tie-broken by summed BRNG. Measured across the sample reports,
**that is the flattest signal available**. Per-bin p90/median ratios:

    BRNG         1.16 - 1.95      <- what ranking uses today
    TOUT         1.41 - 2.80
    SATAVG       1.29 - 3.08
    YDIF         1.80 - 9.49
    deflicker    4.11 - 131.1

On a noisy tape every bin saturates — all 187 content bins of JPC_AV_01772
have >95% of frames violating — so ranking comes down to separating bins whose
mean out-of-range share differs by ~25%, while TOUT, VREP and geometry drift
vary by multiples over the same tape and are not consulted at all.

This module scores each bin over three families of evidence, so a dropout
burst or a stretch of illegal chroma can win a period on a tape where BRNG
cannot discriminate. (cropdetect was measured too and deliberately left out —
see the note under METRICS.)

**The score is a targeting score, not a severity measure.** It says where the
worst of *this* tape is, not how bad the tape is: the top bin of a pristine
transfer scores the same 1.0 as the top bin of a ruined one. That is what
period selection needs — it always takes the top N — but it means the number
must never be shown to a user as a quality figure.

Normalization is **within-file rank of the excess over a floor**, not a
z-score. Two properties of the data force that:

* Several metrics are zero across most of a tape (VREP is exactly 0 in 89-100%
  of content bins), so their median *and* MAD are both 0 and any z-score
  divides by zero. Rank handles a zero-heavy distribution natively: ties at
  the bottom all score 0, and a rare spike lands near 1.
* A metric with no spread at all contributes nothing, with no special case —
  every bin ties, so every bin ranks 0.

Where a metric has a meaningful absolute floor (BRNG's existing violation
threshold; the illegal-chroma level for SATMAX) the excess over it is what
gets ranked, so bins that are entirely legal contribute nothing rather than
being ranked against each other.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from AV_Spex.checks.qctools_bin_profile import PROFILE_BIN_SIZE, BinProfile
from AV_Spex.checks.bin_suitability import BinVerdict

# --- Families -------------------------------------------------------------
#
# 'legality'    — pixels outside broadcast-legal range. What BRNG analysis and
#                 signalstats are there to characterize.
# 'impulsive'   — dropouts and the deck's concealment of them. The gap this
#                 module exists to close: nothing in selection sees TOUT/VREP
#                 today.
# 'instability' — the picture changing brightness when it should not. Weighted
#                 lower than the other two: its one metric is a per-bin maximum,
#                 so a single hard cut can set it.
FAMILY_WEIGHTS = {
    'legality': 0.4,
    'impulsive': 0.4,
    'instability': 0.2,
}

# Illegal chroma, 8-bit scale, scaled ×4 for a 10-bit report. The classic
# signalstats limit: saturation above it cannot be represented in RGB.
SATURATION_LEGAL_LIMIT_8BIT = 88.7

# The BRNG level the violation scan has always counted as a violation.
BRNG_VIOLATION_FLOOR = 0.01

# A bin has to score at least this well to be worth naming as the evidence
# behind a period. Low enough that a period always has something to point at
# on a tape with real problems, high enough not to list every bin it covers.
EVIDENCE_MIN_SCORE = 0.5

# Mean share of temporal-outlier pixels over a bin, below which the bin has no
# sustained impulsive damage worth targeting. Calibrated against operator-
# verified bins: three confirmed dropout/head-switching regions measure
# 0.0168, 0.0192 and 0.0214, while a confirmed clean-but-graphics-heavy bin
# measures 0.0086. Without a floor, rank normalization guarantees that *some*
# bin tops the impulsive family on every tape, including tapes with no
# dropouts at all — the floor is what lets the family score zero across a
# clean transfer and leave selection to legality. JPC_AV_01056 has no bin
# above it; JPC_AV_02212 has 20 of 141.
TOUT_SUSTAINED_FLOOR = 0.012


@dataclass(frozen=True)
class MetricSpec:
    """One measurable signal and how it enters the score.

    field: the BinProfile attribute to read.
    floor: values at or below it contribute nothing; the excess above it is
        what gets ranked. 0.0 means "rank the value itself".
    luma_scaled: the floor is in the report's luma/chroma scale and must be
        multiplied by 4 for a 10-bit report.
    """
    field: str
    family: str
    floor: float = 0.0
    luma_scaled: bool = False


METRICS: Tuple[MetricSpec, ...] = (
    MetricSpec('brng_mean', 'legality', floor=BRNG_VIOLATION_FLOOR),
    MetricSpec('satmax_max', 'legality', floor=SATURATION_LEGAL_LIMIT_8BIT,
               luma_scaled=True),
    MetricSpec('tout_mean', 'impulsive', floor=TOUT_SUSTAINED_FLOOR),
    MetricSpec('vrep_mean', 'impulsive'),
    MetricSpec('deflicker_absmax', 'instability'),
)

# crop_edge_iqr_max is deliberately absent, after being measured against the
# sample set. cropdetect reports the bounding box of non-black content, so the
# box moves whenever the *content* changes shape or brightness — the bins it
# pushed to the top were a dark scene on one tape (YAVG 218 against a file
# median of 355) and a bright one on another, i.e. not a consistent artifact,
# just "the box moved". Three of the nine JPC sample reports carry no
# cropdetect at all, and on those that do, boxes with x2 <= x1 or y2 <= y1 run
# from 1.2% to 32.6% of frames. Rejecting those degenerate boxes (which
# BinProfiler now does) did not change any selection on the JPC files, which
# is what settled it: the influence was never detector failure, it was content.
# The medians are still collected — they are a plausible input for border
# detection — but nothing scores on them.

# YDIF is deliberately not a metric of its own. It measures motion, which is
# content rather than damage — the busiest bin of a healthy tape is a fast cut,
# not a defect. The Stage-0 gate uses it only as an extreme-value rule (frames
# uncorrelated with their predecessors), where it means something different.
#
# Subtracting YDIF's rank from TOUT's was tried and rejected. TOUT rank does
# correlate with YDIF rank (+0.44 to +0.88 across the sample reports), so the
# motion-contamination theory looked right, but on operator-verified bins no
# subtraction coefficient separated true from false positives: at every
# strength from 0.25 to 1.0 the confirmed false positive stayed level with or
# above a confirmed true positive, and at full strength it *beat* one (0.76 vs
# 0.00). The reason is that damaged passages are often the high-motion ones —
# dubbed material is both a generation down and cut quickly — so removing
# motion removes real damage with it.
#
# What actually separated the cases was the statistic, not the confound:
# tout_mean over tout_p95. A bin of transitions has a high p95 (a handful of
# outlier frames) but a low mean; sustained dropout raises both. On the four
# verified bins, p95 gave 0.0219-0.0407 for true positives against 0.0190 for
# the false positive — overlapping — while the mean gave 0.0168-0.0214 against
# 0.0086, a clean 2x gap.


@dataclass
class BinScore:
    """One bin's targeting score and where it came from."""
    bin_start: float
    score: float                                    # 0.0 - 1.0 within this file
    family_scores: Dict[str, float] = field(default_factory=dict)
    metric_ranks: Dict[str, float] = field(default_factory=dict)
    # Family contributing most of the score — what Phase 3 diversifies over,
    # and what a report can name. None when the bin scored zero everywhere.
    dominant_family: Optional[str] = None


def _rank_within_file(values: List[Optional[float]]) -> List[float]:
    """Rank each value as the share of measured values strictly below it.

    Ties resolve downward on purpose: when 90% of a tape's bins read exactly
    zero, all of them rank 0.0 and the one bin that spiked ranks ~0.9. A
    midpoint or average-rank convention would instead hand every quiet bin
    half a point of score for being quiet.

    None entries (metric not measured in that bin) return 0.0 — unmeasured is
    never evidence of a problem.
    """
    measured = [v for v in values if v is not None]
    if len(measured) < 2:
        return [0.0] * len(values)

    ordered = sorted(measured)
    denominator = float(len(measured) - 1)
    ranks = []
    for value in values:
        if value is None:
            ranks.append(0.0)
            continue
        # Number of measured values strictly below this one.
        low, high = 0, len(ordered)
        while low < high:
            mid = (low + high) // 2
            if ordered[mid] < value:
                low = mid + 1
            else:
                high = mid
        ranks.append(min(1.0, low / denominator))
    return ranks


def _excess(profile: BinProfile, spec: MetricSpec, bit_depth_10: bool) -> Optional[float]:
    """The part of a metric that is above its floor, or None when unmeasured."""
    value = getattr(profile, spec.field, None)
    if value is None:
        return None
    floor = spec.floor
    if floor and spec.luma_scaled and bit_depth_10:
        floor *= 4.0
    return max(0.0, value - floor)


def score_bins(profiles: Dict[float, BinProfile],
               verdicts: Optional[Dict[float, BinVerdict]] = None,
               bit_depth_10: bool = True) -> Dict[float, BinScore]:
    """Score every analyzable bin against the rest of the same file.

    profiles: `QCToolsParser.bin_profiles`.
    verdicts: `SuitabilityAssessment.verdicts`. Unsuitable bins are left out
        of both the ranking population and the result — they are not places a
        period can go, and including them would shift every other bin's rank.
    bit_depth_10: `QCToolsParser.bit_depth_10`.

    Returns {bin_start: BinScore} for the suitable bins only.
    """
    if not profiles:
        return {}

    if verdicts:
        bins = [b for b in sorted(profiles)
                if b not in verdicts or verdicts[b].suitable]
    else:
        bins = sorted(profiles)
    if not bins:
        return {}

    # Rank each metric across the file, then fold the ranks into families.
    ranks_by_metric: Dict[str, List[float]] = {}
    families_present = set()
    for spec in METRICS:
        excesses = [_excess(profiles[b], spec, bit_depth_10) for b in bins]
        if all(value is None for value in excesses):
            continue                    # report does not carry this metric
        ranks_by_metric[spec.field] = _rank_within_file(excesses)
        families_present.add(spec.family)

    if not families_present:
        return {b: BinScore(bin_start=b, score=0.0) for b in bins}

    # Weights renormalize over the families this report can actually measure,
    # so a sparse sidecar produces scores on the same 0-1 scale as a rich one.
    total_weight = sum(FAMILY_WEIGHTS[f] for f in families_present)

    scores: Dict[float, BinScore] = {}
    for index, bin_start in enumerate(bins):
        metric_ranks = {field: ranks[index] for field, ranks in ranks_by_metric.items()}

        # Within a family, the strongest signal speaks for it: the family asks
        # "is there evidence of this kind of problem", and two weak metrics
        # should not add up to one strong one.
        family_scores = {}
        for family in families_present:
            family_scores[family] = max(
                (metric_ranks[spec.field] for spec in METRICS
                 if spec.family == family and spec.field in metric_ranks),
                default=0.0)

        weighted = {f: family_scores[f] * FAMILY_WEIGHTS[f] / total_weight
                    for f in family_scores}
        score = sum(weighted.values())
        dominant = max(weighted, key=weighted.get) if weighted else None
        if dominant is not None and weighted[dominant] <= 0.0:
            dominant = None

        scores[bin_start] = BinScore(
            bin_start=bin_start,
            score=score,
            family_scores=family_scores,
            metric_ranks=metric_ranks,
            dominant_family=dominant,
        )
    return scores


def rank_order(scores: Dict[float, BinScore]) -> List[Tuple[float, BinScore]]:
    """Bins worst-first, with a stable tie-break on bin start."""
    return sorted(scores.items(), key=lambda item: (-item[1].score, item[0]))


def evidence_within(period_start: float, period_duration: float,
                    scores: Dict[float, BinScore],
                    bin_size: float = PROFILE_BIN_SIZE,
                    min_score: float = EVIDENCE_MIN_SCORE) -> List[BinScore]:
    """The scored bins inside a period that are worth pointing a reader at.

    A period is six bins wide and the evidence that won it is often a single
    bin: on JPC_AV_01772 the damage an operator confirmed ran 18:54-19:02
    inside an 18:25-19:25 period, so 52 of its 60 seconds are clean. The
    period is still the right unit to *measure* — it gives signalstats a
    stable sample — but nothing should make someone scrub a minute of tape to
    find the eight seconds that earned it.

    Returned in time order, which is the order someone reviewing the tape
    moves through them. Callers wanting the strongest evidence rather than the
    first should sort on `.score` themselves.
    """
    period_end = period_start + period_duration
    inside = [score for bin_start, score in scores.items()
              if bin_start >= period_start - 1e-9
              and bin_start + bin_size <= period_end + 1e-9
              and score.score >= min_score]
    return sorted(inside, key=lambda score: score.bin_start)


def describe_scores(scores: Dict[float, BinScore], limit: int = 10) -> List[str]:
    """Log lines for the top-scoring bins, naming what drove each."""
    lines = []
    for bin_start, score in rank_order(scores)[:limit]:
        families = ', '.join(
            f"{family} {value:.2f}"
            for family, value in sorted(score.family_scores.items(),
                                        key=lambda kv: -kv[1])
            if value > 0)
        lines.append(f"    {bin_start:.1f}s: score {score.score:.3f}"
                     f"{f' ({families})' if families else ''}")
    return lines
