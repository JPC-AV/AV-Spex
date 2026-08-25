"""
Duplicate (frozen) frame detection.

Split out of EnhancedFrameAnalysis for the same reason as dropped-sample
detection: it reads the video and returns a result without participating in the
border/BRNG refinement loop.

QCTools YDIF/UDIF/VDIF values act as a cheap candidate filter, and each
candidate run is then verified with OpenCV by computing MSE between the
freeze's first frame and its predecessor — the QCTools pass alone flags
flat-field (signal-loss) frames that are not true freezes.
"""

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from AV_Spex.utils.log_setup import logger
# Positions are reported as the file's own NDF/DF timecode, not raw
# wall-clock seconds — see CLAUDE.md on time positions.
from AV_Spex.checks.qct_parse import (
    _tc_format_timecode,
    _tc_parse_start_timecode,
    _get_video_start_timecode,
)

@dataclass
class DuplicateFrameRun:
    """A run of consecutive frames flagged as likely duplicates by QCTools metrics"""
    start_time: float           # timestamp of the first duplicate frame in the run
    end_time: float             # timestamp of the last duplicate frame in the run
    duplicate_count: int        # number of duplicate frames (each a near-zero YDIF reading)
    frozen_frames: int          # total identical frames in the freeze (duplicate_count + 1)
    estimated_loss_seconds: float
    avg_ydif: float
    max_ydif: float
    avg_udif: float
    avg_vdif: float
    avg_vrep: float             # vertical line repetition (corroborating, not gating)
    cv_mse: Optional[float]     # OpenCV-verified MSE (None if verification skipped)
    cv_verified: bool           # True if MSE confirms near-identical frames
    first_frame_thumbnail: Optional[str] = None  # path to JPG of run's first frame
    last_frame_thumbnail: Optional[str] = None   # path to JPG of run's last frame
    start_timecode: Optional[str] = None  # file timecode (NDF/DF, start-TC offset) of run start
    end_timecode: Optional[str] = None    # file timecode of run end


@dataclass
class DuplicateFrameResult:
    """Results from duplicate frame detection analysis"""
    status: str                 # 'clean', 'warning', 'critical'
    message: str
    total_runs: int
    total_duplicate_frames: int
    estimated_loss_seconds: float
    bit_depth_10: bool
    ydif_threshold: float
    udif_threshold: float
    vdif_threshold: float
    min_run_length: int
    runs: List[DuplicateFrameRun] = None


