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
