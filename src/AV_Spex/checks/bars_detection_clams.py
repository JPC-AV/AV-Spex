"""
CLAMS-style SMPTE color bars detector.

Adapted from clamsproject/app-barsdetection
(https://github.com/clamsproject/app-barsdetection). The upstream project is
distributed under the Apache License 2.0; the full upstream license text lives
at src/AV_Spex/config/clams_bars/LICENSE.

Modifications from the upstream:
  - CLAMS / MMIF I/O removed; only the SSIM-based detection core is ported.
  - The pickled reference array (grey.p) is replaced by a PNG asset
    (config/clams_bars/smpte_bars_reference.png). Pixel values are byte-equal
    to the original, so SSIM scores match.
  - Source FPS is read via ffprobe instead of an MMIF VideoDocument property.
  - Per-frame SSIM scores are written to their own CSV for review.
  - The durations CSV mirrors AV Spex's qct-parse format so a single parser
    handles both detectors' output.
"""

import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np
from scipy.ndimage import uniform_filter

from AV_Spex.utils.log_setup import logger


def structural_similarity(im1, im2, win_size=7, data_range=255.0,
                          K1=0.01, K2=0.03):
    """Mean structural similarity (MSSIM) between two equal-shape 2D images.

    Drop-in replacement for ``skimage.metrics.structural_similarity`` as it was
    previously called here: two same-shape grayscale arrays, all-default
    parameters. Faithfully reproduces skimage's 2D path — uniform (box) filter
    over ``win_size``, sample-covariance normalization, and border crop — so the
    returned scores are byte-for-byte comparable to the prior implementation.
    Uses ``scipy.ndimage`` (already bundled) instead of pulling in scikit-image
    and its large transitive dependency tail.
    """
    im1 = im1.astype(np.float64)
    im2 = im2.astype(np.float64)

    # Number of pixels in the sliding window (2D).
    NP = win_size ** im1.ndim

    # uniform_filter defaults to 'reflect' mode, matching skimage.
    ux = uniform_filter(im1, size=win_size)
    uy = uniform_filter(im2, size=win_size)
    uxx = uniform_filter(im1 * im1, size=win_size)
    uyy = uniform_filter(im2 * im2, size=win_size)
    uxy = uniform_filter(im1 * im2, size=win_size)

    # Sample-covariance normalization (skimage default use_sample_covariance=True).
    cov_norm = NP / (NP - 1)
    vx = cov_norm * (uxx - ux * ux)
    vy = cov_norm * (uyy - uy * uy)
    vxy = cov_norm * (uxy - ux * uy)

    C1 = (K1 * data_range) ** 2
    C2 = (K2 * data_range) ** 2

    A1 = 2 * ux * uy + C1
    A2 = 2 * vxy + C2
    B1 = ux * ux + uy * uy + C1
    B2 = vx + vy + C2
    S = (A1 * A2) / (B1 * B2)

    # Crop the border (filter footprint) before averaging, like skimage.
    pad = (win_size - 1) // 2
    return S[pad:-pad, pad:-pad].mean(dtype=np.float64)


REFERENCE_FILENAME = "smpte_bars_reference.png"
DURATIONS_CSV_NAME = "clams_bars_colorbars_durations.csv"
SSIM_SCORES_CSV_SUFFIX = "_clams_bars_ssim_scores.csv"


def _bundle_dir() -> str:
    """Return the AV_Spex package root, in dev or PyInstaller-frozen mode."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "AV_Spex")
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_reference_path() -> str:
    path = os.path.join(_bundle_dir(), "config", "clams_bars", REFERENCE_FILENAME)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"CLAMS bars reference image not found at {path}. "
            "Expected a bundled grayscale PNG."
        )
    return path


def get_video_fps(video_path: str) -> Optional[float]:
    """Read the video stream's frame rate via ffprobe. Returns None on failure."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        for key in ("r_frame_rate", "avg_frame_rate"):
            value = stream.get(key, "")
            if "/" in value:
                num, den = value.split("/")
                num, den = float(num), float(den)
                if den > 0:
                    return num / den
            elif value:
                return float(value)
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        pass
    return None


