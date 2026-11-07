"""Tests for AV_Spex.checks.dropped_sample_detection.

The point of interest is the failure path. A healthy file and a detector that
could not run both find nothing, so "measured, found nothing" and "could not
measure" have to be separate states. That actually happened: a refactor left
five names unimported, every failure was logged and swallowed, and the
detector reported "clean — No indicators of dropped samples detected".

The verdict rests on the audio discontinuity scan; the spectrogram is a
picture for the report and the A/V duration difference is context. These
tests pin both of those roles.
"""

from pathlib import Path

import pytest

from AV_Spex.checks import dropped_sample_detection as ds
from AV_Spex.checks.audio_discontinuity import DiscontinuityScan, SilenceGap, SpliceEvent


def _scan(splices=(), gaps=(), measured=True):
    return DiscontinuityScan(
        splices=list(splices), silence_gaps=list(gaps),
        streams_scanned=1 if measured else 0, seconds_scanned=60.0,
        sample_rate=48000, measured=measured)


@pytest.fixture
def detect(tmp_path, monkeypatch):
    """Run the detector with each input stubbed to a chosen outcome."""
    def _run(spectrogram=None, scan=None, durations=(60.0, 60.0, 48000)):
        monkeypatch.setattr(ds, '_generate_spectrogram', lambda *a, **k: spectrogram)
        monkeypatch.setattr(ds, '_scan_audio', lambda *a, **k: scan)
        monkeypatch.setattr(ds, '_get_av_durations', lambda *a, **k: durations)
        # No real file: keep timecode labelling off ffprobe.
        monkeypatch.setattr(ds.ffprobe_probe, 'frame_rate', lambda *a, **k: 30000 / 1001)
        monkeypatch.setattr(ds, '_get_video_start_timecode', lambda *a, **k: None)
        return ds.detect_dropped_samples(
            Path('/v.mkv'), 'V1', tmp_path, signals=None, check_cancelled=lambda: False)
    return _run


# ---------------------------------------------------------------------------
# The scan could not run — must not read as clean
# ---------------------------------------------------------------------------

def test_scan_failure_is_unknown_not_clean(detect):
    result = detect(scan=None)
    assert result.status == 'unknown', "a failed detector must not report clean"
    assert result.audio_scan_measured is False


def test_scan_that_decoded_nothing_is_unknown(detect):
    result = detect(scan=_scan(measured=False))
    assert result.status == 'unknown'


def test_unknown_message_says_the_check_did_not_run(detect):
    result = detect(scan=None)
    assert 'not a clean result' in result.message
    assert 'did not run' in result.message


def test_unknown_does_not_claim_a_zero_finding(detect):
    result = detect(scan=None)
    assert result.spike_count == 0
    assert result.combined_score == 0.0
    assert result.splice_events == [] and result.silence_gaps == []


def test_helper_raising_does_not_produce_a_clean_verdict(tmp_path, monkeypatch):
    """An exception inside the scan is swallowed and logged; the verdict must
    still not come back clean."""
    def _boom(*a, **k):
        raise NameError("name 'time' is not defined")

    monkeypatch.setattr(ds, '_generate_spectrogram', lambda *a, **k: None)
    monkeypatch.setattr(ds, '_get_av_durations', lambda *a, **k: (None, None, 0))
    monkeypatch.setattr(ds.audio_discontinuity, 'scan_audio_discontinuities', _boom)
    monkeypatch.setattr(ds.ffprobe_probe, 'duration', lambda *a, **k: None)

    result = ds.detect_dropped_samples(
        Path('/v.mkv'), 'V1', tmp_path, signals=None, check_cancelled=lambda: False)
    assert result.status == 'unknown'


# ---------------------------------------------------------------------------
# Verdicts from the scan
# ---------------------------------------------------------------------------

def test_measured_and_nothing_found_is_clean(detect):
    result = detect(scan=_scan())
    assert result.status == 'clean'
    assert 'No indicators' in result.message


def test_one_splice_is_a_warning_with_timecode(detect):
    result = detect(scan=_scan(splices=[SpliceEvent(4.004, 0, 8400.0, -15.5, 2)]))
    assert result.status == 'warning'
    assert result.spike_count == 1
    assert result.spike_timestamps == [4.004]
    assert result.splice_events[0]['timecode'] == '00:00:04:00'


def test_three_splices_are_critical(detect):
    ev = [SpliceEvent(t, 0, 100.0, -20.0, 2) for t in (10.0, 20.0, 30.0)]
    assert detect(scan=_scan(splices=ev)).status == 'critical'


def test_a_silence_gap_is_critical(detect):
    result = detect(scan=_scan(gaps=[SilenceGap(13.6512, 15.2481, 0)]))
    assert result.status == 'critical'
    assert result.silence_gaps[0]['duration_ms'] == pytest.approx(1596.9, abs=0.1)
    assert result.silence_gaps[0]['start_timecode'] == '00:00:13:19'
    assert result.estimated_loss_ms == pytest.approx(1596.9, abs=0.1)


def test_the_same_gap_on_two_streams_counts_once(detect):
    """A capture losing its input shows the gap on every stream."""
    gaps = [SilenceGap(13.0, 14.0, 0), SilenceGap(13.0, 14.0, 1)]
    result = detect(scan=_scan(gaps=gaps))
    assert result.message.startswith('1 digital-silence gap')
    assert result.estimated_loss_ms == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Supporting signals don't drive the verdict
# ---------------------------------------------------------------------------

def test_duration_difference_alone_is_not_evidence(detect):
    """Healthy captures routinely differ by 20-400 ms."""
    result = detect(scan=_scan(), durations=(60.4, 60.0, 48000))
    assert result.status == 'clean'
    assert result.duration_diff_ms == pytest.approx(400.0, abs=0.5)


def test_missing_spectrogram_does_not_change_the_verdict(detect, tmp_path):
    png = tmp_path / "spec.png"; png.write_text("")
    with_png = detect(spectrogram=png, scan=_scan())
    without = detect(spectrogram=None, scan=_scan())
    assert with_png.status == without.status == 'clean'
    assert without.spectrogram_measured is False


def test_missing_durations_still_gives_a_verdict(detect):
    result = detect(scan=_scan(), durations=(None, None, 0))
    assert result.status == 'clean'
    assert result.durations_measured is False
