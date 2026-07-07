"""Tests for checks/tone_leak_check.py — reference-tone leak detection.

Unit tests exercise the window metrics, channel verdict, and event merging on
synthetic signals. An integration test decodes a synthesized WAV through the
real ffmpeg pipe when ffmpeg is available.
"""

import csv
import os
import shutil
import wave

import numpy as np
import pytest

from AV_Spex.checks import tone_leak_check as tlc


SR = tlc.TONE_LEAK_ANALYSIS_SR
WIN = tlc.TONE_LEAK_WINDOW_SEC * SR

RNG = np.random.default_rng(seed=1701)


def _noise(level_db, n=WIN):
    """White noise at roughly level_db dBFS RMS."""
    return RNG.standard_normal(n) * 10 ** (level_db / 20)


def _tone_comb(level_db, n=WIN):
    """A distorted 1 kHz tone: fundamental + harmonics, each at level_db dBFS."""
    t = np.arange(n) / SR
    amp = np.sqrt(2) * 10 ** (level_db / 20)  # sine amplitude for given RMS
    sig = np.zeros(n)
    for h in tlc.TONE_LEAK_HARMONICS:
        sig += amp * np.sin(2 * np.pi * h * t)
    return sig


# ---------------------------------------------------------------------------
# _window_metrics
# ---------------------------------------------------------------------------

def test_window_metrics_comb_stands_out_over_hiss():
    seg = _noise(-60) + _tone_comb(-75)
    rms_db, comb_db, tone_level_db = tlc._window_metrics(seg)
    assert rms_db > tlc.TONE_LEAK_SILENCE_FLOOR_DB
    assert comb_db >= tlc.TONE_LEAK_COMB_THRESHOLD_DB
    # The reported 1 kHz level should approximate the synthesized -75 dBFS.
    assert abs(tone_level_db - (-75)) < 2.0


def test_window_metrics_hiss_only_scores_low():
    seg = _noise(-60)
    _, comb_db, _ = tlc._window_metrics(seg)
    assert comb_db < tlc.TONE_LEAK_COMB_THRESHOLD_DB


def test_window_metrics_digital_silence_below_floor():
    seg = np.zeros(WIN)
    rms_db, _, _ = tlc._window_metrics(seg)
    assert rms_db <= tlc.TONE_LEAK_SILENCE_FLOOR_DB


# ---------------------------------------------------------------------------
# _channel_verdict
# ---------------------------------------------------------------------------

def _fake_metrics(n_active, n_flagged, n_silent=0):
    """Build a metrics list: flagged windows (comb 20), active-clean (comb 4), silent."""
    m = [(-30.0, 20.0, -90.0)] * n_flagged
    m += [(-30.0, 4.0, -110.0)] * (n_active - n_flagged)
    m += [(-95.0, 3.0, -140.0)] * n_silent
    return m


def test_channel_verdict_flags_persistent_comb():
    v = tlc._channel_verdict(_fake_metrics(n_active=100, n_flagged=10))
    assert v['tone_leak_detected'] is True
    assert v['flagged_windows'] == 10
    assert v['active_windows'] == 100


def test_channel_verdict_needs_min_window_count():
    # 3 of 20 windows = 15% > 5% fraction, but under the 4-window minimum.
    v = tlc._channel_verdict(_fake_metrics(n_active=20, n_flagged=3))
    assert v['tone_leak_detected'] is False


def test_channel_verdict_needs_min_fraction():
    # 4 of 200 = 2% < 5% fraction, despite meeting the window count.
    v = tlc._channel_verdict(_fake_metrics(n_active=200, n_flagged=4))
    assert v['tone_leak_detected'] is False


def test_channel_verdict_silence_excluded_from_active():
    v = tlc._channel_verdict(_fake_metrics(n_active=40, n_flagged=6, n_silent=400))
    assert v['active_windows'] == 40
    assert v['tone_leak_detected'] is True


def test_channel_verdict_all_silent_channel_is_clean():
    v = tlc._channel_verdict(_fake_metrics(n_active=0, n_flagged=0, n_silent=50))
    assert v['tone_leak_detected'] is False
    assert v['active_windows'] == 0
    assert v['median_comb_db'] is None


# ---------------------------------------------------------------------------
# _flagged_events
# ---------------------------------------------------------------------------

