"""Tests for checks.bars_detection_clams.

Covers:
* _format_timestamp / write_durations_csv — pure formatting + I/O
* _resolve_reference_path — bundled PNG existence check
* get_video_fps — ffprobe wrapping (fraction, plain number, fallback, all failure modes)
* run_clams_bars_detection — orchestrator with cv2.VideoCapture + SSIM mocked.
  Verifies: fps==None short-circuit, reference-load failure short-circuit,
  VideoCapture-fail short-circuit, SSIM-driven run detection, min_frame_count
  filter, stop_after_one early exit, in-progress run flush at end of buffer,
  per-frame SSIM CSV format + append vs overwrite mode, signal emission.
"""

import csv
import os
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from AV_Spex.checks import bars_detection_clams as bd
from AV_Spex.utils import ffprobe_probe


# ---------------------------------------------------------------------------
# _format_timestamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0.0, "00:00:00.000"),
    (1.5, "00:00:01.500"),
    (61.25, "00:01:01.250"),
    (3661.001, "01:01:01.001"),
])
def test_format_timestamp(seconds, expected):
    assert bd._format_timestamp(seconds) == expected


# ---------------------------------------------------------------------------
# write_durations_csv
# ---------------------------------------------------------------------------

def _read_csv_rows(path):
    with open(path) as f:
        return list(csv.reader(f))


def test_write_durations_csv_with_runs(tmp_path):
    bd.write_durations_csv(str(tmp_path), [
        ("primary", 0.0, 5.0),
        ("second_pass", 60.0, 65.5),
    ])

    rows = _read_csv_rows(tmp_path / bd.DURATIONS_CSV_NAME)
    assert rows[0] == ["clams bars detection color bars found:"]
    assert rows[1] == ["primary", "00:00:00.000", "00:00:05.000"]
    assert rows[2] == ["second_pass", "00:01:00.000", "00:01:05.500"]


def test_write_durations_csv_empty(tmp_path):
    bd.write_durations_csv(str(tmp_path), [])
    rows = _read_csv_rows(tmp_path / bd.DURATIONS_CSV_NAME)
    assert rows == [["clams bars detection found no color bars"]]


# ---------------------------------------------------------------------------
# _resolve_reference_path
# ---------------------------------------------------------------------------

def test_resolve_reference_path_returns_existing_bundled_png():
    """The bundled SMPTE bars reference should ship with the package."""
    path = bd._resolve_reference_path()
    assert os.path.isfile(path)
    assert path.endswith(bd.REFERENCE_FILENAME)


def test_resolve_reference_path_missing_file_raises(monkeypatch):
    monkeypatch.setattr(bd, "_bundle_dir", lambda: "/no/such/dir")
    with pytest.raises(FileNotFoundError):
        bd._resolve_reference_path()


# ---------------------------------------------------------------------------
# get_video_fps
# ---------------------------------------------------------------------------

def _ffprobe_response(streams):
    """Wrap a `streams=[{...}]` dict as a JSON-encoded subprocess result."""
    import json
    return MagicMock(returncode=0, stdout=json.dumps({"streams": streams}), stderr="")


def test_get_video_fps_fraction_r_frame_rate(monkeypatch):
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: _ffprobe_response([
        {"r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001"}
    ]))
    fps = bd.get_video_fps("/v.mkv")
    assert fps == pytest.approx(29.97, abs=0.01)


def test_get_video_fps_integer_string(monkeypatch):
    """Plain numeric string also accepted as fps value."""
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: _ffprobe_response([
        # No slash — function treats this as a plain float
        {"r_frame_rate": "25", "avg_frame_rate": ""}
    ]))
    fps = bd.get_video_fps("/v.mkv")
    assert fps == 25.0


def test_get_video_fps_avg_frame_rate_fallback(monkeypatch):
    """If r_frame_rate is empty/invalid, avg_frame_rate is consulted."""
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: _ffprobe_response([
        {"r_frame_rate": "", "avg_frame_rate": "60/1"}
    ]))
    fps = bd.get_video_fps("/v.mkv")
    assert fps == 60.0


def test_get_video_fps_zero_denominator_returns_none(monkeypatch):
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: _ffprobe_response([
        {"r_frame_rate": "30/0", "avg_frame_rate": "0/0"}
    ]))
    assert bd.get_video_fps("/v.mkv") is None


def test_get_video_fps_malformed_json_returns_none(monkeypatch):
    proc = MagicMock(returncode=0, stdout="not json", stderr="")
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: proc)
    assert bd.get_video_fps("/v.mkv") is None


