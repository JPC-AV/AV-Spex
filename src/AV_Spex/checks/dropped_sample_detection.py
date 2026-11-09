"""
Dropped audio sample detection.

Split out of EnhancedFrameAnalysis: this step reads the video and returns a
result, and takes no part in the border/BRNG refinement loop that couples the
other frame-analysis steps together. Keeping it separate makes it testable on
its own and keeps the orchestrator to orchestration.

The verdict rests on a scan of the decoded audio (checks/audio_discontinuity.py)
for the two signatures lost samples leave behind:

* **splices** — a step discontinuity where the two sides of a gap were butted
  together off a zero crossing (heard as a click; seen as a thin full-band
  line in a spectrogram), and
* **digital-silence gaps** — runs of exact zeros mid-program, where the capture
  chain stopped receiving audio (input lost, de-embedder unlocked).

A spectrogram PNG is still rendered for the report, as a picture for the
operator. It is no longer analyzed: reading spike positions off a rendered
image missed the splice in the LC SmashEdit test file entirely and mapped the
DropOut file's edges to the wrong times, because one 1280-px column spans
tens of milliseconds and the plot-area crop was found heuristically.

The audio/video stream duration difference is reported but does not drive the
verdict: healthy captures routinely differ by 20–400 ms (capture start/stop
granularity — 17 of 19 JPC/LC sample files, mostly with the audio *longer*),
so treating any difference as evidence flagged nearly every file.
"""

import json
import os
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr
from AV_Spex.utils import ffprobe_probe
from AV_Spex.checks import audio_discontinuity
# Positions are reported as the file's own NDF/DF timecode, not raw
# wall-clock seconds — see CLAUDE.md on time positions.
from AV_Spex.checks.qct_parse import (
    _tc_format_timecode,
    _tc_parse_start_timecode,
    _get_video_start_timecode,
)

# Progress split between the two passes that read the file.
_SPECTROGRAM_PROGRESS_END = 30

# Plot area of the spectrogram, in pixels. showspectrumpic centres this
# rectangle in the PNG with its legend margins around it, and spans the
# rendered stream's full duration across the width — the report maps finding
# times onto it to mark them (generate_report._spectrogram_markers).
SPECTROGRAM_PLOT_SIZE = (1280, 480)
_SCAN_PROGRESS_END = 95


@dataclass
class DroppedSampleResult:
    """Results from dropped sample detection analysis"""
    # 'clean' / 'warning' / 'critical', or 'unknown' when the audio scan could
    # not run — a failed detector must not read as a clean file.
    status: str
    message: str
    # Number of splice discontinuities found (kept under its old name, which
    # the report and older JSON sidecars read).
    spike_count: int
    duration_diff_ms: float
    audio_duration: float
    video_duration: float
    combined_score: float  # weighted risk score 0.0-1.0
    estimated_loss_ms: float = 0.0  # total digital-silence gap length (worst stream)
    sample_rate: int = 0  # audio sample rate in Hz
    spectrogram_path: Optional[str] = None
    # Splice positions in wall-clock seconds (old field name kept).
    spike_timestamps: List[float] = None
    # Splices and silence gaps as dicts, each carrying file-timecode labels.
    splice_events: List[Dict[str, Any]] = field(default_factory=list)
    silence_gaps: List[Dict[str, Any]] = field(default_factory=list)
    # What could actually be measured. The verdict needs the audio scan;
    # the spectrogram is illustration only and the durations are context.
    audio_scan_measured: bool = True
    spectrogram_measured: bool = True
    durations_measured: bool = True
    streams_scanned: int = 0


