"""
Dropped audio sample detection.

Split out of EnhancedFrameAnalysis: this step reads the video and returns a
result, and takes no part in the border/BRNG refinement loop that couples the
other frame-analysis steps together. Keeping it separate makes it testable on
its own and keeps the orchestrator to orchestration.

Two independent signals are combined into a weighted risk score: vertical
spikes in an FFmpeg ``showspectrumpic`` spectrogram, and the difference between
the audio and video stream durations reported by ffprobe.
"""

import json
import os
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr
from AV_Spex.utils import ffprobe_probe

@dataclass
class DroppedSampleResult:
    """Results from dropped sample detection analysis"""
    status: str  # 'clean', 'warning', 'critical'
    message: str
    spike_count: int
    duration_diff_ms: float
    audio_duration: float
    video_duration: float
    combined_score: float  # weighted risk score 0.0-1.0
    estimated_loss_ms: float = 0.0  # estimated duration loss from detected spikes
    sample_rate: int = 0  # audio sample rate in Hz
    spectrogram_path: Optional[str] = None
    spike_timestamps: List[float] = None


def detect_dropped_samples(video_path, video_id, output_dir,
                           signals=None, check_cancelled=None,
                           color_bars_end_time: float = None) -> Optional[DroppedSampleResult]:
    """
    Detect potential dropped audio samples by:
    1. Generating a spectrogram image via FFmpeg showspectrumpic and analyzing it for vertical spikes
    2. Comparing audio and video stream durations from the ffprobe sidecar
    3. Combining both signals into a weighted risk score

    Args:
        color_bars_end_time: End time of color bars (unused for now, reserved for future filtering)

    Returns:
        DroppedSampleResult or None if detection fails
    """
    #logger.info("Running dropped sample detection...")

    # Step 1: Generate spectrogram image
    spectrogram_path = _generate_spectrogram(video_path, video_id, output_dir, signals, check_cancelled)

    # Step 2: Analyze spectrogram for vertical spikes
    spike_count = 0
    spike_timestamps = []
    if spectrogram_path:
        spike_count, spike_timestamps = _analyze_spectrogram_spikes(video_path, spectrogram_path)
        if spike_count > 0:
            logger.warning(f"Detected {spike_count} potential dropped sample spike(s) in spectrogram\n")
        else:
            logger.info("No dropped sample spikes detected in spectrogram")

    # Step 3: Compare audio/video durations
    audio_duration, video_duration, sample_rate = _get_av_durations(video_path, video_id, output_dir)
    duration_diff_ms = 0.0
    if audio_duration is not None and video_duration is not None:
        duration_diff_ms = abs(audio_duration - video_duration) * 1000.0
        if duration_diff_ms > 0:
            logger.warning(f"Audio/video duration mismatch: {duration_diff_ms:.3f}ms")
            logger.debug(f"  Audio duration: {audio_duration:.6f}s")
            logger.debug(f"  Video duration: {video_duration:.6f}s\n")
        else:
            logger.info(f"Audio and video durations match\n")
    else:
        logger.warning(f"Could not determine audio and/or video duration for comparison\n")
        audio_duration = audio_duration or 0.0
        video_duration = video_duration or 0.0

    # Step 4: Estimate duration loss from detected spikes and compare with measured difference
    # Each spike represents ~1 dropped sample. At the given sample rate,
    # 1 sample = 1/sample_rate seconds.
    estimated_loss_ms = 0.0
    if spike_count > 0 and sample_rate > 0:
        estimated_loss_ms = (spike_count / sample_rate) * 1000.0
        logger.info(f"Estimated duration loss from {spike_count} dropped sample(s) at {sample_rate}Hz: {estimated_loss_ms:.4f}ms")
        if duration_diff_ms > 0:
            ratio = duration_diff_ms / estimated_loss_ms if estimated_loss_ms > 0 else 0
            logger.info(f"  Measured duration difference: {duration_diff_ms:.3f}ms")
            logger.info(f"  Ratio (measured / estimated): {ratio:.1f}x")
            if ratio > 10:
                logger.info(f"  Duration difference is {ratio:.0f}x larger than detected spikes account for — "
                            f"additional undetected drops or systematic offset likely")
            elif ratio < 0.5:
                logger.info(f"  Duration difference is smaller than detected spikes — "
                            f"some spikes may be content transients rather than drops")

    # Step 5: Compute combined score and status
    combined_score, status = _compute_dropped_sample_score(spike_count, duration_diff_ms)

    # Build message
    parts = []
    if spike_count > 0:
        parts.append(f"{spike_count} spectrogram spike(s) detected")
    if duration_diff_ms > 0:
        parts.append(f"{duration_diff_ms:.3f}ms audio/video duration difference")
    if estimated_loss_ms > 0:
        parts.append(f"estimated loss from spikes: {estimated_loss_ms:.4f}ms")
    if not parts:
        message = "No indicators of dropped samples detected"
    else:
        message = "; ".join(parts)

    logger.info(f"\nDropped sample detection result: {status} — {message}\n")

    return DroppedSampleResult(
        status=status,
        message=message,
        spike_count=spike_count,
        duration_diff_ms=duration_diff_ms,
        audio_duration=audio_duration,
        video_duration=video_duration,
        combined_score=combined_score,
        estimated_loss_ms=estimated_loss_ms,
        sample_rate=sample_rate,
        spectrogram_path=str(spectrogram_path) if spectrogram_path else None,
        spike_timestamps=spike_timestamps
    )


