"""Tests for the AnalysisStep driver in EnhancedFrameAnalysis.

The end-to-end frame-analysis regression runs with signals=None, so the
progress-reset and completion-signal branches never execute there. They are
covered here instead, with a recording stand-in for the GUI signals object.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from AV_Spex.checks.frame_analysis import AnalysisStep, EnhancedFrameAnalysis


class _Signals:
    """Records what the driver emitted, in order."""

    def __init__(self):
        self.progress = []
        self.completed = []
        self.frame_analysis_progress = SimpleNamespace(emit=self.progress.append)
        self.step_completed = SimpleNamespace(emit=self.completed.append)


def _driver(signals=None, cancelled=False):
    """An EnhancedFrameAnalysis with just the attributes the driver touches."""
    obj = object.__new__(EnhancedFrameAnalysis)
    obj.signals = signals
    obj.check_cancelled = (lambda: cancelled)
    return obj


def _step(key='k', label='Label', enabled=True, value='v', **kw):
    return AnalysisStep(key=key, label=label, enabled=enabled,
                        run=lambda: value, **kw)


def test_result_is_stored_under_the_step_key():
    results = {}
    obj = _driver()
    assert obj._run_analysis_steps([_step(key='bitplane_check', value={'status': 'valid'})],
                                   results, None) is True
    assert results['bitplane_check'] == {'status': 'valid'}


def test_none_result_stores_nothing():
    results = {}
    _driver()._run_analysis_steps([_step(value=None)], results, None)
    assert results == {}


def test_completion_signal_is_emitted_with_the_prefixed_label():
    signals = _Signals()
    _driver(signals)._run_analysis_steps([_step(label='Duplicate Frame Detection')], {}, signals)
    assert signals.completed == ['Frame Analysis - Duplicate Frame Detection']


def test_progress_bar_is_reset_before_a_step_runs():
    """The GUI shows a per-step bar, so each step restarts it at zero."""
    signals = _Signals()
    _driver(signals)._run_analysis_steps([_step()], {}, signals)
    assert signals.progress == [0]


def test_progress_reset_can_be_opted_out():
    """Bitplane deliberately does not reset the bar."""
    signals = _Signals()
    _driver(signals)._run_analysis_steps([_step(reset_progress=False)], {}, signals)
    assert signals.progress == []


def test_no_signals_object_is_not_an_error():
    """Frame analysis also runs headless from the CLI."""
    results = {}
    assert _driver(None)._run_analysis_steps([_step()], results, None) is True
    assert results['k'] == 'v'


def test_disabled_step_does_not_run_and_logs_the_skip(caplog):
    ran = []
    step = AnalysisStep(key='k', label='L', enabled=False,
                        run=lambda: ran.append(1),
                        skip_message="Skipping the thing (disabled in config)")
    results = {}
    with caplog.at_level('WARNING'):
        _driver()._run_analysis_steps([step], results, None)
    assert ran == [] and results == {}
    assert "Skipping the thing" in caplog.text


def test_disabled_step_emits_no_completion_signal():
    signals = _Signals()
    _driver(signals)._run_analysis_steps([_step(enabled=False)], {}, signals)
    assert signals.completed == []


def test_cancellation_stops_before_running_anything():
    ran = []
    step = AnalysisStep(key='k', label='L', enabled=True, run=lambda: ran.append(1))
    assert _driver(cancelled=True)._run_analysis_steps([step], {}, None) is False
    assert ran == []


def test_steps_run_in_order():
    order = []
    steps = [
        AnalysisStep(key='a', label='A', enabled=True, run=lambda: order.append('a')),
        AnalysisStep(key='b', label='B', enabled=True, run=lambda: order.append('b')),
    ]
    _driver()._run_analysis_steps(steps, {}, None)
    assert order == ['a', 'b']


def test_start_message_is_logged(caplog):
    with caplog.at_level('INFO'):
        _driver()._run_analysis_steps(
            [_step(start_message="Starting dropped sample detection...")], {}, None)
    assert "Starting dropped sample detection" in caplog.text


# ===========================================================================
# analyze() must honour the FrameAnalysisConfig it is given
#
# It previously read self.checks_config unconditionally, so an explicitly
# passed config controlled method but NOT the enable_* flags —
# those silently came from whatever was last saved in the GUI.
# ===========================================================================

def _analyzer(saved_config):
    """An EnhancedFrameAnalysis wired with a known 'saved' config."""
    from unittest.mock import MagicMock
    from pathlib import Path
    obj = object.__new__(EnhancedFrameAnalysis)
    obj.video_path = Path('/v.mkv')
    obj.video_id = 'V1'
    obj.output_dir = Path('/tmp')
    obj.signals = None
    obj.check_cancelled = lambda: False
    obj.checks_config = SimpleNamespace(
        outputs=SimpleNamespace(frame_analysis=saved_config))
    obj.qctools_report = None
    obj.qctools_parser = None
    obj.signalstats_analyzer = MagicMock()
    obj.border_detector = MagicMock()
    obj.brng_analyzer = None
    return obj


def _steps_chosen(analyzer, **analyze_kwargs):
    """Run analyze() only as far as the enable decisions.

    _run_analysis_steps returning False makes analyze() return immediately,
    with results['steps_enabled'] already populated.
    """
    analyzer._run_analysis_steps = lambda steps, results, signals: False
    return analyzer.analyze(signals=None, **analyze_kwargs)['steps_enabled']


def _config(**overrides):
    from AV_Spex.utils.config_setup import FrameAnalysisConfig
    return FrameAnalysisConfig(**overrides)


def test_passed_config_overrides_the_saved_one():
    saved = _config(enable_border_detection=False, enable_brng_analysis=False,
                    enable_signalstats=False)
    chosen = _steps_chosen(_analyzer(saved), frame_config=_config())
    assert chosen['border_detection'] is True
    assert chosen['brng_analysis'] is True
    assert chosen['signalstats'] is True


def test_passed_config_can_also_disable():
    saved = _config()  # everything on
    off = _config(enable_bitplane_check=False, enable_border_detection=False,
                  enable_brng_analysis=False, enable_signalstats=False,
                  enable_dropped_sample_detection=False,
                  enable_duplicate_frame_detection=False)
    chosen = _steps_chosen(_analyzer(saved), frame_config=off)
    assert not any(chosen.values())


def test_without_an_argument_the_saved_config_still_applies():
    """Callers that pass nothing keep the previous behaviour."""
    saved = _config(enable_border_detection=False, enable_signalstats=False)
    chosen = _steps_chosen(_analyzer(saved))
    assert chosen['border_detection'] is False
    assert chosen['signalstats'] is False
    assert chosen['bitplane_check'] is True


def test_entry_point_forwards_the_config_to_analyze(monkeypatch):
    """analyze_frame_quality must hand its config down, not just read fields."""
    from unittest.mock import MagicMock
    from AV_Spex.checks import frame_analysis as fa

    fake = MagicMock()
    fake.analyze.return_value = {}
    monkeypatch.setattr(fa, "EnhancedFrameAnalysis", lambda *a, **kw: fake)

    cfg = _config(enable_signalstats=False)
    fa.analyze_frame_quality("/v.mkv", frame_config=cfg)

    assert fake.analyze.call_args.kwargs["frame_config"] is cfg


# ===========================================================================
# Border refinement loop stops when a round makes no meaningful improvement
#
# _is_meaningful_improvement() used to be computed and then ignored, so the
# loop always ran until BRNG stopped asking for adjustment or the retry limit
# was hit — re-running border detection and BRNG on borders that no longer
# moved.
# ===========================================================================

def _brng_result(n_violations, worst_pct, edge_pct, requires_adjustment=True):
    from AV_Spex.checks.frame_analysis import BRNGAnalysisResult, FrameViolation
    violations = [
        FrameViolation(frame_num=i, timestamp=float(i), brng_value=worst_pct,
                       violation_score=worst_pct / 100, violation_percentage=worst_pct)
        for i in range(n_violations)
    ]
    return BRNGAnalysisResult(
        violations=violations,
        aggregate_patterns={'edge_violation_percentage': edge_pct,
                            'continuous_edge_percentage': 0.0,
                            'expansion_recommendations': {'bottom': 5}},
        actionable_report={}, thumbnails=[],
        requires_border_adjustment=requires_adjustment,
        refinement_recommendations={'bottom': 5},
    )


def _run_refinement(monkeypatch, tmp_path, brng_rounds, areas):
    """Run analyze() with sophisticated borders + BRNG, feeding BRNG results
    and refined active areas in order. Returns (results, brng_call_count)."""
    from AV_Spex.checks import frame_analysis as fa
    from AV_Spex.checks.frame_analysis import BorderDetectionResult

    def border(area):
        return BorderDetectionResult(active_area=area, border_regions={},
                                     detection_method='sophisticated',
                                     quality_frame_hints=[])

    brng_iter = iter(brng_rounds)
    calls = []

    class FakeBRNG:
        def __init__(self, *a, **kw):
            self.could_not_run_reason = None

        def analyze_with_differential_detection(self, **kw):
            calls.append(kw)
            return next(brng_iter)

    monkeypatch.setattr(fa, "DifferentialBRNGAnalyzer", FakeBRNG)

    config = _config(border_detection_mode='sophisticated', enable_signalstats=False,
                     enable_bitplane_check=False, enable_dropped_sample_detection=False,
                     enable_duplicate_frame_detection=False, auto_retry_borders=True,
                     max_border_retries=3)
    analyzer = _analyzer(config)
    analyzer.output_dir = tmp_path
    analyzer._run_analysis_steps = lambda steps, results, signals: True
    analyzer._detect_color_bars_duration = lambda: 0
    analyzer._get_video_duration = lambda: 600.0
    analyzer._log_brng_analysis_summary = lambda *a, **kw: None
    analyzer._create_refinement_comparison = lambda **kw: None
    analyzer._generate_summary = lambda results: ""
    analyzer._save_results = lambda results: None
    analyzer.signalstats_analyzer.last_resort_period_note = None
    analyzer.signalstats_analyzer._validate_periods_against_black_segments = lambda p, *a, **kw: p

    area_iter = iter(areas)
    analyzer.border_detector.detect_borders_with_quality_assessment.return_value = border(next(area_iter))
    analyzer.border_detector.generate_border_visualization.return_value = True
    analyzer.border_detector.refine_borders.side_effect = lambda *a, **kw: border(next(area_iter))

    results = analyzer.analyze(method='sophisticated', max_refinement_iterations=3,
                               frame_config=config, signals=None)
    return results, len(calls)


def test_refinement_stops_when_a_round_does_not_improve(monkeypatch, tmp_path):
    rounds = [
        _brng_result(100, 5.0, 40.0),   # initial
        _brng_result(95, 5.0, 38.0),    # refinement 1: <20% fewer, same worst, edge barely down
        _brng_result(50, 2.0, 10.0),    # would-be refinement 2 (must not run)
        _brng_result(10, 1.0, 5.0),
    ]
    areas = [(10, 10, 700, 466), (10, 10, 700, 461), (10, 10, 700, 456), (10, 10, 700, 451)]

    results, brng_calls = _run_refinement(monkeypatch, tmp_path, rounds, areas)

    assert results['refinement_iterations'] == 1
    assert brng_calls == 2
    assert results['refinement_history'][0]['improved'] is False
    assert results['final_borders']['active_area'] == (10, 10, 700, 461)


def test_refinement_continues_while_rounds_keep_improving(monkeypatch, tmp_path):
    rounds = [
        _brng_result(100, 5.0, 40.0),
        _brng_result(60, 5.0, 40.0),    # 40% fewer
        _brng_result(30, 5.0, 40.0),    # 50% fewer
        _brng_result(10, 5.0, 40.0),    # 67% fewer, still asks for adjustment
    ]
    areas = [(10, 10, 700, 466), (10, 10, 700, 461), (10, 10, 700, 456), (10, 10, 700, 451)]

    results, brng_calls = _run_refinement(monkeypatch, tmp_path, rounds, areas)

    assert results['refinement_iterations'] == 3   # retry limit
    assert brng_calls == 4
    assert all(h['improved'] for h in results['refinement_history'])


def test_refinement_stops_when_borders_stop_moving(monkeypatch, tmp_path):
    """Edge violations still dominant but refine_borders changed nothing: repeating
    the same analysis can't help."""
    rounds = [
        _brng_result(100, 5.0, 80.0),
        _brng_result(100, 5.0, 80.0),
        _brng_result(100, 5.0, 80.0),
    ]
    same = (10, 10, 700, 466)
    areas = [same, same, same]

    results, brng_calls = _run_refinement(monkeypatch, tmp_path, rounds, areas)

    assert results['refinement_iterations'] == 1
    assert brng_calls == 2


