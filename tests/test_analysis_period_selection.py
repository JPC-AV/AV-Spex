# tests/test_analysis_period_selection.py
from types import SimpleNamespace

import pytest

from AV_Spex.checks.frame_analysis import (
    EnhancedFrameAnalysis,
    IntegratedSignalstatsAnalyzer,
)


@pytest.fixture
def analyzer():
    """EnhancedFrameAnalysis without running its heavy __init__"""
    return EnhancedFrameAnalysis.__new__(EnhancedFrameAnalysis)


@pytest.fixture
def signalstats_analyzer():
    """IntegratedSignalstatsAnalyzer without running its heavy __init__"""
    instance = IntegratedSignalstatsAnalyzer.__new__(IntegratedSignalstatsAnalyzer)
    instance.duration = 1800.0
    return instance


def _histogram(clusters, bin_size=10.0):
    """Build a {bin_start: count} histogram from (start, end, count_per_bin) clusters"""
    histogram = {}
    for start, end, count in clusters:
        bin_start = int(start // bin_size) * bin_size
        while bin_start < end:
            histogram[bin_start] = histogram.get(bin_start, 0) + count
            bin_start += bin_size
    return histogram


def test_distribution_targets_distinct_clusters(analyzer):
    # Three well-separated clusters on a 30-minute tape
    histogram = _histogram([(100, 130, 250), (700, 730, 200), (1400, 1430, 150)])
    periods = analyzer._analyze_qctools_violation_distribution(
        [], num_periods=3, period_duration=60,
        video_duration=1800.0, histogram=histogram)

    assert len(periods) == 3
    starts = [start for start, _ in periods]
    assert starts == sorted(starts)
    # Each cluster gets a period containing it
    for cluster_start in (100, 700, 1400):
        assert any(start <= cluster_start + 15 <= start + dur for start, dur in periods)


def test_distribution_min_separation_prevents_stacking(analyzer):
    # One dominant burst: adjacent saturated bins must not claim all periods
    histogram = _histogram([(1000, 1060, 300), (200, 220, 50), (500, 520, 40)])
    periods = analyzer._analyze_qctools_violation_distribution(
        [], num_periods=3, period_duration=60,
        video_duration=1800.0, histogram=histogram)

    assert len(periods) == 3
    starts = sorted(start for start, _ in periods)
    # Periods sit apart, not stacked around 1000s
    assert starts[1] - starts[0] >= 60
    assert starts[2] - starts[1] >= 60
    assert sum(1 for s in starts if 900 <= s <= 1100) <= 2


def test_distribution_excludes_black_and_tail_bins(analyzer):
    histogram = _histogram([
        (300, 330, 200),      # real content cluster
        (600, 630, 300),      # inside black segment
        (1780, 1800, 300),    # end-of-tape static
    ])
    periods = analyzer._analyze_qctools_violation_distribution(
        [], num_periods=1, period_duration=60,
        video_duration=1800.0,
        black_segments=[(595.0, 640.0)],
        histogram=histogram)

    assert len(periods) == 1
    start, dur = periods[0]
    # The only eligible cluster is at 300s
    assert start <= 315 <= start + dur


def test_distribution_severity_breaks_count_ties(analyzer):
    # Both bins saturated at 300; severity must decide
    histogram = {100.0: 300, 900.0: 300}
    severity = {100.0: 5.0, 900.0: 50.0}
    periods = analyzer._analyze_qctools_violation_distribution(
        [], num_periods=1, period_duration=60,
        video_duration=1800.0, histogram=histogram, severity=severity)

    start, dur = periods[0]
    assert start <= 905 <= start + dur


def test_distribution_falls_back_to_violations_list(analyzer):
    violations = [SimpleNamespace(timestamp=t, violation_score=1.0)
                  for t in (100.0, 101.0, 102.0, 500.0)]
    periods = analyzer._analyze_qctools_violation_distribution(
        violations, num_periods=1, period_duration=60, video_duration=1800.0)

    assert len(periods) == 1
    start, dur = periods[0]
    assert start <= 101 <= start + dur


def test_distribution_empty(analyzer):
    assert analyzer._analyze_qctools_violation_distribution(
        [], num_periods=3, period_duration=60, video_duration=1800.0) == []


def test_find_analysis_periods_tops_up_to_requested_count(signalstats_analyzer):
    # QCTools suggested only one period; the deficit is filled evenly
    periods = signalstats_analyzer._find_analysis_periods(
        content_start=10.0, color_bars_end=0.0, duration=60, num_periods=3,
        qctools_periods=[(1000.0, 60)])

    assert len(periods) == 3
    starts = [start for start, _ in periods]
    assert starts == sorted(starts)
    assert (1000.0, 60) in periods
    # No overlapping periods
    for (s1, d1), (s2, d2) in zip(periods, periods[1:]):
        assert s1 + d1 <= s2


def test_fill_periods_avoids_black_segments(signalstats_analyzer):
    black = [(0.0, 900.0)]  # first half of the tape is black
    periods = signalstats_analyzer._fill_periods_to_count(
        [], num_periods=2, duration=60, effective_start=20.0,
        black_segments=black)

    assert len(periods) == 2
    for start, dur in periods:
        overlap = max(0.0, min(start + dur, 900.0) - max(start, 0.0))
        assert overlap / dur <= 0.25


def test_fill_periods_no_room_returns_unchanged(signalstats_analyzer):
    signalstats_analyzer.duration = 100.0  # too short for a 60s period after margins
    existing = [(10.0, 60)]
    result = signalstats_analyzer._fill_periods_to_count(
        existing, num_periods=3, duration=60, effective_start=10.0)
    assert result == existing