def detect_dropped_samples(video_path, video_id, output_dir,
                           signals=None, check_cancelled=None,
                           color_bars_end_time: float = None) -> Optional[DroppedSampleResult]:
    """
    Detect potential dropped audio samples by:
    1. Rendering a spectrogram PNG for the report (illustration only)
    2. Scanning the decoded audio for splices and digital-silence gaps
    3. Comparing audio and video stream durations (context, not scored)

    Args:
        color_bars_end_time: unused. Bars/tone is where a splice is easiest
            to see, so it is deliberately not excluded.

    Returns:
        DroppedSampleResult
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    check_cancelled = check_cancelled or (lambda: False)

    # Step 1: spectrogram picture for the report.
    spectrogram_path = _generate_spectrogram(
        video_path, video_id, output_dir, signals, check_cancelled,
        p_end=_SPECTROGRAM_PROGRESS_END)
    if check_cancelled():
        return None

    # Step 2: audio-domain discontinuity scan.
    scan = _scan_audio(video_path, signals, check_cancelled)
    if check_cancelled():
        return None
    audio_scan_measured = scan is not None and scan.measured

    # Step 3: audio/video durations (context).
    audio_duration, video_duration, sample_rate = _get_av_durations(video_path, video_id, output_dir)
    spectrogram_measured = spectrogram_path is not None
    durations_measured = audio_duration is not None and video_duration is not None
    duration_diff_ms = 0.0
    if durations_measured:
        duration_diff_ms = abs(audio_duration - video_duration) * 1000.0
        logger.debug(f"  Audio/video duration difference: {duration_diff_ms:.3f}ms "
                     f"(audio {audio_duration:.6f}s, video {video_duration:.6f}s)")
    else:
        logger.debug("Could not determine audio and/or video duration for comparison")
    audio_duration = audio_duration or 0.0
    video_duration = video_duration or 0.0
    if scan is not None and scan.sample_rate:
        sample_rate = sample_rate or scan.sample_rate

    if not audio_scan_measured:
        message = (
            "Could not measure dropped samples: the audio could not be decoded "
            "for the discontinuity scan. This is not a clean result — the check "
            "did not run."
        )
        logger.warning(f"\nDropped sample detection result: unknown — {message}\n")
        return DroppedSampleResult(
            status='unknown',
            message=message,
            spike_count=0,
            duration_diff_ms=duration_diff_ms,
            audio_duration=audio_duration,
            video_duration=video_duration,
            combined_score=0.0,
            sample_rate=sample_rate,
            spectrogram_path=str(spectrogram_path) if spectrogram_path else None,
            spike_timestamps=[],
            audio_scan_measured=False,
            spectrogram_measured=spectrogram_measured,
            durations_measured=durations_measured,
        )

    splice_events, silence_gaps = _label_findings(video_path, scan)
    splice_count = len(splice_events)
    gap_count = len(silence_gaps)
    # Gaps are reported per stream; a multi-stream file losing its input
    # shows the same gap on every stream, so count the worst stream's total.
    per_stream_loss: Dict[int, float] = {}
    for g in silence_gaps:
        per_stream_loss[g['stream']] = per_stream_loss.get(g['stream'], 0.0) + g['duration_ms']
    gap_loss_ms = max(per_stream_loss.values(), default=0.0)
    distinct_gaps = max((sum(1 for g in silence_gaps if g['stream'] == s) for s in per_stream_loss),
                        default=0)

    combined_score, status = _compute_dropped_sample_score(splice_count, distinct_gaps)

    parts = []
    if distinct_gaps:
        parts.append(f"{distinct_gaps} digital-silence gap(s) mid-program "
                     f"({gap_loss_ms:.0f}ms of audio missing)")
    if splice_count:
        parts.append(f"{splice_count} audio splice discontinuit{'y' if splice_count == 1 else 'ies'}")
    message = "; ".join(parts) if parts else "No indicators of dropped samples detected"

    for g in silence_gaps:
        logger.warning(f"  Digital-silence gap (stream {g['stream']}): "
                       f"{g['start_timecode']} – {g['end_timecode']} ({g['duration_ms']:.0f}ms)")
    for e in splice_events:
        logger.warning(f"  Audio splice (stream {e['stream']}): {e['timecode']} "
                       f"(score {e['score']:.0f}, step {e['step_dbfs']:.0f} dBFS)")
    log = logger.warning if status != 'clean' else logger.info
    log(f"\nDropped sample detection result: {status} — {message}\n")

    return DroppedSampleResult(
        status=status,
        message=message,
        spike_count=splice_count,
        duration_diff_ms=duration_diff_ms,
        audio_duration=audio_duration,
        video_duration=video_duration,
        combined_score=combined_score,
        estimated_loss_ms=round(gap_loss_ms, 1),
        sample_rate=sample_rate,
        spectrogram_path=str(spectrogram_path) if spectrogram_path else None,
        spike_timestamps=[e['time_s'] for e in splice_events],
        splice_events=splice_events,
        silence_gaps=silence_gaps,
        audio_scan_measured=True,
        spectrogram_measured=spectrogram_measured,
        durations_measured=durations_measured,
        streams_scanned=scan.streams_scanned,
    )


def _scan_audio(video_path, signals, check_cancelled) -> Optional[audio_discontinuity.DiscontinuityScan]:
    """Run the discontinuity scan, driving the progress bar from the decode."""
    can_track = signals is not None and hasattr(signals, 'frame_analysis_progress')
    span = _SCAN_PROGRESS_END - _SPECTROGRAM_PROGRESS_END
    last = [_SPECTROGRAM_PROGRESS_END]

    def _progress(frac):
        pct = _SPECTROGRAM_PROGRESS_END + int(span * frac)
        if can_track and pct > last[0]:
            signals.frame_analysis_progress.emit(pct)
            last[0] = pct

    try:
        scan = audio_discontinuity.scan_audio_discontinuities(
            video_path, check_cancelled=check_cancelled, progress=_progress,
            duration_s=ffprobe_probe.duration(str(video_path)))
        if can_track:
            signals.frame_analysis_progress.emit(_SCAN_PROGRESS_END)
        return scan
    except Exception as e:
        logger.warning(f"Audio discontinuity scan failed: {e}")
        logger.debug(traceback.format_exc())
        return None


def _label_findings(video_path, scan) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Scan findings as dicts, with positions labelled in the file's own
    NDF/DF timecode (offset by the start TC) so they match an NLE."""
    fps = None
    start_frames, drop_frame = 0, False
    if scan.splices or scan.silence_gaps:
        try:
            fps = ffprobe_probe.frame_rate(str(video_path))
            nominal = max(1, int(round(fps))) if fps else 30
            start_frames, drop_frame = _tc_parse_start_timecode(
                _get_video_start_timecode(str(video_path)), nominal)
        except Exception as e:
            logger.debug(f"Could not read timecode for dropped-sample labels: {e}")

    def tc(t):
        return _tc_format_timecode(t, fps, start_frames, drop_frame)

    splices = []
    for e in sorted(scan.splices, key=lambda e: (e.time_s, e.stream)):
        d = asdict(e)
        d['timecode'] = tc(e.time_s)
        splices.append(d)
    gaps = []
    for g in sorted(scan.silence_gaps, key=lambda g: (g.start_s, g.stream)):
        gaps.append({
            'start_s': g.start_s, 'end_s': g.end_s, 'stream': g.stream,
            'duration_ms': round(g.duration_ms, 1),
            'start_timecode': tc(g.start_s), 'end_timecode': tc(g.end_s),
        })
    return splices, gaps