def _generate_spectrogram(video_path, video_id, output_dir, signals=None, check_cancelled=None) -> Optional[Path]:
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
    output_path = output_dir / f"{video_id}_spectrogram.png"

    audio_duration, _, _ = _get_av_durations(video_path, video_id, output_dir)
    p_start, p_end = 0, 95
    can_track_progress = (
        signals is not None
        and hasattr(signals, 'frame_analysis_progress')
    )

    cmd = [
        'ffmpeg', '-y',
        '-hide_banner', '-loglevel', 'error',
        '-i', str(video_path),
        '-vn', '-lavfi', 'showspectrumpic=s=1280x480',
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


def _analyze_spectrogram_spikes(video_path, spectrogram_path: Path) -> Tuple[int, List[float]]:
    """
    Analyze a spectrogram PNG for vertical bright lines (spikes) that indicate
    dropped audio samples. These appear as bright columns spanning the full
    frequency range.

    Returns:
        Tuple of (spike_count, estimated_timestamps)
    """
    try:
        img = cv2.imread(str(spectrogram_path))
        if img is None:
            logger.warning("Could not load spectrogram image for analysis")
            return 0, []

        height, width = img.shape[:2]
        gray_full = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        # Dynamically find the actual spectrogram plot area.
        # showspectrumpic adds: left axis labels, right colorbar + dBFS scale,
        # and top/bottom time/frequency labels around the plot.
        #
        # Strategy: use a narrow horizontal band (40-60% height) to avoid text,
        # then find gaps of >= 10 dark columns that separate the plot from
        # the axis labels (left) and colorbar (right).

        band_top = int(height * 0.4)
        band_bottom = int(height * 0.6)
        band = gray_full[band_top:band_bottom, :]
        col_means = np.mean(band, axis=0)

        gap_threshold = 10  # Minimum dark columns to count as a structural gap

        # Find left edge: scan from left, skip dark margin, skip axis labels,
        # find the first sustained gap, then the plot starts after it
        plot_left = 0
        c = 0
        while c < width and col_means[c] < 2:
            c += 1
        # Now in axis labels region — scan for the first gap of >= gap_threshold
        while c < width // 2:
            if col_means[c] < 2:
                gap_start = c
                while c < width and col_means[c] < 2:
                    c += 1
                gap_len = c - gap_start
                if gap_len >= gap_threshold:
                    plot_left = c
                    break
            else:
                c += 1

        # Find right edge: scan from right, skip dark margin, skip colorbar/labels,
        # find the first sustained gap, then the plot ends before it
        plot_right = width - 1
        c = width - 1
        while c > 0 and col_means[c] < 2:
            c -= 1
        # Now in colorbar/labels region — scan for the first gap
        while c > width // 2:
            if col_means[c] < 2:
                gap_end = c
                while c > 0 and col_means[c] < 2:
                    c -= 1
                gap_start = c + 1
                gap_len = gap_end - gap_start + 1
                if gap_len >= gap_threshold:
                    plot_right = gap_start - 1
                    # Skip any bright axis border line at the edge
                    while plot_right > plot_left and col_means[plot_right] > 150:
                        plot_right -= 1
                    break
            else:
                c -= 1

        # Find top/bottom edges using the plot column range
        top_edges = []
        bottom_edges = []
        for col in range(plot_left, plot_right, max(1, (plot_right - plot_left) // 20)):
            col_data = gray_full[:, col]
            for r in range(height):
                if col_data[r] > 15:
                    top_edges.append(r)
                    break
            for r in range(height - 1, 0, -1):
                if col_data[r] > 15:
                    bottom_edges.append(r)
                    break

        plot_top = int(np.median(top_edges)) if top_edges else 0
        plot_bottom = int(np.median(bottom_edges)) if bottom_edges else height

        plot_area = img[plot_top:plot_bottom, plot_left:plot_right]

        if plot_area.size == 0:
            logger.warning("    Spectrogram plot area is empty after cropping")
            return 0, []

        # Convert to grayscale and compute mean brightness per column
        gray = cv2.cvtColor(plot_area, cv2.COLOR_BGR2GRAY)
        plot_height, plot_width = gray.shape

        logger.debug(f"    Spectrogram plot area: ({plot_left},{plot_top}) to ({plot_right},{plot_bottom}), "
                     f"size {plot_width}x{plot_height}")

        column_means = np.mean(gray, axis=0)

        # Use a rolling median with a wide window to establish local baseline
        window_size = max(51, plot_width // 20)
        if window_size % 2 == 0:
            window_size += 1

        # Pad for rolling computation
        padded = np.pad(column_means, window_size // 2, mode='edge')
        rolling_median = np.array([
            np.median(padded[i:i + window_size])
            for i in range(len(column_means))
        ])

        # Compute deviation from local median
        deviations = column_means - rolling_median

        # Use MAD (median absolute deviation) for robust threshold
        mad = np.median(np.abs(deviations))
        if mad == 0:
            mad = np.std(deviations)
        if mad == 0:
            return 0, []

        threshold = 3.0 * mad  # 3x MAD for spike detection

        # Also require the column to be bright across most of the frequency range
        # A true dropped sample spike lights up the full spectrum
        spike_columns = []
        for col_idx in range(plot_width):
            if deviations[col_idx] > threshold:
                # Check that the brightness spans most of the frequency range
                col_data = gray[:, col_idx]
                # Count rows above the overall median brightness
                overall_median = np.median(gray)
                bright_fraction = np.sum(col_data > overall_median + threshold) / plot_height
                if bright_fraction > 0.3:  # At least 30% of frequency range is bright
                    spike_columns.append(col_idx)

        if not spike_columns:
            return 0, []

        # Group adjacent spike columns into single events
        groups = []
        current_group = [spike_columns[0]]
        for i in range(1, len(spike_columns)):
            if spike_columns[i] - spike_columns[i-1] <= 2:  # Adjacent within 2px
                current_group.append(spike_columns[i])
            else:
                groups.append(current_group)
                current_group = [spike_columns[i]]
        groups.append(current_group)

        # Reject groups wider than 2 columns — a true dropped sample is a
        # single-sample impulse (~20us at 48kHz) which should appear as at most
        # 1-2 pixel columns in the spectrogram. Wider bright regions are more
        # likely loud content transients (music hits, speech plosives, etc.).
        max_spike_width = 2
        spikes = [g for g in groups if len(g) <= max_spike_width]
        rejected = len(groups) - len(spikes)
        if rejected > 0:
            logger.debug(f"    Rejected {rejected} spike group(s) wider than {max_spike_width} columns (likely content transients)")

        # Estimate timestamps by mapping column position to video duration
        video_duration = ffprobe_probe.duration(str(video_path)) or 0
        spike_timestamps = []
        for group in spikes:
            center_col = sum(group) / len(group)
            timestamp = (center_col / plot_width) * video_duration
            spike_timestamps.append(round(timestamp, 2))

        logger.debug(f"    Spike detection: {len(spikes)} spike(s) found at columns {[g[0] for g in spikes]}")
        if spike_timestamps:
            logger.debug(f"    Estimated timestamps: {spike_timestamps}\n")

        return len(spikes), spike_timestamps

    except Exception as e:
        logger.warning(f"Error analyzing spectrogram: {e}")
        import traceback
        logger.debug(traceback.format_exc())
        return 0, []


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


def _compute_dropped_sample_score(spike_count: int, duration_diff_ms: float) -> Tuple[float, str]:
    """
    Compute a combined risk score from spectrogram spikes and duration mismatch.

    Scoring:
    - Spike score: 0 spikes = 0.0, 1-5 = 0.3-0.5, 6+ = 0.6-0.8
    - Duration score: 0ms = 0.0, >0ms = 0.3
    - If both present, escalate

    Returns:
        Tuple of (score 0.0-1.0, status string)
    """
    spike_score = 0.0
    if spike_count > 0:
        spike_score = min(0.8, 0.2 + spike_count * 0.06)

    duration_score = 0.0
    if duration_diff_ms > 0:
        duration_score = 0.3

    # Combined score: weighted sum with escalation when both are present
    combined = spike_score * 0.7 + duration_score * 0.3
    if spike_count > 0 and duration_diff_ms > 0:
        combined = min(1.0, combined + 0.15)  # escalation bonus

    # Determine status
    if combined == 0:
        status = 'clean'
    elif combined < 0.4:
        status = 'warning'
    else:
        status = 'critical'

    return round(combined, 3), status


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
