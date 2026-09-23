"""Tests for checks.bin_suitability.

Phase 1 of the period-selection rework: decide which 10-second bins hold
analyzable picture at all, so period placement can avoid signal loss, static
and concealment repetition the way it already avoids black and bars.

The calibration tests at the bottom use values measured from real sample
reports — healthy JPC tapes must stay suitable, and the known-bad bins of the
LC tape must be gated. Those are the tests that fail if a threshold is moved
carelessly; the rest pin the structure of the decision.
"""

import pytest

from AV_Spex.checks import bin_suitability as bs
from AV_Spex.checks.qctools_bin_profile import BinProfile


def _profile(bin_start=0.0, frames=300, black_frames=0, **kwargs):
    return BinProfile(bin_start=bin_start, frames=frames,
                      black_frames=black_frames, **kwargs)


# ===========================================================================
# Hard reasons — no corroboration required
# ===========================================================================

def test_bin_with_no_picture_frames_is_unsuitable():
    a = bs.assess_bins({0.0: _profile(frames=300, black_frames=300)})
    assert a.verdicts[0.0].suitable is False
    assert a.verdicts[0.0].hard is True
    assert 'no picture frames' in a.verdicts[0.0].reasons[0]


def test_empty_bin_is_unsuitable():
    """A bin whose frames were all excluded as bars has nothing to measure."""
    a = bs.assess_bins({0.0: _profile(frames=0, excluded_frames=300)})
    assert a.verdicts[0.0].suitable is False


def test_sub_black_average_luma_is_unsuitable_alone():
    """Average luma under broadcast black means no picture, only noise."""
    a = bs.assess_bins({0.0: _profile(yavg_mean=54.0, ydif_mean=11.0)})
    verdict = a.verdicts[0.0]
    assert verdict.suitable is False
    assert verdict.hard is True
    assert 'below broadcast black' in verdict.reasons[0]


def test_sub_black_threshold_uses_report_scale():
    """The same frame is 54 in a 10-bit report and 13 in an 8-bit one."""
    ten = bs.assess_bins({0.0: _profile(yavg_mean=54.0)}, bit_depth_10=True)
    eight_ok = bs.assess_bins({0.0: _profile(yavg_mean=54.0)}, bit_depth_10=False)
    eight_bad = bs.assess_bins({0.0: _profile(yavg_mean=13.0)}, bit_depth_10=False)
    assert ten.verdicts[0.0].suitable is False
    assert eight_ok.verdicts[0.0].suitable is True     # 54 is normal picture at 8-bit
    assert eight_bad.verdicts[0.0].suitable is False


def test_luma_just_above_black_is_suitable():
    """JPC_AV_01056's noisy analog black averages 65 — black detection's job."""
    a = bs.assess_bins({0.0: _profile(yavg_mean=65.0)})
    assert a.verdicts[0.0].suitable is True


# ===========================================================================
# Soft reasons — corroboration required
# ===========================================================================