def detect_duplicate_frames(
    video_path,
    output_dir,
    qctools_parser=None,
    check_cancelled=None,
    color_bars_end_time: Optional[float] = None,
    black_segments: Optional[List[Tuple[float, float]]] = None,
    min_run_length: int = 2,
) -> Optional[DuplicateFrameResult]:
    """
    Detect runs of likely duplicate frames using QCTools YDIF/UDIF/VDIF
    as a candidate filter, then verify each candidate with OpenCV by
    computing MSE between the freeze's first frame and its predecessor.

    Args:
        color_bars_end_time: End of detected color bars to exclude
        black_segments: Known black segments to exclude
        min_run_length: Minimum consecutive low-diff frames to report

    Returns:
        DuplicateFrameResult or None if no QCTools report is available
    """
    # Callers may pass str or Path; the body does path arithmetic on both.
    video_path = Path(video_path)
    output_dir = Path(output_dir)

    if not qctools_parser:
        logger.warning("Skipping duplicate frame detection — no QCTools report available\n")
        return None

    logger.info("Running duplicate frame detection...")

    candidate_runs, thresholds = qctools_parser.find_duplicate_frame_candidates(
        color_bars_end_time=color_bars_end_time or 0,
        black_segments=black_segments,
        min_run_length=min_run_length,
    )
    logger.info(
        f"  QCTools candidates: {len(candidate_runs)} run(s) below "
        f"YDIF<{thresholds['ydif']}, UDIF<{thresholds['udif']}, VDIF<{thresholds['vdif']}"
    )

    # Open the video once for verification across all candidate runs.
    cap = None
    fps = qctools_parser.fps or 29.97
    frame_period = 1.0 / fps if fps else 1.0 / 29.97
    try:
        cap = cv2.VideoCapture(str(video_path))
        if cap.isOpened():
            file_fps = cap.get(cv2.CAP_PROP_FPS)
            if file_fps and file_fps > 0:
                fps = file_fps
                frame_period = 1.0 / fps
        else:
            logger.warning("  OpenCV could not open video for verification — reporting unverified candidates")
            cap = None
    except Exception as e:
        logger.warning(f"  OpenCV verification unavailable: {e}")
        cap = None

    # MSE threshold: an actual duplicate has near-zero MSE; allow a tiny
    # margin for codec noise. Computed on luma only (matches YDIF semantics).
    # Calibrated against vendor LC tapes: verified freezes measure MSE
    # 0.02-0.09; merely similar frames (low-motion content) start at ~0.7.
    mse_threshold = 0.5

    verified_runs: List[DuplicateFrameRun] = []
    flat_runs_rejected = 0
    for idx, candidate in enumerate(candidate_runs):
        if check_cancelled():
            break

        cv_mse: Optional[float] = None
        cv_verified = False

        if cap is not None:
            # Sample up to 3 consecutive frame pairs from within the run,
            # starting one frame before run_start to compare the first
            # duplicate against its predecessor.
            seek_time_s = max(0.0, candidate['start_time'] - frame_period)
            pairs_to_test = min(3, candidate['duplicate_count'])
            mse_values: List[float] = []
            frame_stds: List[float] = []
            try:
                cap.set(cv2.CAP_PROP_POS_MSEC, seek_time_s * 1000.0)
                ret, prev_frame = cap.read()
                if ret and prev_frame is not None:
                    prev_y = cv2.cvtColor(prev_frame, cv2.COLOR_BGR2GRAY)
                    for _ in range(pairs_to_test):
                        ret, cur_frame = cap.read()
                        if not ret or cur_frame is None:
                            break
                        cur_y = cv2.cvtColor(cur_frame, cv2.COLOR_BGR2GRAY)
                        if cur_y.shape != prev_y.shape:
                            break
                        diff = cur_y.astype(np.int32) - prev_y.astype(np.int32)
                        mse_values.append(float((diff * diff).mean()))
                        frame_stds.append(float(cur_y.std()))
                        prev_y = cur_y
            except Exception as e:
                logger.debug(f"  OpenCV verification error for run {idx + 1}: {e}")

            # Flat-field rejection (mirrors the QCTools-level exclusion,
            # for reports that lack YMIN/YMAX): a frame with essentially
            # no spatial variation is the deck's signal-loss black/mute
            # output, not frozen picture content.
            if frame_stds and max(frame_stds) < 1.0:
                flat_runs_rejected += 1
                logger.debug(
                    f"  Run {idx + 1} at {candidate['start_time']:.3f}s rejected: "
                    f"flat-field frames (signal loss), not a freeze"
                )
                continue

            if mse_values:
                cv_mse = sum(mse_values) / len(mse_values)
                cv_verified = cv_mse < mse_threshold

        estimated_loss = candidate['duplicate_count'] * frame_period
        verified_runs.append(DuplicateFrameRun(
            start_time=candidate['start_time'],
            end_time=candidate['end_time'],
            duplicate_count=candidate['duplicate_count'],
            frozen_frames=candidate['duplicate_count'] + 1,
            estimated_loss_seconds=estimated_loss,
            avg_ydif=candidate['avg_ydif'],
            max_ydif=candidate['max_ydif'],
            avg_udif=candidate['avg_udif'],
            avg_vdif=candidate['avg_vdif'],
            avg_vrep=candidate['avg_vrep'],
            cv_mse=cv_mse,
            cv_verified=cv_verified,
        ))

    if cap is not None:
        cap.release()

    if flat_runs_rejected:
        logger.info(
            f"  OpenCV verification rejected {flat_runs_rejected} run(s) as "
            f"flat-field signal loss (not frozen picture)"
        )

    # Keep only runs that OpenCV verified (when verification was available)
    if cap is None:
        final_runs = verified_runs
    else:
        final_runs = [r for r in verified_runs if r.cv_verified]
        dropped = len(verified_runs) - len(final_runs)
        if dropped > 0:
            logger.info(f"  OpenCV verification rejected {dropped} candidate run(s) as false positives")

    # Save first/last frame thumbnails for the HTML report
    if final_runs:
        thumb_dir = output_dir / "duplicate_frame_thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        thumb_cap = None
        try:
            thumb_cap = cv2.VideoCapture(str(video_path))
            if thumb_cap.isOpened():
                for i, run in enumerate(final_runs, 1):
                    if check_cancelled():
                        break
                    for label, ts in (('first', run.start_time), ('last', run.end_time)):
                        try:
                            thumb_cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000.0)
                            ret, frame = thumb_cap.read()
                            if ret and frame is not None:
                                out_path = thumb_dir / f"run_{i:03d}_{label}.jpg"
                                # Downscale oversized frames so embedded
                                # HTML reports stay small; SD passes through.
                                MAX_THUMB_WIDTH = 800
                                if frame.shape[1] > MAX_THUMB_WIDTH:
                                    scale = MAX_THUMB_WIDTH / frame.shape[1]
                                    frame = cv2.resize(
                                        frame,
                                        (MAX_THUMB_WIDTH, int(frame.shape[0] * scale)),
                                        interpolation=cv2.INTER_AREA,
                                    )
                                cv2.imwrite(str(out_path), frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
                                if label == 'first':
                                    run.first_frame_thumbnail = str(out_path)
                                else:
                                    run.last_frame_thumbnail = str(out_path)
                        except Exception as e:
                            logger.debug(f"  Could not save {label} thumbnail for run {i}: {e}")
        finally:
            if thumb_cap is not None:
                thumb_cap.release()

    # Label each run with the file's own timecode (NDF/DF aware, offset by
    # the stream's start TC) so reported positions match what an NLE shows,
    # rather than the raw QCTools wall-clock seconds.
    if final_runs:
        tc_nominal = max(1, int(round(fps))) if fps else 30
        tc_start_frames, tc_drop_frame = _tc_parse_start_timecode(
            _get_video_start_timecode(str(video_path)), tc_nominal
        )
        for r in final_runs:
            r.start_timecode = _tc_format_timecode(
                r.start_time, fps, tc_start_frames, tc_drop_frame)
            r.end_timecode = _tc_format_timecode(
                r.end_time, fps, tc_start_frames, tc_drop_frame)

    total_dupes = sum(r.duplicate_count for r in final_runs)
    total_loss = sum(r.estimated_loss_seconds for r in final_runs)

    # Status: clean if none, warning for any verified runs, critical for many/long
    if not final_runs:
        status = 'clean'
        message = "No duplicate frame runs detected"
    else:
        longest = max(r.frozen_frames for r in final_runs)
        if len(final_runs) >= 5 or longest >= 10:
            status = 'critical'
        else:
            status = 'warning'
        message = (
            f"{len(final_runs)} freeze run(s) detected, "
            f"{total_dupes} duplicate frame(s) total, "
            f"~{total_loss:.3f}s estimated loss"
        )

    logger.info(f"Duplicate frame detection result: {status} — {message}\n")
    if final_runs:
        for i, r in enumerate(final_runs[:10], 1):
            start_tc = r.start_timecode or f"{int(r.start_time // 60):02d}:{r.start_time % 60:05.2f}"
            vrep_str = f", VREP={r.avg_vrep:.1f}" if r.avg_vrep > 0 else ""
            mse_str = f", MSE={r.cv_mse:.2f}" if r.cv_mse is not None else ""
            logger.info(
                f"  Run {i}: start={start_tc}, frozen={r.frozen_frames} frames"
                f", YDIF avg={r.avg_ydif:.3f}{vrep_str}{mse_str}"
            )
        if len(final_runs) > 10:
            logger.info(f"  ... and {len(final_runs) - 10} more")

    return DuplicateFrameResult(
        status=status,
        message=message,
        total_runs=len(final_runs),
        total_duplicate_frames=total_dupes,
        estimated_loss_seconds=total_loss,
        bit_depth_10=qctools_parser.bit_depth_10,
        ydif_threshold=thresholds['ydif'],
        udif_threshold=thresholds['udif'],
        vdif_threshold=thresholds['vdif'],
        min_run_length=min_run_length,
        runs=final_runs,
    )
