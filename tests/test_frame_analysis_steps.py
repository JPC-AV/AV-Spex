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
# passed config controlled method/duration_limit but NOT the enable_* flags —
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