def _format_timestamp(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - (hours * 3600) - (minutes * 60)
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"


def write_durations_csv(report_directory: str, runs: List[Tuple[str, float, float]]) -> None:
    """
    Write the bars-detection durations CSV from a combined list of runs.

    Each entry is (pass_label, start_seconds, end_seconds). The pass label is
    written as the first column so the report can distinguish primary detections
    from cross-validation second-pass hits.
    """
    path = Path(report_directory) / DURATIONS_CSV_NAME
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        if runs:
            writer.writerow(["clams bars detection color bars found:"])
            for pass_label, start_seconds, end_seconds in runs:
                writer.writerow([
                    pass_label,
                    _format_timestamp(start_seconds),
                    _format_timestamp(end_seconds),
                ])
        else:
            writer.writerow(["clams bars detection found no color bars"])


def runs_from_recorded_scores(
    report_directory: str,
    video_id: str,
    threshold: float,
    max_frame: int,
    min_frame_count: int,
    fps: float,
    pass_label: str = "primary",
) -> List[Tuple[float, float]]:
    """
    Re-evaluate already-recorded SSIM scores against a different threshold.

    The per-frame SSIM CSV stores raw scores, so a relaxed re-check over a
    range the primary pass already covered needs no video decoding — replay
    the recorded samples with the new threshold using the same run logic as
    run_clams_bars_detection.

    Returns:
        List of (start_seconds, end_seconds) runs, in frame order.
    """
    ssim_csv = Path(report_directory) / f"{video_id}{SSIM_SCORES_CSV_SUFFIX}"
    if not ssim_csv.exists() or not fps or fps <= 0:
        return []

    samples = []
    try:
        with open(ssim_csv, newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # header
            for row in reader:
                if len(row) < 4 or row[0] != pass_label:
                    continue
                try:
                    frame = int(row[1])
                    score = float(row[3])
                except ValueError:
                    continue
                if frame < max_frame:
                    samples.append((frame, score))
    except OSError as e:
        logger.warning(f"CLAMS bars score replay: could not read {ssim_csv}: {e}")
        return []

    samples.sort(key=lambda t: t[0])
    bars_runs = []
    in_run = False
    run_start_frame: Optional[int] = None
    cur_frame = 0
    for cur_frame, score in samples:
        if score > threshold:
            if not in_run:
                in_run = True
                run_start_frame = cur_frame
        elif in_run:
            in_run = False
            if cur_frame - run_start_frame > min_frame_count:
                bars_runs.append((run_start_frame, cur_frame))
    if in_run and run_start_frame is not None and cur_frame - run_start_frame > min_frame_count:
        bars_runs.append((run_start_frame, cur_frame))

    return [(s_frame / fps, e_frame / fps) for s_frame, e_frame in bars_runs]


def run_clams_bars_detection(
    video_path: str,
    report_directory: str,
    video_id: str,
    threshold: float = 0.7,
    sample_ratio: int = 30,
    start_frame: int = 0,
    stop_at_frame: int = 9000,
    min_frame_count: int = 10,
    stop_after_one: bool = True,
    fps: Optional[float] = None,
    pass_label: str = "primary",
    append_ssim_csv: bool = False,
    check_cancelled=None,
    signals=None,
) -> List[Tuple[float, float]]:
    """
    Detect SMPTE color bars by SSIM comparison against a bundled reference image.

    Parameters mirror the upstream CLAMS app's runtime parameters; AV Spex adds
    `start_frame` so a caller can scan a windowed range, and `pass_label` /
    `append_ssim_csv` so the per-frame SSIM CSV can grow across passes.

    Returns:
        List of (start_seconds, end_seconds) for every run that met the minimum
        frame count, in scan order. Empty list if none were found.

    Writes per-sampled-frame SSIM scores to its own CSV. Does NOT write the
    durations CSV — the caller composes primary and second-pass results and
    calls `write_durations_csv` once.
    """
    report_path = Path(report_directory)
    report_path.mkdir(parents=True, exist_ok=True)
    ssim_csv = report_path / f"{video_id}{SSIM_SCORES_CSV_SUFFIX}"

    if fps is None:
        fps = get_video_fps(video_path)
    if not fps or fps <= 0:
        logger.warning(f"CLAMS bars detection: could not determine fps for {video_path}; skipping.")
        return []

    reference = cv2.imread(_resolve_reference_path(), cv2.IMREAD_GRAYSCALE)
    if reference is None:
        logger.warning("CLAMS bars detection: failed to load reference PNG; skipping.")
        return []

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning(f"CLAMS bars detection: could not open {video_path}; skipping.")
        return []

    sampled_frames = list(range(
        max(start_frame, 0),
        max(stop_at_frame, 0),
        max(sample_ratio, 1),
    ))
    bars_runs = []
    in_run = False
    run_start_frame: Optional[int] = None
    cur_frame = sampled_frames[0] if sampled_frames else 0

    total_samples = len(sampled_frames)
    progress_interval = max(1, total_samples // 100)
    last_progress_pct = 0
    emit_progress = signals is not None and hasattr(signals, 'clams_detection_progress') and total_samples > 0

    ssim_mode = "a" if append_ssim_csv and ssim_csv.exists() else "w"
    try:
        with open(ssim_csv, ssim_mode, newline="") as ssim_file:
            ssim_writer = csv.writer(ssim_file)
            if ssim_mode == "w":
                ssim_writer.writerow(["pass", "frame", "timestamp", "ssim_score", "exceeds_threshold"])

            for sample_idx, cur_frame in enumerate(sampled_frames):
                if check_cancelled and check_cancelled():
                    break

                if emit_progress and sample_idx % progress_interval == 0:
                    pct = int((sample_idx / total_samples) * 100)
                    pct = min(99, max(0, pct))
                    if pct > last_progress_pct:
                        signals.clams_detection_progress.emit(pct)
                        last_progress_pct = pct

                seek_target = max(cur_frame - 1, 0)
                cap.set(cv2.CAP_PROP_POS_FRAMES, seek_target)
                ret, frame = cap.read()
                if not ret:
                    break

                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if gray.shape != reference.shape:
                    gray = cv2.resize(gray, (reference.shape[1], reference.shape[0]))
                score = float(structural_similarity(gray, reference))
                exceeds = score > threshold

                ssim_writer.writerow([
                    pass_label,
                    cur_frame,
                    _format_timestamp(cur_frame / fps),
                    f"{score:.6f}",
                    "true" if exceeds else "false",
                ])

                if exceeds:
                    if not in_run:
                        in_run = True
                        run_start_frame = cur_frame
                elif in_run:
                    in_run = False
                    if cur_frame - run_start_frame > min_frame_count:
                        bars_runs.append((run_start_frame, cur_frame))
                    if stop_after_one:
                        break
    finally:
        cap.release()

    if in_run and run_start_frame is not None and cur_frame - run_start_frame > min_frame_count:
        bars_runs.append((run_start_frame, cur_frame))

    if emit_progress:
        signals.clams_detection_progress.emit(100)

    return [(s_frame / fps, e_frame / fps) for s_frame, e_frame in bars_runs]
