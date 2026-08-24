"""Tests for AV_Spex.utils.ffprobe_probe — the single ffprobe entry point.

Covers the accessors and, in particular, the two fallbacks and the parsing
robustness that the scattered per-module copies used to lack.
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from AV_Spex.utils import ffprobe_probe as fp


def _proc(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _responses(monkeypatch, *outputs):
    """Feed successive ffprobe calls the given stdout values, in order."""
    queue = list(outputs)
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(cmd)
        out = queue.pop(0) if queue else ""
        return out if isinstance(out, MagicMock) else _proc(out)

    monkeypatch.setattr(fp.subprocess, "run", _run)
    return calls


# ---------------------------------------------------------------------------
# _as_float — the shared rational/number parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("29.97", 29.97),
    ("25", 25.0),
    ("30000/1001", 30000 / 1001),
    ("60/1", 60.0),
])
def test_as_float_parses_numbers_and_rationals(raw, expected):
    assert fp._as_float(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "   ", "N/A", "30/0", "0/0", "garbage", None])
def test_as_float_rejects_unusable_values(raw):
    assert fp._as_float(raw) is None


def test_as_float_tolerates_trailing_separator():
    """ffprobe's csv output gains a trailing separator when the stream carries
    side data, which is what broke the old per-module parsers."""
    assert fp._as_float("25/1,".rstrip(',')) == 25.0


# ---------------------------------------------------------------------------
# duration
# ---------------------------------------------------------------------------

def test_duration_reads_the_container(monkeypatch):
    _responses(monkeypatch, "1800.5\n")
    assert fp.duration("/v.mkv") == pytest.approx(1800.5)


def test_duration_falls_back_to_the_video_stream(monkeypatch):
    """Some MKVs report N/A for format=duration; the stream still knows."""
    calls = _responses(monkeypatch, "N/A\n", "1800.5\n")
    assert fp.duration("/v.mkv") == pytest.approx(1800.5)
    assert len(calls) == 2
    assert "-select_streams" in calls[1]


def test_duration_returns_none_when_both_are_unavailable(monkeypatch):
    _responses(monkeypatch, "N/A\n", "")
    assert fp.duration("/v.mkv") is None


def test_duration_returns_none_when_ffprobe_fails(monkeypatch):
    monkeypatch.setattr(fp.subprocess, "run",
                        MagicMock(side_effect=OSError("no ffprobe")))
    assert fp.duration("/v.mkv") is None


def test_duration_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(fp.subprocess, "run",
                        MagicMock(side_effect=subprocess.TimeoutExpired("ffprobe", 10)))
    assert fp.duration("/v.mkv") is None


def test_probe_passes_a_timeout(monkeypatch):
    """A probe that can hang would stall the whole run."""
    seen = {}

    def _run(cmd, *a, **kw):
        seen.update(kw)
        return _proc("1.0")

    monkeypatch.setattr(fp.subprocess, "run", _run)
    fp.duration("/v.mkv")
    assert seen.get("timeout") == fp.DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# frame_rate
# ---------------------------------------------------------------------------

def _stream_json(**fields):
    return json.dumps({"streams": [fields]})


def test_frame_rate_reads_r_frame_rate(monkeypatch):
    _responses(monkeypatch, _stream_json(r_frame_rate="30000/1001",
                                         avg_frame_rate="30000/1001"))
    assert fp.frame_rate("/v.mkv") == pytest.approx(29.97, abs=0.01)


def test_frame_rate_falls_back_to_avg_frame_rate(monkeypatch):
    _responses(monkeypatch, _stream_json(r_frame_rate="", avg_frame_rate="60/1"))
    assert fp.frame_rate("/v.mkv") == 60.0


def test_frame_rate_accepts_a_plain_number(monkeypatch):
    _responses(monkeypatch, _stream_json(r_frame_rate="25", avg_frame_rate=""))
    assert fp.frame_rate("/v.mkv") == 25.0


def test_frame_rate_rejects_a_zero_denominator(monkeypatch):
    _responses(monkeypatch, _stream_json(r_frame_rate="30/0", avg_frame_rate="0/0"))
    assert fp.frame_rate("/v.mkv") is None


def test_frame_rate_survives_side_data(monkeypatch):
    """A stream with side_data_list used to defeat the csv-based parsers."""
    _responses(monkeypatch, json.dumps({"streams": [
        {"r_frame_rate": "25/1", "avg_frame_rate": "25/1", "side_data_list": [{}]}
    ]}))
    assert fp.frame_rate("/v.mov") == 25.0


def test_frame_rate_returns_none_without_streams(monkeypatch):
    _responses(monkeypatch, json.dumps({"streams": []}))
    assert fp.frame_rate("/v.mkv") is None


def test_frame_rate_returns_none_on_malformed_json(monkeypatch):
    _responses(monkeypatch, "not json")
    assert fp.frame_rate("/v.mkv") is None


# ---------------------------------------------------------------------------
# start_timecode
# ---------------------------------------------------------------------------

def test_start_timecode_prefers_the_stream_tag(monkeypatch):
    calls = _responses(monkeypatch, "01:00:00;00\n")
    assert fp.start_timecode("/v.mkv") == "01:00:00;00"
    assert len(calls) == 1


def test_start_timecode_falls_back_to_the_container_tag(monkeypatch):
    calls = _responses(monkeypatch, "", "00:00:00:09\n")
    assert fp.start_timecode("/v.mkv") == "00:00:00:09"
    assert len(calls) == 2


def test_start_timecode_absent_is_none_not_an_error(monkeypatch):
    """Most files carry no timecode tag; that is normal, not a failure."""
    _responses(monkeypatch, "", "")
    assert fp.start_timecode("/v.mkv") is None


# ---------------------------------------------------------------------------
# audio
# ---------------------------------------------------------------------------

def _audio_json(*pairs):
    return json.dumps({"streams": [{"index": i, "channels": c} for i, c in pairs]})


def test_audio_streams_parses_index_and_channels(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 1), (2, 1)))
    assert fp.audio_streams("/v.mxf") == [fp.AudioStream(1, 1), fp.AudioStream(2, 1)]


def test_audio_stream_channels_single_stereo(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 2)))
    assert fp.audio_stream_channels("/v.mkv") == [2]


def test_audio_stream_channels_two_mono_streams(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 1), (2, 1)))
    assert fp.audio_stream_channels("/v.mxf") == [1, 1]


def test_audio_stream_count_counts_streams_not_channels(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 2)))
    assert fp.audio_stream_count("/v.mkv") == 1


def test_audio_stream_count_multiple_mono_streams(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 1), (2, 1), (3, 1), (4, 1)))
    assert fp.audio_stream_count("/v.mxf") == 4


def test_no_audio_is_an_empty_list_not_a_failure(monkeypatch):
    """A file with no audio must be distinguishable from a failed probe —
    callers that rebuild audio depend on telling them apart."""
    _responses(monkeypatch, json.dumps({"streams": []}))
    assert fp.audio_streams("/v.mkv") == []
    _responses(monkeypatch, json.dumps({"streams": []}))
    assert fp.audio_stream_count("/v.mkv") == 0


def test_audio_probe_failure_is_none(monkeypatch):
    monkeypatch.setattr(fp.subprocess, "run", MagicMock(side_effect=OSError("boom")))
    assert fp.audio_streams("/v.mkv") is None
    assert fp.audio_stream_channels("/v.mkv") is None
    assert fp.audio_stream_count("/v.mkv") is None


def test_first_audio_channel_count(monkeypatch):
    _responses(monkeypatch, _audio_json((1, 2), (2, 1)))
    assert fp.first_audio_channel_count("/v.mkv") == 2


def test_first_audio_channel_count_without_audio_is_none(monkeypatch):
    _responses(monkeypatch, json.dumps({"streams": []}))
    assert fp.first_audio_channel_count("/v.mkv") is None


# ---------------------------------------------------------------------------
# video_dimensions
# ---------------------------------------------------------------------------

def test_video_dimensions(monkeypatch):
    _responses(monkeypatch, "720,486\n")
    assert fp.video_dimensions("/v.mkv") == (720, 486)


def test_video_dimensions_tolerates_a_trailing_separator(monkeypatch):
    """ffprobe emits '720,576,' when the stream carries side data — the old
    'x'-separated parser produced int('576x') and gave up."""
    _responses(monkeypatch, "720,576,\n")
    assert fp.video_dimensions("/v.mov") == (720, 576)


def test_video_dimensions_empty_returns_none(monkeypatch):
    _responses(monkeypatch, "\n")
    assert fp.video_dimensions("/v.mkv") is None


def test_video_dimensions_garbage_returns_none(monkeypatch):
    _responses(monkeypatch, "abc,def\n")
    assert fp.video_dimensions("/v.mkv") is None


# ---------------------------------------------------------------------------
# failure reporting
# ---------------------------------------------------------------------------

def test_nonzero_exit_is_reported_as_failure(monkeypatch):
    monkeypatch.setattr(fp.subprocess, "run",
                        lambda *a, **kw: _proc("", returncode=1, stderr="bad file"))
    assert fp.duration("/v.mkv") is None
    assert fp.frame_rate("/v.mkv") is None
    assert fp.audio_streams("/v.mkv") is None