def test_get_video_fps_no_streams_returns_none(monkeypatch):
    monkeypatch.setattr(ffprobe_probe.subprocess, "run", lambda *a, **kw: _ffprobe_response([]))
    assert bd.get_video_fps("/v.mkv") is None


def test_get_video_fps_subprocess_error_returns_none(monkeypatch):
    monkeypatch.setattr(
        bd.subprocess, "run",
        MagicMock(side_effect=bd.subprocess.SubprocessError("ffprobe missing")),
    )
    assert bd.get_video_fps("/v.mkv") is None


# ---------------------------------------------------------------------------
# run_clams_bars_detection — orchestrator with mocked cv2 + SSIM
# ---------------------------------------------------------------------------

class _FakeVideoCapture:
    """Minimal cv2.VideoCapture stand-in. Returns successful reads forever
    (the iteration is bounded by the sample range in the function under test)."""

    def __init__(self, opened=True):
        self._opened = opened

    def isOpened(self):
        return self._opened

    def set(self, _prop, _value):
        return True

    def read(self):
        # Return a 1x1x3 BGR frame (cvtColor will reduce to 1x1 grayscale,
        # which then gets resized to the reference shape).
        frame = np.zeros((1, 1, 3), dtype=np.uint8)
        return True, frame

    def release(self):
        pass


def _patch_cv2_and_reference(monkeypatch, ssim_scores):
    """Patch the cv2 + skimage call surface used by run_clams_bars_detection.

    `ssim_scores` is a list of scores returned in order on each SSIM call.
    Reference image is a 1x1 grayscale array.
    """
    monkeypatch.setattr(bd, "_resolve_reference_path", lambda: "/fake/ref.png")
    monkeypatch.setattr(bd.cv2, "imread", lambda *_a, **_kw: np.zeros((1, 1), dtype=np.uint8))
    monkeypatch.setattr(bd.cv2, "VideoCapture", lambda _p: _FakeVideoCapture(opened=True))
    score_iter = iter(ssim_scores)
    monkeypatch.setattr(bd, "structural_similarity", lambda *_a, **_kw: next(score_iter))


def test_run_clams_bars_detection_no_fps_returns_empty(monkeypatch):
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: None)
    runs = bd.run_clams_bars_detection("/v.mkv", "/tmp", "video_x")
    assert runs == []


def test_run_clams_bars_detection_reference_load_failure(monkeypatch):
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 30.0)
    monkeypatch.setattr(bd, "_resolve_reference_path", lambda: "/fake/ref.png")
    monkeypatch.setattr(bd.cv2, "imread", lambda *_a, **_kw: None)  # imread fail returns None
    runs = bd.run_clams_bars_detection("/v.mkv", "/tmp", "video_x")
    assert runs == []


def test_run_clams_bars_detection_video_open_failure(monkeypatch):
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 30.0)
    monkeypatch.setattr(bd, "_resolve_reference_path", lambda: "/fake/ref.png")
    monkeypatch.setattr(bd.cv2, "imread", lambda *_a, **_kw: np.zeros((1, 1), dtype=np.uint8))
    monkeypatch.setattr(bd.cv2, "VideoCapture", lambda _p: _FakeVideoCapture(opened=False))

    runs = bd.run_clams_bars_detection("/v.mkv", "/tmp", "video_x")
    assert runs == []


def test_run_clams_bars_detection_finds_run_meeting_min_frame_count(monkeypatch, tmp_path):
    """Sequence: 2 below, 5 above, 5 below.
    sample_ratio=1, stop=12, min_frame_count=3.
    Expected: one run from frame 2 to frame 7 (gap of 5 > 3)."""
    scores = [0.0, 0.0,    # frames 0, 1 below threshold
              1.0, 1.0, 1.0, 1.0, 1.0,  # frames 2-6 above
              0.0, 0.0, 0.0, 0.0, 0.0]  # frames 7-11 below
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)  # 1 fps → seconds == frame index

    runs = bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=12,
        min_frame_count=3,
        stop_after_one=False,
    )
    # One run, ending when score drops below at frame 7 (start=2, end=7)
    assert runs == [(2.0, 7.0)]


def test_run_clams_bars_detection_filters_run_below_min_frame_count(monkeypatch, tmp_path):
    """Sequence with a single above-threshold frame should be filtered out."""
    scores = [0.0,         # frame 0
              1.0,         # frame 1 (single in-run frame)
              0.0, 0.0, 0.0]
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    runs = bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=5,
        min_frame_count=2,
        stop_after_one=False,
    )
    # 2 - 1 = 1, not > 2 → filtered
    assert runs == []


