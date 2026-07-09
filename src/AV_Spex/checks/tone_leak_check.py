#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Reference-tone leak ("tone leak") detection.

Detects a continuous ~1 kHz calibration/reference tone leaking into the
record chain of a transfer station — heard as a faint high-pitched whine or
"squeaking" during quiet passages. The leak's fingerprint is a harmonic comb
at exact integer multiples of 1 kHz (the tone is distorted/square-ish), which
stands tens of dB above the local spectral floor even when the tone itself is
far below program level.

Unlike the other qct-parse audio checks, this analysis reads decoded PCM
straight from the video file via an ffmpeg pipe — the QCTools sidecar's audio
stats have no narrowband frequency resolution, so the comb is invisible there.
Every audio stream and channel is analyzed independently, so multi-stream
(discrete mono) sources are supported.

Method, per channel, over consecutive 8-second FFT windows:
  - windows whose RMS is at/below the digital-silence floor are ignored
  - each harmonic's peak power is compared to the local median floor
    (+/- 60 Hz, excluding the bins nearest the harmonic)
  - comb score = mean excess (dB) across the harmonics
A channel is flagged when enough windows exceed the comb-score threshold
(both an absolute count and a fraction of active windows). There is
deliberately NO gate on the tone's absolute level: the leak is a confirmed
audible artifact at ~-90 dBFS on channels where nothing masks it.