def test_flagged_events_merges_consecutive_windows():
    clean = (-30.0, 4.0, -110.0)
    hot = (-30.0, 20.0, -90.0)
    metrics = [clean, hot, hot, hot, clean, clean, hot, hot]
    events = tlc._flagged_events(metrics)
    assert len(events) == 2
    start_s, end_s, mean_comb, peak_comb, _ = events[0]
    assert start_s == 1 * tlc.TONE_LEAK_WINDOW_SEC
    assert end_s == 4 * tlc.TONE_LEAK_WINDOW_SEC
    assert mean_comb == pytest.approx(20.0)
    assert peak_comb == pytest.approx(20.0)
    # Second run extends to the end of the metrics list.
    assert events[1][0] == 6 * tlc.TONE_LEAK_WINDOW_SEC
    assert events[1][1] == 8 * tlc.TONE_LEAK_WINDOW_SEC


def test_flagged_events_silent_windows_break_runs():
    hot = (-30.0, 20.0, -90.0)
    silent_but_comby = (-95.0, 20.0, -140.0)
    events = tlc._flagged_events([hot, silent_but_comby, hot])
    assert len(events) == 2


# ---------------------------------------------------------------------------
# analyzeToneLeak end-to-end on a synthesized WAV (real ffmpeg/ffprobe)
# ---------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _write_stereo_wav(path, left, right):
    data = np.stack([left, right], axis=1)
    pcm = (np.clip(data, -1, 1) * 32767).astype('<i2')
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


@needs_ffmpeg
def test_analyze_tone_leak_flags_leaky_channel_only(tmp_path):
    n = 5 * WIN  # 40 seconds -> 5 windows, enough to meet the 4-window minimum
    left = _noise(-60, n) + _tone_comb(-75, n)
    right = _noise(-60, n)
    wav_path = tmp_path / "synthetic.wav"
    _write_stereo_wav(wav_path, left, right)

    results = tlc.analyzeToneLeak(str(wav_path), str(tmp_path))

    assert results is not None
    assert results['tone_leak_detected'] is True
    assert results['flagged_channels'] == [(0, 0)]
    assert results['channels'][(0, 1)]['tone_leak_detected'] is False

    summary_csv = tmp_path / "qct-parse_tone_leak_summary.csv"
    events_csv = tmp_path / "qct-parse_tone_leak_events.csv"
    assert summary_csv.is_file() and events_csv.is_file()

    with open(summary_csv) as f:
        rows = list(csv.reader(f))
    verdicts = {(r[0], r[1]): r[-1] for r in rows if r and r[0] in ("0", "1")}
    assert verdicts[("0", "0")] == "Yes"
    assert verdicts[("0", "1")] == "No"

    with open(events_csv) as f:
        ev_rows = list(csv.reader(f))
    # Header + at least one flagged region on stream 0 channel 0.
    assert len(ev_rows) >= 2
    assert ev_rows[1][2] == "0" and ev_rows[1][3] == "0"


class _FakeSignals:
    """Stands in for ProcessingSignals: captures tone_leak_progress emits."""

    class _Sig:
        def __init__(self):
            self.values = []

        def emit(self, value):
            self.values.append(value)

    def __init__(self):
        self.tone_leak_progress = self._Sig()


@needs_ffmpeg
def test_analyze_tone_leak_emits_own_progress_pass(tmp_path):
    n = 5 * WIN
    wav_path = tmp_path / "progress.wav"
    _write_stereo_wav(wav_path, _noise(-60, n), _noise(-60, n))

    signals = _FakeSignals()
    results = tlc.analyzeToneLeak(str(wav_path), str(tmp_path), signals=signals,
                                  total_duration=n / SR)
    assert results is not None

    values = signals.tone_leak_progress.values
    # A dedicated 0-100 pass: starts at 0, ends at 100, never goes backwards.
    assert values[0] == 0
    assert values[-1] == 100
    assert values == sorted(values)
    # Intermediate per-window updates were emitted, not just the endpoints.
    assert len(values) > 2


@needs_ffmpeg
def test_analyze_tone_leak_clean_file_writes_empty_events(tmp_path):
    n = 5 * WIN
    left = _noise(-60, n)
    right = _noise(-62, n)
    wav_path = tmp_path / "clean.wav"
    _write_stereo_wav(wav_path, left, right)

    results = tlc.analyzeToneLeak(str(wav_path), str(tmp_path))

    assert results is not None
    assert results['tone_leak_detected'] is False
    assert results['flagged_channels'] == []

    with open(tmp_path / "qct-parse_tone_leak_events.csv") as f:
        ev_rows = list(csv.reader(f))
    assert len(ev_rows) == 1  # header only
