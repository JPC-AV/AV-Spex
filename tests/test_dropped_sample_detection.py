"""Tests for AV_Spex.checks.dropped_sample_detection.

The point of interest is the failure path. Dropped-sample detection combines
two independent signals — spectrogram spikes and the audio/video duration
difference — and a healthy file scores zero on both. So when both signals fail
to be *measured*, the naive result is indistinguishable from a genuine pass.
That actually happened: a refactor left five names unimported, every failure was
logged and swallowed, and the detector reported
"clean — No indicators of dropped samples detected".

These tests pin the distinction between "measured, found nothing" and
"could not measure".
"""

from pathlib import Path

import pytest

from AV_Spex.checks import dropped_sample_detection as ds


@pytest.fixture
def detect(tmp_path, monkeypatch):
    """Run the detector with both signals stubbed to chosen outcomes."""
    def _run(spectrogram=None, spikes=(0, []), durations=(None, None, 0)):
        monkeypatch.setattr(ds, '_generate_spectrogram', lambda *a, **k: spectrogram)
        monkeypatch.setattr(ds, '_analyze_spectrogram_spikes', lambda *a, **k: spikes)
        monkeypatch.setattr(ds, '_get_av_durations', lambda *a, **k: durations)
        return ds.detect_dropped_samples(
            Path('/v.mkv'), 'V1', tmp_path, signals=None, check_cancelled=lambda: False)
    return _run


# ---------------------------------------------------------------------------
# Both signals failed — the case that used to read as clean
# ---------------------------------------------------------------------------

def test_both_signals_unmeasurable_is_unknown_not_clean(detect, tmp_path):
    result = detect(spectrogram=None, durations=(None, None, 0))
    assert result.status == 'unknown', "a failed detector must not report clean"
    assert result.spectrogram_measured is False
    assert result.durations_measured is False


def test_unknown_message_says_the_check_did_not_run(detect):
    result = detect(spectrogram=None, durations=(None, None, 0))
    assert 'not a clean result' in result.message
    assert 'did not run' in result.message


def test_unknown_does_not_claim_a_zero_finding(detect):
    """Reporting 0 spikes would imply the spectrogram was searched."""
    result = detect(spectrogram=None, durations=(None, None, 0))
    assert result.spike_count == 0
    assert result.combined_score == 0.0
    assert result.spectrogram_path is None


# ---------------------------------------------------------------------------
# A genuinely clean file still reports clean
# ---------------------------------------------------------------------------

def test_measured_and_nothing_found_is_clean(detect, tmp_path):
    png = tmp_path / "spec.png"; png.write_text("")
    result = detect(spectrogram=png, spikes=(0, []), durations=(60.0, 60.0, 48000))
    assert result.status == 'clean'
    assert result.spectrogram_measured is True
    assert result.durations_measured is True
    assert 'No indicators' in result.message


def test_findings_are_reported_as_before(detect, tmp_path):
    png = tmp_path / "spec.png"; png.write_text("")
    result = detect(spectrogram=png, spikes=(37, [1.0, 2.0]), durations=(60.0, 60.077, 48000))
    assert result.status in ('warning', 'critical')
    assert result.spike_count == 37
    assert result.duration_diff_ms == pytest.approx(77.0, abs=0.5)


# ---------------------------------------------------------------------------
# One signal missing — a usable verdict, but say so
# ---------------------------------------------------------------------------

def test_missing_spectrogram_still_uses_the_duration_signal(detect):
    result = detect(spectrogram=None, durations=(60.0, 60.077, 48000))
    assert result.status != 'unknown', "one working signal is still a measurement"
    assert result.spectrogram_measured is False
    assert result.durations_measured is True
    assert 'spectrogram unavailable' in result.message


def test_missing_durations_still_uses_the_spectrogram_signal(detect, tmp_path):
    png = tmp_path / "spec.png"; png.write_text("")
    result = detect(spectrogram=png, spikes=(12, [1.0]), durations=(None, None, 0))
    assert result.status != 'unknown'
    assert result.durations_measured is False
    assert 'duration comparison unavailable' in result.message


def test_a_clean_verdict_from_one_signal_is_qualified(detect, tmp_path):
    """Clean-on-one-signal must not read like clean-on-both."""
    png = tmp_path / "spec.png"; png.write_text("")
    result = detect(spectrogram=png, spikes=(0, []), durations=(None, None, 0))
    assert result.status == 'clean'
    assert 'unavailable' in result.message


# ---------------------------------------------------------------------------
# The regression that motivated all of this
# ---------------------------------------------------------------------------

def test_helper_raising_does_not_produce_a_clean_verdict(tmp_path, monkeypatch):
    """A NameError inside a helper is swallowed and logged; the verdict must
    still not come back clean."""
    def _boom(*a, **k):
        raise NameError("name 'time' is not defined")

    monkeypatch.setattr(ds, '_generate_spectrogram', lambda *a, **k: None)
    monkeypatch.setattr(ds, '_get_av_durations', lambda *a, **k: (None, None, 0))
    monkeypatch.setattr(ds, '_analyze_spectrogram_spikes', _boom)

    result = ds.detect_dropped_samples(
        Path('/v.mkv'), 'V1', tmp_path, signals=None, check_cancelled=lambda: False)
    assert result.status == 'unknown'