# ===========================================================================
# Skip Color Bars off + no bars detected
#
# processing passes color_bars_end_time=None when no bars were found. With
# brng_skip_color_bars off it was never replaced, and period selection /
# the BRNG fallback raised TypeError — caught upstream, so the whole frame
# analysis silently returned nothing.
# ===========================================================================

def _run_without_bars(monkeypatch, tmp_path, enable_signalstats, skip_color_bars=False,
                      color_bars_end_time=None, bars_regions=None, duplicate_calls=None):
    from AV_Spex.checks import frame_analysis as fa
    from AV_Spex.checks.frame_analysis import BorderDetectionResult

    brng_calls = []

    class FakeBRNG:
        def __init__(self, *a, **kw):
            self.could_not_run_reason = None

        def analyze_with_differential_detection(self, **kw):
            brng_calls.append(kw)
            return _brng_result(0, 0.0, 0.0, requires_adjustment=False)

    monkeypatch.setattr(fa, "DifferentialBRNGAnalyzer", FakeBRNG)

    config = _config(border_detection_mode='simple', enable_signalstats=enable_signalstats,
                     enable_bitplane_check=False, enable_dropped_sample_detection=False,
                     enable_duplicate_frame_detection=duplicate_calls is not None,
                     brng_skip_color_bars=skip_color_bars)
    analyzer = _analyzer(config)
    analyzer.output_dir = tmp_path
    if duplicate_calls is None:
        analyzer._run_analysis_steps = lambda steps, results, signals: True
    else:
        analyzer._detect_duplicate_frames = lambda **kw: duplicate_calls.append(kw)
        analyzer._run_analysis_steps = lambda steps, results, signals: (
            [step.run() for step in steps if step.enabled], True)[1]
    analyzer._get_video_duration = lambda: 600.0
    analyzer._log_brng_analysis_summary = lambda *a, **kw: None
    analyzer._generate_summary = lambda results: ""
    analyzer._save_results = lambda results: None
    analyzer._create_signalstats_frame_thumbnails = lambda *a, **kw: None
    analyzer.signalstats_analyzer.last_resort_period_note = None
    analyzer.signalstats_analyzer._validate_periods_against_black_segments = lambda p, *a, **kw: p
    analyzer.signalstats_analyzer.analyze_with_signalstats.return_value = fa.SignalstatsResult(
        violation_percentage=0.0, max_brng=0.0, avg_brng=0.0,
        analysis_periods=[(10.0, 60), (200.0, 60), (400.0, 60)],
        diagnosis="", used_qctools=False, comparison_results=[])
    analyzer.border_detector.detect_borders_with_quality_assessment.return_value = BorderDetectionResult(
        active_area=(25, 25, 670, 436), border_regions={}, detection_method='simple',
        quality_frame_hints=[])
    analyzer.border_detector.generate_border_visualization.return_value = True
    analyzer.border_detector.width, analyzer.border_detector.height = 720, 486

    results = analyzer.analyze(method='simple', skip_color_bars=skip_color_bars,
                               color_bars_end_time=color_bars_end_time, bars_regions=bars_regions,
                               frame_config=config, signals=None)
    return results, brng_calls, analyzer