def test_run_clams_bars_detection_stop_after_one_breaks_after_first_run(monkeypatch, tmp_path):
    """After completing one run, stop_after_one=True should halt scanning."""
    # Two distinct above-threshold runs. With stop_after_one, only the first is kept.
    scores = [1.0, 1.0, 1.0, 1.0, 0.0,    # run 1: frames 0..3 then drop
              1.0, 1.0, 1.0, 1.0, 0.0]    # run 2: frames 5..8 then drop
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    runs = bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=10,
        min_frame_count=2,
        stop_after_one=True,
    )
    assert len(runs) == 1
    assert runs[0] == (0.0, 4.0)  # first run only


def test_run_clams_bars_detection_in_progress_run_flushed_at_end(monkeypatch, tmp_path):
    """If the loop ends while still in a run, the run should be appended."""
    scores = [0.0, 1.0, 1.0, 1.0, 1.0]  # in-run from frame 1 onward, never drops
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    runs = bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=5,
        min_frame_count=2,
    )
    # Run was still active when scan ended; appended at end of function
    # cur_frame after last sample is 4, run_start_frame is 1 → 4-1=3 > 2, kept.
    assert runs == [(1.0, 4.0)]


def test_run_clams_bars_detection_writes_ssim_csv(monkeypatch, tmp_path):
    scores = [0.5, 0.9]
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.7,
        sample_ratio=1,
        stop_at_frame=2,
        min_frame_count=10,  # ensure no run is recorded
        stop_after_one=False,
        pass_label="primary",
    )

    csv_path = tmp_path / f"video_x{bd.SSIM_SCORES_CSV_SUFFIX}"
    assert csv_path.exists()
    rows = _read_csv_rows(csv_path)
    # Header + 2 score rows
    assert rows[0] == ["pass", "frame", "timestamp", "ssim_score", "exceeds_threshold"]
    assert len(rows) == 3
    # Frame index, score, exceeds flag
    assert rows[1][0] == "primary"
    assert rows[1][1] == "0"
    assert rows[1][3] == "0.500000"
    assert rows[1][4] == "false"   # 0.5 not > 0.7
    assert rows[2][3] == "0.900000"
    assert rows[2][4] == "true"    # 0.9 > 0.7


def test_run_clams_bars_detection_append_ssim_csv_preserves_prior_rows(monkeypatch, tmp_path):
    """When append_ssim_csv=True and the CSV already exists, header is not rewritten."""
    csv_path = tmp_path / f"video_x{bd.SSIM_SCORES_CSV_SUFFIX}"
    csv_path.write_text("pass,frame,timestamp,ssim_score,exceeds_threshold\nprior,0,00:00:00.000,0.123456,false\n")

    scores = [0.5]
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.7,
        sample_ratio=1,
        stop_at_frame=1,
        min_frame_count=10,
        stop_after_one=False,
        pass_label="second_pass",
        append_ssim_csv=True,
    )

    rows = _read_csv_rows(csv_path)
    # Header + prior + new row → 3 rows; header should appear only once
    assert rows[0] == ["pass", "frame", "timestamp", "ssim_score", "exceeds_threshold"]
    assert rows[1][0] == "prior"
    assert rows[2][0] == "second_pass"


def test_run_clams_bars_detection_emits_progress_signals(monkeypatch, tmp_path):
    # Many samples so progress emit fires at least once during the scan
    scores = [0.0] * 200
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    signals = MagicMock()
    bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=200,
        min_frame_count=10,
        stop_after_one=False,
        signals=signals,
    )
    # Final 100% emit at minimum
    assert signals.clams_detection_progress.emit.called
    assert signals.clams_detection_progress.emit.call_args_list[-1].args[0] == 100


def test_run_clams_bars_detection_check_cancelled_breaks_early(monkeypatch, tmp_path):
    scores = [1.0] * 20
    _patch_cv2_and_reference(monkeypatch, scores)
    monkeypatch.setattr(bd, "get_video_fps", lambda _p: 1.0)

    cancel = MagicMock(side_effect=[False, True] + [True] * 100)

    bd.run_clams_bars_detection(
        "/v.mkv", str(tmp_path), "video_x",
        threshold=0.5,
        sample_ratio=1,
        stop_at_frame=20,
        min_frame_count=2,
        check_cancelled=cancel,
    )
    assert cancel.called
