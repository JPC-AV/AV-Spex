"""Tests for checks.bin_scoring.

Phase 2 of the period-selection rework: rank bins by a composite of every
family of evidence the report carries, instead of BRNG density alone.

The properties worth pinning are mostly about the *normalization*, because the
data forced the choice: several metrics are zero across most of a tape, so
median/MAD z-scores divide by zero, and the score has to stay meaningful when
a metric has no spread, is missing entirely, or is present in only some bins.
"""

import pytest

from AV_Spex.checks import bin_scoring as sc
from AV_Spex.checks.bin_suitability import BinVerdict
from AV_Spex.checks.qctools_bin_profile import BinProfile


def _profiles(**metric_series):
    """Build N bins at 10s spacing from {metric: [v0, v1, ...]} series."""
    length = len(next(iter(metric_series.values())))
    out = {}
    for i in range(length):
        start = float(i * 10)
        kwargs = {name: series[i] for name, series in metric_series.items()}
        out[start] = BinProfile(bin_start=start, frames=300, **kwargs)
    return out


# ===========================================================================
# Rank normalization
# ===========================================================================

def test_rank_is_share_of_values_strictly_below():
    assert sc._rank_within_file([1.0, 2.0, 3.0, 4.0, 5.0]) == [0.0, 0.25, 0.5, 0.75, 1.0]


def test_ties_resolve_downward():
    """A tape where 3 of 4 bins read zero: the zeros must score 0, not 0.375.

    Average-rank would hand every quiet bin score for being quiet, which on a
    metric that is zero 90% of the time is most of the tape.
    """
    assert sc._rank_within_file([0.0, 0.0, 0.0, 5.0]) == [0.0, 0.0, 0.0, 1.0]


def test_metric_with_no_spread_contributes_nothing():
    """Every bin identical → every bin ranks 0, with no special case."""
    assert sc._rank_within_file([7.0, 7.0, 7.0]) == [0.0, 0.0, 0.0]


def test_unmeasured_bins_rank_zero():
    """None is 'not measured here', never evidence of a problem."""
    assert sc._rank_within_file([None, 1.0, 2.0, 3.0]) == [0.0, 0.0, 0.5, 1.0]


def test_single_measured_value_cannot_be_ranked():
    assert sc._rank_within_file([None, 4.0, None]) == [0.0, 0.0, 0.0]


# ===========================================================================
# Floors
# ===========================================================================

def test_values_below_the_floor_contribute_nothing():
    """Entirely legal bins are not ranked against each other."""
    profiles = _profiles(brng_mean=[0.001, 0.002, 0.003, 0.05])
    scores = sc.score_bins(profiles)
    assert scores[0.0].metric_ranks['brng_mean'] == 0.0
    assert scores[10.0].metric_ranks['brng_mean'] == 0.0
    assert scores[30.0].metric_ranks['brng_mean'] == 1.0


def test_excess_above_the_floor_is_what_ranks():
    profiles = _profiles(brng_mean=[0.02, 0.03, 0.04, 0.05])
    scores = sc.score_bins(profiles)
    ranks = [scores[float(i * 10)].metric_ranks['brng_mean'] for i in range(4)]
    assert ranks == [0.0, pytest.approx(1 / 3), pytest.approx(2 / 3), 1.0]


def test_saturation_floor_scales_with_bit_depth():
    """SATMAX 300 is illegal at 8-bit scale and legal at 10-bit."""
    profiles = _profiles(satmax_max=[100.0, 200.0, 300.0])
    eight = sc.score_bins(profiles, bit_depth_10=False)
    ten = sc.score_bins(profiles, bit_depth_10=True)
    assert eight[20.0].metric_ranks['satmax_max'] == 1.0
    assert ten[20.0].metric_ranks['satmax_max'] == 0.0     # all below 88.7*4


# ===========================================================================
# Families
# ===========================================================================

def test_family_takes_its_strongest_metric():
    """Two weak signals should not add up to one strong one."""
    profiles = _profiles(tout_mean=[0.0, 0.0, 0.0, 1.0],
                         vrep_mean=[0.0, 0.0, 0.0, 0.0])
    scores = sc.score_bins(profiles)
    assert scores[30.0].family_scores['impulsive'] == 1.0


def test_dominant_family_names_the_biggest_contributor():
    profiles = _profiles(brng_mean=[0.02, 0.02, 0.02, 0.02],
                         tout_mean=[0.0, 0.0, 0.0, 1.0])
    scores = sc.score_bins(profiles)
    assert scores[30.0].dominant_family == 'impulsive'


def test_dominant_family_is_none_for_a_bin_that_scored_zero():
    profiles = _profiles(brng_mean=[0.02, 0.05, 0.09])
    scores = sc.score_bins(profiles)
    assert scores[0.0].score == 0.0
    assert scores[0.0].dominant_family is None