def test_no_bars_with_skip_off_brng_fallback_periods_do_not_crash(monkeypatch, tmp_path):
    results, brng_calls, _ = _run_without_bars(monkeypatch, tmp_path, enable_signalstats=False)

    assert 'brng_analysis' in results
    assert len(brng_calls) == 1
    periods = brng_calls[0]['analysis_periods']
    assert len(periods) == 3
    assert periods[0][0] >= 10   # content starts 10s in when there are no bars


def test_no_bars_with_skip_off_signalstats_gets_zero_not_none(monkeypatch, tmp_path):
    results, brng_calls, analyzer = _run_without_bars(monkeypatch, tmp_path, enable_signalstats=True)

    call = analyzer.signalstats_analyzer.analyze_with_signalstats.call_args
    assert call.kwargs['color_bars_end_time'] == 0
    assert 'color_bars_end_time' not in results   # no bars is still reported as no bars


def test_find_analysis_periods_accepts_no_bars():
    from AV_Spex.checks.frame_analysis import IntegratedSignalstatsAnalyzer
    analyzer = IntegratedSignalstatsAnalyzer.__new__(IntegratedSignalstatsAnalyzer)
    analyzer.duration = 600.0
    analyzer.last_resort_period_note = None

    periods = analyzer._find_analysis_periods(10, None, 60, 3, None,
                                              qctools_periods=None, black_segments=[])

    assert len(periods) == 3