def _generate_spectrogram(video_path, video_id, output_dir, signals=None, check_cancelled=None,
                          p_end: int = 95) -> Optional[Path]:
    """Generate a spectrogram image using FFmpeg's showspectrumpic filter.

    Drives progress from the Python side using elapsed wall-clock time
    against an estimated processing duration (~100x realtime for
    showspectrumpic). The bar moves continuously while ffmpeg runs,
    capped at 90% so we can finalize at p_end on actual completion.

    We don't parse ffmpeg's stderr here: showspectrumpic is a single-
    output-frame filter so `-progress` reports output time stuck at 0,
    and `-stats` emits `\\r`-terminated lines that libc holds in its
    stdio buffer over a pipe. Time-based estimation is the same kind of
    Python-driven approach used by `generate_color_strip_base64`.
    """
    # Callers may pass str or Path; the body does path arithmetic on both.
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    check_cancelled = check_cancelled or (lambda: False)

    output_path = output_dir / f"{video_id}_spectrogram.png"

    audio_duration, _, _ = _get_av_durations(video_path, video_id, output_dir)
    p_start = 0
    can_track_progress = (
        signals is not None
        and hasattr(signals, 'frame_analysis_progress')
    )

    cmd = [
        'ffmpeg', '-y',
        '-hide_banner', '-loglevel', 'error',
        '-i', str(video_path),
        # The first audio stream, explicitly (as the filter's input label —
        # `-map 0:a:0` would route the raw audio to the PNG muxer): left to
        # itself ffmpeg picks the stream with the most channels, and the
        # report boxes findings on audio stream 1 onto this picture.
        '-vn', '-lavfi',
        '[0:a:0]showspectrumpic=s={}x{}'.format(*SPECTROGRAM_PLOT_SIZE),
        str(output_path)
    ]

    # ~100x realtime is typical for showspectrumpic (e.g. 30 min audio
    # ≈ 18s wall-clock). Floor of 4s so very short clips still animate.
    if audio_duration and audio_duration > 0:
        estimated_s = max(audio_duration / 100.0, 4.0)
    else:
        estimated_s = 30.0

    try:
        logger.debug(f"    Generating spectrogram: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        start_time = time.time()
        timeout_s = 300.0
        timed_out = False
        last_pct = p_start
        poll_interval = 0.3
        cap_fraction = 0.9  # leave room for final emit at p_end

        while True:
            if proc.poll() is not None:
                break
            if check_cancelled():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return None
            elapsed = time.time() - start_time
            if elapsed > timeout_s:
                timed_out = True
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if can_track_progress:
                fraction = min(cap_fraction, elapsed / estimated_s)
                pct = p_start + int((p_end - p_start) * fraction)
                if pct > last_pct:
                    signals.frame_analysis_progress.emit(pct)
                    last_pct = pct
            time.sleep(poll_interval)

        stderr_text = proc.stderr.read() if proc.stderr else ''

        if timed_out:
            logger.warning("Spectrogram generation timed out (300s limit)")
            return None

        if proc.returncode == 0 and output_path.exists():
            logger.info(f"Spectrogram saved to: {output_path.name}")
            if can_track_progress:
                signals.frame_analysis_progress.emit(p_end)
            return output_path
        else:
            logger.warning(f"FFmpeg spectrogram generation failed (exit code {proc.returncode})")
            report_ffmpeg_stderr(stderr_text, "spectrogram", failure=True)
    except Exception as e:
        logger.warning(f"Error generating spectrogram: {e}")

    return None


def _get_av_durations(video_path, video_id, output_dir) -> Tuple[Optional[float], Optional[float], int]:
    """
    Get audio and video stream durations and audio sample rate from the
    ffprobe sidecar JSON. Falls back to a fresh ffprobe call if not found.

    Handles both standard duration fields and Matroska containers where
    per-stream durations are stored in tags.DURATION as HH:MM:SS.nnnnnnnnn.

    Returns:
        Tuple of (audio_duration, video_duration, sample_rate).
        Durations in seconds (None if unavailable), sample_rate in Hz (0 if unavailable).
    """
    audio_duration = None
    video_duration = None
    sample_rate = 0

    # Try reading from ffprobe sidecar (run_tools.py saves it as .txt despite JSON content)
    sidecar_path = video_path.parent / f"{video_id}_qc_metadata" / f"{video_id}_ffprobe_output.txt"
    if not sidecar_path.exists():
        # Also check the destination output dir
        sidecar_path = output_dir / f"{video_id}_ffprobe_output.txt"

    if sidecar_path.exists():
        try:
            with open(sidecar_path, 'r') as f:
                ffprobe_data = json.load(f)

            audio_duration, video_duration, sample_rate = _extract_stream_durations(
                ffprobe_data.get('streams', [])
            )
            logger.debug(f"    Durations from ffprobe sidecar: audio={audio_duration}, video={video_duration}, sample_rate={sample_rate}\n")
            if audio_duration is not None and video_duration is not None:
                return audio_duration, video_duration, sample_rate
        except Exception as e:
            logger.debug(f"Could not read ffprobe sidecar: {e}\n")

    # Fallback: run ffprobe directly, requesting both duration and DURATION tag
    logger.debug("Falling back to fresh ffprobe call for stream durations")
    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-show_entries', 'stream=codec_type,duration,sample_rate:stream_tags=DURATION',
            '-of', 'json',
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            audio_duration, video_duration, sample_rate = _extract_stream_durations(
                data.get('streams', [])
            )
    except Exception as e:
        logger.warning(f"Could not get stream durations via ffprobe: {e}")

    return audio_duration, video_duration, sample_rate


def _compute_dropped_sample_score(splice_count: int, gap_count: int) -> Tuple[float, str]:
    """
    Risk score and status from the audio scan.

    - A digital-silence gap mid-program is near-certain evidence the capture
      chain lost audio: critical.
    - Splices are strong but not certain evidence (a rare content transient
      can pass both detectors): warning, escalating to critical at 3+.

    Returns:
        Tuple of (score 0.0-1.0, status string)
    """
    if gap_count:
        score = min(1.0, 0.85 + 0.05 * (gap_count - 1) + 0.02 * splice_count)
    elif splice_count:
        score = min(0.8, 0.25 + 0.1 * splice_count)
    else:
        score = 0.0

    if score == 0:
        status = 'clean'
    elif gap_count == 0 and splice_count < 3:
        status = 'warning'
    else:
        status = 'critical'
    return round(score, 3), status


def _extract_stream_durations(streams: list) -> Tuple[Optional[float], Optional[float], int]:
    """
    Extract audio and video durations and audio sample rate from ffprobe stream data.
    Checks the 'duration' field first, then falls back to tags.DURATION
    (used by Matroska/WebM containers which store duration as HH:MM:SS.nnnnnnnnn).
    """
    audio_duration = None
    video_duration = None
    sample_rate = 0

    for stream in streams:
        codec_type = stream.get('codec_type', '')
        if codec_type not in ('video', 'audio'):
            continue

        duration = None
        # Try direct duration field first
        duration_str = stream.get('duration')
        if duration_str is not None:
            try:
                duration = float(duration_str)
            except (ValueError, TypeError):
                pass

        # Fall back to tags.DURATION (Matroska format: HH:MM:SS.nnnnnnnnn)
        if duration is None:
            tags = stream.get('tags', {})
            tag_duration = tags.get('DURATION') or tags.get('duration')
            if tag_duration:
                duration = _parse_duration_tag(tag_duration)

        if duration is not None:
            if codec_type == 'video' and video_duration is None:
                video_duration = duration
            elif codec_type == 'audio' and audio_duration is None:
                audio_duration = duration

        # Extract sample rate from audio stream
        if codec_type == 'audio' and sample_rate == 0:
            sr = stream.get('sample_rate')
            if sr is not None:
                try:
                    sample_rate = int(sr)
                except (ValueError, TypeError):
                    pass

    return audio_duration, video_duration, sample_rate


def _parse_duration_tag(duration_str: str) -> Optional[float]:
    """Parse a duration string in HH:MM:SS.nnnnnnnnn format to seconds."""
    try:
        parts = duration_str.split(':')
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        else:
            return float(duration_str)
    except (ValueError, TypeError):
        return None