def test_dropout_evidence_can_outrank_the_worst_brng_bin():
    """The point of the change.

    Bin 3 is not the file's worst BRNG bin, so BRNG-density ranking would
    never choose it; a TOUT spike on top of unremarkable BRNG puts it first.
    This is the shape of the real cases — JPC_AV_02041's top bin scores
    impulsive 1.00 alongside legality 0.97.
    """
    profiles = _profiles(brng_mean=[0.05, 0.03, 0.03, 0.045],
                         tout_mean=[0.001, 0.001, 0.001, 0.4])
    ranked = sc.rank_order(sc.score_bins(profiles))
    assert ranked[0][0] == 30.0


def test_equal_family_weights_give_families_equal_say():
    """Top-of-legality and top-of-impulsive tie, and break on bin order.

    Not an accident: legality and impulsive carry the same weight, so a bin
    that is worst on one and clean on the other cannot beat its opposite.
    """
    profiles = _profiles(brng_mean=[0.05, 0.04, 0.03, 0.02],
                         tout_mean=[0.001, 0.001, 0.001, 0.4])
    scores = sc.score_bins(profiles)
    assert scores[0.0].score == pytest.approx(scores[30.0].score)
    assert scores[0.0].dominant_family == 'legality'
    assert scores[30.0].dominant_family == 'impulsive'


def test_weights_renormalize_over_available_families():
    """One measurable family carries the whole 0-1 scale on its own.

    Without renormalizing, a report carrying only legality evidence would top
    out at 0.4 and every bin would look mild next to a richer report's.
    """
    profiles = _profiles(brng_mean=[0.02, 0.03, 0.04, 0.09])
    scores = sc.score_bins(profiles)
    assert set(scores[30.0].family_scores) == {'legality'}
    assert scores[30.0].score == pytest.approx(1.0)

    both = sc.score_bins(_profiles(brng_mean=[0.02, 0.03, 0.04, 0.09],
                                   tout_mean=[0.0, 0.0, 0.0, 0.0]))
    assert set(both[30.0].family_scores) == {'legality', 'impulsive'}
    assert both[30.0].score == pytest.approx(0.5)     # legality 1.0, impulsive 0.0


def test_missing_families_are_not_penalized():
    """Same legality evidence scores the same with or without a flicker tag."""
    without = sc.score_bins(_profiles(brng_mean=[0.02, 0.09]))
    with_flicker = sc.score_bins(_profiles(brng_mean=[0.02, 0.09],
                                           deflicker_absmax=[0.0, 0.0]))
    assert without[10.0].family_scores['legality'] == \
        with_flicker[10.0].family_scores['legality']


def test_crop_edge_iqr_is_not_scored():
    """cropdetect tracks content shape and brightness, not tracking error.

    Measured on the sample set: it was absent from 3 of 9 JPC reports, and
    where present the bins it promoted were a dark scene on one tape and a
    bright one on another. Rejecting degenerate boxes did not change any
    selection, which is what ruled it out — the influence was content.
    """
    assert 'crop_edge_iqr_max' not in {spec.field for spec in sc.METRICS}
    profiles = _profiles(crop_edge_iqr_max=[0.0, 700.0])
    assert sc.score_bins(profiles)[10.0].score == 0.0


def test_ydif_is_not_scored():
    """Motion is content, not damage — the gate uses it, scoring must not."""
    assert 'ydif_p95' not in {spec.field for spec in sc.METRICS}
    assert 'ydif_mean' not in {spec.field for spec in sc.METRICS}
    profiles = _profiles(ydif_p95=[1.0, 500.0])
    assert sc.score_bins(profiles)[10.0].score == 0.0


# ===========================================================================
# Suitability interaction
# ===========================================================================

def test_unsuitable_bins_are_left_out_entirely():
    profiles = _profiles(brng_mean=[0.02, 0.03, 0.9])
    verdicts = {20.0: BinVerdict(bin_start=20.0, suitable=False,
                                 reasons=('signal loss',), hard=True)}
    scores = sc.score_bins(profiles, verdicts)
    assert set(scores) == {0.0, 10.0}


def test_unsuitable_bins_do_not_shift_other_bins_ranks():
    """Ranking against a bin no period can occupy would distort every rank."""
    profiles = _profiles(brng_mean=[0.02, 0.03, 0.9])
    verdicts = {20.0: BinVerdict(bin_start=20.0, suitable=False,
                                 reasons=('signal loss',), hard=True)}
    scores = sc.score_bins(profiles, verdicts)
    assert scores[10.0].metric_ranks['brng_mean'] == 1.0   # top of what remains