def test_first_signalstats_pass_does_not_pre_add_the_bars_margin(monkeypatch, tmp_path):
    """The margin is added once, inside period selection; analyze() must not add it too."""
    results, brng_calls, analyzer = _run_without_bars(monkeypatch, tmp_path, enable_signalstats=True)
    call = analyzer.signalstats_analyzer.analyze_with_signalstats.call_args
    assert call.kwargs['content_start_time'] == 0


# ===========================================================================
# Skip Color Bars off: detected bars stay in the BRNG-side analysis
#
# The option used to have almost no effect: head and mid-file bars arrive in
# bars_regions and were excluded from period placement, signalstats and BRNG
# whatever it was set to. Off now means bars are analyzed there, as the docs
# describe. Duplicate-frame detection still excludes them either way.
# ===========================================================================

BARS = dict(color_bars_end_time=52.0, bars_regions=[(30.0, 52.0), (900.0, 910.0)])


def test_skip_color_bars_on_excludes_bars_everywhere(monkeypatch, tmp_path):
    dup = []
    _, brng_calls, analyzer = _run_without_bars(
        monkeypatch, tmp_path, enable_signalstats=True, skip_color_bars=True,
        duplicate_calls=dup, **BARS)

    ss = analyzer.signalstats_analyzer.analyze_with_signalstats.call_args.kwargs
    assert ss['color_bars_end_time'] == 52.0
    assert (30.0, 52.0) in ss['black_segments'] and (900.0, 910.0) in ss['black_segments']
    assert dup[0]['color_bars_end_time'] == 52.0
    assert (900.0, 910.0) in dup[0]['black_segments']