def test_single_soft_reason_is_not_enough():
    """Decorrelated frames alone could be a cut-heavy passage."""
    a = bs.assess_bins({0.0: _profile(ssim_mean=0.20, repeated_frames=0,
                                      entropy_mean=0.85, ydif_mean=12.0,
                                      vrep_mean=0.0, yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is True
    assert a.verdicts[0.0].reasons == ()


def test_two_soft_reasons_gate_the_bin():
    a = bs.assess_bins({0.0: _profile(ssim_mean=0.22, repeated_frames=160,
                                      entropy_mean=0.85, ydif_mean=12.0,
                                      vrep_mean=0.0, yavg_mean=300.0)})
    verdict = a.verdicts[0.0]
    assert verdict.suitable is False
    assert verdict.hard is False
    assert len(verdict.reasons) == 2


def test_repeated_field_fraction_is_relative_to_picture_frames():
    """Half the bin black, every picture frame repeated → gated on fraction."""
    a = bs.assess_bins({0.0: _profile(frames=300, black_frames=150,
                                      repeated_frames=140, ssim_mean=0.2,
                                      yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is False


def test_flat_field_and_repetition_gate_together():
    a = bs.assess_bins({0.0: _profile(entropy_mean=0.10, repeated_frames=200,
                                      ssim_mean=0.9, ydif_mean=5.0,
                                      yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is False


def test_decorrelated_luma_needs_scale_aware_threshold():
    """YDIF 300 is hash at 10-bit scale and impossible at 8-bit."""
    ten = bs.assess_bins({0.0: _profile(ydif_mean=300.0, ssim_mean=0.18,
                                        yavg_mean=500.0)}, bit_depth_10=True)
    assert ten.verdicts[0.0].suitable is False
    eight = bs.assess_bins({0.0: _profile(ydif_mean=40.0, ssim_mean=0.18,
                                          yavg_mean=120.0)}, bit_depth_10=False)
    assert eight.verdicts[0.0].suitable is True    # 40 < 0.20 * 255


def test_line_repetition_corroborates():
    a = bs.assess_bins({0.0: _profile(vrep_mean=0.9, ssim_mean=0.2,
                                      yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is False


# ===========================================================================
# Guards
# ===========================================================================

def test_small_picture_sample_is_not_soft_gated():
    """A bin that is 299/300 black yields one frame's statistics."""
    a = bs.assess_bins({0.0: _profile(frames=300, black_frames=299,
                                      ssim_mean=0.18, ydif_mean=400.0,
                                      yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is True
    assert a.verdicts[0.0].soft_metrics_available == 0


def test_soft_gate_needs_two_available_metrics():
    """One measure cannot corroborate itself, so the gate stays off."""
    a = bs.assess_bins({0.0: _profile(ssim_mean=0.10, yavg_mean=300.0)})
    assert a.verdicts[0.0].suitable is True
    assert a.verdicts[0.0].soft_metrics_available == 1


def test_signalstats_only_report_still_gets_hard_rules():
    """No ssim/entropy/idet — sub-black still gates, and YDIF+VREP can pair."""
    a = bs.assess_bins({0.0: _profile(yavg_mean=54.0, ydif_mean=11.0,
                                      vrep_mean=0.0)})
    assert a.verdicts[0.0].suitable is False
    assert a.verdicts[0.0].hard is True


def test_mostly_unanalyzable_tape_drops_the_soft_gate():
    """When static is the tape's normal, there is no better place to sample."""
    profiles = {float(i * 10): _profile(bin_start=float(i * 10), ssim_mean=0.2,
                                        repeated_frames=200, yavg_mean=300.0)
                for i in range(10)}
    a = bs.assess_bins(profiles)
    assert a.soft_gate_applied is False
    assert a.unsuitable_regions == []
    assert a.note is not None
    assert all(v.suitable for v in a.verdicts.values())


def test_hard_gating_survives_the_global_guard():
    """Sub-black bins are excluded however much of the tape they cover."""
    profiles = {float(i * 10): _profile(bin_start=float(i * 10), yavg_mean=50.0)
                for i in range(10)}
    a = bs.assess_bins(profiles)
    assert all(not v.suitable for v in a.verdicts.values())
    assert a.unsuitable_regions == [(0.0, 100.0)]


def test_guard_counts_only_content_bins():
    """Black bins are not evidence that the soft gate is over-firing."""
    profiles = {float(i * 10): _profile(bin_start=float(i * 10), frames=300,
                                        black_frames=300)
                for i in range(8)}
    profiles[80.0] = _profile(bin_start=80.0, ssim_mean=0.2,
                              repeated_frames=200, yavg_mean=300.0)
    profiles[90.0] = _profile(bin_start=90.0, ssim_mean=0.95,
                              repeated_frames=0, yavg_mean=300.0)
    a = bs.assess_bins(profiles)
    assert a.soft_gate_applied is True
    assert a.verdicts[80.0].suitable is False
    assert a.verdicts[90.0].suitable is True


# ===========================================================================
# Regions and reporting
# ===========================================================================

def test_contiguous_unsuitable_bins_merge_into_one_region():
    profiles = {}
    for i in range(6):
        start = float(i * 10)
        bad = 1 <= i <= 3
        profiles[start] = _profile(bin_start=start,
                                   yavg_mean=50.0 if bad else 300.0)
    a = bs.assess_bins(profiles)
    assert a.unsuitable_regions == [(10.0, 40.0)]


def test_separated_unsuitable_bins_stay_separate():
    profiles = {0.0: _profile(yavg_mean=50.0),
                10.0: _profile(bin_start=10.0, yavg_mean=300.0),
                20.0: _profile(bin_start=20.0, yavg_mean=50.0)}
    a = bs.assess_bins(profiles)
    assert a.unsuitable_regions == [(0.0, 10.0), (20.0, 30.0)]


def test_all_suitable_yields_no_regions():
    a = bs.assess_bins({0.0: _profile(yavg_mean=300.0, ssim_mean=0.95,
                                      repeated_frames=0, entropy_mean=0.8,
                                      ydif_mean=8.0, vrep_mean=0.0)})
    assert a.unsuitable_regions == []
    assert a.verdicts[0.0].suitable is True


def test_empty_profiles_yield_empty_assessment():
    a = bs.assess_bins({})
    assert a.verdicts == {}
    assert a.unsuitable_regions == []
    assert a.note is None


def test_reasons_for_unknown_bin_is_empty():
    a = bs.assess_bins({0.0: _profile(yavg_mean=300.0)})
    assert a.reasons_for(999.0) == ()


def test_describe_assessment_lists_regions_with_reasons():
    a = bs.assess_bins({0.0: _profile(yavg_mean=50.0)})
    lines = bs.describe_assessment(a)
    assert len(lines) == 1
    assert '0.0s - 10.0s' in lines[0]
    assert 'broadcast black' in lines[0]


def test_describe_assessment_truncates():
    profiles = {}
    for i in range(0, 20, 2):
        profiles[float(i * 10)] = _profile(bin_start=float(i * 10), yavg_mean=50.0)
        profiles[float((i + 1) * 10)] = _profile(bin_start=float((i + 1) * 10),
                                                 yavg_mean=300.0)
    a = bs.assess_bins(profiles)
    lines = bs.describe_assessment(a, max_regions=3)
    assert len(lines) == 4
    assert lines[-1].strip().startswith('...')


# ===========================================================================
# Calibration against measured sample values
# ===========================================================================
# Values below are the real per-bin measurements from the sample reports; the
# comment names the file and bin. If a threshold change breaks one of these,
# it is changing a decision on a tape someone has actually looked at.

HEALTHY_BINS = {
    # JPC_AV_01581 @ 10s — ordinary content, 10-bit
    '01581 content': dict(ssim_mean=0.952, repeated_frames=0, entropy_mean=0.761,
                          ydif_mean=5.17, vrep_mean=0.0, yavg_mean=389.0),
    # JPC_AV_01663 @ 1860s — cut-heavy/noisy passage, the lowest-SSIM healthy bin
    '01663 busy': dict(ssim_mean=0.753, repeated_frames=0, entropy_mean=0.849,
                       ydif_mean=124.5, vrep_mean=0.0, yavg_mean=419.6),
    # JPC_AV_01710 @ 1250s — low motion, 28% of frames flagged repeated
    '01710 low motion': dict(ssim_mean=0.820, repeated_frames=83, entropy_mean=0.899,
                             ydif_mean=51.0, vrep_mean=0.0, yavg_mean=437.2),
    # JPC_AV_01056 @ 90s — noisy analog tape, lowest healthy entropy
    '01056 noisy': dict(ssim_mean=0.930, repeated_frames=0, entropy_mean=0.401,
                        ydif_mean=8.4, vrep_mean=0.0, yavg_mean=200.0),
    # JPC_AV_02212 @ 1700s — signalstats-only report, no ssim/entropy/idet
    '02212 sparse': dict(ydif_mean=70.3, vrep_mean=0.0, yavg_mean=396.8),
}

UNANALYZABLE_BINS = {
    # 21459403 @ 80s — lead-in static
    '21459403 static': dict(ssim_mean=0.229, repeated_frames=283, entropy_mean=0.767,
                            ydif_mean=136.5, vrep_mean=0.004, yavg_mean=226.2),
    # 21459403 @ 2080s — signal loss, whole frame below broadcast black
    '21459403 signal loss': dict(ssim_mean=0.723, repeated_frames=107,
                                 entropy_mean=0.298, ydif_mean=11.0,
                                 vrep_mean=0.02, yavg_mean=53.9),
    # 21459403 @ 2970s — end-of-tape hash
    '21459403 hash': dict(ssim_mean=0.182, repeated_frames=0, entropy_mean=0.905,
                          ydif_mean=411.8, vrep_mean=0.0, yavg_mean=552.3),
}


@pytest.mark.parametrize('name', sorted(HEALTHY_BINS))
def test_measured_healthy_bins_stay_suitable(name):
    a = bs.assess_bins({0.0: _profile(**HEALTHY_BINS[name])})
    assert a.verdicts[0.0].suitable is True, a.verdicts[0.0].reasons


@pytest.mark.parametrize('name', sorted(UNANALYZABLE_BINS))
def test_measured_unanalyzable_bins_are_gated(name):
    a = bs.assess_bins({0.0: _profile(**UNANALYZABLE_BINS[name])})
    assert a.verdicts[0.0].suitable is False