def test_all_bins_unsuitable_yields_no_scores():
    profiles = _profiles(brng_mean=[0.9, 0.9])
    verdicts = {b: BinVerdict(bin_start=b, suitable=False, reasons=('x',), hard=True)
                for b in (0.0, 10.0)}
    assert sc.score_bins(profiles, verdicts) == {}


# ===========================================================================
# Edges and reporting
# ===========================================================================

def test_empty_profiles_yield_no_scores():
    assert sc.score_bins({}) == {}


def test_report_with_no_scorable_metric_scores_zero():
    profiles = _profiles(entropy_mean=[0.8, 0.9])
    scores = sc.score_bins(profiles)
    assert all(s.score == 0.0 for s in scores.values())


def test_rank_order_is_worst_first_and_stable():
    profiles = _profiles(brng_mean=[0.09, 0.02, 0.09])
    ranked = sc.rank_order(sc.score_bins(profiles))
    assert [b for b, _ in ranked] == [0.0, 20.0, 10.0]   # ties break on bin start


def test_describe_scores_names_the_families():
    profiles = _profiles(brng_mean=[0.02, 0.09])
    lines = sc.describe_scores(sc.score_bins(profiles))
    assert any('legality' in line for line in lines)
    assert len(lines) == 2


def test_describe_scores_respects_limit():
    profiles = _profiles(brng_mean=[0.02, 0.03, 0.04, 0.05, 0.06])
    assert len(sc.describe_scores(sc.score_bins(profiles), limit=2)) == 2


# ===========================================================================
# TOUT: sustained damage, not transitions
# ===========================================================================

def test_tout_is_scored_as_a_mean_not_a_percentile():
    """A bin of cuts has a high TOUT p95 and a low mean; dropout raises both.

    On operator-verified bins, p95 gave 0.0219-0.0407 for confirmed dropout
    regions against 0.0190 for a confirmed clean one — overlapping. The mean
    gave 0.0168-0.0214 against 0.0086.
    """
    fields = {spec.field for spec in sc.METRICS}
    assert 'tout_mean' in fields
    assert 'tout_p95' not in fields


def test_tout_below_the_floor_contributes_nothing():
    """Rank alone would promote some bin on every tape, dropouts or not."""
    profiles = _profiles(tout_mean=[0.002, 0.004, 0.006, 0.0086])
    scores = sc.score_bins(profiles)
    assert all(s.metric_ranks['tout_mean'] == 0.0 for s in scores.values())
    assert all(s.family_scores['impulsive'] == 0.0 for s in scores.values())


def test_verified_dropout_levels_clear_the_floor():
    """The three confirmed true positives, against the confirmed false one."""
    profiles = _profiles(tout_mean=[0.0086, 0.0168, 0.0192, 0.0214])
    scores = sc.score_bins(profiles)
    assert scores[0.0].family_scores['impulsive'] == 0.0        # false positive
    assert scores[10.0].family_scores['impulsive'] > 0.0
    assert scores[30.0].family_scores['impulsive'] == 1.0


def test_a_tape_with_no_dropouts_lets_legality_decide():
    """Impulsive scoring zero across the file is the correct outcome."""
    profiles = _profiles(tout_mean=[0.003, 0.005, 0.007, 0.004],
                         brng_mean=[0.02, 0.03, 0.04, 0.09])
    ranked = sc.rank_order(sc.score_bins(profiles))
    assert ranked[0][0] == 30.0                                  # worst BRNG bin
    assert ranked[0][1].dominant_family == 'legality'


# ===========================================================================
# evidence_within
# ===========================================================================

def _scores(**by_bin):
    return {float(b): sc.BinScore(bin_start=float(b), score=v,
                                  dominant_family='legality')
            for b, v in by_bin.items()}


def test_evidence_within_returns_scoring_bins_worst_first():
    scores = _scores(**{'0': 0.9, '10': 0.6, '20': 0.2})
    found = sc.evidence_within(0.0, 30.0, scores)
    assert [e.bin_start for e in found] == [0.0, 10.0]


def test_evidence_within_excludes_bins_outside_the_period():
    scores = _scores(**{'0': 0.9, '60': 0.9})
    found = sc.evidence_within(0.0, 60.0, scores)
    assert [e.bin_start for e in found] == [0.0]


def test_evidence_within_requires_the_whole_bin_inside():
    """A bin straddling the period edge describes time the period misses."""
    scores = _scores(**{'55': 0.9})
    assert sc.evidence_within(0.0, 60.0, scores) == []


def test_evidence_within_can_be_empty():
    """A period placed where nothing scored says so, rather than inventing."""
    scores = _scores(**{'0': 0.2, '10': 0.3})
    assert sc.evidence_within(0.0, 60.0, scores) == []