def test_skip_color_bars_off_keeps_bars_in_signalstats_and_brng(monkeypatch, tmp_path):
    dup = []
    _, brng_calls, analyzer = _run_without_bars(
        monkeypatch, tmp_path, enable_signalstats=True, skip_color_bars=False,
        duplicate_calls=dup, **BARS)

    ss = analyzer.signalstats_analyzer.analyze_with_signalstats.call_args.kwargs
    assert ss['color_bars_end_time'] == 0
    assert ss['black_segments'] == []
    assert brng_calls[0]['skip_start_seconds'] == 0
    # duplicate-frame detection still excludes the bars
    assert dup[0]['color_bars_end_time'] == 52.0
    assert (30.0, 52.0) in dup[0]['black_segments'] and (900.0, 910.0) in dup[0]['black_segments']


def test_skip_color_bars_off_brng_fallback_periods_may_start_in_bars(monkeypatch, tmp_path):
    """Without signalstats, the BRNG fallback spreads periods from 10s in, not bars end + 10s."""
    _, brng_calls, _ = _run_without_bars(
        monkeypatch, tmp_path, enable_signalstats=False, skip_color_bars=False, **BARS)
    on_calls = _run_without_bars(
        monkeypatch, tmp_path, enable_signalstats=False, skip_color_bars=True, **BARS)[1]

    assert brng_calls[0]['analysis_periods'][0][0] < on_calls[0]['analysis_periods'][0][0]
