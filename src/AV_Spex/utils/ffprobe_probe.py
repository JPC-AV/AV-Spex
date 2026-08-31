"""
One place that shells out to ffprobe for a file's properties.

Before this module, duration and frame rate each had three or four
implementations scattered across checks/ and utils/, and they had drifted:
some passed a timeout and some could hang, some logged a warning on failure and
some swallowed it silently, and only one of each pair carried the fallback that
makes it work on real files (container duration reported as ``N/A``, or a
missing ``r_frame_rate``). The accessors here are the union of that robustness.

**Failure convention**: every accessor returns ``None`` (or an empty result
where noted) rather than raising, so callers must handle a missing value.
Two cases are deliberately distinguished:

- ffprobe failed to run, timed out, or returned non-zero — logged at warning,
  because something is wrong.
- ffprobe ran fine and the value simply is not present — returned quietly. A
  file with no timecode tag or no audio is normal, not an error.

This module does not cache. A full report render issues about nine ffprobe
calls, so the redundancy costs milliseconds; a cache would add a staleness
failure mode for no real gain. Revisit if a caller ever probes in a loop.
"""

import json
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Tuple

from AV_Spex.utils.log_setup import logger

# ffprobe is reading headers, not decoding; anything slower than this is stuck.
DEFAULT_TIMEOUT = 10


@dataclass(frozen=True)
class AudioStream:
    """One audio stream's absolute index and channel count."""
    index: int
    channels: int


def _run(video_path, args, timeout=DEFAULT_TIMEOUT) -> Optional[str]:
    """Run ffprobe with the given args; return stripped stdout, or None.

    Returns None and logs a warning when ffprobe cannot run or reports an
    error. An empty result is returned as an empty string, so callers can tell
    "no such value" from "the probe failed".
    """
    command = ['ffprobe', '-v', 'error', *args, video_path]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"ffprobe could not run on {video_path}: {e}")
        return None

    if result.returncode != 0:
        logger.warning(
            f"ffprobe returned {result.returncode} for {video_path}: "
            f"{(result.stderr or '').strip()}"
        )
        return None

    return (result.stdout or '').strip()


def _run_json(video_path, args, timeout=DEFAULT_TIMEOUT) -> Optional[dict]:
    """Run ffprobe with JSON output; return the parsed object, or None."""
    out = _run(video_path, [*args, '-of', 'json'], timeout=timeout)
    if not out:
        return None
    try:
        return json.loads(out)
    except ValueError as e:
        logger.warning(f"Could not parse ffprobe JSON for {video_path}: {e}")
        return None


def _as_float(value) -> Optional[float]:
    """Parse a plain number or an ffmpeg 'num/den' rational."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == 'N/A':
        return None
    try:
        if '/' in text:
            num, den = text.split('/', 1)
            denominator = float(den)
            return float(num) / denominator if denominator else None
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Video
# ---------------------------------------------------------------------------

def duration(video_path) -> Optional[float]:
    """File duration in seconds.

    Reads the container duration, falling back to the first video stream's —
    some MKVs report ``N/A`` for ``format=duration``.
    """
    out = _run(video_path, ['-show_entries', 'format=duration', '-of', 'csv=p=0'])
    seconds = _as_float(out)
    if seconds is not None:
        return seconds

    out = _run(video_path, ['-select_streams', 'v:0',
                            '-show_entries', 'stream=duration', '-of', 'csv=p=0'])
    return _as_float(out)


def frame_rate(video_path) -> Optional[float]:
    """Video frame rate in frames per second (e.g. 29.97 for NTSC).

    Prefers ``r_frame_rate``, falling back to ``avg_frame_rate`` for files that
    do not report the former.
    """
    data = _run_json(video_path, ['-select_streams', 'v:0', '-show_entries',
                                  'stream=r_frame_rate,avg_frame_rate'])
    if not data:
        return None
    streams = data.get('streams') or []
    if not streams:
        return None
    for key in ('r_frame_rate', 'avg_frame_rate'):
        rate = _as_float(streams[0].get(key))
        if rate:
            return rate
    return None


def start_timecode(video_path) -> Optional[str]:
    """The stream's start timecode tag, e.g. '00:00:00:09' (NDF) or
    '01:00:00;00' (DF).

    Checked on the video stream first, then the container. Returns None when the
    file simply carries no timecode tag, which is normal and not logged.
    """
    queries = (
        ['-select_streams', 'v:0', '-show_entries', 'stream_tags=timecode'],
        ['-show_entries', 'format_tags=timecode'],
    )
    for query in queries:
        out = _run(video_path, [*query, '-of', 'default=noprint_wrappers=1:nokey=1'])
        if out:
            return out.splitlines()[0].strip()
    return None


def video_dimensions(video_path) -> Optional[Tuple[int, int]]:
    """(width, height) of the first video stream."""
    out = _run(video_path, ['-select_streams', 'v:0',
                            '-show_entries', 'stream=width,height', '-of', 'csv=p=0'])
    if not out:
        return None
    parts = [p for p in out.replace('\n', ',').split(',') if p.strip()]
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def audio_streams(video_path) -> Optional[List[AudioStream]]:
    """Every audio stream's absolute index and channel count, in stream order.

    Returns an empty list for a file with no audio, and None if the probe
    failed — the two are different, and callers that rebuild audio depend on
    telling them apart.
    """
    data = _run_json(video_path, ['-select_streams', 'a',
                                  '-show_entries', 'stream=index,channels'])
    if data is None:
        return None
    streams = []
    for stream in data.get('streams') or []:
        try:
            streams.append(AudioStream(int(stream['index']), int(stream.get('channels', 0))))
        except (KeyError, TypeError, ValueError):
            continue
    return streams


def audio_stream_channels(video_path) -> Optional[List[int]]:
    """Channel count per audio stream, in order.

    ``[2]`` for a single stereo stream (typical MKV), ``[1, 1]`` for two
    separate mono streams (typical broadcast MXF, where each track is its own
    mono PCM stream).
    """
    streams = audio_streams(video_path)
    if streams is None:
        return None
    return [s.channels for s in streams]


def audio_stream_count(video_path) -> Optional[int]:
    """Number of separate audio streams.

    A count greater than 1 means the QCTools report cannot describe the real
    per-stream audio (qcli downmixes them), so audio analysis reads a generated
    per-stream stats sidecar instead — see checks/audio_stream_stats.py.
    """
    streams = audio_streams(video_path)
    return None if streams is None else len(streams)


def first_audio_channel_count(video_path) -> Optional[int]:
    """Channel count of the first audio stream, or None if there is no audio."""
    channels = audio_stream_channels(video_path)
    if not channels:
        return None
    return channels[0]