Thresholds were calibrated against a 13-file ground truth set (July 2026):
3 LC masters + 1 access file with the artifact, 8 JPC MKVs + 1 access file
without. Clean channels showed 0% of windows at or above 12 dB comb score
(worst p95 = 9.1 dB); artifact channels showed 20-99%.
"""

import csv
import json
import os
import subprocess

import numpy as np

from AV_Spex.utils.log_setup import logger

# Analysis window length. 8 s gives 0.125 Hz FFT bins — narrow enough that
# the rock-stable leak tone (measured 1000.0 Hz +/- ~0.02 Hz) always lands
# in the peak bins, while program material spreads across the local floor.
TONE_LEAK_WINDOW_SEC = 8

# Harmonics scored. 5 kHz is skipped (weakest in measured files) and nothing
# above 6 kHz is needed — the low harmonics carry the discrimination.
TONE_LEAK_HARMONICS = (1000, 2000, 3000, 4000, 6000)

# A window is "flagged" when its comb score reaches this. Ground truth allows
# anywhere in ~10-15 dB without changing any verdict; 12 dB is the midpoint.
TONE_LEAK_COMB_THRESHOLD_DB = 12.0

# Channel verdict: flagged windows must be at least this fraction of active
# windows AND at least this many windows, so a single noisy window on a short
# file can't flag, and a brief real leak on a long file isn't diluted away.
TONE_LEAK_MIN_FLAGGED_FRACTION = 0.05
TONE_LEAK_MIN_FLAGGED_WINDOWS = 4

# Windows at/below this RMS are digital silence (unrecorded channel), not
# evidence either way; they are excluded from the active-window count.
TONE_LEAK_SILENCE_FLOOR_DB = -85.0

# All streams are resampled to this rate before analysis so the FFT bin
# math is identical regardless of the source sample rate.
TONE_LEAK_ANALYSIS_SR = 48000

# Local spectral floor for each harmonic: median power within +/- this many
# Hz, excluding +/- the exclusion bins around the harmonic itself.
_FLOOR_SPAN_HZ = 60
_FLOOR_EXCLUDE_BINS = 8
_PEAK_BINS = 2  # harmonic peak searched within +/- this many bins


def _probe_audio_streams(video_path):
    """Return a list of channel counts, one per audio stream, or None on error."""
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=channels", "-of", "json", video_path,
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(f"Tone-leak detection: ffprobe invocation failed: {exc}")
        return None
    if proc.returncode != 0:
        logger.warning(
            f"Tone-leak detection: ffprobe returned {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
        return None
    try:
        streams = json.loads(proc.stdout.decode('utf-8'))["streams"]
    except (ValueError, KeyError) as exc:
        logger.warning(f"Tone-leak detection: could not parse ffprobe output: {exc}")
        return None
    return [s.get("channels", 0) for s in streams]


def _window_metrics(seg, sr=TONE_LEAK_ANALYSIS_SR, window=None):
    """Compute (rms_db, comb_score_db, tone_level_db) for one analysis window.

    `seg` is a 1-D float array of exactly one window's samples. `window` is an
    optional precomputed Hann window of the same length (saves recomputation
    in the streaming loop). tone_level_db is the 1 kHz fundamental's level in
    dBFS (sine RMS convention), reported for diagnostics only.
    """
    n = len(seg)
    if window is None:
        window = np.hanning(n)
    rms_db = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12)

    spec = np.abs(np.fft.rfft(seg * window)) ** 2
    bin_hz = sr / n
    excesses = []
    for h in TONE_LEAK_HARMONICS:
        b = int(round(h / bin_hz))
        peak = spec[b - _PEAK_BINS: b + _PEAK_BINS + 1].max()
        lo = int((h - _FLOOR_SPAN_HZ) / bin_hz)
        hi = int((h + _FLOOR_SPAN_HZ) / bin_hz)
        floor_bins = np.concatenate([
            spec[lo: b - _FLOOR_EXCLUDE_BINS],
            spec[b + _FLOOR_EXCLUDE_BINS + 1: hi],
        ])
        floor = np.median(floor_bins)
        if floor > 0:
            excesses.append(10 * np.log10(peak / floor))
        else:
            excesses.append(0.0)
    comb_db = float(np.mean(excesses))

    b0 = int(round(TONE_LEAK_HARMONICS[0] / bin_hz))
    fundamental_power = spec[b0 - _PEAK_BINS: b0 + _PEAK_BINS + 1].sum()
    # A Hann-windowed exact-bin sine spreads to 1.5x its center-bin power
    # across the +/-2 bin span summed above; back out the sine amplitude.
    amplitude = np.sqrt(fundamental_power / 1.5) / (window.sum() / 2)
    tone_level_db = 20 * np.log10(amplitude / np.sqrt(2) + 1e-15)

    return rms_db, comb_db, tone_level_db


def _channel_verdict(metrics):
    """Evaluate one channel's per-window metrics list [(rms, comb, level), ...].

    Returns a dict with the verdict and its supporting statistics.
    """
    total = len(metrics)
    active = [m for m in metrics if m[0] > TONE_LEAK_SILENCE_FLOOR_DB]
    flagged = [m for m in active if m[1] >= TONE_LEAK_COMB_THRESHOLD_DB]
    n_active, n_flagged = len(active), len(flagged)
    fraction = (n_flagged / n_active) if n_active else 0.0
    detected = (
        fraction >= TONE_LEAK_MIN_FLAGGED_FRACTION
        and n_flagged >= TONE_LEAK_MIN_FLAGGED_WINDOWS
    )
    return {
        'tone_leak_detected': detected,
        'total_windows': total,
        'active_windows': n_active,
        'flagged_windows': n_flagged,
        'flagged_fraction': fraction,
        'median_comb_db': float(np.median([m[1] for m in active])) if active else None,
        'median_flagged_comb_db': float(np.median([m[1] for m in flagged])) if flagged else None,
        'median_flagged_tone_level_db': float(np.median([m[2] for m in flagged])) if flagged else None,
    }


def _flagged_events(metrics, window_sec=TONE_LEAK_WINDOW_SEC):
    """Merge consecutive flagged windows into (start_s, end_s, mean_comb, peak_comb, median_level) events."""
    events = []
    run = []  # list of (index, comb, level)
    for i, (rms_db, comb_db, level_db) in enumerate(metrics):
        if rms_db > TONE_LEAK_SILENCE_FLOOR_DB and comb_db >= TONE_LEAK_COMB_THRESHOLD_DB:
            run.append((i, comb_db, level_db))
        elif run:
            events.append(run)
            run = []
    if run:
        events.append(run)
    out = []
    for run in events:
        combs = [c for _, c, _ in run]
        levels = [l for _, _, l in run]
        out.append((
            run[0][0] * window_sec,
            (run[-1][0] + 1) * window_sec,
            float(np.mean(combs)),
            float(np.max(combs)),
            float(np.median(levels)),
        ))
    return out


def _analyze_stream(video_path, stream_index, num_channels, check_cancelled=None,
                    progress_cb=None):
    """Decode one audio stream via ffmpeg and return per-channel metrics lists.

    `progress_cb`, when given, is called with the number of windows analyzed
    so far after each window. Returns {channel: [(rms_db, comb_db,
    tone_level_db), ...]} or None on decode failure / cancellation.
    """
    n = TONE_LEAK_WINDOW_SEC * TONE_LEAK_ANALYSIS_SR
    window = np.hanning(n)
    cmd = [
        "ffmpeg", "-v", "error", "-i", video_path,
        "-map", f"0:a:{stream_index}",
        "-ar", str(TONE_LEAK_ANALYSIS_SR),
        "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(f"Tone-leak detection: ffmpeg invocation failed: {exc}")
        return None

    metrics = {ch: [] for ch in range(num_channels)}
    bytes_per_window = n * num_channels * 4
    try:
        while True:
            if check_cancelled and check_cancelled():
                proc.kill()
                return None
            buf = proc.stdout.read(bytes_per_window)
            if len(buf) < bytes_per_window:
                break  # trailing partial window (< one window of audio) is dropped
            block = np.frombuffer(buf, dtype=np.float32).reshape(n, num_channels)
            for ch in range(num_channels):
                seg = block[:, ch].astype(np.float64)
                metrics[ch].append(_window_metrics(seg, window=window))
            if progress_cb:
                progress_cb(len(metrics[0]))
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode('utf-8', 'replace').strip()
        proc.stderr.close()
        proc.wait()
    if proc.returncode != 0 and not any(metrics.values()):
        logger.warning(f"Tone-leak detection: ffmpeg decode failed: {stderr}")
        return None
    return metrics


def _write_tone_leak_results(report_directory, stream_results, fps=None,
                             tc_start_frames=0, tc_drop_frame=False):
    """Write summary + events CSVs. Event timestamps are the file's own timecode."""
    # Imported here (not at module top) because qct_parse imports this module.
    from AV_Spex.checks.qct_parse import _tc_format_timecode

    summary_csv = os.path.join(report_directory, "qct-parse_tone_leak_summary.csv")
    with open(summary_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Tone Leak Detection Results"])
        writer.writerow(["Harmonics Scored (Hz)", " ".join(str(h) for h in TONE_LEAK_HARMONICS)])
        writer.writerow(["Window Size (seconds)", TONE_LEAK_WINDOW_SEC])
        writer.writerow(["Comb Score Threshold (dB)", TONE_LEAK_COMB_THRESHOLD_DB])
        writer.writerow(["Min Flagged Fraction", TONE_LEAK_MIN_FLAGGED_FRACTION])
        writer.writerow(["Min Flagged Windows", TONE_LEAK_MIN_FLAGGED_WINDOWS])
        writer.writerow(["Silence Floor (dBFS)", TONE_LEAK_SILENCE_FLOOR_DB])
        writer.writerow([])
        writer.writerow(["Stream", "Channel", "Total Windows", "Active Windows",
                         "Flagged Windows", "Flagged %", "Median Comb (dB)",
                         "Median Flagged Comb (dB)", "Median 1kHz Level (dBFS)",
                         "Tone Leak Detected"])
        for (si, ch), res in sorted(stream_results.items()):
            v = res['verdict']
            writer.writerow([
                si, ch, v['total_windows'], v['active_windows'], v['flagged_windows'],
                f"{100 * v['flagged_fraction']:.1f}",
                f"{v['median_comb_db']:.1f}" if v['median_comb_db'] is not None else "n/a",
                f"{v['median_flagged_comb_db']:.1f}" if v['median_flagged_comb_db'] is not None else "n/a",
                f"{v['median_flagged_tone_level_db']:.1f}" if v['median_flagged_tone_level_db'] is not None else "n/a",
                "Yes" if v['tone_leak_detected'] else "No",
            ])

    all_events = []
    for (si, ch), res in sorted(stream_results.items()):
        if res['verdict']['tone_leak_detected']:
            for start_s, end_s, mean_comb, peak_comb, med_level in res['events']:
                all_events.append((start_s, end_s, si, ch, mean_comb, peak_comb, med_level))
    all_events.sort()

    events_csv = os.path.join(report_directory, "qct-parse_tone_leak_events.csv")
    with open(events_csv, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["Timestamp Start", "Timestamp End", "Stream", "Channel",
                         "Duration (s)", "Mean Comb (dB)", "Peak Comb (dB)",
                         "Median 1kHz Level (dBFS)"])
        for start_s, end_s, si, ch, mean_comb, peak_comb, med_level in all_events:
            writer.writerow([
                _tc_format_timecode(start_s, fps, tc_start_frames, tc_drop_frame),
                _tc_format_timecode(end_s, fps, tc_start_frames, tc_drop_frame),
                si, ch,
                f"{end_s - start_s:.0f}",
                f"{mean_comb:.1f}", f"{peak_comb:.1f}", f"{med_level:.1f}",
            ])

    return summary_csv, events_csv


def analyzeToneLeak(video_path, report_directory, fps=None, tc_start_frames=0,
                    tc_drop_frame=False, check_cancelled=None, signals=None,
                    total_duration=None):
    """
    Detect reference-tone leak on every audio stream/channel of `video_path`.

    Parameters:
        video_path (str): Path to the video file being analyzed.
        report_directory (str): Path to the {video_id}_report_csvs directory.
        fps (float or None): Video frame rate, for file-timecode formatting.
        tc_start_frames (int): Stream start timecode offset in frames.
        tc_drop_frame (bool): Whether the file's timecode is drop-frame.
        check_cancelled (callable): Optional cancellation check.
        signals: Optional signals object; when it has tone_leak_progress, the
            detection emits its own 0-100 pass on the detail progress bar.
        total_duration (float or None): File duration in seconds, used to
            scale progress emission.

    Returns:
        dict: {'tone_leak_detected', 'flagged_channels', 'channels'} or None if
        the analysis could not run (no audio / decode failure / cancelled).
    """
    channel_counts = _probe_audio_streams(video_path)
    if not channel_counts:
        logger.warning("Tone-leak detection: no audio streams found\n")
        return None

    emit_progress = signals is not None and hasattr(signals, 'tone_leak_progress')
    n_streams = sum(1 for nch in channel_counts if nch > 0)
    expected_windows = int(total_duration // TONE_LEAK_WINDOW_SEC) if total_duration else 0
    last_pct = -1

    def _emit(pct):
        nonlocal last_pct
        if emit_progress and pct != last_pct:
            signals.tone_leak_progress.emit(pct)
            last_pct = pct

    _emit(0)

    stream_results = {}
    stream_num = 0
    for si, nch in enumerate(channel_counts):
        if nch <= 0:
            continue

        def _stream_progress(windows_done, _base=stream_num):
            if expected_windows and n_streams:
                frac = (_base + min(1.0, windows_done / expected_windows)) / n_streams
                _emit(min(99, int(round(100 * frac))))

        metrics = _analyze_stream(video_path, si, nch, check_cancelled=check_cancelled,
                                  progress_cb=_stream_progress if emit_progress else None)
        stream_num += 1
        if metrics is None:
            if check_cancelled and check_cancelled():
                return None
            continue
        for ch, m in metrics.items():
            if not m:
                continue
            stream_results[(si, ch)] = {
                'verdict': _channel_verdict(m),
                'events': _flagged_events(m),
            }

    if not stream_results:
        logger.warning("Tone-leak detection could not analyze any audio channel\n")
        return None

    summary_csv, _ = _write_tone_leak_results(
        report_directory, stream_results, fps=fps,
        tc_start_frames=tc_start_frames, tc_drop_frame=tc_drop_frame,
    )

    _emit(100)

    flagged = [(si, ch) for (si, ch), res in sorted(stream_results.items())
               if res['verdict']['tone_leak_detected']]
    if flagged:
        chan_list = ", ".join(f"stream {si} channel {ch}" for si, ch in flagged)
        logger.warning(
            f"Reference-tone leak detected on {chan_list}. "
            f"See {os.path.basename(summary_csv)}\n"
        )
    else:
        logger.debug(f"No reference-tone leak detected. Results in {os.path.basename(summary_csv)}\n")

    return {
        'tone_leak_detected': bool(flagged),
        'flagged_channels': flagged,
        'channels': {key: res['verdict'] for key, res in stream_results.items()},
    }
