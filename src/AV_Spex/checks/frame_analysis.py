#!/usr/bin/env python3
"""
Enhanced Frame Analysis Module
Combines the efficiency of the refactored version with the sophistication of the original implementation.
"""

import os
import sys
import json
import gzip
import time
import cv2
import numpy as np
import subprocess
import shlex
import re
import csv
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from collections import defaultdict
import xml.etree.ElementTree as ET
from scipy import ndimage, signal
import logging

import matplotlib
matplotlib.use('Agg')  # Use non-interactive backend
import matplotlib.pyplot as plt
import matplotlib.patches as patches

from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.checks.qct_parse import (
    _tc_format_timecode,
    _tc_parse_start_timecode,
    _get_video_start_timecode,
)

# Data classes for structured results
@dataclass
class FrameViolation:
    """Represents a frame with BRNG violations"""
    frame_num: int
    timestamp: float
    brng_value: float  # Changed from brng_low and brng_high
    violation_score: float
    violation_pixels: int = 0
    violation_percentage: float = 0.0
    diagnostics: List[str] = None
    pattern_analysis: Dict = None

@dataclass
class BorderDetectionResult:
    """Results from border detection"""
    active_area: Tuple[int, int, int, int]  # x, y, width, height
    border_regions: Dict
    detection_method: str
    quality_frame_hints: List[Tuple[float, float]]  # (timestamp, quality_score)
    head_switching_artifacts: Optional[Dict] = None
    requires_refinement: bool = False
    expansion_recommendations: Dict = None

@dataclass
class BRNGAnalysisResult:
    """Results from BRNG analysis"""
    violations: List[FrameViolation]
    aggregate_patterns: Dict
    actionable_report: Dict
    thumbnails: List[str]
    requires_border_adjustment: bool
    refinement_recommendations: Dict = None
    analysis_periods: List[Tuple[float, int]] = None
    period_summaries: List[Dict] = None

@dataclass
class SignalstatsResult:
    """Results from signalstats analysis"""
    violation_percentage: float
    max_brng: float
    avg_brng: float
    analysis_periods: List[Dict]
    diagnosis: str
    used_qctools: bool
    comparison_results: List[Dict] = None

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

@dataclass
class UpstreamAnalysisContext:
    """
    Packages findings from border detection and signalstats
    that should inform BRNG analysis decisions.
    
    Built by EnhancedFrameAnalysis.analyze() after signalstats completes,
    passed to DifferentialBRNGAnalyzer to adapt sensitivity, sampling
    density, edge detection, and thumbnail selection.
    """
    # Per-period signalstats findings (keyed by 0-based period index)
    period_diagnoses: Dict[int, str]          # 'border_violations' | 'content_violations' | 'minimal_violations'
    period_active_area_brng: Dict[int, Dict]  # {'max_brng': float, 'violation_pct': float}
    period_full_frame_brng: Dict[int, Dict]   # {'max_brng': float, 'violation_pct': float}
    
    # Aggregate signalstats findings
    avg_active_area_brng: float
    overall_diagnosis: str
    
    # Border detection findings relevant to BRNG
    head_switching: Optional[Dict] = None
    border_widths: Optional[Dict] = None      # {'left': px, 'right': px, 'top': px, 'bottom': px}
    
    # How much of the violation budget is border vs content (0.0–1.0)
    border_violation_fraction: float = 0.0


class QCToolsParser:
    """Memory-efficient QCTools parser with streaming capabilities"""
    
    def __init__(self, report_path: str, fps: float = 29.97):
        self.report_path = report_path
        self.fps = fps
        self.bit_depth_10 = self._detect_bit_depth()
        
    def _detect_bit_depth(self) -> bool:
        """Detect whether the QCTools stats are in 10-bit scale.

        Primary signal: the chroma midpoint. UAVG/VAVG sits near 512 in a
        10-bit-scale report and near 128 in an 8-bit one, regardless of
        content — including the black leader many tapes open with, which
        defeats a YMAX-based scan (dark head frames never exceed 250, so a
        10-bit file reads as 8-bit). Note QCTools may compute stats in
        10-bit scale even when the frame element's pix_fmt says 8-bit, so
        the stats themselves are the only reliable source. Falls back to
        the YMAX heuristic when chroma tags are absent.
        """
        try:
            if self.report_path.endswith('.gz'):
                file_handle = gzip.open(self.report_path, 'rt')
            else:
                file_handle = open(self.report_path, 'r')

            parser = ET.iterparse(file_handle, events=['start', 'end'])
            parser = iter(parser)
            event, root = next(parser)

            frame_count = 0
            for event, elem in parser:
                if event == 'end' and elem.tag == 'frame':
                    # Real QCTools tags are empty elements (<tag key=... value=.../>),
                    # so read the value attribute; text content is a fallback.
                    for chroma_key in ('UAVG', 'VAVG'):
                        tag = elem.find(f'.//tag[@key="lavfi.signalstats.{chroma_key}"]')
                        if tag is not None:
                            avg = tag.get('value') or tag.text
                            if avg:
                                file_handle.close()
                                return float(avg) > 300
                    ymax_tag = elem.find('.//tag[@key="lavfi.signalstats.YMAX"]')
                    ymax = 255.0
                    if ymax_tag is not None:
                        ymax = float(ymax_tag.get('value') or ymax_tag.text or '255')
                    if ymax > 250:
                        file_handle.close()
                        return True
                    frame_count += 1
                    if frame_count > 100:  # Check first 100 frames
                        break
                    elem.clear()
                    root.clear()

            file_handle.close()
            return False
        except:
            return False

    def _is_black_frame(self, elem) -> bool:
        """Check whether a frame element's luma stats indicate an all-black frame.

        Thresholds are in 10-bit scale (broadcast black = 64) and scaled down
        by 4 for 8-bit reports. There is deliberately no YMIN gate: analog
        tape black carries sub-black noise that keeps YMIN well above zero
        (e.g. 26-39 on a noisy black tail), so requiring a near-zero YMIN
        misclassifies real black segments as content.
        """
        ymax_tag = elem.find('.//tag[@key="lavfi.signalstats.YMAX"]')
        yhigh_tag = elem.find('.//tag[@key="lavfi.signalstats.YHIGH"]')
        ylow_tag = elem.find('.//tag[@key="lavfi.signalstats.YLOW"]')

        if ymax_tag is None or yhigh_tag is None or ylow_tag is None:
            return False

        scale = 1.0 if self.bit_depth_10 else 0.25
        ymax = float(ymax_tag.get('value', '1000'))
        yhigh = float(yhigh_tag.get('value', '1000'))
        ylow = float(ylow_tag.get('value', '1000'))

        return ymax < 300.0 * scale and yhigh < 115.0 * scale and ylow < 97.0 * scale

    def parse_for_violations_streaming_period(self, start_time: float, end_time: float,
                                        period_num: int, max_frames: int = 100, 
                                        skip_color_bars: bool = True) -> List[FrameViolation]:
        """Stream parse QCTools report for BRNG violations in specific time period"""
        violations = []
        
        # Counters for this specific period
        frames_in_period = 0
        frames_with_violations = 0
        max_brng_value = 0
        frames_checked = 0
        
        try:
            if self.report_path.endswith('.gz'):
                file_handle = gzip.open(self.report_path, 'rt')
            else:
                file_handle = open(self.report_path, 'r')
            
            parser = ET.iterparse(file_handle, events=['start', 'end'])
            parser = iter(parser)
            event, root = next(parser)
            
            for event, elem in parser:
                if event == 'end' and elem.tag == 'frame':
                    frames_checked += 1
                    
                    # Get timestamp from the frame element
                    timestamp_str = elem.get('pkt_pts_time')
                    if not timestamp_str:
                        elem.clear()
                        root.clear()
                        continue
                    
                    timestamp = float(timestamp_str)
                    
                    # Skip frames outside our period
                    if timestamp < start_time:
                        elem.clear()
                        root.clear()
                        continue
                        
                    if timestamp > end_time:
                        elem.clear()
                        root.clear()
                        break  # We've passed our period, stop parsing
                    
                    frames_in_period += 1
                    
                    # Extract frame data
                    frame_data = self._extract_frame_violations(elem, frame_num=None)
                    if frame_data:
                        frames_with_violations += 1
                        max_brng_value = max(max_brng_value, frame_data.brng_value)
                        violations.append(frame_data)
                    
                    elem.clear()
                    root.clear()
                    
                    # Stop if we have enough violations
                    if len(violations) >= max_frames:
                        break
            
            file_handle.close()
            
            # Log period-specific summary
            if frames_in_period > 0:
                violation_pct = (frames_with_violations / frames_in_period * 100)
                logger.debug(f"    Period {period_num}: {frames_in_period:,} frames analyzed, "
                        f"{frames_with_violations:,} with violations ({violation_pct:.1f}%)")
                if frames_with_violations > 0:
                    logger.debug(f"    Period {period_num} max BRNG: {max_brng_value:.4f}%")
            else:
                logger.debug(f"    Period {period_num}: No frames found in time range {start_time:.1f}s - {end_time:.1f}s")
            
        except Exception as e:
            logger.error(f"Error parsing QCTools report for period {period_num}: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        violations.sort(key=lambda x: x.violation_score, reverse=True)
        return violations[:max_frames]
    
    def parse_for_violations_streaming(self, max_frames: int = 100, 
                             skip_color_bars: bool = True,
                             color_bars_end_time: float = 0) -> List[FrameViolation]:
        """Stream parse QCTools report for BRNG violations"""
        violations = []
        chunk_size = 1000
        
        # Counters
        total_frames_checked = 0
        frames_after_color_bars = 0
        frames_with_violations = 0
        frames_skipped = 0
        black_frames_skipped = 0  # NEW COUNTER
        max_brng_value = 0
        
        try:
            if self.report_path.endswith('.gz'):
                file_handle = gzip.open(self.report_path, 'rt')
            else:
                file_handle = open(self.report_path, 'r')
            
            parser = ET.iterparse(file_handle, events=['start', 'end'])
            parser = iter(parser)
            event, root = next(parser)
            
            frame_buffer = []
            
            for event, elem in parser:
                if event == 'end' and elem.tag == 'frame':
                    total_frames_checked += 1
                    
                    # Get timestamp from the frame element
                    timestamp_str = elem.get('pkt_pts_time')
                    if not timestamp_str:
                        elem.clear()
                        root.clear()
                        continue
                        
                    timestamp = float(timestamp_str)
                    
                    # Skip color bars based on timestamp
                    if skip_color_bars and color_bars_end_time > 0 and timestamp < color_bars_end_time:
                        frames_skipped += 1
                        elem.clear()
                        root.clear()
                        continue
                    
                    frames_after_color_bars += 1
                    
                    # Extract frame data - this now includes black frame detection
                    frame_data_before = frames_with_violations
                    frame_data = self._extract_frame_violations(elem, frame_num=None)
                    
                    # Check if this might have been a black frame
                    # (we can detect this by checking if no violation was returned despite BRNG being present)
                    brng_tag = elem.find('.//tag[@key="lavfi.signalstats.BRNG"]')
                    if brng_tag is not None and frame_data is None:
                        if self._is_black_frame(elem):
                            black_frames_skipped += 1
                    
                    if frame_data:
                        frames_with_violations += 1
                        max_brng_value = max(max_brng_value, frame_data.brng_value)
                        frame_buffer.append(frame_data)
                    
                    elem.clear()
                    root.clear()
                    
                    # Process buffer when full
                    if len(frame_buffer) >= chunk_size:
                        violations.extend(self._process_violation_buffer(frame_buffer))
                        frame_buffer = []
                        
                        if len(violations) > max_frames * 2:
                            violations.sort(key=lambda x: x.violation_score, reverse=True)
                            violations = violations[:max_frames]
            
            # Process remaining buffer
            if frame_buffer:
                violations.extend(self._process_violation_buffer(frame_buffer))
            
            file_handle.close()
            
            # Log summary
            logger.debug(f"  Checked {total_frames_checked:,} frames from QCTools report")
            if frames_skipped > 0:
                logger.debug(f"  Skipped {frames_skipped:,} color bar frames (first {color_bars_end_time:.1f}s)")
            if black_frames_skipped > 0:
                logger.debug(f"  Skipped {black_frames_skipped:,} all-black frames")
            
            if frames_after_color_bars > 0:
                violation_pct = (frames_with_violations / frames_after_color_bars * 100) if frames_after_color_bars > 0 else 0
                logger.debug(f"  Analyzed {frames_after_color_bars:,} content frames")
                logger.debug(f"  Found {frames_with_violations:,} frames with BRNG violations ({violation_pct:.1f}% of content)")
                if frames_with_violations > 0:
                    logger.debug(f"  Max BRNG value: {max_brng_value:.4f}%\n")
            else:
                logger.warning("  No frames analyzed after color bars - check color bars duration")
            
        except Exception as e:
            logger.error(f"Error parsing QCTools report: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        violations.sort(key=lambda x: x.violation_score, reverse=True)
        return violations[:max_frames]
        
    def _extract_frame_violations(self, elem, frame_num: int = None) -> Optional[FrameViolation]:
        """Extract violation data from frame element"""
        try:
            # Get frame number from element if not provided
            if frame_num is None:
                frame_num_str = elem.get('n') or elem.get('pkt_pts')
                if frame_num_str:
                    frame_num = int(frame_num_str)
                else:
                    return None
            
            # Get timestamp
            timestamp_str = elem.get('pkt_pts_time')
            if timestamp_str:
                timestamp = float(timestamp_str)
            else:
                timestamp = frame_num / self.fps if frame_num else 0
            
            # Skip all-black frames BEFORE checking BRNG violations — noisy
            # analog black sits below broadcast range and would otherwise
            # dominate the violation list
            if self._is_black_frame(elem):
                return None

            # Now check for BRNG violations (only for non-black frames)
            brng_value = None
            brng_tag = elem.find('.//tag[@key="lavfi.signalstats.BRNG"]')
            if brng_tag is not None:
                brng_str = brng_tag.get('value')
                if brng_str:
                    brng_value = float(brng_str)
            
            # Check if this is a violation (BRNG > 0.01 means > 1% of pixels out of range)
            if brng_value is not None and brng_value > 0.01:
                return FrameViolation(
                    frame_num=frame_num,
                    timestamp=timestamp,
                    brng_value=brng_value * 100,  # Convert to percentage
                    violation_score=brng_value
                )
                
        except Exception as e:
            if not hasattr(self, '_logged_extraction_error'):
                logger.debug(f"Error extracting frame violations: {e}")
                self._logged_extraction_error = True
        
        return None
    
    def _process_violation_buffer(self, buffer: List[FrameViolation]) -> List[FrameViolation]:
        """Process buffer of violations"""
        return [v for v in buffer if v is not None]

    def detect_black_segments(self, min_duration: float = 2.0) -> List[Tuple[float, float]]:
        """
        Scan QCTools report for contiguous segments of all-black frames.

        Uses the same luma thresholds as _extract_frame_violations (see
        _is_black_frame), then merges contiguous black frames into segments.
        
        Args:
            min_duration: Minimum duration in seconds for a segment to be reported.
                          Short black flashes (< min_duration) are ignored.
        
        Returns:
            List of (start_time, end_time) tuples for each black segment.
        """
        black_segments = []
        current_black_start = None
        last_black_time = None
        # Allow small gaps (e.g., a single non-black frame in the middle of a black segment)
        gap_tolerance = 0.5  # seconds
        
        try:
            if self.report_path.endswith('.gz'):
                file_handle = gzip.open(self.report_path, 'rt')
            else:
                file_handle = open(self.report_path, 'r')
            
            parser = ET.iterparse(file_handle, events=['start', 'end'])
            parser = iter(parser)
            event, root = next(parser)
            
            for event, elem in parser:
                if event == 'end' and elem.tag == 'frame':
                    timestamp_str = elem.get('pkt_pts_time')
                    if not timestamp_str:
                        elem.clear()
                        root.clear()
                        continue
                    
                    timestamp = float(timestamp_str)

                    # Check black frame using same thresholds as _extract_frame_violations
                    is_black = self._is_black_frame(elem)

                    if is_black:
                        if current_black_start is None:
                            current_black_start = timestamp
                        last_black_time = timestamp
                    else:
                        # Non-black frame: close current segment if gap exceeds tolerance
                        if current_black_start is not None:
                            if last_black_time is not None and (timestamp - last_black_time) > gap_tolerance:
                                duration = last_black_time - current_black_start
                                if duration >= min_duration:
                                    black_segments.append((current_black_start, last_black_time))
                                current_black_start = None
                                last_black_time = None
                    
                    elem.clear()
                    root.clear()
            
            # Close any remaining segment at end of file
            if current_black_start is not None and last_black_time is not None:
                duration = last_black_time - current_black_start
                if duration >= min_duration:
                    black_segments.append((current_black_start, last_black_time))
            
            file_handle.close()
            
        except Exception as e:
            logger.error(f"Error detecting black segments: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        if black_segments:
            logger.info(f"  Detected {len(black_segments)} black segment(s):")
            for start, end in black_segments:
                dur = end - start
                start_tc = f"{int(start // 60):02d}:{start % 60:05.2f}"
                end_tc = f"{int(end // 60):02d}:{end % 60:05.2f}"
                logger.info(f"    {start_tc} – {end_tc} ({dur:.1f}s)")
        else:
            logger.debug(f"  No black segments detected (min duration: {min_duration}s)")
        
        return black_segments

    def find_duplicate_frame_candidates(
        self,
        color_bars_end_time: float = 0,
        black_segments: Optional[List[Tuple[float, float]]] = None,
        min_run_length: int = 2,
    ) -> Tuple[List[Dict], Dict[str, float]]:
        """
        Scan QCTools report for runs of consecutive frames whose YDIF/UDIF/VDIF
        values are below bit-depth-aware thresholds. These are candidate duplicate
        frames likely caused by TBC/framesync error concealment.

        A run of K consecutive low-diff frames represents a freeze of K+1 identical
        frames (the first frame plus K repeats).

        Color bars and known black segments are excluded.

        Args:
            color_bars_end_time: End of color bars to exclude
            black_segments: List of (start, end) tuples to exclude
            min_run_length: Minimum K (consecutive low-diff frames) required to
                            report a run. Default 2 → freeze of ≥3 frames.

        Returns:
            (runs, thresholds) where:
              runs is a list of dicts with keys:
                start_time, end_time, duplicate_count, avg_ydif, max_ydif,
                avg_udif, avg_vdif, avg_vrep
              thresholds is {'ydif': float, 'udif': float, 'vdif': float}
        """
        # Bit-depth-aware thresholds. A genuine TBC duplicate has effectively
        # zero diff; the margin only needs to cover capture noise on the
        # repeated frame. Calibrated against vendor LC tapes (10-bit scale):
        # real freezes sit at YDIF ~0.6-0.8 while ordinary low-motion content
        # starts around 1.5, so 1.0 separates them cleanly. 4.0 (the naive
        # "10-bit codes are 4x" scaling) admits low-motion content. 8-bit
        # threshold is the proportional equivalent.
        if self.bit_depth_10:
            ydif_thresh = 1.0
            udif_thresh = 1.0
            vdif_thresh = 1.0
        else:
            ydif_thresh = 0.25
            udif_thresh = 0.25
            vdif_thresh = 0.25

        thresholds = {'ydif': ydif_thresh, 'udif': udif_thresh, 'vdif': vdif_thresh}
        # Flat-field exclusion: during signal loss the deck/TBC outputs a
        # synthetic frame in which every pixel is identical (YMIN == YMAX,
        # zero saturation). Runs of those are bit-identical and sail through
        # the diff thresholds, but they're signal loss, not a TBC/framesync
        # freeze of picture content. Real frozen picture keeps spatial
        # structure (YMAX - YMIN in the hundreds, even for dark scenes).
        flat_spread_thresh = 8.0 if self.bit_depth_10 else 2.0
        flat_frames_excluded = 0
        black_segments = black_segments or []
        runs: List[Dict] = []

        # Running state for the current candidate run
        run_start_time: Optional[float] = None
        run_last_time: Optional[float] = None
        run_ydifs: List[float] = []
        run_udifs: List[float] = []
        run_vdifs: List[float] = []
        run_vreps: List[float] = []

        def _close_run():
            """Finalize the in-progress run if it meets min_run_length."""
            nonlocal run_start_time, run_last_time
            nonlocal run_ydifs, run_udifs, run_vdifs, run_vreps
            if run_start_time is None or len(run_ydifs) < min_run_length:
                run_start_time = None
                run_last_time = None
                run_ydifs = []
                run_udifs = []
                run_vdifs = []
                run_vreps = []
                return
            runs.append({
                'start_time': run_start_time,
                'end_time': run_last_time,
                'duplicate_count': len(run_ydifs),
                'avg_ydif': sum(run_ydifs) / len(run_ydifs),
                'max_ydif': max(run_ydifs),
                'avg_udif': sum(run_udifs) / len(run_udifs) if run_udifs else 0.0,
                'avg_vdif': sum(run_vdifs) / len(run_vdifs) if run_vdifs else 0.0,
                'avg_vrep': sum(run_vreps) / len(run_vreps) if run_vreps else 0.0,
            })
            run_start_time = None
            run_last_time = None
            run_ydifs = []
            run_udifs = []
            run_vdifs = []
            run_vreps = []

        def _in_excluded_region(ts: float) -> bool:
            if color_bars_end_time and ts <= color_bars_end_time:
                return True
            for bs_start, bs_end in black_segments:
                if bs_start <= ts <= bs_end:
                    return True
            return False

        try:
            if self.report_path.endswith('.gz'):
                file_handle = gzip.open(self.report_path, 'rt')
            else:
                file_handle = open(self.report_path, 'r')

            parser = ET.iterparse(file_handle, events=['start', 'end'])
            parser = iter(parser)
            event, root = next(parser)

            for event, elem in parser:
                if event != 'end' or elem.tag != 'frame':
                    continue

                timestamp_str = elem.get('pkt_pts_time')
                if not timestamp_str:
                    elem.clear()
                    root.clear()
                    continue
                timestamp = float(timestamp_str)

                ydif_tag = elem.find('.//tag[@key="lavfi.signalstats.YDIF"]')
                # YDIF is the primary signal. If absent, can't classify.
                if ydif_tag is None:
                    _close_run()
                    elem.clear()
                    root.clear()
                    continue

                ydif = float(ydif_tag.get('value', '999'))
                udif_tag = elem.find('.//tag[@key="lavfi.signalstats.UDIF"]')
                vdif_tag = elem.find('.//tag[@key="lavfi.signalstats.VDIF"]')
                vrep_tag = elem.find('.//tag[@key="lavfi.signalstats.VREP"]')
                udif = float(udif_tag.get('value', '999')) if udif_tag is not None else 999.0
                vdif = float(vdif_tag.get('value', '999')) if vdif_tag is not None else 999.0
                vrep = float(vrep_tag.get('value', '0')) if vrep_tag is not None else 0.0

                is_low_diff = (
                    ydif < ydif_thresh
                    and udif < udif_thresh
                    and vdif < vdif_thresh
                )

                is_flat = False
                if is_low_diff:
                    ymin_tag = elem.find('.//tag[@key="lavfi.signalstats.YMIN"]')
                    ymax_tag = elem.find('.//tag[@key="lavfi.signalstats.YMAX"]')
                    if ymin_tag is not None and ymax_tag is not None:
                        y_spread = (float(ymax_tag.get('value', '999'))
                                    - float(ymin_tag.get('value', '0')))
                        is_flat = y_spread < flat_spread_thresh
                    if is_flat:
                        flat_frames_excluded += 1

                is_candidate = (
                    is_low_diff
                    and not is_flat
                    and not _in_excluded_region(timestamp)
                )

                if is_candidate:
                    if run_start_time is None:
                        run_start_time = timestamp
                    run_last_time = timestamp
                    run_ydifs.append(ydif)
                    run_udifs.append(udif)
                    run_vdifs.append(vdif)
                    run_vreps.append(vrep)
                else:
                    _close_run()

                elem.clear()
                root.clear()

            # Close any open run at end of file
            _close_run()
            file_handle.close()

            if flat_frames_excluded:
                logger.info(
                    f"  Excluded {flat_frames_excluded} flat-field frame(s) "
                    f"(signal-loss black/mute output, not frozen picture)"
                )

        except Exception as e:
            logger.error(f"Error scanning QCTools report for duplicate frames: {e}")
            import traceback
            logger.error(traceback.format_exc())

        return runs, thresholds


class SophisticatedBorderDetector:
    """Advanced border detection with quality assessment and refinement capabilities"""
    
    def __init__(self, video_path: str, signals=None, check_cancelled_fn=None):
        self.video_path = str(video_path)
        self.signals = signals
        self.check_cancelled = check_cancelled_fn or (lambda: False)
        self._init_video_properties()

    def _emit_progress(self, percent: int):
        """Emit border detection progress as a percentage (0-100)."""
        if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
            safe_percent = min(100, max(0, int(percent)))
            self.signals.frame_analysis_progress.emit(safe_percent)

    def _init_video_properties(self):
        """Initialize video properties"""
        cap = cv2.VideoCapture(self.video_path)
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration = self.total_frames / self.fps if self.fps > 0 else 0
        cap.release()
    
    def detect_borders_with_quality_assessment(self, 
                                              violations: List[FrameViolation] = None,
                                              method: str = 'sophisticated') -> BorderDetectionResult:
        """
        Detect borders using sophisticated quality assessment or simple method.
        
        Args:
            violations: List of frames with known violations for focused detection
            method: 'sophisticated' or 'simple'
        """
        if method == 'simple':
            return self._detect_simple_borders()
        else:
            return self._detect_sophisticated_borders(violations)
    
    def _detect_simple_borders(self, border_size: int = 25) -> BorderDetectionResult:
        """Simple fixed-size border detection"""
        active_x = border_size
        active_y = border_size
        active_width = self.width - (2 * border_size)
        active_height = self.height - (2 * border_size)
        
        # Ensure valid dimensions
        if active_width <= 0 or active_height <= 0:
            active_x = active_y = 0
            active_width = self.width
            active_height = self.height
            border_size = 0
        
        border_regions = self._calculate_border_regions(
            active_x, active_y, active_width, active_height
        )
        
        return BorderDetectionResult(
            active_area=(active_x, active_y, active_width, active_height),
            border_regions=border_regions,
            detection_method='simple',
            quality_frame_hints=[],
            requires_refinement=False
        )
    
    def _detect_sophisticated_borders(self, 
                                     violations: List[FrameViolation] = None) -> BorderDetectionResult:
        """Sophisticated border detection with quality assessment"""
        cap = cv2.VideoCapture(self.video_path)
        
        # Select quality frames for analysis
        self._emit_progress(0)
        quality_frames = self._select_quality_frames(cap, violations)

        if self.check_cancelled():
            cap.release()
            return self._detect_simple_borders()

        if len(quality_frames) < 5:
            logger.warning("Insufficient quality frames, falling back to simple detection")
            cap.release()
            self._emit_progress(100)
            return self._detect_simple_borders()
        
        self._emit_progress(15)

        # Detect borders using quality frames
        borders = self._analyze_borders_from_frames(cap, quality_frames)

        if self.check_cancelled():
            cap.release()
            return self._detect_simple_borders()

        self._emit_progress(25)

        # Detect head switching artifacts
        head_switching = self._detect_head_switching(cap, borders)

        if self.check_cancelled():
            cap.release()
            return self._detect_simple_borders()

        self._emit_progress(35)

        # Check for vertical blanking lines
        vertical_blanking = self._detect_vertical_blanking(cap, quality_frames)
        
        # Adjust borders based on blanking lines
        if vertical_blanking:
            borders = self._adjust_for_blanking(borders, vertical_blanking)
        
        self._emit_progress(40)
        
        cap.release()
        
        # Calculate active area, using head switching height for bottom crop if larger
        bottom_crop = borders['bottom']
        if head_switching and head_switching.get('detected'):
            hs_avg = head_switching.get('avg_height_px', 0)
            if hs_avg > bottom_crop:
                bottom_crop = hs_avg
                logger.debug(f"  Bottom crop expanded from {borders['bottom']}px to {hs_avg}px based on head switching artifact height")

        active_x = borders['left']
        active_y = borders['top']
        active_width = self.width - borders['left'] - borders['right']
        active_height = self.height - borders['top'] - bottom_crop

        # Log the detection results
        logger.debug(f"  Detected borders - L:{borders['left']}px R:{borders['right']}px T:{borders['top']}px B:{bottom_crop}px")
        logger.debug(f"  Active picture area: {active_width}x{active_height} (from {self.width}x{self.height})")
        logger.debug(f"  Using {len(quality_frames)} quality frames for detection\n")
        
        # Add padding for safety
        padding = 5
        active_x += padding
        active_y += padding
        active_width -= 2 * padding
        active_height -= 2 * padding
        
        border_regions = self._calculate_border_regions(
            active_x, active_y, active_width, active_height
        )
        
        # Generate quality hints for signalstats
        quality_hints = [(f['timestamp'], f['quality']) for f in quality_frames[:10]]
        
        self._emit_progress(45)
        
        return BorderDetectionResult(
            active_area=(active_x, active_y, active_width, active_height),
            border_regions=border_regions,
            detection_method='sophisticated',
            quality_frame_hints=quality_hints,
            head_switching_artifacts=head_switching,
            requires_refinement=False
        )
    
    def _select_quality_frames(self, cap, violations: List[FrameViolation] = None) -> List[Dict]:
        """Select high-quality frames for border detection"""
        quality_frames = []
        
        # If we have violations, prioritize those frames
        if violations:
            violation_batch = violations[:30]
            for i, v in enumerate(violation_batch):
                if self.check_cancelled():
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, v.frame_num)
                ret, frame = cap.read()
                if ret:
                    quality = self._assess_frame_quality(frame)
                    if quality['is_suitable']:
                        quality_frames.append({
                            'frame_num': v.frame_num,
                            'timestamp': v.timestamp,
                            'frame': frame,
                            'quality': quality['overall_quality']
                        })
                # Emit progress: violation frames span 1→8%
                if (i + 1) % 5 == 0 or i == len(violation_batch) - 1:
                    self._emit_progress(1 + int((i + 1) / len(violation_batch) * 7))
        
        # If we need more frames, sample evenly
        if len(quality_frames) < 30:
            sample_indices = np.linspace(0, self.total_frames - 1, 50, dtype=int)
            for j, idx in enumerate(sample_indices):
                if self.check_cancelled():
                    break
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ret, frame = cap.read()
                if ret:
                    quality = self._assess_frame_quality(frame)
                    if quality['is_suitable']:
                        quality_frames.append({
                            'frame_num': idx,
                            'timestamp': idx / self.fps,
                            'frame': frame,
                            'quality': quality['overall_quality']
                        })
                # Emit progress: sampled frames span 8→14%
                if (j + 1) % 5 == 0 or j == len(sample_indices) - 1:
                    self._emit_progress(8 + int((j + 1) / len(sample_indices) * 6))
        
        # Sort by quality
        quality_frames.sort(key=lambda x: x['quality'], reverse=True)
        return quality_frames[:30]
    
    def _assess_frame_quality(self, frame) -> Dict:
        """Assess frame quality for suitability"""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        result = {
            'is_suitable': False,
            'overall_quality': 0.0,
            'reasons_rejected': []
        }
        
        # Check brightness
        mean_brightness = np.mean(gray)
        std_brightness = np.std(gray)
        
        if mean_brightness < 15:
            result['reasons_rejected'].append("Too dark")
        elif mean_brightness > 240:
            result['reasons_rejected'].append("Too bright")
        elif std_brightness < 15:
            result['reasons_rejected'].append("Low contrast")
        else:
            # Calculate quality score
            brightness_score = 1.0 - abs(mean_brightness - 120) / 120
            contrast_score = min(std_brightness / 50.0, 1.0)
            result['overall_quality'] = (brightness_score + contrast_score) / 2
            result['is_suitable'] = True
        
        return result
    
    def _analyze_borders_from_frames(self, cap, quality_frames: List[Dict]) -> Dict:
        """Analyze borders from quality frames"""
        borders = {'left': [], 'right': [], 'top': [], 'bottom': []}
        threshold = 10
        edge_sample_width = 100
        
        for frame_data in quality_frames:
            frame = frame_data['frame']
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            
            # Detect left border
            for x in range(min(edge_sample_width, w)):
                if np.mean(gray[:, x]) > threshold:
                    borders['left'].append(x)
                    break
            
            # Detect right border
            for x in range(w-1, max(w-edge_sample_width-1, -1), -1):
                if np.mean(gray[:, x]) > threshold:
                    borders['right'].append(w - x - 1)
                    break
            
            # Detect top border
            for y in range(min(20, h)):
                if np.mean(gray[y, w//3:2*w//3]) > threshold:
                    borders['top'].append(y)
                    break
            
            # Detect bottom border
            for y in range(h-1, max(h-20, -1), -1):
                if np.mean(gray[y, w//3:2*w//3]) > threshold:
                    borders['bottom'].append(h - y - 1)
                    break
        
        # Calculate median borders
        return {
            'left': int(np.median(borders['left'])) if borders['left'] else 0,
            'right': int(np.median(borders['right'])) if borders['right'] else 0,
            'top': int(np.median(borders['top'])) if borders['top'] else 0,
            'bottom': int(np.median(borders['bottom'])) if borders['bottom'] else 0
        }
    
    def _detect_vertical_blanking(self, cap, quality_frames: List[Dict]) -> Optional[Dict]:
        """Detect vertical blanking lines"""
        blanking = {'left': None, 'right': None}
        edge_width = 30
        
        for frame_data in quality_frames[:10]:
            frame = frame_data['frame']
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            
            # Check left edge
            left_region = gray[:, :edge_width]
            for x in range(edge_width):
                column = left_region[:, x]
                if np.mean(column) < 20 and np.std(column) < 10:
                    if blanking['left'] is None or x > blanking['left']:
                        blanking['left'] = x
            
            # Check right edge
            right_region = gray[:, -edge_width:]
            for x in range(edge_width):
                column = right_region[:, x]
                if np.mean(column) < 20 and np.std(column) < 10:
                    if blanking['right'] is None or (w - edge_width + x) < blanking['right']:
                        blanking['right'] = w - edge_width + x
        
        return blanking if any(blanking.values()) else None
    
    def _adjust_for_blanking(self, borders: Dict, blanking: Dict) -> Dict:
        """Adjust borders based on detected blanking lines"""
        if blanking.get('left') and blanking['left'] > borders['left']:
            borders['left'] = blanking['left'] + 2
        if blanking.get('right') and blanking['right'] < self.width - borders['right']:
            borders['right'] = self.width - blanking['right'] + 2
        return borders
    
    def _detect_head_switching(self, cap, borders: Dict) -> Optional[Dict]:
        """Detect head switching artifacts at bottom of frame.

        Measures the height of the artifact region by scanning upward from the
        bottom of the frame to find where the asymmetry pattern ends.
        """
        # Sample frames for analysis
        max_scan_lines = 30  # scan up to 30 lines from the bottom
        sample_frames = np.linspace(0, self.total_frames - 1, 20, dtype=int)
        artifact_count = 0
        artifact_heights = []

        for frame_idx in sample_frames:
            if self.check_cancelled():
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            # Analyze bottom lines for asymmetry (characteristic of head switching)
            bottom_region = gray[-max_scan_lines:, :]
            frame_has_artifact = False
            artifact_height = 0

            # Scan from bottom up to find extent of artifact
            for i in range(len(bottom_region) - 1, -1, -1):
                line = bottom_region[i]
                left_half = line[:len(line)//2]
                right_half = line[len(line)//2:]

                left_mean = np.mean(left_half)
                right_mean = np.mean(right_half)

                if left_mean > 10:
                    asymmetry = abs(left_mean - right_mean) / left_mean
                    if asymmetry > 0.5:
                        frame_has_artifact = True
                        artifact_height = len(bottom_region) - i
                    else:
                        # Stop scanning once we hit a non-artifact line
                        if frame_has_artifact:
                            break
                else:
                    if frame_has_artifact:
                        break

            if frame_has_artifact:
                artifact_count += 1
                artifact_heights.append(artifact_height)

        if artifact_count > len(sample_frames) * 0.2:
            avg_height = int(round(np.mean(artifact_heights))) if artifact_heights else 0
            max_height = int(max(artifact_heights)) if artifact_heights else 0
            return {
                'detected': True,
                'severity': 'high' if artifact_count > len(sample_frames) * 0.5 else 'moderate',
                'percentage': (artifact_count / len(sample_frames)) * 100,
                'avg_height_px': avg_height,
                'max_height_px': max_height,
            }

        return None
    
    def _calculate_border_regions(self, x: int, y: int, w: int, h: int) -> Dict:
        """Calculate border region coordinates"""
        regions = {}
        
        if x > 10:
            regions['left_border'] = (0, 0, x, self.height)
        else:
            regions['left_border'] = None
        
        right_start = x + w
        if right_start < self.width - 10:
            regions['right_border'] = (right_start, 0, self.width - right_start, self.height)
        else:
            regions['right_border'] = None
        
        if y > 10:
            regions['top_border'] = (0, 0, self.width, y)
        else:
            regions['top_border'] = None
        
        bottom_start = y + h
        if bottom_start < self.height - 10:
            regions['bottom_border'] = (0, bottom_start, self.width, self.height - bottom_start)
        else:
            regions['bottom_border'] = None
        
        return regions
    
    def refine_borders(self, current_borders: BorderDetectionResult, 
                       brng_results: BRNGAnalysisResult) -> BorderDetectionResult:
        """
        Refine borders based on BRNG analysis results.
        This implements the smart expansion logic from the original.
        """
        if not brng_results.requires_border_adjustment:
            return current_borders
        
        x, y, w, h = current_borders.active_area
        expansions = brng_results.refinement_recommendations or {}
        
        # Apply recommended expansions
        new_x = max(0, x + expansions.get('left', 0))
        new_y = max(0, y + expansions.get('top', 0))
        new_w = max(100, w - expansions.get('left', 0) - expansions.get('right', 0))
        new_h = max(100, h - expansions.get('top', 0) - expansions.get('bottom', 0))
        
        # Ensure within video bounds
        new_w = min(new_w, self.width - new_x)
        new_h = min(new_h, self.height - new_y)
        
        # Update border regions
        border_regions = self._calculate_border_regions(new_x, new_y, new_w, new_h)
        
        return BorderDetectionResult(
            active_area=(new_x, new_y, new_w, new_h),
            border_regions=border_regions,
            detection_method=current_borders.detection_method + '_refined',
            quality_frame_hints=current_borders.quality_frame_hints,
            head_switching_artifacts=current_borders.head_switching_artifacts,
            requires_refinement=False,
            expansion_recommendations=expansions
        )
    
    def find_good_representative_frame(self, target_time=150, search_window=120):
        """
        Find a good representative frame using enhanced quality assessment
        
        Args:
            target_time: Target time in seconds for frame selection
            search_window: Window in seconds to search around target time
            
        Returns:
            Frame (numpy array) or None if no suitable frame found
        """
        # Calculate search range
        target_frame = int(target_time * self.fps)
        window_frames = int(search_window * self.fps)
        
        start_frame = max(0, target_frame - window_frames // 2)
        end_frame = min(self.total_frames - 1, target_frame + window_frames // 2)
        
        if end_frame >= self.total_frames:
            mid_point = self.total_frames // 2
            start_frame = max(0, mid_point - window_frames // 2)
            end_frame = min(self.total_frames - 1, mid_point + window_frames // 2)
        
        logger.debug(f"Searching for good representative frame between {start_frame/self.fps:.1f}s and {end_frame/self.fps:.1f}s...")
        
        # Open video for frame selection
        cap = cv2.VideoCapture(self.video_path)
        
        # Check frames every 1 second in the search window
        check_interval = max(1, int(self.fps))
        frame_indices = list(range(start_frame, end_frame, check_interval))
        
        # Use existing quality assessment to find the best frame
        quality_frames = []
        total_to_check = len(frame_indices)
        for i, idx in enumerate(frame_indices):
            if self.check_cancelled():
                break
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret:
                continue
            
            # Use existing _assess_frame_quality method
            quality = self._assess_frame_quality(frame)
            if quality['is_suitable']:
                quality_frames.append((idx, frame.copy(), quality['overall_quality']))
            
            # Emit progress: frame search spans 45→95% of border detection
            if (i + 1) % 5 == 0 or i == total_to_check - 1:
                self._emit_progress(45 + int((i + 1) / total_to_check * 50))
        
        cap.release()
        
        if quality_frames:
            # Sort by quality score and return the best frame
            quality_frames.sort(key=lambda x: x[2], reverse=True)
            best_frame_idx, best_frame, best_score = quality_frames[0]
            logger.info(f"✓ Selected high-quality frame at {best_frame_idx/self.fps:.1f}s (quality score: {best_score:.3f})\n")
            return best_frame
        else:
            # Final fallback - use target frame
            logger.warning(f"⚠️ No suitable frame found, using target frame as fallback\n")
            cap = cv2.VideoCapture(self.video_path)
            fallback_frame = target_frame if target_frame < self.total_frames else self.total_frames // 2
            cap.set(cv2.CAP_PROP_POS_FRAMES, fallback_frame)
            ret, frame = cap.read()
            cap.release()
            return frame if ret else None
        
    def generate_border_visualization(self, output_path, active_area=None, head_switching_results=None, target_time=150, search_window=120, detection_method='sophisticated'):
        """
        Generate visual showing detected borders and active area
        
        Creates a side-by-side comparison with:
        - Left: Full frame with border regions highlighted in red
        - Right: Active picture area only
        
        Args:
            output_path: Path to save the visualization image
            active_area: Tuple (x, y, w, h) of active picture area
            head_switching_results: Results from head switching analysis
            target_time: Target time for frame selection (seconds)
            search_window: Window to search for good frame (seconds)
            detection_method: Detection method used ('simple' or 'sophisticated')
            
        Returns:
            True if successful, False otherwise
        """
        # Import matplotlib (should be at top of file)
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
        
        # Find a good representative frame
        frame = self.find_good_representative_frame(target_time, search_window)
        
        self._emit_progress(96)
        
        if frame is None:
            logger.warning("Could not find suitable frame for border visualization")
            self._emit_progress(100)
            return False
            
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 8))
        
        # Full frame with regions marked
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        ax1.imshow(frame_rgb)
        
        # Add title indicating detection method
        if detection_method == 'simple':
            ax1.set_title('Full Frame with Border Detection\n(Simple Border Detection Used)', fontsize=10, color='darkred')
        else:
            ax1.set_title('Full Frame with Border Detection')
        ax1.axis('off')
        
        if active_area:
            x, y, w, h = active_area
            
            # Mark border regions differently based on detection method
            border_added = False
            
            if detection_method == 'simple':
                # Use dotted red lines for simple border detection
                line_style = '--'  # Dashed line style
                line_width = 2.5
                line_color = 'red'
                
                # Draw outline around active area with dotted red lines
                # Left border
                if x > 10:
                    ax1.plot([x, x], [0, self.height], linestyle=line_style, 
                            linewidth=line_width, color=line_color, label='Border Edge (Simple Detection)')
                    border_added = True
                
                # Right border
                if x + w < self.width - 10:
                    ax1.plot([x + w, x + w], [0, self.height], linestyle=line_style, 
                            linewidth=line_width, color=line_color)
                    if not border_added:
                        ax1.plot([x + w, x + w], [0, self.height], linestyle=line_style, 
                                linewidth=line_width, color=line_color, label='Border Edge (Simple Detection)')
                        border_added = True
                
                # Top border
                if y > 10:
                    ax1.plot([0, self.width], [y, y], linestyle=line_style, 
                            linewidth=line_width, color=line_color)
                    if not border_added:
                        ax1.plot([0, self.width], [y, y], linestyle=line_style, 
                                linewidth=line_width, color=line_color, label='Border Edge (Simple Detection)')
                        border_added = True
                
                # Bottom border
                if y + h < self.height - 10:
                    ax1.plot([0, self.width], [y + h, y + h], linestyle=line_style, 
                            linewidth=line_width, color=line_color)
                    if not border_added:
                        ax1.plot([0, self.width], [y + h, y + h], linestyle=line_style, 
                                linewidth=line_width, color=line_color, label='Border Edge (Simple Detection)')
                        border_added = True
            else:
                # Use shaded rectangles for sophisticated border detection
                if x > 10:  # Left border
                    left_rect = patches.Rectangle((0, 0), x, self.height, linewidth=2,
                                                edgecolor='red', facecolor='red', alpha=0.3,
                                                label='Border Regions')
                    ax1.add_patch(left_rect)
                    border_added = True
                
                if x + w < self.width - 10:  # Right border
                    right_rect = patches.Rectangle((x + w, 0), self.width - (x + w), self.height, 
                                                linewidth=2, edgecolor='red', facecolor='red', alpha=0.3)
                    if not border_added:
                        right_rect.set_label('Border Regions')
                        border_added = True
                    ax1.add_patch(right_rect)
                    
                if y > 10:  # Top border
                    top_rect = patches.Rectangle((0, 0), self.width, y, linewidth=2,
                                            edgecolor='red', facecolor='red', alpha=0.3)
                    if not border_added:
                        top_rect.set_label('Border Regions')
                        border_added = True
                    ax1.add_patch(top_rect)
                    
                if y + h < self.height - 10:  # Bottom border
                    bottom_rect = patches.Rectangle((0, y + h), self.width, self.height - (y + h), 
                                                linewidth=2, edgecolor='red', facecolor='red', alpha=0.3)
                    if not border_added:
                        bottom_rect.set_label('Border Regions')
                    ax1.add_patch(bottom_rect)
            
            # Highlight head switching artifacts if detected
            if head_switching_results and head_switching_results.get('detected'):
                hs_height = head_switching_results.get('avg_height_px', 1)
                hs_rect = patches.Rectangle(
                    (0, self.height - hs_height), self.width, hs_height,
                    linewidth=2, edgecolor='orange', facecolor='orange', alpha=0.35,
                    label=f'Head Switching Region ({hs_height}px)')
                ax1.add_patch(hs_rect)

            if border_added or (head_switching_results and head_switching_results.get('detected')):
                ax1.legend()

            # Active area only
            active_frame = frame_rgb[y:y+h, x:x+w]
            ax2.imshow(active_frame)
            ax2.set_title('Active Picture Area Only')
            ax2.axis('off')

            # Add text annotations
            border_info = f'Border sizes: L={x}px, R={self.width-x-w}px, T={y}px, B={self.height-y-h}px'
            fig.text(0.5, 0.02, border_info, ha='center', fontsize=10)

            # Add head switching info
            if head_switching_results and head_switching_results.get('detected'):
                percentage = head_switching_results.get('percentage', 0)
                avg_h = head_switching_results.get('avg_height_px', 0)
                hs_info = f"Head switching: {percentage:.1f}% of frames, avg height {avg_h}px"
                fig.text(0.5, 0.95, hs_info, ha='center', fontsize=11, weight='bold', color='orange')
            else:
                fig.text(0.5, 0.95, 'No head switching artifacts detected', ha='center', fontsize=12, weight='bold', color='green')
        else:
            ax2.text(0.5, 0.5, 'No borders detected', 
                    ha='center', va='center', transform=ax2.transAxes)
            ax2.axis('off')
            
            # Still show head switching info even without borders
            if head_switching_results and head_switching_results.get('detected'):
                hs_height = head_switching_results.get('avg_height_px', 1)
                hs_rect = patches.Rectangle(
                    (0, self.height - hs_height), self.width, hs_height,
                    linewidth=2, edgecolor='orange', facecolor='orange', alpha=0.35,
                    label=f'Head Switching Region ({hs_height}px)')
                ax1.add_patch(hs_rect)
                ax1.legend()

                percentage = head_switching_results.get('percentage', 0)
                avg_h = head_switching_results.get('avg_height_px', 0)
                hs_info = f"Head switching: {percentage:.1f}% of frames, avg height {avg_h}px"
                fig.text(0.5, 0.95, hs_info, ha='center', fontsize=11, weight='bold', color='orange')
            else:
                fig.text(0.5, 0.95, 'No head switching artifacts detected', ha='center', fontsize=12, weight='bold', color='green')
            
        plt.tight_layout()
        plt.savefig(output_path, dpi=100, bbox_inches='tight')
        plt.close()

        self._emit_progress(100)

        # logger.info(f"Border detection visualization saved to: {output_path}")
        return True


class DifferentialBRNGAnalyzer:
    """
    BRNG analyzer using differential detection method.
    Compares highlighted vs original frames to eliminate false positives.
    """
    
    def __init__(self, video_path: str, border_data: BorderDetectionResult = None,
                 check_cancelled_fn=None, signals=None):
        self.video_path = Path(video_path)
        self.border_data = border_data
        self.active_area = border_data.active_area if border_data else None
        self.check_cancelled = check_cancelled_fn or (lambda: False)
        self.signals = signals
        self._init_video_properties()

    def _emit_progress(self, percent: int):
        """Emit BRNG analysis progress as a percentage (0-100)."""
        if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
            safe_percent = min(100, max(0, int(percent)))
            self.signals.frame_analysis_progress.emit(safe_percent)

    def _init_video_properties(self):
        """Initialize video properties"""
        cap = cv2.VideoCapture(str(self.video_path))
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration = self.total_frames / self.fps if self.fps > 0 else 0
        cap.release()
    

    def analyze_with_differential_detection(self, 
                                       output_dir: Path,
                                       duration_limit: int = 300,
                                       skip_start_seconds: float = 0,
                                       qctools_violations: List[FrameViolation] = None,
                                       analysis_periods: List[Tuple[float, int]] = None,
                                       upstream_context: 'UpstreamAnalysisContext' = None) -> BRNGAnalysisResult:
        """
        Perform differential BRNG detection by creating highlighted and original versions.
        Now supports analyzing specific periods from signalstats.
        
        Args:
            upstream_context: Findings from border detection and signalstats that
                inform sensitivity, sampling density, and thumbnail selection.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(exist_ok=True)
        
        # Store upstream context for use in submethods
        self.upstream_context = upstream_context
        
        # Store paths to temporary videos for thumbnail creation
        temp_video_paths = []

        self._emit_progress(0)

        # Use analysis periods if provided, otherwise fall back to original behavior
        if analysis_periods:
            # logger.debug(f"  Using {len(analysis_periods)} analysis periods from signalstats")
            all_violations = []
            period_summaries = []
            total_periods = len(analysis_periods)
            
            for i, (start_time, duration) in enumerate(analysis_periods):
                if self.check_cancelled():
                    break
                logger.info(f"  Analyzing period {i+1}: {start_time:.1f}s - {start_time+duration:.1f}s")
                
                # Determine sensitivity and sampling density from upstream context
                period_sensitivity = 'normal'
                period_target_samples = None  # None = use default logic
                if self.upstream_context and i in self.upstream_context.period_diagnoses:
                    diagnosis = self.upstream_context.period_diagnoses[i]
                    active_data = self.upstream_context.period_active_area_brng.get(i, {})
                    active_pct = active_data.get('violation_pct', 0)
                    active_max = active_data.get('max_brng', 0)
                    
                    if diagnosis == 'border_violations':
                        period_sensitivity = 'strict'
                        logger.debug(f"    Strict sensitivity (signalstats: border violations)")
                    elif diagnosis == 'content_violations':
                        period_sensitivity = 'normal'
                        logger.debug(f"    Normal sensitivity (signalstats: content violations)")
                    elif diagnosis == 'minimal_violations':
                        period_sensitivity = 'strict'
                        logger.debug(f"    Strict sensitivity (signalstats: minimal violations)")
                    
                    # Adapt sampling density based on active-area BRNG magnitude
                    if active_pct < 1.0 and active_max < 0.01:
                        period_target_samples = 30
                        logger.debug(f"    Light sampling ({period_target_samples} frames): "
                                   f"active area BRNG negligible ({active_pct:.1f}%)")
                    elif active_pct > 30 or active_max > 1.0:
                        period_target_samples = 200
                        logger.debug(f"    Dense sampling ({period_target_samples} frames): "
                                   f"significant active area BRNG ({active_pct:.1f}%)")
                
                # Calculate progress range for this period (each period gets equal share of 0-85%)
                period_base = int(i / total_periods * 85)
                period_vid_done = int((i + 0.4) / total_periods * 85)
                period_analysis_start = int((i + 0.45) / total_periods * 85)
                period_end = int((i + 1) / total_periods * 85)
                
                self._emit_progress(period_base)
                
                # Create temporary directory for this period
                temp_dir = output_dir / f"temp_brng_period_{i+1}"
                temp_dir.mkdir(exist_ok=True)
                
                # Generate comparison videos for this period
                highlighted_path = temp_dir / f"{self.video_path.stem}_highlighted_p{i+1}.mp4"
                original_path = temp_dir / f"{self.video_path.stem}_original_p{i+1}.mp4"
                
                if not self._create_comparison_videos_for_period(
                    highlighted_path, original_path, start_time, duration,
                    progress_range=(period_base, period_vid_done)):
                    logger.error(f"Failed to create comparison videos for period {i+1}")
                    continue
                
                self._emit_progress(period_vid_done)
                
                # Store paths for later thumbnail creation
                temp_video_paths.append({
                    'highlighted': highlighted_path,
                    'original': original_path,
                    'start_time': start_time,
                    'duration': duration,
                    'temp_dir': temp_dir
                })
                
                # Analyze violations for this period
                period_violations, period_stats = self._analyze_differential_violations(
                    highlighted_path, original_path, start_time,
                    qctools_violations=qctools_violations,
                    progress_range=(period_analysis_start, period_end),
                    sensitivity=period_sensitivity,
                    target_samples=period_target_samples,
                    period_index=i
                )
                
                # Track per-period summary
                period_summary = {
                    'period_num': i + 1,
                    'start_time': start_time,
                    'end_time': start_time + duration,
                    'qctools_frames_targeted': period_stats['qctools_frames_targeted'],
                    'frames_mapped': period_stats['frames_mapped_to_period'],
                    'total_samples': period_stats['total_samples_analyzed'],
                    'frames_checked': period_stats['frames_checked'],
                    'violations_found': period_stats['violations_found'],
                    'sensitivity_used': period_sensitivity,
                }
                
                # Include signalstats diagnosis if available from upstream context
                if self.upstream_context and i in self.upstream_context.period_diagnoses:
                    period_summary['signalstats_diagnosis'] = self.upstream_context.period_diagnoses[i]
                    active_data = self.upstream_context.period_active_area_brng.get(i, {})
                    period_summary['signalstats_active_area_pct'] = active_data.get('violation_pct', 0)
                    period_summary['signalstats_active_area_max'] = active_data.get('max_brng', 0)
                
                period_summaries.append(period_summary)
                
                all_violations.extend(period_violations)
                
                self._emit_progress(period_end)
            
            violations = all_violations
            logger.info(f"  Analyzed {len(violations)} frames with potential violations across all periods\n")
        else:
            # Original single-period analysis (similar handling)
            logger.debug(f"  Creating temporary comparison videos (duration: {duration_limit}s)")
            
            temp_dir = output_dir / "temp_brng"
            temp_dir.mkdir(exist_ok=True)
            
            highlighted_path = temp_dir / f"{self.video_path.stem}_highlighted.mp4"
            original_path = temp_dir / f"{self.video_path.stem}_original.mp4"
            
            if not self._create_comparison_videos(highlighted_path, original_path, 
                                                duration_limit, skip_start_seconds):
                logger.error("Failed to create comparison videos")
                return None
            
            self._emit_progress(40)
            
            temp_video_paths.append({
                'highlighted': highlighted_path,
                'original': original_path,
                'start_time': skip_start_seconds,
                'duration': duration_limit,
                'temp_dir': temp_dir
            })
            
            violations = self._analyze_differential_violations(
                highlighted_path, original_path, skip_start_seconds,
                qctools_violations=qctools_violations,
                progress_range=(40, 85)
            )
            # Handle both tuple (new) and list (legacy) returns
            period_summaries = []
            if isinstance(violations, tuple):
                violations, single_stats = violations
                period_summaries.append({
                    'period_num': 1,
                    'start_time': skip_start_seconds,
                    'end_time': skip_start_seconds + duration_limit,
                    'qctools_frames_targeted': single_stats['qctools_frames_targeted'],
                    'frames_mapped': single_stats['frames_mapped_to_period'],
                    'total_samples': single_stats['total_samples_analyzed'],
                    'frames_checked': single_stats['frames_checked'],
                    'violations_found': single_stats['violations_found']
                })
        
        self._emit_progress(85)
        
        # Generate patterns and reports
        aggregate_patterns = self._analyze_aggregate_patterns(violations)
        actionable_report = self._generate_actionable_report(violations, aggregate_patterns)
        
        # Create thumbnails using temporal diversity selection
        thumbnails = []
        if violations and len(violations) > 0:
            thumb_dir = output_dir / "brng_thumbnails"
            thumb_dir.mkdir(exist_ok=True)
            
            # Clean up previous thumbnails to ensure only current run's thumbnails are shown
            for old_thumb in thumb_dir.glob("*.jpg"):
                try:
                    old_thumb.unlink()
                    logger.debug(f"  Removed previous thumbnail: {old_thumb.name}")
                except Exception as e:
                    logger.warning(f"  Could not remove old thumbnail {old_thumb.name}: {e}")
            for old_thumb in thumb_dir.glob("*.png"):
                try:
                    old_thumb.unlink()
                    logger.debug(f"  Removed previous thumbnail: {old_thumb.name}")
                except Exception as e:
                    logger.warning(f"  Could not remove old thumbnail {old_thumb.name}: {e}")
            
            # Select violations with temporal spacing and upstream-context-aware priority
            selected_violations = self._select_diverse_violations_for_thumbnails(
                violations, 
                max_thumbnails=5, 
                min_time_separation=5.0,
                analysis_periods=analysis_periods
            )
            
            logger.debug(f"  Creating diagnostic thumbnails for {len(selected_violations)} temporally diverse violations\n")
            thumbnails = self._create_diagnostic_thumbnails(selected_violations, temp_video_paths, thumb_dir)
            logger.info(f"Saved {len(thumbnails)} thumbnails to {thumb_dir}")
        
        self._emit_progress(95)
        
        # Clean up all temporary files
        for video_info in temp_video_paths:
            try:
                video_info['highlighted'].unlink()
                video_info['original'].unlink()
                video_info['temp_dir'].rmdir()
            except:
                pass
        
        self._emit_progress(100)
        
        return BRNGAnalysisResult(
            violations=violations,
            aggregate_patterns=aggregate_patterns,
            actionable_report=actionable_report,
            thumbnails=thumbnails,
            requires_border_adjustment=aggregate_patterns.get('requires_border_adjustment', False),
            refinement_recommendations=aggregate_patterns.get('expansion_recommendations'),
            analysis_periods=analysis_periods if analysis_periods else None,
            period_summaries=period_summaries if period_summaries else None
        )
    
    def _create_comparison_videos_for_period(self, highlighted_path: Path, original_path: Path,
                                            start_time: float, duration: int,
                                            progress_range: Tuple[int, int] = None) -> bool:
        """Create highlighted and original versions for a specific time period"""
        # Build crop filter if active area exists
        crop_filter = ""
        if self.active_area:
            x, y, w, h = self.active_area
            crop_filter = f"crop={w}:{h}:{x}:{y},"
        
        # Create highlighted version for this period
        highlighted_cmd = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", str(self.video_path),
            "-t", str(duration),
            "-vf", f"{crop_filter}signalstats=out=brng:color=magenta",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-y",
            str(highlighted_path)
        ]
        
        # Create original version for this period
        original_cmd = [
            "ffmpeg",
            "-ss", str(start_time),
            "-i", str(self.video_path),
            "-t", str(duration),
            "-vf", crop_filter.rstrip(',') if crop_filter else "null",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-y",
            str(original_path)
        ]
        
        try:
            subprocess.run(highlighted_cmd, capture_output=True, check=True)
            # Emit midpoint progress between the two ffmpeg calls
            if progress_range:
                mid = progress_range[0] + (progress_range[1] - progress_range[0]) // 2
                self._emit_progress(mid)
            subprocess.run(original_cmd, capture_output=True, check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"FFmpeg error: {e}")
            return False
    
    def _analyze_differential_violations(self, highlighted_path: Path, 
                        original_path: Path,
                        period_start_time: float,  # Renamed for clarity
                        qctools_violations: List[FrameViolation] = None,
                        progress_range: Tuple[int, int] = None,
                        sensitivity: str = 'normal',
                        target_samples: int = None,
                        period_index: int = None) -> List[FrameViolation]:
        """Analyze violations using differential detection.
        
        Args:
            sensitivity: 'strict', 'normal', or 'visualization' — controls detection
                thresholds in _detect_differential_violations_frame.
            target_samples: Override for how many frames to analyze. None = default logic.
                Set by upstream context based on active-area BRNG magnitude.
            period_index: 0-based index of this period, used for upstream context lookups.
        """
        violations = []
        
        cap_h = cv2.VideoCapture(str(highlighted_path))
        cap_o = cv2.VideoCapture(str(original_path))
        
        # Get video properties
        total_frames = int(cap_h.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap_h.get(cv2.CAP_PROP_FPS)
        duration = total_frames / fps if fps > 0 else 0
        
        # Calculate the time range this video segment represents
        period_end_time = period_start_time + duration
        
        # Sampling thresholds — adapt based on upstream context
        min_qctools_samples = target_samples or 50
        distributed_fill_count = target_samples or 100
        max_sample_cap = target_samples or 200
        
        # Use QCTools violations to target specific frames
        if qctools_violations and len(qctools_violations) > 0:
            logger.debug(f"  Targeting {len(qctools_violations)} frames identified by QCTools")
            
            sample_indices = []
            for v in qctools_violations[:500]:  # Increased limit
                # Check if this violation is within our period
                if period_start_time <= v.timestamp < period_end_time:
                    # Convert to frame position in the extracted video segment
                    relative_time = v.timestamp - period_start_time
                    frame_in_segment = int(relative_time * fps)
                    if 0 <= frame_in_segment < total_frames:
                        sample_indices.append(frame_in_segment)
            
            logger.debug(f"  Mapped {len(sample_indices)} violation frames to processed video positions")
            
            # If we didn't get enough samples from QCTools, add some distributed samples
            if len(sample_indices) < min_qctools_samples:
                logger.debug(f"  Adding distributed samples to reach minimum coverage ({min_qctools_samples})")
                additional_samples = np.linspace(0, total_frames - 1, distributed_fill_count, dtype=int)
                for sample in additional_samples:
                    if sample not in sample_indices:
                        sample_indices.append(sample)
                sample_indices = sorted(sample_indices)[:max_sample_cap]
        else:
            # Fallback to distributed sampling
            logger.info(f"  No QCTools violations provided, using distributed sampling")
            num_samples = min(target_samples or 500, total_frames)
            sample_indices = np.linspace(0, total_frames - 1, num_samples, dtype=int).tolist()
        
        logger.debug(f"  Analyzing {len(sample_indices)} frame samples...")
        
        # Calculate progress emission interval
        total_samples = len(sample_indices)
        # Emit every ~10% of frames, minimum every 5 frames
        emit_interval = max(5, total_samples // 10)
        
        frames_checked = 0
        for sample_num, idx in enumerate(sample_indices):
            if self.check_cancelled():
                break
            cap_h.set(cv2.CAP_PROP_POS_FRAMES, idx)
            cap_o.set(cv2.CAP_PROP_POS_FRAMES, idx)
            
            ret_h, frame_h = cap_h.read()
            ret_o, frame_o = cap_o.read()
            
            if not ret_h or not ret_o:
                continue
            
            frames_checked += 1
            
            # Emit per-frame progress within the assigned range
            if progress_range and (sample_num % emit_interval == 0 or sample_num == total_samples - 1):
                p_start, p_end = progress_range
                pct = p_start + int((sample_num + 1) / total_samples * (p_end - p_start))
                self._emit_progress(pct)
            
            # Detect violations differentially using period-appropriate sensitivity
            violation_mask = self._detect_differential_violations_frame(frame_h, frame_o, sensitivity=sensitivity)
            violation_pixels = int(np.sum(violation_mask > 0))
            
            # Lower threshold for detection (was 5, now 2)
            if violation_pixels > 2:
                # Analyze patterns
                pattern_analysis = self._analyze_violation_patterns(violation_mask, frame_o)
                
                # Calculate actual timestamp in original video
                actual_timestamp = (idx / fps) + period_start_time
                
                # Calculate BRNG value from violation pixels
                total_pixels = frame_h.shape[0] * frame_h.shape[1]
                brng_value = (violation_pixels / total_pixels) * 100
                
                violation = FrameViolation(
                    frame_num=int(actual_timestamp * fps),  # Frame number in original video
                    timestamp=actual_timestamp,
                    brng_value=brng_value,
                    violation_score=violation_pixels / total_pixels,
                    violation_pixels=violation_pixels,
                    violation_percentage=brng_value,
                    diagnostics=pattern_analysis.get('diagnostics', []),
                    pattern_analysis=pattern_analysis
                )
                violations.append(violation)
        
        cap_h.release()
        cap_o.release()
        
        # Sort by violation score
        violations.sort(key=lambda x: x.violation_score, reverse=True)
        
        logger.debug(f"  Checked {frames_checked} frames, found {len(violations)} with violations above threshold\n")
        
        # Return violations and analysis stats
        analysis_stats = {
            'qctools_frames_targeted': len(qctools_violations) if qctools_violations else 0,
            'frames_mapped_to_period': len([v for v in (qctools_violations or []) 
                                           if period_start_time <= v.timestamp < period_end_time]),
            'total_samples_analyzed': len(sample_indices),
            'frames_checked': frames_checked,
            'violations_found': len(violations)
        }
        
        return violations, analysis_stats
    
    def _detect_differential_violations_frame(self, highlighted: np.ndarray, 
                                     original: np.ndarray, 
                                     sensitivity: str = 'normal') -> np.ndarray:
        """
        Detect violations by comparing highlighted vs original frame
        
        Args:
            sensitivity: 'strict', 'normal', or 'visualization' 
                    - 'strict': Conservative detection for analysis
                    - 'normal': Balanced detection 
                    - 'visualization': More sensitive for thumbnail display
        """
        
        # Adjust parameters based on sensitivity
        if sensitivity == 'strict':
            magenta_threshold = 12
            min_change = 10
            voting_threshold = 2  # Require 2/4 methods
            min_component_size = 15
            morph_iterations = 2
        elif sensitivity == 'visualization':
            magenta_threshold = 6
            min_change = 5
            voting_threshold = 1  # Require only 1/4 methods
            min_component_size = 3
            morph_iterations = 1
        else:  # normal
            magenta_threshold = 10
            min_change = 8
            voting_threshold = 2  # Require 2/4 methods
            min_component_size = 10
            morph_iterations = 2
        
        # Split channels for analysis
        h_b, h_g, h_r = cv2.split(highlighted)
        o_b, o_g, o_r = cv2.split(original)
        
        # Calculate channel differences
        blue_diff = h_b.astype(np.float32) - o_b.astype(np.float32)
        red_diff = h_r.astype(np.float32) - o_r.astype(np.float32)
        green_diff = h_g.astype(np.float32) - o_g.astype(np.float32)
        
        # Method 1: Strict magenta detection
        strict_magenta = (
            (blue_diff > magenta_threshold) & 
            (red_diff > magenta_threshold) & 
            (green_diff < magenta_threshold/2)
        )
        
        # Method 2: Ratio-based detection
        significant_change = (np.abs(blue_diff) > min_change) | (np.abs(red_diff) > min_change)
        safe_blue = np.where(np.abs(blue_diff) > 0.1, blue_diff, 1)
        safe_red = np.where(np.abs(red_diff) > 0.1, red_diff, 1)
        
        rb_ratio = np.minimum(blue_diff/safe_red, red_diff/safe_blue)
        ratio_tolerance = 0.4
        
        ratio_based = significant_change & (rb_ratio > (1 - ratio_tolerance)) & (rb_ratio < (1 + ratio_tolerance))
        ratio_based = ratio_based & (blue_diff > 0) & (red_diff > 0)
        
        # Method 3: HSV-based magenta detection
        highlighted_hsv = cv2.cvtColor(highlighted, cv2.COLOR_BGR2HSV)
        original_hsv = cv2.cvtColor(original, cv2.COLOR_BGR2HSV)
        
        h_h, h_s, h_v = cv2.split(highlighted_hsv)
        o_h, o_s, o_v = cv2.split(original_hsv)
        
        magenta_hue_low = 140
        magenta_hue_high = 160
        near_zero_threshold = 10
        near_180_threshold = 170
        
        is_magenta_hue = (
            ((h_h >= magenta_hue_low) & (h_h <= magenta_hue_high)) |
            (h_h < near_zero_threshold) |
            (h_h > near_180_threshold)
        )
        
        sat_increase = (h_s.astype(np.float32) - o_s.astype(np.float32)) > 15
        value_stable = (h_v.astype(np.float32) - o_v.astype(np.float32)) > -10
        hsv_magenta = is_magenta_hue & sat_increase & value_stable
        
        # Method 4: Green-channel-drop detection for above-broadcast pixels
        # When bright/super-white pixels (e.g. blown-out sky) are replaced with
        # magenta by signalstats, R and B stay high (already near 255) but G drops
        # dramatically. Methods 1-2 miss this because they require R/B to *increase*.
        green_drop_threshold = magenta_threshold * 2  # Require significant green loss
        green_drop = (
            (green_diff < -green_drop_threshold) &          # Green dropped significantly
            (h_r.astype(np.float32) > 150) &                # Highlighted R is high (magenta)
            (h_b.astype(np.float32) > 150) &                # Highlighted B is high (magenta)
            (h_g.astype(np.float32) < 100) &                # Highlighted G is low (magenta)
            (np.abs(blue_diff) < green_drop_threshold) &    # B didn't change much
            (np.abs(red_diff) < green_drop_threshold)       # R didn't change much
        )
        
        # Combine methods with voting (4 methods, threshold still requires 2)
        method1 = strict_magenta.astype(np.uint8)
        method2 = ratio_based.astype(np.uint8)
        method3 = hsv_magenta.astype(np.uint8)
        method4 = green_drop.astype(np.uint8)
        
        vote_sum = method1 + method2 + method3 + method4
        combined_mask = (vote_sum >= voting_threshold).astype(np.uint8) * 255
        
        # Morphological operations (less aggressive for visualization)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        cleaned_mask = cv2.morphologyEx(combined_mask, cv2.MORPH_OPEN, kernel, iterations=morph_iterations)
        
        if sensitivity != 'strict':
            # Skip closing for more sensitive detection
            cleaned_mask = cv2.morphologyEx(cleaned_mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        
        # Component filtering
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(cleaned_mask, connectivity=8)
        final_mask = np.zeros_like(cleaned_mask)
        
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= min_component_size:
                width = stats[i, cv2.CC_STAT_WIDTH]
                height = stats[i, cv2.CC_STAT_HEIGHT]
                aspect_ratio = min(width, height) / max(width, height) if max(width, height) > 0 else 0
                
                # More lenient aspect ratio check for visualization
                min_aspect = 0.05 if sensitivity == 'visualization' else 0.1
                min_area_override = 25 if sensitivity == 'visualization' else 50
                
                if aspect_ratio > min_aspect or area > min_area_override:
                    final_mask[labels == i] = 255
        
        return final_mask

    def _analyze_violation_patterns(self, violation_mask: np.ndarray,
                               frame: np.ndarray) -> Dict:
        """
        Enhanced pattern analysis adapted from ActiveAreaBrngAnalyzer.
        Provides edge blanking, linear patterns, and luma zone diagnostics.
        """
        h, w = violation_mask.shape

        # Edge analysis with linear pattern detection
        edge_violations = self._detect_edge_violations_enhanced(violation_mask, edge_width=15)

        # Edge-based spatial analysis
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150)

        edge_violations_mask = cv2.bitwise_and(violation_mask, edges)
        edge_violation_ratio = np.sum(edge_violations_mask > 0) / max(1, np.sum(violation_mask > 0))

        spatial_patterns = {
            'edge_concentrated': bool(edge_violation_ratio > 0.6),
            'has_boundary_artifacts': edge_violations['has_edge_violations'],
            'boundary_edges': edge_violations['edges_affected'],
            'boundary_severity': edge_violations['severity'],
            'linear_patterns_detected': any(v > 30 for v in edge_violations.get('linear_patterns', {}).values())
        }

        # Luma zone distribution (unless strong edge issues dominate)
        luma_distribution = {}
        if not (edge_violations['has_edge_violations'] and edge_violations['severity'] in ['medium', 'high']):
            luma_distribution = self._analyze_luma_zone_violations(violation_mask, gray)

        # Diagnostics
        diagnostics = self._generate_enhanced_diagnostic(spatial_patterns,
                                                        edge_violations,
                                                        luma_distribution)

        # Violation percentage
        violation_pixels = int(np.sum(violation_mask > 0))
        violation_percentage = (violation_pixels / (w * h)) * 100 if w * h > 0 else 0

        return {
            'spatial_patterns': spatial_patterns,
            'edge_violations': edge_violations,
            'luma_distribution': luma_distribution,
            'edge_violation_ratio': float(edge_violation_ratio),
            'diagnostics': diagnostics,
            'boundary_artifacts': edge_violations,
            'linear_pattern_info': edge_violations.get('linear_patterns', {}),
            'expansion_recommendations': edge_violations.get('expansion_recommendations', {}),
            'violation_percentage': violation_percentage
        }
    
    def _select_diverse_violations_for_thumbnails(self, violations: List[FrameViolation], 
                                                max_thumbnails: int = 5, 
                                                min_time_separation: float = 10.0,
                                                analysis_periods: List[Tuple[float, int]] = None) -> List[FrameViolation]:
        """
        Select violations for thumbnails ensuring temporal diversity and diagnostic value.
        
        When upstream context is available, prioritizes violations from periods that
        signalstats diagnosed as 'content_violations' — these are the actionable findings
        a conservator needs to see. Border-dominated violations are deprioritized since
        they're already documented in the border detection section of the report.
        
        Args:
            violations: List of violations sorted by violation_score (highest first)
            max_thumbnails: Maximum number of thumbnails to create
            min_time_separation: Minimum time separation between selected frames (seconds)
            analysis_periods: List of (start_time, duration) tuples for period lookups
        
        Returns:
            List of violations for thumbnail creation
        """
        if not violations:
            return []
        
        # If we have upstream context, reorder violations to prioritize content violations
        priority_ordered = violations
        if (hasattr(self, 'upstream_context') and self.upstream_context 
            and self.upstream_context.period_diagnoses and analysis_periods):
            
            # Build a lookup: which period does each violation belong to?
            def _get_period_diagnosis(timestamp):
                for idx, (start, dur) in enumerate(analysis_periods):
                    if start <= timestamp < start + dur:
                        return self.upstream_context.period_diagnoses.get(idx, '')
                return ''
            
            content_violations = []
            border_violations = []
            other_violations = []
            
            for v in violations:
                diag = _get_period_diagnosis(v.timestamp)
                if diag == 'content_violations':
                    content_violations.append(v)
                elif diag == 'border_violations':
                    border_violations.append(v)
                else:
                    other_violations.append(v)
            
            # Content first, then unclassified, then border
            priority_ordered = content_violations + other_violations + border_violations
            
            if content_violations:
                logger.debug(f"  Thumbnail priority: {len(content_violations)} content, "
                           f"{len(other_violations)} other, {len(border_violations)} border violations")
        
        selected_violations = []
        
        # Always include the highest-priority violation
        selected_violations.append(priority_ordered[0])
        logger.info(f"\n  Selected thumbnail 1: Frame {priority_ordered[0].frame_num} at {priority_ordered[0].timestamp:.1f}s (score: {priority_ordered[0].violation_score:.4f})")
        
        # Select additional violations with time separation constraint
        for violation in priority_ordered[1:]:
            if len(selected_violations) >= max_thumbnails:
                break
                
            # Check if this violation is far enough from all previously selected ones
            is_far_enough = True
            for selected in selected_violations:
                time_diff = abs(violation.timestamp - selected.timestamp)
                if time_diff < min_time_separation:
                    is_far_enough = False
                    break
            
            if is_far_enough:
                selected_violations.append(violation)
                logger.info(f"  Selected thumbnail {len(selected_violations)}: Frame {violation.frame_num} at {violation.timestamp:.1f}s (score: {violation.violation_score:.4f})\n")
        
        # If we couldn't find enough diverse violations, fill in with the best remaining ones
        # (but log this situation)
        if len(selected_violations) < max_thumbnails and len(priority_ordered) > len(selected_violations):
            remaining_needed = max_thumbnails - len(selected_violations)
            logger.info(f"  Only found {len(selected_violations)} violations with {min_time_separation}s separation")
            
            # Add the best remaining violations regardless of time separation
            for violation in priority_ordered:
                if violation not in selected_violations:
                    selected_violations.append(violation)
                    logger.info(f"  Added thumbnail {len(selected_violations)} (relaxed spacing): Frame {violation.frame_num} at {violation.timestamp:.1f}s\n")
                    remaining_needed -= 1
                    if remaining_needed <= 0:
                        break
        
        if len(selected_violations) > 1:
            logger.debug(f"  Final selection: {len(selected_violations)} thumbnails spanning {selected_violations[-1].timestamp - selected_violations[0].timestamp:.1f} seconds")
        
        return selected_violations
    
    def _generate_enhanced_diagnostic(self, spatial_patterns: Dict,
                                      edge_violations: Dict,
                                      luma_distribution: Dict) -> List[str]:
        """Human-readable diagnostics with linear pattern info."""
        diagnostics = []

        if edge_violations.get('has_edge_violations'):
            edges_str = ', '.join(edge_violations.get('edges_affected', []))
            linear_patterns = edge_violations.get('linear_patterns', {})

            high_linear = [e for e, v in linear_patterns.items() if v > 50]
            if high_linear:
                diagnostics.append(f"Linear blanking patterns on: {', '.join(high_linear)}")
            elif edge_violations.get('continuous_edges'):
                diagnostics.append(f"Continuous edge artifacts ({edges_str})")
            else:
                diagnostics.append(f"Edge artifacts ({edges_str})")

            if edge_violations.get('expansion_recommendations'):
                diagnostics.append("Border adjustment recommended")

            if edge_violations.get('severity') == 'high':
                diagnostics.append("Border detection likely missed blanking")
            elif edge_violations.get('severity') == 'medium':
                diagnostics.append("Moderate blanking detected")

        if not edge_violations.get('has_edge_violations') or edge_violations.get('severity') in ('low', 'none'):
            if luma_distribution:
                primary_zone = luma_distribution.get('primary_zone')
                if primary_zone == 'highlights' and luma_distribution.get('highlight_ratio', 0) > 0.7:
                    diagnostics.append("Highlight clipping")
                elif primary_zone == 'subblack' and luma_distribution.get('subblack_ratio', 0) > 0.7:
                    diagnostics.append("Sub-black detected")

        return diagnostics if diagnostics else ["General broadcast range violations"]

    def _detect_edge_violations_enhanced(self, violation_mask, edge_width=15):
        """
        Enhanced edge violation detection that identifies blanking patterns
        by comparing edge violation density against interior density.
        
        Without this comparison, frames with violations spread throughout
        the frame (content-level BRNG issues) get misclassified as having
        edge artifacts, because the edge strips naturally contain violations
        too. The interior baseline distinguishes true edge-specific patterns
        (blanking, border artifacts) from general broadcast range violations.
        """
        h, w = violation_mask.shape
        edge_info = {
            'has_edge_violations': False,
            'edges_affected': [],
            'edge_percentages': {},
            'continuous_edges': [],
            'linear_patterns': {},
            'blanking_depth': {},
            'severity': 'none',
            'expansion_recommendations': {},
            'interior_density': 0.0
        }
        
        # Determine per-edge widths. Widen bottom edge if head switching was detected
        # upstream — head switching noise produces expected BRNG violations that should
        # be classified as edge artifacts, not content issues.
        bottom_edge_width = edge_width
        if (hasattr(self, 'upstream_context') and self.upstream_context 
            and self.upstream_context.head_switching):
            hs = self.upstream_context.head_switching
            hs_height = hs.get('artifact_height', 0)
            hs_pct = hs.get('affected_percentage', 0)
            if hs_pct > 30 and hs_height > edge_width:
                bottom_edge_width = min(hs_height + 5, 40)  # Cap at 40px
                logger.debug(f"    Bottom edge width expanded to {bottom_edge_width}px "
                           f"(head switching: {hs_height}px in {hs_pct:.0f}% of frames)")
        
        # Calculate interior violation density as baseline for comparison.
        # This is the region inset by edge_width on all sides (using bottom_edge_width for bottom).
        interior = violation_mask[edge_width:-bottom_edge_width, edge_width:-edge_width] if bottom_edge_width < h else violation_mask[edge_width:-edge_width, edge_width:-edge_width]
        interior_density = (np.sum(interior > 0) / interior.size * 100) if interior.size > 0 else 0
        edge_info['interior_density'] = interior_density
        
        # Define edges with increased scan depth (bottom uses head-switching-aware width)
        edges_to_check = [
            ('left', violation_mask[:, :edge_width], 'vertical'),
            ('right', violation_mask[:, -edge_width:], 'vertical'),
            ('top', violation_mask[:edge_width, :], 'horizontal'),
            ('bottom', violation_mask[-bottom_edge_width:, :], 'horizontal')
        ]
        
        for edge_name, edge_region, orientation in edges_to_check:
            if edge_region.size == 0:
                continue
            
            # Basic violation percentage in this edge strip
            violation_pixels = np.sum(edge_region > 0)
            total_pixels = edge_region.size
            violation_percentage = (violation_pixels / total_pixels) * 100 if total_pixels > 0 else 0
            
            edge_info['edge_percentages'][edge_name] = violation_percentage
            
            # Detect linear patterns (even if not perfectly continuous)
            linear_score = 0
            if orientation == 'vertical':
                for row in range(edge_region.shape[0]):
                    row_violations = edge_region[row, :]
                    if np.any(row_violations > 0):
                        violation_positions = np.where(row_violations > 0)[0]
                        if len(violation_positions) >= 4:  
                            if edge_name == 'left' and np.max(violation_positions) <= 2:
                                linear_score += 1
                            elif edge_name == 'right' and np.min(violation_positions) >= edge_width - 3:
                                linear_score += 1
                
                linear_percentage = (linear_score / edge_region.shape[0]) * 100
                
            else:  # horizontal orientation
                # For top/bottom edges, check for vertical lines of violations
                for col in range(edge_region.shape[1]):
                    col_violations = edge_region[:, col]
                    if np.any(col_violations > 0):
                        violation_positions = np.where(col_violations > 0)[0]
                        if len(violation_positions) >= 2:
                            if edge_name == 'top' and np.max(violation_positions) <= 3:
                                linear_score += 1
                            elif edge_name == 'bottom' and np.min(violation_positions) >= edge_width - 4:
                                linear_score += 1
                
                linear_percentage = (linear_score / edge_region.shape[1]) * 100
            
            edge_info['linear_patterns'][edge_name] = linear_percentage
            
            # Determine how far blanking extends from the edge
            if violation_percentage > 15:
                max_depth = 0
                if orientation == 'vertical':
                    for row in range(edge_region.shape[0]):
                        row_violations = np.where(edge_region[row, :] > 0)[0]
                        if len(row_violations) > 0:
                            if edge_name == 'left':
                                depth = np.max(row_violations)
                            else:  # right
                                depth = edge_width - np.min(row_violations)
                            max_depth = max(max_depth, depth)
                else:  # horizontal
                    for col in range(edge_region.shape[1]):
                        col_violations = np.where(edge_region[:, col] > 0)[0]
                        if len(col_violations) > 0:
                            if edge_name == 'top':
                                depth = np.max(col_violations)
                            else:  # bottom
                                depth = edge_width - np.min(col_violations)
                            max_depth = max(max_depth, depth)
                
                edge_info['blanking_depth'][edge_name] = max_depth
            
            # Compare edge density to interior density to determine if violations
            # are edge-specific or just part of a frame-wide distribution.
            if interior_density > 0:
                density_ratio = violation_percentage / interior_density
                excess_density = violation_percentage - interior_density
            else:
                # No interior violations — any edge violations are edge-specific
                density_ratio = float('inf') if violation_percentage > 0 else 0
                excess_density = violation_percentage
            
            # Edge confinement check: measure violation density in the adjacent
            # band just inside the edge strip (2x edge_width deep).  True blanking
            # artifacts are confined to a narrow strip and drop off sharply.
            # Content violations (e.g. blown-out sky touching the top) persist at
            # similar density beyond the edge strip.
            adjacent_band_depth = edge_width * 2
            if edge_name == 'top':
                adjacent = violation_mask[edge_width:edge_width + adjacent_band_depth, :]
            elif edge_name == 'bottom':
                adjacent = violation_mask[-(edge_width + adjacent_band_depth):-edge_width, :]
            elif edge_name == 'left':
                adjacent = violation_mask[:, edge_width:edge_width + adjacent_band_depth]
            else:  # right
                adjacent = violation_mask[:, -(edge_width + adjacent_band_depth):-edge_width]
            
            adjacent_density = (np.sum(adjacent > 0) / adjacent.size * 100) if adjacent.size > 0 else 0
            
            # If the adjacent band has >= 50% the density of the edge strip,
            # violations are spreading through the frame — not confined to the edge.
            violations_confined_to_edge = True
            if violation_percentage > 0 and adjacent_density / violation_percentage >= 0.5:
                violations_confined_to_edge = False
            
            # Flag as edge violation only if:
            #   - Strong linear patterns (true blanking), OR
            #   - Edge density is meaningfully higher than interior AND
            #     violations are actually confined to the edge strip
            is_edge_specific = (
                linear_percentage > 50 or
                (violation_percentage > 15 and (density_ratio >= 2.0 or excess_density >= 15)
                 and violations_confined_to_edge)
            )
            
            if is_edge_specific:
                edge_info['edges_affected'].append(edge_name)
                edge_info['has_edge_violations'] = True
                
                if linear_percentage > 70:
                    edge_info['continuous_edges'].append(edge_name)
                
                if edge_name in edge_info['blanking_depth']:
                    recommended_expansion = edge_info['blanking_depth'][edge_name] + 2
                    edge_info['expansion_recommendations'][edge_name] = recommended_expansion
        
        # Refine severity assessment
        if len(edge_info['continuous_edges']) >= 3:
            edge_info['severity'] = 'high'
        elif len(edge_info['continuous_edges']) >= 2:
            edge_info['severity'] = 'medium'
        elif len(edge_info['edges_affected']) >= 3:
            edge_info['severity'] = 'low'
        elif len(edge_info['edges_affected']) >= 2 and max(edge_info['linear_patterns'].values(), default=0) > 60:
            edge_info['severity'] = 'low'
        
        return edge_info

    def _analyze_luma_zone_violations(self, mask, gray_frame):
        """
        Analyze where violations occur in terms of brightness zones.
        """
        # Define luma zones
        subblack = (gray_frame < 64)
        midtones = (gray_frame >= 64) & (gray_frame < 192)
        highlights = (gray_frame >= 192)
        
        violations = mask > 0
        
        subblack_violations = np.sum(violations & subblack)
        midtone_violations = np.sum(violations & midtones)
        highlight_violations = np.sum(violations & highlights)
        
        total_violations = max(1, subblack_violations + midtone_violations + highlight_violations)
        
        return {
            'subblack_ratio': float(subblack_violations / total_violations),
            'midtone_ratio': float(midtone_violations / total_violations),
            'highlight_ratio': float(highlight_violations / total_violations),
            'primary_zone': self._get_primary_zone(subblack_violations, midtone_violations, highlight_violations)
        }

    def _get_primary_zone(self, subblack, midtone, highlight):
        """Determine primary brightness zone for violations"""
        zones = {'subblack': subblack, 'midtones': midtone, 'highlights': highlight}
        return max(zones, key=zones.get) if zones else 'none'


    def _analyze_aggregate_patterns(self, violations: List[FrameViolation]) -> Dict:
        """Analyze aggregate patterns across all violations with linear pattern detection"""
        if not violations:
            return {
                'requires_border_adjustment': False,
                'edge_violation_percentage': 0,
                'continuous_edge_percentage': 0,
                'linear_pattern_percentage': 0
            }
        
        # Count different types of violations
        edge_violations = 0
        continuous_edges = 0
        linear_patterns = 0
        all_affected_edges = []
        all_linear_scores = defaultdict(list)
        
        for v in violations:
            if v.pattern_analysis:
                edge_info = v.pattern_analysis.get('edge_violations', {})
                if edge_info.get('has_edge_violations'):
                    edge_violations += 1
                    all_affected_edges.extend(edge_info.get('edges_affected', []))
                if edge_info.get('continuous_edges'):
                    continuous_edges += 1
                
                # Check for linear patterns
                if v.pattern_analysis.get('spatial_patterns', {}).get('linear_patterns_detected'):
                    linear_patterns += 1
                
                # Collect linear pattern scores
                for edge, score in edge_info.get('linear_patterns', {}).items():
                    all_linear_scores[edge].append(score)
        
        edge_violation_pct = (edge_violations / len(violations)) * 100
        continuous_edge_pct = (continuous_edges / len(violations)) * 100
        linear_pattern_pct = (linear_patterns / len(violations)) * 100
        
        # Calculate average linear pattern scores
        avg_linear_patterns = {}
        for edge, scores in all_linear_scores.items():
            if scores:
                avg_linear_patterns[edge] = np.mean(scores)
        
        # Calculate expansion recommendations
        expansion_recs = {}
        unique_edges = list(set(all_affected_edges))
        for edge in unique_edges:
            edge_count = all_affected_edges.count(edge)
            if edge_count > len(violations) * 0.2:
                expansion_recs[edge] = 10 if edge_count > len(violations) * 0.5 else 5
        
        # Determine if border adjustment is needed with more nuanced logic
        # Log the values feeding into the decision for diagnostic visibility
        max_linear_score = max(avg_linear_patterns.values(), default=0)
        logger.debug(f"  Border adjustment decision inputs:")
        logger.debug(f"    edge_violation_pct: {edge_violation_pct:.1f}%")
        logger.debug(f"    continuous_edge_pct: {continuous_edge_pct:.1f}%")
        logger.debug(f"    linear_pattern_pct: {linear_pattern_pct:.1f}%")
        logger.debug(f"    max avg linear score: {max_linear_score:.1f}")
        logger.debug(f"    unique edges affected: {unique_edges}")
        
        requires_adjustment = (
            linear_pattern_pct > 20 or  # Strong linear patterns
            continuous_edge_pct > 15 or  # Many continuous edges
            (edge_violation_pct > 30 and continuous_edge_pct > 0) or  # Edge violations with SOME linear evidence
            (edge_violation_pct > 60 and continuous_edge_pct == 0) or  # Overwhelming edge concentration even without lines
            (continuous_edge_pct > 10 and len(unique_edges) >= 2) or  # Multiple edges affected
            any(score > 40 for score in avg_linear_patterns.values())  # High linear scores
        )
        
        if requires_adjustment:
            # Log which condition(s) triggered
            triggers = []
            if linear_pattern_pct > 20:
                triggers.append(f"linear_pattern_pct ({linear_pattern_pct:.1f}%) > 20%")
            if continuous_edge_pct > 15:
                triggers.append(f"continuous_edge_pct ({continuous_edge_pct:.1f}%) > 15%")
            if edge_violation_pct > 30 and continuous_edge_pct > 0:
                triggers.append(f"edge_violation_pct ({edge_violation_pct:.1f}%) > 30% with continuous edges")
            if edge_violation_pct > 60 and continuous_edge_pct == 0:
                triggers.append(f"edge_violation_pct ({edge_violation_pct:.1f}%) > 60% (no continuous edges)")
            if continuous_edge_pct > 10 and len(unique_edges) >= 2:
                triggers.append(f"continuous_edge_pct ({continuous_edge_pct:.1f}%) > 10% on {len(unique_edges)} edges")
            if any(score > 40 for score in avg_linear_patterns.values()):
                high_scores = {e: s for e, s in avg_linear_patterns.items() if s > 40}
                triggers.append(f"high avg linear scores: {high_scores}")
            logger.debug(f"    → Requires adjustment, triggered by: {'; '.join(triggers)}")
        else:
            logger.debug(f"    → No border adjustment needed")
        
        return {
            'requires_border_adjustment': requires_adjustment,
            'edge_violation_percentage': edge_violation_pct,
            'continuous_edge_percentage': continuous_edge_pct,
            'linear_pattern_percentage': linear_pattern_pct,
            'linear_pattern_scores': avg_linear_patterns,
            'boundary_edges_detected': unique_edges,
            'expansion_recommendations': expansion_recs
        }
    
    def _generate_actionable_report(self, violations: List[FrameViolation],
                           aggregate_patterns: Dict) -> Dict:
        """Generate detailed report based on violation patterns"""
        if not violations:
            return {
                'overall_assessment': 'No BRNG violations detected',
                'summary_statistics': {
                    'total_violations': 0,
                    'average_violation_percentage': 0,
                    'max_violation_percentage': 0,
                    'edge_violation_percentage': 0,
                    'linear_pattern_percentage': 0
                }
            }
        
        # Calculate statistics
        avg_violation_pct = np.mean([v.violation_percentage for v in violations])
        max_violation_pct = max([v.violation_percentage for v in violations])
        
        return {
            'overall_assessment': self._get_overall_assessment(violations, aggregate_patterns),
            'summary_statistics': {
                'total_violations': len(violations),
                'average_violation_percentage': avg_violation_pct,
                'max_violation_percentage': max_violation_pct,
                'edge_violation_percentage': aggregate_patterns.get('edge_violation_percentage', 0),
                'linear_pattern_percentage': aggregate_patterns.get('linear_pattern_percentage', 0)
            }
        }

    def _get_overall_assessment(self, violations: List[FrameViolation],
                        aggregate_patterns: Dict) -> str:
        """Generate descriptive assessment text based on analysis findings"""
        if not violations:
            return "No BRNG violations detected in analyzed frames"
        
        # Build description based on what was found
        edge_pct = aggregate_patterns.get('edge_violation_percentage', 0)
        linear_pct = aggregate_patterns.get('linear_pattern_percentage', 0)
        avg_violation = np.mean([v.violation_percentage for v in violations])
        
        parts = []

        # Describe violation level. The percentage is a pixel percentage
        # within flagged frames — not a share of all analyzed frames — so
        # spell that out rather than reporting a bare "Average BRNG"
        precision = 2 if avg_violation >= 0.1 else 3
        sentence = (
            f"Among the analyzed frames that had any out-of-range pixels, "
            f"on average {avg_violation:.{precision}f}% of each frame's pixels "
            f"were outside broadcast-safe range"
        )
        if avg_violation >= 10.0:
            parts.append(sentence)
        elif avg_violation >= 0.1:
            parts.append(f"{sentence} (low-level)")
        else:
            parts.append(f"{sentence} (minimal)")
        
        # Describe spatial distribution
        if edge_pct > 70:
            parts.append(f"{edge_pct:.0f}% of violations at frame edges")
        elif edge_pct > 40:
            parts.append(f"{edge_pct:.0f}% edge violations, {100-edge_pct:.0f}% content area")
        elif edge_pct > 0:
            parts.append(f"Violations primarily in content area ({100-edge_pct:.0f}%)")
        
        # Note linear patterns if present
        if linear_pct > 20:
            parts.append(f"linear blanking patterns detected ({linear_pct:.0f}%)")
        
        return "; ".join(parts)
    
    def _create_diagnostic_thumbnails(self, violations: List[FrameViolation],
                             temp_video_paths: List[Dict],
                             output_dir: Path) -> List[str]:
        """Create 4-quadrant diagnostic thumbnails with enhanced violation visibility"""
        thumbnails = []
        
        for i, violation in enumerate(violations[:5]):
            # Find which video contains this violation
            video_info = None
            for vinfo in temp_video_paths:
                if vinfo['start_time'] <= violation.timestamp < vinfo['start_time'] + vinfo['duration']:
                    video_info = vinfo
                    break
            
            if not video_info:
                logger.warning(f"Could not find video segment for violation at {violation.timestamp}s")
                continue
            
            # Open the videos
            cap_h = cv2.VideoCapture(str(video_info['highlighted']))
            cap_o = cv2.VideoCapture(str(video_info['original']))
            
            if not cap_h.isOpened() or not cap_o.isOpened():
                logger.warning(f"Failed to open videos for thumbnail")
                cap_h.release()
                cap_o.release()
                continue
            
            fps = cap_h.get(cv2.CAP_PROP_FPS)
            
            # Calculate frame position relative to this video segment
            relative_time = violation.timestamp - video_info['start_time']
            frame_idx = int(relative_time * fps)
            
            cap_h.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            cap_o.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            
            ret_h, frame_h = cap_h.read()
            ret_o, frame_o = cap_o.read()
            
            if not ret_h or not ret_o:
                cap_h.release()
                cap_o.release()
                continue
            
            # Create 4-quadrant visualization
            h, w = frame_h.shape[:2]
            padding = 10
            viz_height = h * 2 + padding
            viz_width = w * 2 + padding
            viz = np.full((viz_height, viz_width, 3), (64, 64, 64), dtype=np.uint8)
            
            # Top-left: Original
            viz[0:h, 0:w] = frame_o
            
            # Top-right: Highlighted
            viz[0:h, w+padding:w*2+padding] = frame_h
            
            # Bottom-left: Enhanced violations visualization
            violations_only = self._create_enhanced_violations_display(frame_h, frame_o)
            viz[h+padding:h*2+padding, 0:w] = violations_only
            
            # Bottom-right: Analysis info
            info_panel = np.zeros((h, w, 3), dtype=np.uint8)
            self._add_info_text(info_panel, violation)
            viz[h+padding:h*2+padding, w+padding:w*2+padding] = info_panel
            
            # Add labels with better visibility
            self._add_quadrant_labels(viz, w, h, padding)

            # Downscale oversized composites so embedded HTML reports stay
            # small; SD-source composites (~1450 px) pass through unchanged.
            MAX_THUMB_WIDTH = 1600
            if viz.shape[1] > MAX_THUMB_WIDTH:
                scale = MAX_THUMB_WIDTH / viz.shape[1]
                viz = cv2.resize(
                    viz,
                    (MAX_THUMB_WIDTH, int(viz.shape[0] * scale)),
                    interpolation=cv2.INTER_AREA,
                )

            # Save thumbnail
            thumb_filename = f"{self.video_path.stem}_brng_{i:03d}_{violation.timestamp:.1f}s.jpg"
            thumb_path = output_dir / thumb_filename
            cv2.imwrite(str(thumb_path), viz, [cv2.IMWRITE_JPEG_QUALITY, 82])
            thumbnails.append(str(thumb_path))
            
            cap_h.release()
            cap_o.release()
        
        return thumbnails

    def _create_enhanced_violations_display(self, frame_h: np.ndarray, frame_o: np.ndarray) -> np.ndarray:
        """Create violations display by extracting magenta pixels from the highlighted frame"""
        
        # Extract magenta pixels directly from the highlighted frame
        # This ensures we show exactly what ffmpeg's signalstats detected
        magenta_mask = self._extract_magenta_pixels(frame_h)
        
        # Create the visualization with yellow highlighting
        violations_only = np.zeros_like(frame_o)
        violations_only[magenta_mask > 0] = [0, 255, 255]  # Yellow in BGR
        
        # Optionally add slight transparency overlay for better visibility
        if np.any(magenta_mask > 0):
            violation_overlay = np.zeros_like(frame_o)
            violation_overlay[magenta_mask > 0] = frame_o[magenta_mask > 0] * 0.3
            violations_only = cv2.addWeighted(violations_only, 0.8, violation_overlay, 0.2, 0)
        
        return violations_only
    
    def _extract_magenta_pixels(self, frame: np.ndarray) -> np.ndarray:
        """Extract magenta-colored pixels from a frame (as added by ffmpeg signalstats filter)"""
        
        # Split channels (BGR format)
        b, g, r = cv2.split(frame)
        
        # Magenta has high blue and red, low green
        # Use multiple detection methods for robustness
        
        # Method 1: Direct BGR thresholding
        # Magenta should have B>threshold, R>threshold, G<threshold
        magenta_threshold = 200  # Fairly bright magenta
        green_threshold = 100    # Green should be lower
        
        bgr_magenta = (
            (b > magenta_threshold) & 
            (r > magenta_threshold) & 
            (g < green_threshold)
        )
        
        # Method 2: Ratio-based detection (R and B similar, G much lower)
        # Avoid division by zero
        safe_g = np.maximum(g, 1).astype(np.float32)
        rb_avg = ((r.astype(np.float32) + b.astype(np.float32)) / 2)
        
        ratio_magenta = (rb_avg / safe_g > 1.8) & (b > 150) & (r > 150)
        
        # Method 3: HSV-based detection
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        
        # Magenta hue is around 140-160 in OpenCV's 0-180 range
        # Also check near 0/180 (red-magenta boundary)
        hsv_magenta = (
            (((h >= 140) & (h <= 160)) | (h < 5) | (h > 175)) &
            (s > 100) &  # Saturated
            (v > 150)    # Bright
        )
        
        # Combine methods - use any 2 out of 3
        method1 = bgr_magenta.astype(np.uint8)
        method2 = ratio_magenta.astype(np.uint8)
        method3 = hsv_magenta.astype(np.uint8)
        
        vote_sum = method1 + method2 + method3
        magenta_mask = (vote_sum >= 2).astype(np.uint8) * 255
        
        # Clean up noise
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        magenta_mask = cv2.morphologyEx(magenta_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        magenta_mask = cv2.morphologyEx(magenta_mask, cv2.MORPH_CLOSE, kernel, iterations=1)
        
        return magenta_mask

    def _create_simple_difference_mask(self, frame_h: np.ndarray, frame_o: np.ndarray) -> np.ndarray:
        """Create a simple difference mask as backup for visualization"""
        
        # Convert to grayscale for difference calculation
        gray_h = cv2.cvtColor(frame_h, cv2.COLOR_BGR2GRAY)
        gray_o = cv2.cvtColor(frame_o, cv2.COLOR_BGR2GRAY)
        
        # Calculate absolute difference
        diff = cv2.absdiff(gray_h, gray_o)
        
        # Threshold for any noticeable change
        _, diff_mask = cv2.threshold(diff, 5, 255, cv2.THRESH_BINARY)
        
        # Remove very small differences (noise)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        diff_mask = cv2.morphologyEx(diff_mask, cv2.MORPH_OPEN, kernel, iterations=1)
        
        # Additional filtering for magenta-like colors
        h_b, h_g, h_r = cv2.split(frame_h)
        o_b, o_g, o_r = cv2.split(frame_o)
        
        # Look for any increase in red or blue channels
        red_increase = (h_r.astype(np.float32) - o_r.astype(np.float32)) > 3
        blue_increase = (h_b.astype(np.float32) - o_b.astype(np.float32)) > 3
        
        # Combine with difference mask
        color_change = (red_increase | blue_increase).astype(np.uint8) * 255
        final_mask = cv2.bitwise_and(diff_mask, color_change)
        
        return final_mask

    def _add_quadrant_labels(self, viz: np.ndarray, w: int, h: int, padding: int):
        """Add clear labels to each quadrant"""
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.7
        text_color = (255, 255, 255)
        bg_color = (0, 0, 0)
        thickness = 2
        
        labels = [
            ("Original", (10, 25)),
            ("BRNG Highlighted", (w + padding + 10, 25)),
            ("Violations Only", (10, h + padding + 25)),
            ("Analysis Data", (w + padding + 10, h + padding + 25))
        ]
        
        for text, (x, y) in labels:
            # Get text size for background rectangle
            (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)
            
            # Draw background rectangle
            cv2.rectangle(viz, (x - 5, y - text_h - 10), 
                        (x + text_w + 5, y + baseline), bg_color, -1)
            
            # Draw text
            cv2.putText(viz, text, (x, y - 5), font, font_scale, text_color, thickness)

    def _add_info_text(self, panel: np.ndarray, violation: FrameViolation):
        """Add analysis info text to panel"""
        h, w = panel.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        
        lines = [
            f"Frame: {violation.frame_num}",
            f"Time: {violation.timestamp:.2f}s",
            f"BRNG: {violation.brng_value:.4f}%",
            f"Violations: {violation.violation_percentage:.4f}%",
            f"Pixels: {violation.violation_pixels}"
        ]
        
        if violation.diagnostics:
            lines.append(f"Issues: {', '.join(violation.diagnostics[:2])}")
        
        y = 30
        for line in lines:
            cv2.putText(panel, line, (10, y), font, 0.5, (255, 255, 255), 1)
            y += 25


class IntegratedSignalstatsAnalyzer:
    """Signalstats analyzer with QCTools integration"""
    
    def __init__(self, video_path: str, qctools_report: str = None,
                 check_cancelled_fn=None, signals=None):
        self.video_path = str(video_path)
        self.qctools_report = qctools_report if qctools_report else self._find_qctools_report(log_result=False)
        self.check_cancelled = check_cancelled_fn or (lambda: False)
        self.signals = signals
        self._init_video_properties()
        self._brng_cache = None
        self._brng_cache_active_area = None

    def _emit_progress(self, percent: int):
        """Emit signalstats analysis progress as a percentage (0-100)."""
        if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
            safe_percent = min(100, max(0, int(percent)))
            self.signals.frame_analysis_progress.emit(safe_percent)

    def _init_video_properties(self):
        """Initialize video properties"""
        cap = cv2.VideoCapture(self.video_path)
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = cap.get(cv2.CAP_PROP_FPS)
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.duration = self.total_frames / self.fps if self.fps > 0 else 0
        cap.release()
    
    def _find_qctools_report(self, log_result: bool = True) -> Optional[str]:
        """Find existing QCTools report"""
        video_path = Path(self.video_path)
        video_id = video_path.stem
        video_filename = video_path.name  # Full filename with extension
        
        search_patterns = [
            # Try with full filename first (e.g., JPC_AV_01709.mkv.qctools.xml.gz)
            video_path.parent / f"{video_filename}.qctools.xml.gz",
            video_path.parent / f"{video_id}_qc_metadata" / f"{video_filename}.qctools.xml.gz",
            video_path.parent / f"{video_id}_vrecord_metadata" / f"{video_filename}.qctools.xml.gz",
            # Then try without extension (e.g., JPC_AV_01709.qctools.xml.gz)
            video_path.parent / f"{video_id}.qctools.xml.gz",
            video_path.parent / f"{video_id}_qc_metadata" / f"{video_id}.qctools.xml.gz",
            video_path.parent / f"{video_id}_vrecord_metadata" / f"{video_id}.qctools.xml.gz",
        ]
        
        for pattern in search_patterns:
            if pattern.exists():
                if log_result:
                    logger.debug(f"Found QCTools report for Signalstats Analyzer: {pattern}\n")
                return str(pattern)
        
        return None
    
    def analyze_with_signalstats(self,
                        border_data: BorderDetectionResult = None,
                        content_start_time: float = 0,
                        color_bars_end_time: float = None,
                        analysis_duration: int = 60,
                        num_periods: int = 3,
                        qctools_periods: List[Tuple[float, int]] = None,
                        black_segments: List[Tuple[float, float]] = None) -> SignalstatsResult:
        """
        Analyze using signalstats, comparing full frame (QCTools) vs active area (FFprobe).
        
        Args:
            qctools_periods: Pre-computed periods from QCTools violation distribution analysis
            black_segments: Known all-black segments to avoid when selecting periods
        """
        # Determine analysis periods - now with QCTools priority
        analysis_periods = self._find_analysis_periods(
            content_start_time, 
            color_bars_end_time,
            analysis_duration,
            num_periods,
            border_data.quality_frame_hints if border_data else None,
            qctools_periods=qctools_periods,
            black_segments=black_segments
        )
        
        # Log analysis configuration
        logger.info(f"  Running {len(analysis_periods)} analysis periods:")
        for i, (start_time, duration) in enumerate(analysis_periods):
            end_time = start_time + duration
            start_tc = self._seconds_to_timecode(start_time)
            end_tc = self._seconds_to_timecode(end_time)
            logger.debug(f"    Period {i+1}: {start_tc} - {end_tc} ({duration}s)")
        
        # Log active area vs full frame comparison
        active_area = border_data.active_area if border_data else None
        if active_area:
            x, y, w, h = active_area
            full_w, full_h = self.width, self.height
            crop_pct = (w * h) / (full_w * full_h) * 100
            logger.debug(f"  Comparison mode: Full frame ({full_w}x{full_h}) vs Active area ({w}x{h} at {x},{y})")
            logger.info(f"  Active area is {crop_pct:.1f}% of full frame\n")
        else:
            logger.debug(f"  Comparison mode: Full frame analysis only (no border detection)")
        
        # Analyze periods
        all_results = []
        used_qctools = False
        comparison_results = []
        total_periods = len(analysis_periods)
        
        self._emit_progress(0)
        
        for i, (start_time, duration) in enumerate(analysis_periods):
            if self.check_cancelled():
                break
            logger.debug(f"  Analyzing period {i+1} ({self._seconds_to_timecode(start_time)} - {self._seconds_to_timecode(start_time + duration)}):")
            
            # Calculate progress range for this period (each period gets equal share of 0-90%)
            period_base = int(i / total_periods * 90)
            period_mid = int((i + 0.5) / total_periods * 90)
            period_end = int((i + 1) / total_periods * 90)
            
            period_comparison = {
                'period': i+1,
                'time_range': (start_time, start_time + duration)
            }
            
            # Get QCTools data (full frame)
            qctools_result = None
            if self.qctools_report:
                self._emit_progress(period_base)
                qctools_result = self._parse_qctools_brng_period(start_time, start_time + duration, i+1)
                if qctools_result:
                    used_qctools = True
                    period_comparison['qctools_full_frame'] = {
                        'violations_pct': (qctools_result['frames_with_violations'] / 
                                        qctools_result['frames_analyzed'] * 100) if qctools_result['frames_analyzed'] > 0 else 0,
                        'max_brng': max(qctools_result['brng_values']) * 100 if qctools_result['brng_values'] else 0,
                        'frames_analyzed': qctools_result['frames_analyzed'],
                        'frames_with_violations': qctools_result['frames_with_violations']
                    }
                    logger.debug(f"    QCTools (full frame): {period_comparison['qctools_full_frame']['violations_pct']:.1f}% violations, "
                            f"max BRNG: {period_comparison['qctools_full_frame']['max_brng']:.4f}%")
            
            self._emit_progress(period_mid)
            
            # Get FFprobe data (active area only)
            if active_area:
                logger.debug(f"    Running FFprobe on active area only...")
                ffprobe_result = self._analyze_with_ffprobe_period(
                    active_area, start_time, duration, i+1,
                    progress_range=(period_mid, period_end)
                )
                if ffprobe_result:
                    period_comparison['ffprobe_active_area'] = {
                        'violations_pct': (ffprobe_result['frames_with_violations'] / 
                                        ffprobe_result['frames_analyzed'] * 100) if ffprobe_result['frames_analyzed'] > 0 else 0,
                        'max_brng': max(ffprobe_result['brng_values']) * 100 if ffprobe_result['brng_values'] else 0,
                        'frames_analyzed': ffprobe_result['frames_analyzed'],
                        'frames_with_violations': ffprobe_result['frames_with_violations']
                    }
                    logger.debug(f"    FFprobe (active area): {period_comparison['ffprobe_active_area']['violations_pct']:.1f}% violations, "
                            f"max BRNG: {period_comparison['ffprobe_active_area']['max_brng']:.4f}%")
                    
                    # Compare results
                    if qctools_result and period_comparison.get('qctools_full_frame'):
                        full_violations = period_comparison['qctools_full_frame']['violations_pct']
                        active_violations = period_comparison['ffprobe_active_area']['violations_pct']
                        
                        if full_violations > active_violations + 5 and active_violations < 30:
                            logger.info(f"    → Border violations detected: Full frame has {full_violations - active_violations:.1f}% more violations\n")
                            period_comparison['diagnosis'] = 'border_violations'
                        elif active_violations > 10:
                            logger.info(f"    → Content violations: Active area itself has {active_violations:.1f}% violations\n")
                            period_comparison['diagnosis'] = 'content_violations'
                        else:
                            logger.info(f"    → Minimal violations in both full frame and active area\n")
                            period_comparison['diagnosis'] = 'minimal_violations'
                    
                    # Use FFprobe result for aggregate
                    all_results.append(ffprobe_result)
                else:
                    # Fall back to QCTools if FFprobe fails
                    if qctools_result:
                        all_results.append(qctools_result)
            else:
                # No active area defined, use QCTools or FFprobe on full frame
                if qctools_result:
                    all_results.append(qctools_result)
                else:
                    logger.info(f"    Using FFprobe on full frame (no border detection)")
                    ffprobe_result = self._analyze_with_ffprobe_period(
                        None, start_time, duration, i+1,
                        progress_range=(period_mid, period_end)
                    )
                    if ffprobe_result:
                        all_results.append(ffprobe_result)
            
            comparison_results.append(period_comparison)
            
            # Emit per-period progress at end of this period's analysis
            self._emit_progress(period_end)
        
        # Aggregate results
        if not all_results:
            self._emit_progress(100)
            return SignalstatsResult(
                violation_percentage=0,
                max_brng=0,
                avg_brng=0,
                analysis_periods=analysis_periods,
                diagnosis="No data available",
                used_qctools=False
            )
        
        # Calculate aggregates
        total_violations = sum(r.get('frames_with_violations', 0) for r in all_results)
        total_frames = sum(r.get('frames_analyzed', 0) for r in all_results)
        all_brng_values = []
        for r in all_results:
            all_brng_values.extend(r.get('brng_values', []))
        
        violation_pct = (total_violations / total_frames * 100) if total_frames > 0 else 0
        max_brng = max(all_brng_values) * 100 if all_brng_values else 0
        avg_brng = np.mean(all_brng_values) * 100 if all_brng_values else 0
        
        # Generate comprehensive diagnosis
        diagnosis = self._generate_comprehensive_diagnosis(
            violation_pct, max_brng, comparison_results, active_area is not None
        )
        
        # Log final comparison summary
        logger.info(f"\n  === Signalstats Analysis Summary ===")
        logger.info(f"  Active area results:")
        logger.info(f"    Frames with violations: {total_violations:,} / {total_frames:,} ({violation_pct:.1f}%)")
        logger.info(f"    Max BRNG value: {max_brng:.4f}%")
        logger.info(f"    Average BRNG value: {avg_brng:.4f}%")
        logger.info(f"  Diagnosis: {diagnosis}\n")
        
        self._emit_progress(100)
        
        return SignalstatsResult(
            violation_percentage=violation_pct,
            max_brng=max_brng,
            avg_brng=avg_brng,
            analysis_periods=analysis_periods,
            diagnosis=diagnosis,
            used_qctools=used_qctools,
            comparison_results=comparison_results
        )

    def _generate_comprehensive_diagnosis(self, violation_pct: float, max_brng: float, 
                                        comparison_results: List[Dict], has_active_area: bool) -> str:
        """Generate diagnosis based on full frame vs active area comparison"""
        
        if not has_active_area:
            # No border detection, standard diagnosis
            if violation_pct < 10 and max_brng < 0.1:
                return "Video appears broadcast-compliant"
            elif violation_pct < 50 and max_brng < 1.0:
                return "Minor BRNG violations - likely acceptable"
            elif max_brng > 2.0:
                return "Significant BRNG violations requiring correction"
            else:
                return "Moderate BRNG violations detected"
        
        # Analyze comparison results
        border_violation_periods = sum(1 for r in comparison_results if r.get('diagnosis') == 'border_violations')
        content_violation_periods = sum(1 for r in comparison_results if r.get('diagnosis') == 'content_violations')
        
        if border_violation_periods > content_violation_periods:
            return "BRNG violations primarily in border areas - active content appears broadcast-safe"
        elif content_violation_periods > 0:
            if violation_pct > 50:
                return "Significant BRNG violations in active picture area - requires correction"
            else:
                return "BRNG violations detected in active picture area - review recommended"
        else:
            return "Video appears broadcast-compliant with properly detected borders"
    
    def _seconds_to_timecode(self, seconds: float) -> str:
        """Convert seconds to MM:SS.mmm format"""
        minutes = int(seconds // 60)
        remaining_seconds = seconds % 60
        return f"{minutes:02d}:{remaining_seconds:06.3f}"
    
    def _find_analysis_periods(self, content_start: float, color_bars_end: float,
                      duration: int, num_periods: int,
                      quality_hints: List[Tuple[float, float]] = None,
                      qctools_periods: List[Tuple[float, int]] = None,
                      black_segments: List[Tuple[float, float]] = None) -> List[Tuple[float, int]]:
        """Find good analysis periods, preferring QCTools violation clusters when available.
        
        Validates all candidate periods against known black segments and shifts
        or replaces any that overlap significantly with all-black content.
        """
        
        # Start after color bars with a safety margin
        effective_start = max(content_start, color_bars_end or 0) + 10
        
        logger.debug(f"  Content starts at {effective_start:.1f}s (after color bars at {color_bars_end:.1f}s)\n")
        
        # PRIORITY 1: Use QCTools-based periods if available (already validated upstream)
        if qctools_periods:
            logger.info(f"  Using {len(qctools_periods)} QCTools-based analysis periods (targeting violation clusters)")
            periods = list(qctools_periods)
        
        # PRIORITY 2: Use quality hints if available (from border detection)
        elif quality_hints:
            valid_hints = [(t, q) for t, q in quality_hints if t >= effective_start]
            if len(valid_hints) >= num_periods:
                periods = []
                for time_hint, _ in valid_hints[:num_periods]:
                    period_start = max(effective_start, time_hint - duration/2)
                    if period_start + duration <= self.duration - 30:
                        periods.append((period_start, duration))
                if len(periods) >= num_periods:
                    logger.info(f"  Using {len(periods)} quality hint-based analysis periods")
                else:
                    periods = None  # Fall through to even distribution
            else:
                periods = None
        else:
            periods = None
        
        # PRIORITY 3: Fall back to even distribution
        if periods is None:
            logger.info(f"  Using evenly distributed analysis periods (no violation clusters found)")
            available_duration = self.duration - effective_start - 30
            if available_duration < duration * num_periods:
                if available_duration >= duration:
                    periods = []
                    actual_periods = min(num_periods, int(available_duration / duration))
                    spacing = (available_duration - duration) / max(1, actual_periods - 1)
                    for i in range(actual_periods):
                        start = effective_start + i * spacing
                        periods.append((start, duration))
                else:
                    periods = [(effective_start, min(duration, available_duration))]
            else:
                periods = []
                spacing = (available_duration - duration) / max(1, num_periods - 1)
                for i in range(num_periods):
                    start = effective_start + i * spacing
                    periods.append((start, duration))
        
        # Validate all periods against black segments
        if black_segments and periods:
            periods = self._validate_periods_against_black_segments(
                periods, black_segments, effective_start, duration
            )
        
        return periods

    def _validate_periods_against_black_segments(
            self, 
            periods: List[Tuple[float, int]],
            black_segments: List[Tuple[float, float]],
            effective_start: float,
            period_duration: int) -> List[Tuple[float, int]]:
        """
        Check each candidate period for overlap with black segments.
        
        If a period overlaps with a black segment by more than 25% of its duration,
        attempt to shift it to a nearby non-black region. If no valid shift is found,
        the period is dropped.
        
        Args:
            periods: Candidate (start_time, duration) tuples
            black_segments: Known (start_time, end_time) black segment tuples
            effective_start: Earliest valid start time for any period
            period_duration: Desired period duration in seconds
            
        Returns:
            Validated list of periods with black-overlapping ones shifted or removed.
        """
        validated = []
        
        for start, dur in periods:
            end = start + dur
            
            # Calculate total overlap with all black segments
            total_overlap = 0.0
            for black_start, black_end in black_segments:
                overlap_start = max(start, black_start)
                overlap_end = min(end, black_end)
                if overlap_start < overlap_end:
                    total_overlap += (overlap_end - overlap_start)
            
            overlap_pct = (total_overlap / dur) * 100 if dur > 0 else 0
            
            if overlap_pct <= 25:
                # Period is fine, keep it
                validated.append((start, dur))
            else:
                # Period overlaps significantly with black content
                start_tc = f"{int(start // 60):02d}:{start % 60:05.2f}"
                logger.info(f"  Period at {start_tc} overlaps {overlap_pct:.0f}% with black segment, attempting to shift...")
                
                shifted = self._shift_period_away_from_black(
                    start, dur, black_segments, effective_start,
                    [s for s, d in validated]  # Already-used start times
                )

                if shifted is not None:
                    shifted_tc = f"{int(shifted // 60):02d}:{shifted % 60:05.2f}"
                    logger.info(f"    → Shifted to {shifted_tc}")
                    validated.append((shifted, dur))
                else:
                    # No room for a full-length period anywhere — shrink the
                    # period to fit the largest non-black content gap instead
                    # of dropping it (short tapes may have less non-black
                    # content than one full period)
                    fitted = self._fit_period_in_content_gap(
                        start, dur, black_segments, effective_start, validated
                    )
                    if fitted is not None:
                        fit_start, fit_dur = fitted
                        fit_tc = f"{int(fit_start // 60):02d}:{fit_start % 60:05.2f}"
                        logger.info(f"    → Shrunk to {fit_dur}s and moved to {fit_tc} to fit non-black content")
                        validated.append(fitted)
                    else:
                        logger.warning(f"    → Could not find valid non-black replacement, dropping period")

        return validated

    def _fit_period_in_content_gap(
            self,
            original_start: float,
            duration: int,
            black_segments: List[Tuple[float, float]],
            effective_start: float,
            validated: List[Tuple[float, int]],
            min_period: float = 10.0) -> Optional[Tuple[float, int]]:
        """
        Fit a (possibly shortened) period into the largest non-black content gap.

        Used as a last resort when no full-length shift position exists —
        e.g. a short tape where the non-black content between color bars and
        a black tail is shorter than the configured period duration.

        Returns:
            (start_time, duration) tuple, or None if no gap of at least
            min_period seconds is available.
        """
        # Build non-black gaps between effective_start and near end of file
        window_end = self.duration - 10
        gaps = []
        cursor = effective_start
        for black_start, black_end in sorted(black_segments):
            if black_end <= cursor:
                continue
            if black_start > cursor:
                gaps.append((cursor, min(black_start, window_end)))
            cursor = max(cursor, black_end)
            if cursor >= window_end:
                break
        if cursor < window_end:
            gaps.append((cursor, window_end))

        # Try the largest gaps first
        for gap_start, gap_end in sorted(gaps, key=lambda g: g[1] - g[0], reverse=True):
            gap_len = gap_end - gap_start
            if gap_len < min_period:
                continue

            # Keep the original start if it leaves enough room in this gap,
            # otherwise start at the beginning of the gap
            if gap_start <= original_start and (gap_end - original_start) >= min_period:
                fit_start = original_start
            else:
                fit_start = gap_start
            fit_dur = int(min(duration, gap_end - fit_start))

            # Reject if it overlaps an already-validated period
            overlaps = any(
                fit_start < v_start + v_dur and v_start < fit_start + fit_dur
                for v_start, v_dur in validated
            )
            if not overlaps:
                return (fit_start, fit_dur)

        return None

    def _shift_period_away_from_black(
            self,
            original_start: float,
            duration: int,
            black_segments: List[Tuple[float, float]],
            effective_start: float,
            used_starts: List[float]) -> Optional[float]:
        """
        Find a new start time for a period that avoids black segments.
        
        Searches outward from the original position in both directions,
        checking candidate positions for overlap with black segments and
        already-selected periods.
        
        Returns:
            New start time, or None if no valid position found.
        """
        max_shift = self.duration * 0.5  # Don't search more than half the video
        step = 5.0  # Search in 5-second increments
        
        for offset in np.arange(step, max_shift, step):
            # Try shifting forward, then backward
            for candidate_start in [original_start + offset, original_start - offset]:
                candidate_end = candidate_start + duration
                
                # Check bounds
                if candidate_start < effective_start:
                    continue
                if candidate_end > self.duration - 10:
                    continue
                
                # Check overlap with black segments
                total_overlap = 0.0
                for black_start, black_end in black_segments:
                    overlap_start = max(candidate_start, black_start)
                    overlap_end = min(candidate_end, black_end)
                    if overlap_start < overlap_end:
                        total_overlap += (overlap_end - overlap_start)
                
                if (total_overlap / duration) > 0.1:
                    continue  # Still too much overlap (>10%)
                
                # Check overlap with already-selected periods
                too_close = False
                for used_start in used_starts:
                    if abs(candidate_start - used_start) < duration:
                        too_close = True
                        break
                
                if not too_close:
                    return candidate_start
        
        return None
    
    def _parse_qctools_brng_period(self, start_time: float, end_time: float, period_num: int) -> Optional[Dict]:
        """Parse BRNG values from QCTools report for specific time period"""
        if not self.qctools_report:
            return None
        
        # Create a parser instance for this specific period
        parser = QCToolsParser(self.qctools_report, self.fps)
        
        # Parse violations for the specific time range
        violations = parser.parse_for_violations_streaming_period(
            start_time=start_time,
            end_time=end_time,
            period_num=period_num,
            max_frames=1000
        )
        
        if not violations:
            logger.info(f"    No violations found in period {period_num}")
            return None
        
        return {
            'frames_analyzed': len(violations),
            'frames_with_violations': len([v for v in violations if v.violation_score > 0]),
            'brng_values': [v.violation_score for v in violations],
            'source': 'qctools',
            'period_num': period_num,
            'time_range': (start_time, end_time)
        }
    
    def _should_use_qctools(self, qctools_result: Dict) -> bool:
        """Decide if QCTools data is sufficient"""
        if not qctools_result:
            return False
        
        violation_pct = (qctools_result['frames_with_violations'] / 
                        qctools_result['frames_analyzed'] * 100)
        max_brng = max(qctools_result['brng_values']) if qctools_result['brng_values'] else 0
        
        # Log the QCTools results for this period
        period_num = qctools_result.get('period_num', '?')
        logger.info(f"    Period {period_num} QCTools results: {qctools_result['frames_analyzed']:,} frames, "
                   f"{qctools_result['frames_with_violations']:,} violations ({violation_pct:.1f}%), "
                   f"max BRNG: {max_brng*100:.4f}%")
        
        # Use QCTools if violations are minimal or if we have good data
        return True  # Always use QCTools data when available for consistency
    
    def _analyze_with_ffprobe_period(self, active_area: Tuple,
                                start_time: float, duration: int, period_num: int,
                                progress_range: Optional[Tuple[int, int]] = None) -> Dict:
        """Analyze using FFprobe signalstats for specific period - with fast seeking.

        If progress_range is provided as (p_start, p_end), incremental progress is
        emitted as frames stream out of ffprobe, mapped into that subrange.
        """

        crop_filter = ""
        if active_area:
            x, y, w, h = active_area
            crop_filter = f"crop={w}:{h}:{x}:{y},"

        # Use movie filter's seek_point parameter for fast seeking
        # trim=duration limits output to N seconds after seek point
        filter_chain = f"movie={shlex.quote(self.video_path)}:seek_point={start_time}"
        filter_chain += f",{crop_filter}signalstats=stat=brng"
        filter_chain += f",trim=duration={duration}"

        cmd = [
            'ffprobe',
            '-f', 'lavfi',
            '-i', filter_chain,
            '-show_entries', 'frame_tags=lavfi.signalstats.BRNG',
            '-of', 'csv=p=0'
        ]

        # Estimate frames for the period (~30fps; fine as a denominator for progress)
        expected_frames = max(1, int(duration * 30))
        emit_interval = max(20, expected_frames // 50)
        p_start, p_end = progress_range if progress_range else (0, 0)
        last_pct = p_start

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            brng_values = []
            frame_count = 0
            while True:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                if self.check_cancelled():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    brng_values.append(float(line))
                except ValueError:
                    continue
                frame_count += 1
                if progress_range and frame_count % emit_interval == 0:
                    fraction = min(1.0, frame_count / expected_frames)
                    pct = p_start + int((p_end - p_start) * fraction)
                    pct = min(p_end, max(p_start, pct))
                    if pct > last_pct:
                        self._emit_progress(pct)
                        last_pct = pct

            if proc.returncode != 0:
                logger.warning(f"    FFprobe failed for period {period_num}")
                return None

            frames_with_violations = len([v for v in brng_values if v > 0])
            violation_pct = (frames_with_violations / len(brng_values) * 100) if brng_values else 0
            max_brng = max(brng_values) if brng_values else 0

            logger.debug(f"    Period {period_num} FFprobe results: {len(brng_values):,} frames, "
                       f"{frames_with_violations:,} violations ({violation_pct:.1f}%), "
                       f"max BRNG: {max_brng*100:.4f}%")

            return {
                'frames_analyzed': len(brng_values),
                'frames_with_violations': frames_with_violations,
                'brng_values': brng_values,
                'source': 'ffprobe',
                'period_num': period_num
            }
        except Exception as e:
            logger.error(f"FFprobe error for period {period_num}: {e}")
            return None


class EnhancedFrameAnalysis:
    """
    Main coordinator for enhanced frame analysis.
    Combines efficiency of refactored version with sophistication of original.
    """
    
    def __init__(self, video_path: str, output_dir: str = None, signals=None,
                 check_cancelled_fn=None):
        self.video_path = Path(video_path)
        self.video_id = self.video_path.stem
        self.output_dir = Path(output_dir) if output_dir else self.video_path.parent
        self.output_dir.mkdir(exist_ok=True)
        self.signals = signals  # Store signals for emitting step completion
        self.check_cancelled = check_cancelled_fn or (lambda: False)

        # Store config manager as an instance attribute
        self.config_mgr = ConfigManager()
        self.config_mgr.refresh_configs()
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        
        # Find QCTools report once (with logging)
        self.qctools_report = self._find_qctools_report()
        if not self.qctools_report:
            logger.warning(f"No QCTools report found for {self.video_path.name}")
        
        # Initialize components (pass qctools_report to avoid duplicate search)
        self.border_detector = SophisticatedBorderDetector(video_path, signals=signals, check_cancelled_fn=self.check_cancelled)
        self.brng_analyzer = None  # Will be initialized with border data
        self.signalstats_analyzer = IntegratedSignalstatsAnalyzer(video_path, qctools_report=self.qctools_report, check_cancelled_fn=self.check_cancelled, signals=signals)
        
        # Initialize QCTools parser if report exists
        self.qctools_parser = None
        if self.qctools_report:
            self.qctools_parser = QCToolsParser(self.qctools_report)
    
    def _find_qctools_report(self) -> Optional[str]:
        """Find QCTools report for the video"""
        # Get the full filename with extension
        video_filename = self.video_path.name
        
        search_paths = [
            # Try with full filename first (e.g., JPC_AV_01709.mkv.qctools.xml.gz)
            self.video_path.parent / f"{video_filename}.qctools.xml.gz",
            self.video_path.parent / f"{self.video_id}_qc_metadata" / f"{video_filename}.qctools.xml.gz",
            self.video_path.parent / f"{self.video_id}_vrecord_metadata" / f"{video_filename}.qctools.xml.gz",
            # Then try without extension (e.g., JPC_AV_01709.qctools.xml.gz)
            self.video_path.parent / f"{self.video_id}.qctools.xml.gz",
            self.video_path.parent / f"{self.video_id}_qc_metadata" / f"{self.video_id}.qctools.xml.gz",
            self.video_path.parent / f"{self.video_id}_vrecord_metadata" / f"{self.video_id}.qctools.xml.gz",
        ]
        
        for path in search_paths:
            if path.exists():
                logger.debug(f"Found QCTools report for Frame Analysis: {path}\n")
                return str(path)
        
        return None
    
    def _is_step_enabled(self, flag_value) -> bool:
        """
        Check if a step is enabled, handling both boolean and string types.
        
        Args:
            flag_value: Can be bool (True/False) or str ("yes"/"no")
            
        Returns:
            bool: True if the step should run, False otherwise
        """
        if isinstance(flag_value, bool):
            return flag_value
        elif isinstance(flag_value, str):
            return flag_value.lower() in ('yes', 'true', '1')
        else:
            # Default to True for unknown types to maintain backward compatibility
            return True
        
    def _get_video_duration(self) -> Optional[float]:
        """Get video duration in seconds using ffprobe"""
        try:
            cmd = [
                'ffprobe', '-v', 'quiet',
                '-show_entries', 'format=duration',
                '-of', 'csv=p=0',
                str(self.video_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Could not get video duration: {e}")
        return None

    def _detect_dropped_samples(self, color_bars_end_time: float = None) -> Optional[DroppedSampleResult]:
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
        spectrogram_path = self._generate_spectrogram()

        # Step 2: Analyze spectrogram for vertical spikes
        spike_count = 0
        spike_timestamps = []
        if spectrogram_path:
            spike_count, spike_timestamps = self._analyze_spectrogram_spikes(spectrogram_path)
            if spike_count > 0:
                logger.warning(f"Detected {spike_count} potential dropped sample spike(s) in spectrogram\n")
            else:
                logger.info("No dropped sample spikes detected in spectrogram")

        # Step 3: Compare audio/video durations
        audio_duration, video_duration, sample_rate = self._get_av_durations()
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
        combined_score, status = self._compute_dropped_sample_score(spike_count, duration_diff_ms)

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

    def _detect_duplicate_frames(
        self,
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
        if not self.qctools_parser:
            logger.warning("Skipping duplicate frame detection — no QCTools report available\n")
            return None

        logger.info("Running duplicate frame detection...")

        candidate_runs, thresholds = self.qctools_parser.find_duplicate_frame_candidates(
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
        fps = self.qctools_parser.fps or 29.97
        frame_period = 1.0 / fps if fps else 1.0 / 29.97
        try:
            cap = cv2.VideoCapture(str(self.video_path))
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
            if self.check_cancelled():
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
            thumb_dir = self.output_dir / "duplicate_frame_thumbnails"
            thumb_dir.mkdir(exist_ok=True)
            thumb_cap = None
            try:
                thumb_cap = cv2.VideoCapture(str(self.video_path))
                if thumb_cap.isOpened():
                    for i, run in enumerate(final_runs, 1):
                        if self.check_cancelled():
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
                _get_video_start_timecode(str(self.video_path)), tc_nominal
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
            bit_depth_10=self.qctools_parser.bit_depth_10,
            ydif_threshold=thresholds['ydif'],
            udif_threshold=thresholds['udif'],
            vdif_threshold=thresholds['vdif'],
            min_run_length=min_run_length,
            runs=final_runs,
        )

    def _generate_spectrogram(self) -> Optional[Path]:
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
        output_path = self.output_dir / f"{self.video_id}_spectrogram.png"

        audio_duration, _, _ = self._get_av_durations()
        p_start, p_end = 0, 95
        can_track_progress = (
            self.signals is not None
            and hasattr(self.signals, 'frame_analysis_progress')
        )

        cmd = [
            'ffmpeg', '-y',
            '-hide_banner', '-loglevel', 'error',
            '-i', str(self.video_path),
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
                if self.check_cancelled():
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
                        self.signals.frame_analysis_progress.emit(pct)
                        last_pct = pct
                time.sleep(poll_interval)

            stderr_text = proc.stderr.read() if proc.stderr else ''

            if timed_out:
                logger.warning("Spectrogram generation timed out (300s limit)")
                return None

            if proc.returncode == 0 and output_path.exists():
                logger.info(f"Spectrogram saved to: {output_path.name}")
                if can_track_progress:
                    self.signals.frame_analysis_progress.emit(p_end)
                return output_path
            else:
                logger.warning(f"FFmpeg spectrogram generation failed (exit code {proc.returncode})")
                report_ffmpeg_stderr(stderr_text, "spectrogram", failure=True)
        except Exception as e:
            logger.warning(f"Error generating spectrogram: {e}")

        return None

    def _analyze_spectrogram_spikes(self, spectrogram_path: Path) -> Tuple[int, List[float]]:
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
            video_duration = self._get_video_duration() or 0
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

    def _get_av_durations(self) -> Tuple[Optional[float], Optional[float], int]:
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
        sidecar_path = self.video_path.parent / f"{self.video_id}_qc_metadata" / f"{self.video_id}_ffprobe_output.txt"
        if not sidecar_path.exists():
            # Also check the destination output dir
            sidecar_path = self.output_dir / f"{self.video_id}_ffprobe_output.txt"

        if sidecar_path.exists():
            try:
                with open(sidecar_path, 'r') as f:
                    ffprobe_data = json.load(f)

                audio_duration, video_duration, sample_rate = self._extract_stream_durations(
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
                str(self.video_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0:
                data = json.loads(result.stdout)
                audio_duration, video_duration, sample_rate = self._extract_stream_durations(
                    data.get('streams', [])
                )
        except Exception as e:
            logger.warning(f"Could not get stream durations via ffprobe: {e}")

        return audio_duration, video_duration, sample_rate

    @staticmethod
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
                    duration = EnhancedFrameAnalysis._parse_duration_tag(tag_duration)

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

    @staticmethod
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

    def _compute_dropped_sample_score(self, spike_count: int, duration_diff_ms: float) -> Tuple[float, str]:
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

    def _check_bitplane_noise(self, num_samples: int = 20) -> Dict:
        """
        Check if the 9th and 10th bits (two least significant bits) of a 10-bit
        video contain actual data by running ffprobe's bitplanenoise filter on
        sampled frames.

        TBC and framesync devices can truncate these bits, producing a file that
        is technically 10-bit but effectively 8-bit. When bits are truncated,
        the bitplanenoise value for those bitplanes will be 0.

        Uses -read_intervals to seek to evenly-spaced timestamps (fast) rather
        than decoding every frame sequentially.

        Args:
            num_samples: Number of frames to sample (evenly distributed)

        Returns:
            Dict with check results including per-channel, per-bitplane findings
        """
        # Get video duration to calculate sample timestamps
        duration = self._get_video_duration()
        if duration is None or duration == 0:
            logger.warning("Could not determine video duration for bitplane check")
            return {'status': 'error', 'message': 'Could not determine video duration'}

        logger.debug(f"Checking bitplane noise on {num_samples} sampled frames across {duration:.1f}s...")

        if self.check_cancelled():
            return {'status': 'cancelled', 'message': 'Cancelled before bitplane check started'}

        # Sample frames by seeking with -ss (input seeking, instant) and
        # decoding just 1 frame per position. Each ffmpeg call takes ~0.1-0.2s,
        # so 20 calls ≈ 2-4 seconds total regardless of video length.
        timestamps = [i * duration / num_samples for i in range(num_samples)]

        tag_keys = [
            'lavfi.bitplanenoise.0.1', 'lavfi.bitplanenoise.1.1', 'lavfi.bitplanenoise.2.1',
            'lavfi.bitplanenoise.0.2', 'lavfi.bitplanenoise.1.2', 'lavfi.bitplanenoise.2.2',
            'lavfi.bitplanenoise.0.3', 'lavfi.bitplanenoise.1.3', 'lavfi.bitplanenoise.2.3',
            'lavfi.bitplanenoise.0.4', 'lavfi.bitplanenoise.1.4', 'lavfi.bitplanenoise.2.4'
        ]

        vf = (
            'bitplanenoise=bitplane=1,'
            'bitplanenoise=bitplane=2,'
            'bitplanenoise=bitplane=3,'
            'bitplanenoise=bitplane=4,'
            'metadata=print:file=-'
        )

        frames = []
        for ts in timestamps:
            if self.check_cancelled():
                logger.info("Bitplane noise check cancelled")
                return {'status': 'cancelled', 'message': 'Bitplane check was cancelled'}

            cmd = [
                'ffmpeg', '-nostdin', '-hide_banner', '-v', 'quiet',
                '-ss', f'{ts:.3f}',
                '-i', str(self.video_path),
                '-vf', vf,
                '-an',
                '-frames:v', '1',
                '-f', 'null',
                '-'
            ]

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30
                )
                # Parse metadata=print output from stdout
                current_tags = {}
                for line in result.stdout.splitlines():
                    line = line.strip()
                    if '=' in line:
                        key, _, val = line.partition('=')
                        if key in tag_keys:
                            current_tags[key] = val
                if current_tags:
                    frames.append({'tags': current_tags})
            except subprocess.TimeoutExpired:
                logger.debug(f"Bitplane probe timed out at {ts:.1f}s")
                continue
            except Exception as e:
                logger.debug(f"Bitplane probe error at {ts:.1f}s: {e}")
                continue

        if not frames:
            logger.warning("No frames returned from bitplane noise check")
            return {'status': 'error', 'message': 'No frames analyzed'}

        logger.info(f"Bitplane check analyzed {len(frames)} frames")

        # Channel names for reporting
        channel_names = {0: 'Y', 1: 'Cb', 2: 'Cr'}
        bitplane_names = {1: '9th bit (bit 1)', 2: '10th bit (bit 2)', 3: '8th bit (bit 3)', 4: '7th bit (bit 4)'}

        # Collect per-channel, per-bitplane values
        values = {}
        for plane in range(3):
            for bitplane in [1, 2, 3, 4]:
                key = f'lavfi.bitplanenoise.{plane}.{bitplane}'
                frame_values = []
                for frame in frames:
                    frame_tags = frame.get('tags', {})
                    val_str = frame_tags.get(key)
                    if val_str is not None:
                        frame_values.append(float(val_str))
                values[(plane, bitplane)] = frame_values

        # Analyze results: check if all sampled frames have zero noise for each channel/bitplane
        channel_results = {}
        all_empty = True
        any_empty = False

        for plane in range(3):
            ch_name = channel_names[plane]
            channel_results[ch_name] = {}
            for bitplane in [1, 2, 3, 4]:
                bp_name = bitplane_names[bitplane]
                frame_values = values[(plane, bitplane)]

                if not frame_values:
                    channel_results[ch_name][bp_name] = {
                        'status': 'no_data',
                        'frames_sampled': 0
                    }
                    continue

                zero_count = sum(1 for v in frame_values if v == 0.0)
                total = len(frame_values)
                zero_pct = (zero_count / total) * 100
                avg_noise = sum(frame_values) / total
                max_noise = max(frame_values)

                is_empty = zero_count == total
                if is_empty:
                    any_empty = True
                else:
                    all_empty = False

                channel_results[ch_name][bp_name] = {
                    'status': 'empty' if is_empty else 'active',
                    'frames_sampled': total,
                    'zero_frames': zero_count,
                    'zero_percentage': round(zero_pct, 1),
                    'average_noise': round(avg_noise, 6),
                    'max_noise': round(max_noise, 6)
                }

        if not any_empty:
            all_empty = False

        # Compute overall average noise per bitplane (across all channels)
        overall_bitplane_avg = {}
        for bitplane in [1, 2, 3, 4]:
            bp_name = bitplane_names[bitplane]
            all_values = []
            for plane in range(3):
                all_values.extend(values[(plane, bitplane)])
            if all_values:
                avg = sum(all_values) / len(all_values)
                overall_bitplane_avg[bp_name] = round(avg, 6)
            else:
                overall_bitplane_avg[bp_name] = None

        for bp_name, avg in overall_bitplane_avg.items():
            if avg is not None:
                logger.info(f"  Avg bitplanenoise {bp_name}: {avg}")

        # Compare less significant bits (9th/10th) against more significant bits (7th/8th)
        # The 9th and 10th bits should have noise values closer to 1.0 than the 7th and 8th
        bit_order_check = None
        avg_9th = overall_bitplane_avg.get(bitplane_names[1])   # 9th bit
        avg_10th = overall_bitplane_avg.get(bitplane_names[2])  # 10th bit
        avg_8th = overall_bitplane_avg.get(bitplane_names[3])   # 8th bit
        avg_7th = overall_bitplane_avg.get(bitplane_names[4])   # 7th bit

        if all(v is not None for v in [avg_9th, avg_10th, avg_8th, avg_7th]):
            avg_lsb = (avg_9th + avg_10th) / 2   # average of less significant bits
            avg_msb = (avg_7th + avg_8th) / 2     # average of more significant bits

            if avg_lsb >= avg_msb:
                bit_order_check = {
                    'status': 'expected',
                    'message': 'Less significant bits (9th/10th) have higher noise than more significant bits (7th/8th), as expected',
                    'avg_9th_10th': round(avg_lsb, 6),
                    'avg_7th_8th': round(avg_msb, 6)
                }
                logger.info(f"  Bit order check: expected (9th/10th avg: {avg_lsb:.6f} >= 7th/8th avg: {avg_msb:.6f})")
            else:
                bit_order_check = {
                    'status': 'unexpected',
                    'message': 'Less significant bits (9th/10th) have lower noise than more significant bits (7th/8th) — possible bit truncation or unusual encoding',
                    'avg_9th_10th': round(avg_lsb, 6),
                    'avg_7th_8th': round(avg_msb, 6)
                }
                logger.warning(f"  Bit order check: unexpected (9th/10th avg: {avg_lsb:.6f} < 7th/8th avg: {avg_msb:.6f})")

        # Determine overall status
        if all_empty:
            overall_status = 'truncated'
            overall_message = (
                'All channels show empty 7th–10th bits — '
                'video appears to have significant bit truncation'
            )
            logger.warning(f"Bitplane check: {overall_message}")
        elif any_empty:
            # Find which specific channels/bitplanes are empty
            empty_parts = []
            for ch_name, bp_data in channel_results.items():
                for bp_name, bp_result in bp_data.items():
                    if bp_result.get('status') == 'empty':
                        empty_parts.append(f"{ch_name} {bp_name}")
            overall_status = 'partial_truncation'
            overall_message = (
                f'Some bitplanes appear empty: {", ".join(empty_parts)} — '
                f'possible partial bit truncation'
            )
            logger.warning(f"Bitplane check: {overall_message}")
        else:
            overall_status = 'valid'
            overall_message = '7th–10th bits contain data across all channels'
            logger.info(f"Bitplane check: {overall_message}")

        return {
            'status': overall_status,
            'message': overall_message,
            'frames_sampled': len(frames),
            'video_duration': duration,
            'overall_bitplane_averages': overall_bitplane_avg,
            'channels': channel_results,
            'bit_order_check': bit_order_check
        }

    def analyze(self,
        method: str = 'sophisticated',
        duration_limit: int = 300,
        skip_color_bars: bool = True,
        max_refinement_iterations: int = 3,
        color_bars_end_time: float = None,
        signals=None) -> Dict:
        """
        Run complete enhanced frame analysis with optional iterative refinement.
        
        Args:
            method: 'sophisticated' or 'simple' border detection
            duration_limit: Maximum duration to analyze (seconds)
            skip_color_bars: Whether to skip color bars at start
            max_refinement_iterations: Maximum border refinement iterations
            color_bars_end_time: End time of color bars if detected
            signals: Optional signals object for emitting step completion events
        
        Returns:
            Complete analysis results dictionary
        """
        results = {
            'video_path': str(self.video_path),
            'video_id': self.video_id,
            'analysis_method': 'enhanced',
            'qctools_report_available': self.qctools_report is not None
        }

        # Reset the progress bar at the start of frame analysis
        if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
            self.signals.frame_analysis_progress.emit(0)

        frame_config = self.checks_config.outputs.frame_analysis
        
        # Check which steps are enabled (handle both bool and str types)
        bitplane_check_enabled = self._is_step_enabled(frame_config.enable_bitplane_check)
        border_detection_enabled = self._is_step_enabled(frame_config.enable_border_detection)
        brng_analysis_enabled = self._is_step_enabled(frame_config.enable_brng_analysis)
        signalstats_enabled = self._is_step_enabled(frame_config.enable_signalstats)
        dropped_sample_enabled = self._is_step_enabled(frame_config.enable_dropped_sample_detection)
        duplicate_frame_enabled = self._is_step_enabled(
            getattr(frame_config, 'enable_duplicate_frame_detection', True)
        )

        # Log which steps will run
        logger.info(f"Frame analysis configuration:")
        logger.debug(f"  Bitplane check: {'enabled' if bitplane_check_enabled else 'disabled'}")
        logger.debug(f"  Border detection: {'enabled' if border_detection_enabled else 'disabled'}")
        logger.debug(f"  BRNG analysis: {'enabled' if brng_analysis_enabled else 'disabled'}")
        logger.debug(f"  Signalstats: {'enabled' if signalstats_enabled else 'disabled'}")
        logger.debug(f"  Dropped sample detection: {'enabled' if dropped_sample_enabled else 'disabled'}")
        logger.debug(f"  Duplicate frame detection: {'enabled' if duplicate_frame_enabled else 'disabled'}\n")

        # Track what was actually run
        results['steps_enabled'] = {
            'bitplane_check': bitplane_check_enabled,
            'border_detection': border_detection_enabled,
            'brng_analysis': brng_analysis_enabled,
            'signalstats': signalstats_enabled,
            'dropped_sample_detection': dropped_sample_enabled,
            'duplicate_frame_detection': duplicate_frame_enabled,
        }

        # Step 0: Bitplane check — verify 9th and 10th bits are not empty
        if self.check_cancelled():
            return results
        if bitplane_check_enabled:
            bitplane_results = self._check_bitplane_noise()
            results['bitplane_check'] = bitplane_results
            if signals and hasattr(signals, 'step_completed'):
                signals.step_completed.emit('Frame Analysis - Bitplane Check')

        # Step 1: Detect color bars (always run if skip_color_bars is True, as it's a prerequisite)
        if skip_color_bars:
            if color_bars_end_time is None:
                color_bars_end_time = self._detect_color_bars_duration()
                # Only log if we detected color bars via fallback (not already passed in)
                if color_bars_end_time > 0:
                    logger.info(f"Color bars detected, ending at {color_bars_end_time:.1f}s\n")
            if color_bars_end_time > 0:
                results['color_bars_end_time'] = color_bars_end_time

        # Period selection (QCTools violations + suggested periods) is only
        # needed for the video-frame analysis steps. Dropped sample detection
        # is audio-only and does not consume them.
        needs_period_selection = (
            border_detection_enabled or signalstats_enabled or brng_analysis_enabled
        )
        # Black segments are also consumed by duplicate frame detection (to
        # exclude all-black freezes from the candidate pool).
        needs_black_segments = needs_period_selection or duplicate_frame_enabled

        # Step 2: Parse QCTools for initial violations (needed for BRNG analysis or border detection)
        violations = []
        qctools_suggested_periods = []
        black_segments = []
        if self.check_cancelled():
            return results

        # Detect black segments from QCTools data BEFORE period selection so
        # violation clusters inside all-black content can be excluded, AND to
        # exclude black freezes from duplicate frame candidates.
        if needs_black_segments and self.qctools_report:
            logger.info("Scanning for black segments...")
            parser = QCToolsParser(self.qctools_report)
            black_segments = parser.detect_black_segments(min_duration=2.0)
            if black_segments:
                results['black_segments'] = [
                    {'start': s, 'end': e, 'duration': e - s} for s, e in black_segments
                ]
                logger.info("")  # Blank line after black segment log output

        if self.check_cancelled():
            return results
        if needs_period_selection:
            if self.qctools_report:
                logger.info("Parsing QCTools report for violations...")
                parser = QCToolsParser(self.qctools_report)
                violations = parser.parse_for_violations_streaming(
                    max_frames=100,
                    skip_color_bars=skip_color_bars,
                    color_bars_end_time=color_bars_end_time
                )
                frames_with_qctools_violations = len(violations)

                if frames_with_qctools_violations == 0:
                    results['qctools_violations_found'] = "No BRNG violations detected in content"
                else:
                    results['qctools_violations_found'] = frames_with_qctools_violations
            elif not self.qctools_parser:
                logger.info("No QCTools report found")

            # Analyze QCTools violation distribution to find optimal analysis periods
            if violations:
                qctools_suggested_periods = self._analyze_qctools_violation_distribution(
                    violations,
                    num_periods=frame_config.analysis_period_count,
                    period_duration=frame_config.analysis_period_duration,
                    video_duration=self.signalstats_analyzer.duration,
                    black_segments=black_segments
                )
                logger.info(f"Identified {len(qctools_suggested_periods)} periods with highest violation density\n")


        # Step 3: Border detection (conditional)
        border_results = None
        if self.check_cancelled():
            return results
        if border_detection_enabled:
            logger.info(f"Detecting borders using {method} method...")
            border_results = self.border_detector.detect_borders_with_quality_assessment(
                violations=violations,
                method=method
            )
            results['initial_borders'] = asdict(border_results)

            # Create border detection visualization
            logger.info("Creating border detection visualization...\n")
            viz_filename = f"{self.video_id}_border_detection.jpg"
            viz_output_path = self.output_dir / viz_filename
            try:
                success = self.border_detector.generate_border_visualization(
                    output_path=str(viz_output_path),
                    active_area=border_results.active_area,
                    head_switching_results=border_results.head_switching_artifacts,
                    target_time=150,  # Default to 2.5 minutes in
                    search_window=120,  # Search within 2 minutes
                    detection_method=border_results.detection_method
                )
                if success:
                    results['border_visualization'] = str(viz_output_path)
                    logger.info(f"✓ Border visualization saved to: {viz_output_path}\n")
                else:
                    logger.warning("Failed to generate border visualization")
            except Exception as e:
                logger.warning(f"Error creating border visualization: {e}")
                import traceback
                logger.debug(traceback.format_exc())
            
            # Emit border detection completion signal
            if signals and frame_config.enable_border_detection:
                signals.step_completed.emit("Frame Analysis - Border Detection")
        else:
            logger.warning("Skipping border detection (disabled in config)")
            # Create a default border result for downstream steps if needed
            if brng_analysis_enabled or signalstats_enabled:
                # Use full frame as "active area" when border detection is disabled
                logger.debug("Using full frame dimensions as active area")
                border_results = BorderDetectionResult(
                    active_area=(0, 0, self.border_detector.width, self.border_detector.height),
                    border_regions={},
                    detection_method='disabled',
                    quality_frame_hints=[],
                    head_switching_artifacts=None,
                    requires_refinement=False,
                    expansion_recommendations=None
                )
                results['initial_borders'] = asdict(border_results)
        
        # Step 4: Signalstats analysis (conditional)
        signalstats_results = None
        analysis_periods = []
        if self.check_cancelled():
            return results
        if signalstats_enabled:
            logger.info("Running signalstats analysis on active picture area to identify key analysis periods...")
            signalstats_results = self.signalstats_analyzer.analyze_with_signalstats(
                border_data=border_results,
                content_start_time=color_bars_end_time + 10 if color_bars_end_time else 10,
                color_bars_end_time=color_bars_end_time,
                analysis_duration=frame_config.analysis_period_duration,
                num_periods=frame_config.analysis_period_count,
                qctools_periods=qctools_suggested_periods,
                black_segments=black_segments
            )
            results['signalstats'] = asdict(signalstats_results)

            # Extract the analysis periods from signalstats results
            analysis_periods = signalstats_results.analysis_periods
            logger.info(f"Identified {len(analysis_periods)} key periods for BRNG analysis")

            # Compare with QCTools violation distribution if we have violations
            if violations and qctools_suggested_periods:
                logger.info("\n  === Comparing Period Selection Methods ===")
                
                # Log the signalstats periods for comparison
                logger.debug(f"\n  Signalstats-selected analysis periods:")
                for i, (start, duration) in enumerate(analysis_periods):
                    logger.debug(f"    Period {i+1}: {start:.1f}s - {start+duration:.1f}s")
                
                # Check for overlaps
                logger.debug(f"\n  Checking for overlaps between methods:")
                for i, (ss_start, ss_duration) in enumerate(analysis_periods):
                    ss_end = ss_start + ss_duration
                    overlaps = []
                    
                    for j, (qct_start, qct_duration) in enumerate(qctools_suggested_periods):
                        qct_end = qct_start + qct_duration
                        # Check if periods overlap
                        if not (ss_end < qct_start or ss_start > qct_end):
                            overlap_start = max(ss_start, qct_start)
                            overlap_end = min(ss_end, qct_end)
                            overlap_duration = overlap_end - overlap_start
                            overlaps.append((j+1, overlap_duration))
                    
                    if overlaps:
                        overlap_str = ", ".join([f"QCT Period {j} ({dur:.1f}s)" for j, dur in overlaps])
                        logger.debug(f"    Signalstats Period {i+1} overlaps with: {overlap_str}")
                    else:
                        logger.debug(f"    Signalstats Period {i+1}: NO OVERLAP with QCTools violations")
                
                # Count violations in each signalstats period
                logger.info(f"\n  Actual QCTools violations in signalstats periods:")
                for i, (start, duration) in enumerate(analysis_periods):
                    end = start + duration
                    period_violations = [v for v in violations if start <= v.timestamp < end]
                    logger.debug(f"    Period {i+1} ({start:.1f}s - {end:.1f}s): {len(period_violations)} violations")
                    if period_violations and len(period_violations) <= 5:
                        # Show the actual timestamps if there are only a few
                        for v in period_violations:
                            logger.debug(f"      - Violation at {v.timestamp:.1f}s")
            
            # Emit signalstats completion signal
            if signals and frame_config.enable_signalstats:
                signals.step_completed.emit("Frame Analysis - Signalstats")
        else:
            logger.warning(f"Skipping signalstats analysis (disabled in config)\n")
        
        # Build upstream context from border detection + signalstats for BRNG analyzer
        upstream_context = None
        if signalstats_results and border_results:
            upstream_context = self._build_upstream_context(
                signalstats_results, border_results
            )
            
            # Refine analysis periods: replace low-value periods with better candidates
            if analysis_periods and qctools_suggested_periods:
                analysis_periods = self._refine_periods_from_signalstats(
                    current_periods=analysis_periods,
                    signalstats_results=signalstats_results,
                    qctools_candidate_periods=qctools_suggested_periods,
                    black_segments=black_segments,
                    period_duration=frame_config.analysis_period_duration,
                    color_bars_end_time=color_bars_end_time
                )
        
        # Step 5: BRNG analysis (conditional)
        brng_results = None
        if self.check_cancelled():
            return results
        if brng_analysis_enabled:
            # If signalstats wasn't run, use QCTools periods directly or fall back to even distribution
            if not analysis_periods:
                if qctools_suggested_periods:
                    analysis_periods = qctools_suggested_periods
                    logger.info(f"Using {len(analysis_periods)} QCTools-based analysis periods for BRNG analysis\n")
                else:
                    logger.info(f"Creating evenly distributed analysis periods (no QCTools violations found)\n")
                    video_duration = self._get_video_duration()
                    if video_duration:
                        content_start = color_bars_end_time + 10  # Start 10s after color bars
                        content_duration = video_duration - content_start - 10  # Leave 10s at end
                        if content_duration > 0:
                            period_duration = frame_config.analysis_period_duration
                            num_periods = frame_config.analysis_period_count
                            spacing = content_duration / (num_periods + 1)
                            for i in range(num_periods):
                                start_time = content_start + spacing * (i + 1)
                                analysis_periods.append((start_time, period_duration))
                            logger.debug(f"Created {len(analysis_periods)} evenly distributed analysis periods\n")
                
                # Validate fallback periods against black segments
                if black_segments and analysis_periods:
                    analysis_periods = self.signalstats_analyzer._validate_periods_against_black_segments(
                        analysis_periods, black_segments,
                        effective_start=(color_bars_end_time or 0) + 10,
                        period_duration=frame_config.analysis_period_duration
                    )
            
            logger.info("\nAnalyzing BRNG violations in identified periods...")
            self.brng_analyzer = DifferentialBRNGAnalyzer(self.video_path, border_results,
                                                          check_cancelled_fn=self.check_cancelled,
                                                          signals=self.signals)
            
            brng_results = self.brng_analyzer.analyze_with_differential_detection(
                output_dir=self.output_dir, 
                duration_limit=duration_limit,
                skip_start_seconds=color_bars_end_time,
                qctools_violations=violations,
                analysis_periods=analysis_periods,
                upstream_context=upstream_context
            )
            results['brng_analysis'] = asdict(brng_results) if brng_results else None
            
            # Emit BRNG analysis completion signal
            if signals and frame_config.enable_brng_analysis:
                signals.step_completed.emit("Frame Analysis - BRNG Analysis")
            # Log BRNG analysis summary
            if brng_results:
                self._log_brng_analysis_summary(brng_results, analysis_periods)
        else:
            logger.warning("Skipping BRNG analysis (disabled in config)\n")
            # Log correlation between signalstats and BRNG analysis
            if signalstats_results and brng_results:
                self._log_analysis_correlation(signalstats_results, brng_results)
        
        # Step 6: Iterative border refinement (only if both border detection AND BRNG analysis are enabled)
        refinement_iterations = 0
        refinement_history = []  # Track all iterations for comparison

        if (border_detection_enabled and brng_analysis_enabled and 
            method == 'sophisticated' and brng_results and brng_results.requires_border_adjustment):
            logger.warning("Border refinement needed - detected BRNG violations at frame edges")
            
            edge_pct = brng_results.aggregate_patterns.get('edge_violation_percentage', 0)
            continuous_pct = brng_results.aggregate_patterns.get('continuous_edge_percentage', 0)
            logger.debug(f"  Edge violations (any): {edge_pct:.1f}% of analyzed frames")
            logger.debug(f"  Edge violations (solid line): {continuous_pct:.1f}% of analyzed frames")
            if continuous_pct == 0 and edge_pct > 95:
                logger.info(f"    → Violations are scattered rather than forming a solid line\n")
            
            # Check if auto_retry is enabled
            auto_retry_enabled = self._is_step_enabled(frame_config.auto_retry_borders)
            
            if not auto_retry_enabled:
                logger.info("Auto-retry borders is disabled, skipping refinement loop")
            else:
                # Store initial state for comparison
                initial_borders = border_results
                initial_brng = brng_results

                while (refinement_iterations < max_refinement_iterations and
                    brng_results.requires_border_adjustment):

                    if self.check_cancelled():
                        break

                    refinement_iterations += 1
                    logger.debug(f"Refinement iteration {refinement_iterations}/{max_refinement_iterations}:")

                    # Reset frame analysis steps to pending for this refinement iteration
                    if signals:
                        if frame_config.enable_border_detection:
                            signals.step_reset.emit("Frame Analysis - Border Detection")
                        if frame_config.enable_signalstats and method == 'sophisticated':
                            signals.step_reset.emit("Frame Analysis - Signalstats")
                        if frame_config.enable_brng_analysis:
                            signals.step_reset.emit("Frame Analysis - BRNG Analysis")

                    # Store pre-refinement state
                    previous_area = border_results.active_area
                    previous_brng = brng_results

                    # Refine borders
                    if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
                        self.signals.frame_analysis_progress.emit(0)
                    border_results = self.border_detector.refine_borders(border_results, brng_results)
                    new_area = border_results.active_area

                    logger.info(f"  Active area: {previous_area[2]}x{previous_area[3]} → {new_area[2]}x{new_area[3]}")

                    if self.check_cancelled():
                        break

                    # CREATE VISUALIZATION FOR THIS ITERATION
                    logger.info("  Creating border visualization for refined borders...\n")
                    viz_filename = f"{self.video_id}_border_detection_refined_iter{refinement_iterations}.jpg"
                    viz_output_path = self.output_dir / viz_filename

                    try:
                        success = self.border_detector.generate_border_visualization(
                            output_path=str(viz_output_path),
                            active_area=border_results.active_area,
                            head_switching_results=border_results.head_switching_artifacts,
                            target_time=150,
                            search_window=120,
                            detection_method=border_results.detection_method
                        )

                        if success:
                            logger.info(f"  ✓ Refined border visualization saved: {viz_filename}\n")
                        else:
                            logger.warning(f"  ⚠ Failed to create visualization for iteration {refinement_iterations}")
                    except Exception as e:
                        logger.warning(f"  ⚠ Error creating visualization: {e}")
                        import traceback
                        logger.debug(traceback.format_exc())

                    if self.check_cancelled():
                        break

                    # Mark border detection complete for this iteration
                    if signals and frame_config.enable_border_detection:
                        signals.step_completed.emit("Frame Analysis - Border Detection")

                    # Re-run signalstats with new borders to get updated periods (only if signalstats is enabled)
                    if signalstats_enabled:
                        if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
                            self.signals.frame_analysis_progress.emit(0)
                        logger.debug("  Re-running signalstats with refined borders...\n")
                        signalstats_results = self.signalstats_analyzer.analyze_with_signalstats(
                            border_data=border_results,
                            content_start_time=0,
                            color_bars_end_time=color_bars_end_time,
                            analysis_duration=frame_config.analysis_period_duration,
                            num_periods=frame_config.analysis_period_count,
                            qctools_periods=qctools_suggested_periods,
                            black_segments=black_segments
                        )
                        analysis_periods = signalstats_results.analysis_periods

                    if self.check_cancelled():
                        break

                    # Mark signalstats complete for this iteration
                    if signals and frame_config.enable_signalstats and method == 'sophisticated':
                        signals.step_completed.emit("Frame Analysis - Signalstats")

                    # Re-analyze BRNG with new borders and periods
                    if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
                        self.signals.frame_analysis_progress.emit(0)
                    logger.debug("  Re-analyzing BRNG violations with refined borders...\n")
                    self.brng_analyzer = DifferentialBRNGAnalyzer(self.video_path, border_results,
                                                                  check_cancelled_fn=self.check_cancelled,
                                                                  signals=self.signals)

                    # Rebuild upstream context with updated signalstats/border results
                    if signalstats_results and border_results:
                        upstream_context = self._build_upstream_context(
                            signalstats_results, border_results
                        )

                    brng_results = self.brng_analyzer.analyze_with_differential_detection(
                        output_dir=self.output_dir,
                        duration_limit=duration_limit,
                        skip_start_seconds=color_bars_end_time,
                        qctools_violations=violations,
                        analysis_periods=analysis_periods,
                        upstream_context=upstream_context
                    )

                    if self.check_cancelled():
                        break

                    # Mark BRNG analysis complete for this iteration
                    if signals and frame_config.enable_brng_analysis:
                        signals.step_completed.emit("Frame Analysis - BRNG Analysis")

                    # Track this iteration's results
                    iteration_data = {
                        'iteration': refinement_iterations,
                        'active_area': new_area,
                        'area_change': {
                            'width': new_area[2] - previous_area[2],
                            'height': new_area[3] - previous_area[3]
                        },
                        'violations_before': len(previous_brng.violations) if previous_brng.violations else 0,
                        'violations_after': len(brng_results.violations) if brng_results.violations else 0,
                        'edge_violation_pct': brng_results.aggregate_patterns.get('edge_violation_percentage', 0),
                        'visualization_path': str(viz_output_path) if success else None
                    }
                    refinement_history.append(iteration_data)

                    # Log improvement metrics
                    violation_reduction = iteration_data['violations_before'] - iteration_data['violations_after']
                    if violation_reduction > 0:
                        logger.info(f"  Violations reduced: {iteration_data['violations_before']} → {iteration_data['violations_after']} (-{violation_reduction})")
                    else:
                        logger.info(f"  Violations: {iteration_data['violations_after']} (no reduction)")

                    # Check for improvement
                    improved = self._is_meaningful_improvement(
                        previous_brng, brng_results,
                        previous_area=previous_area,
                        current_area=new_area
                    )

                # After refinement loop completes
                results['refinement_iterations'] = refinement_iterations
                results['refinement_history'] = refinement_history
                results['final_borders'] = asdict(border_results)
                results['final_brng_analysis'] = asdict(brng_results) if brng_results else None
                results['initial_brng_analysis'] = asdict(initial_brng) if initial_brng else None
                if signalstats_enabled:
                    results['final_signalstats'] = asdict(signalstats_results)
                
                # CREATE COMPARISON VISUALIZATION (initial vs final)
                if refinement_iterations > 0:
                    logger.info("\nCreating before/after comparison visualization...\n")
                    comparison_path = self.output_dir / f"{self.video_id}_border_refinement_comparison.jpg"
                    
                    try:
                        self._create_refinement_comparison(
                            initial_borders=initial_borders,
                            final_borders=border_results,
                            initial_brng=initial_brng,
                            final_brng=brng_results,
                            output_path=comparison_path,
                            refinement_history=refinement_history
                        )
                        logger.info(f"✓ Refinement comparison saved: {comparison_path.name}")
                        results['refinement_comparison'] = str(comparison_path)
                    except Exception as e:
                        logger.warning(f"Could not create refinement comparison: {e}")
                
                # Summary of refinement
                logger.debug(f"\n{'='*60}")
                logger.debug(f"Border Refinement Summary:")
                logger.debug(f"  Iterations performed: {refinement_iterations}")
                
                if refinement_iterations > 0:
                    # Area changes
                    initial_area = initial_borders.active_area
                    final_area = border_results.active_area
                    width_change = final_area[2] - initial_area[2]
                    height_change = final_area[3] - initial_area[3]
                    
                    logger.debug(f"  Initial active area: {initial_area[2]}x{initial_area[3]}")
                    logger.debug(f"  Final active area: {final_area[2]}x{final_area[3]}")
                    logger.debug(f"  Size change: width {width_change:+d}px, height {height_change:+d}px")
                    
                    # Violation count (less meaningful but still reported)
                    initial_violations = len(initial_brng.violations) if initial_brng.violations else 0
                    final_violations = len(brng_results.violations) if brng_results.violations else 0
                    violation_change = final_violations - initial_violations
                    logger.debug(f"  Violation frames: {initial_violations} → {final_violations} ({violation_change:+d})")
                    
                    # Edge violation percentage (primary metric for border refinement success)
                    initial_edge_pct = initial_brng.aggregate_patterns.get('edge_violation_percentage', 0)
                    final_edge_pct = brng_results.aggregate_patterns.get('edge_violation_percentage', 0)
                    edge_pct_change = final_edge_pct - initial_edge_pct
                    logger.debug(f"  Edge violations: {initial_edge_pct:.1f}% → {final_edge_pct:.1f}% ({edge_pct_change:+.1f}%)")
                    
                    # BRNG severity changes
                    if initial_brng.violations and brng_results.violations:
                        initial_max = max(v.brng_value for v in initial_brng.violations)
                        final_max = max(v.brng_value for v in brng_results.violations)
                        initial_avg = sum(v.brng_value for v in initial_brng.violations) / len(initial_brng.violations)
                        final_avg = sum(v.brng_value for v in brng_results.violations) / len(brng_results.violations)
                        
                        logger.debug(f"  Max BRNG: {initial_max:.2f}% → {final_max:.2f}%")
                        logger.debug(f"  Avg BRNG: {initial_avg:.2f}% → {final_avg:.2f}%")
                    
                    # Border adjustment status
                    initial_needs_adjustment = initial_brng.requires_border_adjustment
                    final_needs_adjustment = brng_results.requires_border_adjustment
                    if initial_needs_adjustment and not final_needs_adjustment:
                        logger.debug(f"  Border status: ✓ No longer requires adjustment")
                    elif not initial_needs_adjustment and final_needs_adjustment:
                        logger.debug(f"  Border status: ⚠ Now requires adjustment (unexpected)")
                    elif final_needs_adjustment:
                        logger.debug(f"  Border status: Still requires adjustment")
                
                logger.debug(f"{'='*60}\n")
        
        # Step 7: Dropped sample detection (conditional)
        dropped_sample_enabled = self._is_step_enabled(frame_config.enable_dropped_sample_detection)
        if self.check_cancelled():
            return results
        if dropped_sample_enabled:
            logger.info("Starting dropped sample detection...")
            if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
                self.signals.frame_analysis_progress.emit(0)

            dropped_sample_result = self._detect_dropped_samples(
                color_bars_end_time=color_bars_end_time
            )
            if dropped_sample_result:
                results['dropped_sample_detection'] = asdict(dropped_sample_result)

            if signals and frame_config.enable_dropped_sample_detection:
                signals.step_completed.emit("Frame Analysis - Dropped Sample Detection")
        else:
            logger.warning("Skipping dropped sample detection (disabled in config)\n")

        # Step 8: Duplicate frame detection (conditional)
        if self.check_cancelled():
            return results
        if duplicate_frame_enabled:
            if self.signals and hasattr(self.signals, 'frame_analysis_progress'):
                self.signals.frame_analysis_progress.emit(0)

            min_run_length = getattr(frame_config, 'duplicate_min_run_length', 2)
            duplicate_frame_result = self._detect_duplicate_frames(
                color_bars_end_time=color_bars_end_time,
                black_segments=black_segments,
                min_run_length=min_run_length,
            )
            if duplicate_frame_result:
                results['duplicate_frame_detection'] = asdict(duplicate_frame_result)

            if signals:
                signals.step_completed.emit("Frame Analysis - Duplicate Frame Detection")
        else:
            logger.warning("Skipping duplicate frame detection (disabled in config)\n")

        # Step 9: Generate comprehensive summary
        if self.check_cancelled():
            return results
        results['summary'] = self._generate_summary(results)
        
        # Save results
        self._save_results(results)
        
        return results

    def _detect_color_bars_duration(self) -> float:
        """Detect color bars duration from video or QCTools report"""
        # First check if qct-parse already detected color bars
        report_dir = self.video_path.parent / f"{self.video_id}_report_csvs"
        colorbars_csv = report_dir / "qct-parse_colorbars_durations.csv"
        
        if colorbars_csv.exists():
            import csv
            try:
                with open(colorbars_csv, 'r') as f:
                    reader = csv.reader(f)
                    rows = list(reader)
                    if len(rows) >= 2 and "color bars found" in rows[0][0]:
                        # Parse end timestamp
                        end_str = rows[1][1] if len(rows[1]) > 1 else None
                        if end_str:
                            # Convert timestamp to seconds
                            parts = end_str.split(':')
                            if len(parts) == 3:
                                hours = int(parts[0])
                                minutes = int(parts[1])
                                seconds = float(parts[2])
                                return hours * 3600 + minutes * 60 + seconds
            except Exception as e:
                logger.warning(f"Could not parse color bars CSV: {e}")
        
        # If no qct-parse data, could implement direct detection here
        # For now, return 0 to indicate no color bars detected
        return 0
    
    def _is_meaningful_improvement(self, previous: BRNGAnalysisResult, 
                              current: BRNGAnalysisResult,
                              previous_area: Tuple = None,
                              current_area: Tuple = None) -> bool:
        """Check if refinement produced meaningful improvement or if further refinement is still warranted."""
        if not previous or not current:
            return False
        
        prev_violations = len(previous.violations)
        curr_violations = len(current.violations)
        
        # Original check: violation count dropped by 20%+
        if curr_violations < prev_violations * 0.8:
            return True
        
        # Original check: worst-case severity dropped by 20%+
        prev_worst = previous.violations[0].violation_percentage if previous.violations else 0
        curr_worst = current.violations[0].violation_percentage if current.violations else 0
        if curr_worst < prev_worst * 0.8:
            return True
        
        # even if violation count didn't drop (or increased).
        # A high edge % means the border is still cutting through violation regions.
        curr_edge_pct = current.aggregate_patterns.get('edge_violation_percentage', 0)
        prev_edge_pct = previous.aggregate_patterns.get('edge_violation_percentage', 0)
        
        if curr_edge_pct > 50:
            # Edge % still dominant — only stop if borders didn't actually move
            if previous_area and current_area:
                width_change = abs(current_area[2] - previous_area[2])
                height_change = abs(current_area[3] - previous_area[3])
                border_moved = (width_change > 0 or height_change > 0)
                if border_moved:
                    logger.debug(f"  Edge violations still high ({curr_edge_pct:.1f}%) "
                            f"and border moved — continuing refinement")
                    return True
                else:
                    logger.debug(f"  Edge violations high ({curr_edge_pct:.1f}%) "
                            f"but border didn't move — stopping refinement")
                    return False
            # No area info available, but edge % is very high — keep trying
            return True
        
        if prev_edge_pct > 0 and curr_edge_pct < prev_edge_pct * 0.7:
            logger.debug(f"  Edge violation % dropped: {prev_edge_pct:.1f}% → {curr_edge_pct:.1f}%")
            return True
        
        return False
    
    def _create_refinement_comparison(self, initial_borders, final_borders, 
                                 initial_brng, final_brng, output_path, 
                                 refinement_history):
        """
        Create a comparison visualization showing before and after refinement.
        
        This creates a side-by-side comparison showing:
        - Left: Initial border detection
        - Right: Final refined borders
        - Annotations showing the changes and improvements
        
        Args:
            initial_borders: BorderDetectionResult before refinement
            final_borders: BorderDetectionResult after refinement
            initial_brng: BRNGAnalysisResult before refinement
            final_brng: BRNGAnalysisResult after refinement
            output_path: Path to save the comparison image
            refinement_history: List of iteration data dictionaries
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
        
        # Get a good frame for visualization
        frame = self.border_detector.find_good_representative_frame(
            target_time=150,
            search_window=120
        )
        
        if frame is None:
            logger.warning("Could not find frame for refinement comparison")
            return False
        
        # Create figure with 2 columns
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 10))
        
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # LEFT: Initial borders
        ax1.imshow(frame_rgb)
        ax1.set_title('Initial Border Detection', fontsize=14, weight='bold')
        ax1.axis('off')
        
        x1, y1, w1, h1 = initial_borders.active_area
        
        if initial_borders.active_area:
            # Draw initial borders in red
            if x1 > 10:
                left_rect = patches.Rectangle((0, 0), x1, self.border_detector.height,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3,
                                            label='Initial Borders')
                ax1.add_patch(left_rect)
            
            if x1 + w1 < self.border_detector.width - 10:
                right_rect = patches.Rectangle((x1 + w1, 0), 
                                            self.border_detector.width - (x1 + w1), 
                                            self.border_detector.height,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax1.add_patch(right_rect)
            
            if y1 > 10:
                top_rect = patches.Rectangle((0, 0), self.border_detector.width, y1,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax1.add_patch(top_rect)
            
            if y1 + h1 < self.border_detector.height - 10:
                bottom_rect = patches.Rectangle((0, y1 + h1), 
                                            self.border_detector.width, 
                                            self.border_detector.height - (y1 + h1),
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax1.add_patch(bottom_rect)
            
            # Draw active area outline in green
            active_rect = patches.Rectangle((x1, y1), w1, h1,
                                        linewidth=3, edgecolor='green', 
                                        facecolor='none',
                                        label='Active Area')
            ax1.add_patch(active_rect)
            ax1.legend(loc='upper right')
        
        # RIGHT: Final borders
        ax2.imshow(frame_rgb)
        ax2.set_title('After Refinement', fontsize=14, weight='bold')
        ax2.axis('off')
        
        if final_borders.active_area:
            x2, y2, w2, h2 = final_borders.active_area
            
            # Draw final borders in red
            if x2 > 10:
                left_rect = patches.Rectangle((0, 0), x2, self.border_detector.height,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3,
                                            label='Refined Borders')
                ax2.add_patch(left_rect)
            
            if x2 + w2 < self.border_detector.width - 10:
                right_rect = patches.Rectangle((x2 + w2, 0), 
                                            self.border_detector.width - (x2 + w2), 
                                            self.border_detector.height,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax2.add_patch(right_rect)
            
            if y2 > 10:
                top_rect = patches.Rectangle((0, 0), self.border_detector.width, y2,
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax2.add_patch(top_rect)
            
            if y2 + h2 < self.border_detector.height - 10:
                bottom_rect = patches.Rectangle((0, y2 + h2), 
                                            self.border_detector.width, 
                                            self.border_detector.height - (y2 + h2),
                                            linewidth=2, edgecolor='red', 
                                            facecolor='red', alpha=0.3)
                ax2.add_patch(bottom_rect)
            
            # Draw active area outline in green
            active_rect = patches.Rectangle((x2, y2), w2, h2,
                                        linewidth=3, edgecolor='green', 
                                        facecolor='none',
                                        label='Active Area')
            ax2.add_patch(active_rect)
            
            # If borders changed significantly, draw arrows showing the change
            if abs(x2 - x1) > 5:  # Left border moved
                mid_y = self.border_detector.height // 2
                if x2 < x1:  # Border expanded left
                    ax2.annotate('', xy=(x2, mid_y), xytext=(x1, mid_y),
                            arrowprops=dict(arrowstyle='<-', color='cyan', lw=3))
                else:  # Border contracted right
                    ax2.annotate('', xy=(x2, mid_y), xytext=(x1, mid_y),
                            arrowprops=dict(arrowstyle='->', color='cyan', lw=3))
            
            ax2.legend(loc='upper right')
        
        # Add comprehensive comparison text
        comparison_text = self._format_refinement_comparison_text(
            initial_borders, final_borders, initial_brng, final_brng, refinement_history
        )
        
        # Add text box with comparison details
        fig.text(0.5, 0.02, comparison_text, ha='center', fontsize=10,
                bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
        
        # Add main title
        iterations = len(refinement_history)
        title = f'Border Refinement: {iterations} Iteration{"s" if iterations != 1 else ""}'
        fig.text(0.5, 0.97, title, ha='center', fontsize=16, weight='bold')
        
        plt.tight_layout(rect=[0, 0.06, 1, 0.96])
        plt.savefig(output_path, dpi=100, bbox_inches='tight')
        plt.close()
        
        return True


    def _format_refinement_comparison_text(self, initial_borders, final_borders, 
                                        initial_brng, final_brng, refinement_history):
        """
        Format the comparison text for the refinement visualization.
        
        Returns:
            Formatted string with comparison details
        """
        x1, y1, w1, h1 = initial_borders.active_area
        x2, y2, w2, h2 = final_borders.active_area
        
        # Calculate changes
        width_change = w2 - w1
        height_change = h2 - h1
        left_change = x2 - x1
        right_change = (self.border_detector.width - (x2 + w2)) - (self.border_detector.width - (x1 + w1))
        top_change = y2 - y1
        bottom_change = (self.border_detector.height - (y2 + h2)) - (self.border_detector.height - (y1 + h1))
        
        # BRNG violations
        initial_violations = len(initial_brng.violations) if initial_brng.violations else 0
        final_violations = len(final_brng.violations) if final_brng.violations else 0
        violation_reduction = initial_violations - final_violations
        
        # Build text
        lines = []
        lines.append(f"Active Area: {w1}x{h1} → {w2}x{h2} (Δ width: {width_change:+d}px, Δ height: {height_change:+d}px)")
        
        # Border changes
        border_changes = []
        if abs(left_change) > 2:
            direction = "expanded" if left_change < 0 else "contracted"
            border_changes.append(f"Left {direction} {abs(left_change)}px")
        if abs(right_change) > 2:
            direction = "expanded" if right_change > 0 else "contracted"
            border_changes.append(f"Right {direction} {abs(right_change)}px")
        if abs(top_change) > 2:
            direction = "expanded" if top_change < 0 else "contracted"
            border_changes.append(f"Top {direction} {abs(top_change)}px")
        if abs(bottom_change) > 2:
            direction = "expanded" if bottom_change > 0 else "contracted"
            border_changes.append(f"Bottom {direction} {abs(bottom_change)}px")
        
        if border_changes:
            lines.append(f"Border Changes: {', '.join(border_changes)}")
        else:
            lines.append("Border Changes: None")
        
        # Violation improvement
        lines.append(f"BRNG Violations: {initial_violations} → {final_violations} ({violation_reduction:+d})")
        
        if initial_violations > 0:
            improvement_pct = (violation_reduction / initial_violations) * 100
            lines.append(f"Improvement: {improvement_pct:.1f}%")
        
        # Edge violation percentages
        initial_edge_pct = initial_brng.aggregate_patterns.get('edge_violation_percentage', 0)
        final_edge_pct = final_brng.aggregate_patterns.get('edge_violation_percentage', 0)
        lines.append(f"Edge Violations: {initial_edge_pct:.1f}% → {final_edge_pct:.1f}%")
        
        return " | ".join(lines)
    
    def _generate_summary(self, results: Dict) -> str:
        """Generate comprehensive human-readable summary with specific details"""
        lines = []
        lines.append(f"Enhanced Frame Analysis Summary - {self.video_id}")
        lines.append("=" * 60)
        
        # QCTools status
        if results['qctools_report_available']:
            lines.append(f"✓ QCTools report found")
            if 'qctools_violations_found' in results:
                violations = results['qctools_violations_found']
                if isinstance(violations, str):
                    lines.append(f"  {violations}")
                else:
                    lines.append(f"  Frames with BRNG > 0: {violations}")
        
        # Border detection with frame dimensions
        if 'initial_borders' in results:
            borders = results.get('final_borders', results['initial_borders'])
            if borders['active_area']:
                x, y, w, h = borders['active_area']
                lines.append(f"\nBorder Detection:")
                lines.append(f"  Active area: {w}x{h} at ({x},{y})")
                lines.append(f"  Method: {borders['detection_method']}")
                # Calculate border widths
                frame_w, frame_h = 720, 486  # Standard NTSC
                left_border = x
                right_border = frame_w - (x + w)
                top_border = y
                bottom_border = frame_h - (y + h)
                lines.append(f"  Borders: L:{left_border}px R:{right_border}px T:{top_border}px B:{bottom_border}px")
        
        # BRNG analysis statistics
        if results.get('brng_analysis'):
            brng = results.get('final_brng_analysis', results['brng_analysis'])
            stats = brng.get('actionable_report', {}).get('summary_statistics', {})
            aggregate = brng.get('aggregate_patterns', {})
            
            lines.append(f"\nBRNG Analysis:")
            lines.append(f"  Frames analyzed: {stats.get('total_violations', 0)}")
            lines.append(f"  Average BRNG: {stats.get('average_violation_percentage', 0):.2f}%")
            lines.append(f"  Maximum BRNG: {stats.get('max_violation_percentage', 0):.2f}%")
            edge_pct = aggregate.get('edge_violation_percentage', 0)
            continuous_pct = aggregate.get('continuous_edge_percentage', 0)
            lines.append(f"  Edge violations (any): {edge_pct:.1f}% of analyzed frames")
            lines.append(f"  Edge violations (solid line): {continuous_pct:.1f}% of analyzed frames")
            if continuous_pct == 0 and edge_pct > 95:
                lines.append(f"    → Violations are scattered rather than forming a solid line")
            
            # Diagnostic breakdown
            if brng.get('violations'):
                diagnostic_counts = {}
                for v in brng['violations']:
                    if isinstance(v, dict) and v.get('diagnostics'):
                        for diag in v['diagnostics']:
                            if diag.startswith("Edge artifacts"):
                                diagnostic_counts["Edge artifacts"] = diagnostic_counts.get("Edge artifacts", 0) + 1
                            elif diag != "Border adjustment recommended":
                                diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1
                
                if diagnostic_counts:
                    lines.append(f"  Violation types:")
                    for diag, count in sorted(diagnostic_counts.items(), key=lambda x: x[1], reverse=True):
                        lines.append(f"    {diag}: {count} frames")
        
        # Signalstats comparison
        if results.get('signalstats'):
            stats = results['signalstats']
            lines.append(f"\nSignalstats (active area analysis):")
            lines.append(f"  Frames with violations: {stats['violation_percentage']:.1f}%")
            lines.append(f"  Max BRNG: {stats['max_brng']:.2f}%")
            lines.append(f"  Avg BRNG: {stats['avg_brng']:.2f}%")
            
            # Add analysis period info
            periods = stats.get('analysis_periods', [])
            if periods:
                period_strs = [f"{p[0]:.1f}s-{p[0]+p[1]:.1f}s" for p in periods]
                lines.append(f"  Analysis periods: {', '.join(period_strs)}")
        
        # Refinement info
        if 'refinement_iterations' in results and results['refinement_iterations'] > 0:
            lines.append(f"\nBorder refinement performed: {results['refinement_iterations']} iteration(s)")
            if 'initial_borders' in results and 'final_borders' in results:
                initial = results['initial_borders']['active_area']
                final = results['final_borders']['active_area']
                if initial and final:
                    w_change = final[2] - initial[2]
                    h_change = final[3] - initial[3]
                    lines.append(f"  Size change: width {w_change:+d}px, height {h_change:+d}px")
        
        return "\n".join(lines)
    
    def _log_brng_analysis_summary(self, brng_results: BRNGAnalysisResult, 
                                analysis_periods: List[Tuple[float, int]]) -> None:
        """Log a comprehensive summary of BRNG analysis results."""
        if not brng_results or not brng_results.violations:
            logger.info("\n  === BRNG Frame Analysis Summary ===")
            logger.info("  No BRNG violations detected in analyzed periods.\n")
            return
        
        violations = brng_results.violations
        aggregate = brng_results.aggregate_patterns
        report = brng_results.actionable_report
        stats = report.get('summary_statistics', {})
        
        # Calculate time range
        if analysis_periods:
            time_start = min(p[0] for p in analysis_periods)
            time_end = max(p[0] + p[1] for p in analysis_periods)
            time_range_str = f"{time_start:.1f}s - {time_end:.1f}s"
        else:
            time_range_str = "N/A"
        
        logger.info(f"\n  === BRNG Frame Analysis Summary ===")
        logger.info(f"  Analyzed {len(violations)} frames across {len(analysis_periods)} periods ({time_range_str})\n")
        
        # Aggregate diagnostic types from all violations
        diagnostic_counts = {}
        edge_artifact_edges = set()
        
        for v in violations:
            if v.diagnostics:
                for diag in v.diagnostics:
                    # Normalize edge artifact messages
                    if diag.startswith("Edge artifacts"):
                        diagnostic_counts["Edge artifacts"] = diagnostic_counts.get("Edge artifacts", 0) + 1
                        # Extract edge names from message like "Edge artifacts (left, right)"
                        if "(" in diag and ")" in diag:
                            edges_str = diag[diag.find("(")+1:diag.find(")")]
                            for edge in edges_str.split(", "):
                                edge_artifact_edges.add(edge.strip())
                    elif diag == "Border adjustment recommended":
                        continue
                    else:
                        diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1
        
        # Log diagnostic types
        logger.info("  Violation Types Detected:")
        total_violations = len(violations)
        
        # Order diagnostics by relevance
        priority_order = ["Sub-black detected", "Highlight clipping", "Edge artifacts", 
                        "Linear blanking patterns", 
                        "General broadcast range violations"]
        
        logged_any = False
        for diag_type in priority_order:
            if diag_type in diagnostic_counts:
                count = diagnostic_counts[diag_type]
                pct = (count / total_violations) * 100
                
                if diag_type == "Edge artifacts" and edge_artifact_edges:
                    edges_str = ", ".join(sorted(edge_artifact_edges))
                    logger.info(f"    • {diag_type} ({edges_str}): {count} frames ({pct:.1f}%)")
                else:
                    logger.info(f"    • {diag_type}: {count} frames ({pct:.1f}%)")
                logged_any = True
        
        # Log any remaining diagnostics not in priority order
        for diag_type, count in diagnostic_counts.items():
            if diag_type not in priority_order:
                pct = (count / total_violations) * 100
                logger.info(f"    • {diag_type}: {count} frames ({pct:.1f}%)")
                logged_any = True
        
        if not logged_any:
            logger.info("    • No specific diagnostic patterns identified")
        
        # Add warning if edge percentage is high
        edge_pct = aggregate.get('edge_violation_percentage', 0)
        if edge_pct > 50:
            logger.info(f"    ⚠ High edge percentage ({edge_pct:.1f}%) suggests border detection needs adjustment")
        
       # Log violation distribution statistics
        logger.info(f"\n  Violation Statistics:")
        logger.info(f"    Average BRNG: {stats.get('average_violation_percentage', 0):.2f}%")
        logger.info(f"    Maximum BRNG: {stats.get('max_violation_percentage', 0):.2f}%")
        continuous_pct = aggregate.get('continuous_edge_percentage', 0)
        logger.info(f"    Edge violations (any): {edge_pct:.1f}% of analyzed frames")
        logger.info(f"    Edge violations (solid line): {continuous_pct:.1f}% of analyzed frames")
        if continuous_pct == 0 and edge_pct > 95:
            logger.info(f"      → Violations are scattered rather than forming a solid line")
        
        linear_pct = aggregate.get('linear_pattern_percentage', 0)
        if linear_pct > 0:
            logger.info(f"    Linear patterns: {linear_pct:.1f}% of analyzed frames")
        
        logger.info("")  # Blank line for spacing


    def _log_analysis_correlation(self, signalstats_results: SignalstatsResult,
                                brng_results: BRNGAnalysisResult) -> None:
        """Log the correlation between signalstats and BRNG analysis results."""
        if not signalstats_results or not brng_results:
            return
        
        logger.info("  === Analysis Correlation ===\n")
        
        # Signalstats summary
        logger.info("  Signalstats (quantitative full-frame vs active-area comparison):")
        logger.info(f"    Active area violations: {signalstats_results.violation_percentage:.1f}% of frames")
        logger.info(f"    Max BRNG in active area: {signalstats_results.max_brng:.2f}%")
        logger.info(f"    Diagnosis: {signalstats_results.diagnosis}\n")
        
        # BRNG analysis summary
        aggregate = brng_results.aggregate_patterns
        edge_pct = aggregate.get('edge_violation_percentage', 0)
        
        # Determine dominant diagnostic from violations
        diagnostic_counts = {}
        for v in brng_results.violations:
            if v.diagnostics:
                for diag in v.diagnostics:
                    if diag.startswith("Edge artifacts"):
                        diag = "Edge artifacts"
                    elif diag == "Border adjustment recommended":
                        continue  # Skip meta-diagnostics
                    diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1
        
        dominant_diag = max(diagnostic_counts.items(), key=lambda x: x[1])[0] if diagnostic_counts else "Unknown"
        
        logger.info("  BRNG Analysis (qualitative frame inspection):")
        continuous_pct = aggregate.get('continuous_edge_percentage', 0)
        logger.info(f"    Edge violations (any): {edge_pct:.1f}%")
        logger.info(f"    Edge violations (solid line): {continuous_pct:.1f}%")
        if continuous_pct == 0 and edge_pct > 95:
            logger.info(f"      → Violations are scattered rather than forming a solid line")
        logger.info(f"    Dominant diagnostic: {dominant_diag}\n")
        
        # Interpretation
        logger.info("  Interpretation:")
        
        # Determine agreement between methods
        signalstats_says_border = "border" in signalstats_results.diagnosis.lower()
        signalstats_says_content = "active" in signalstats_results.diagnosis.lower() and "requires" in signalstats_results.diagnosis.lower()
        brng_says_border = brng_results.requires_border_adjustment or edge_pct > 50
        brng_says_content = not brng_says_border and edge_pct < 30
        
        if signalstats_says_border and brng_says_border:
            logger.info("    ✓ Both methods agree: violations are concentrated at frame edges")
            logger.info("      → Border detection likely missed some blanking areas")
            logger.info("      → Active picture content appears broadcast-safe once borders are corrected\n")
        elif signalstats_says_content and brng_says_content:
            logger.info("    ✓ Both methods agree: violations are in the active picture area")
            logger.info("      → Content itself has broadcast range issues")
            logger.info("      → Review source material or encoding parameters\n")
        elif signalstats_says_content and brng_says_border:
            logger.info("    ⚠ Methods show mixed results:")
            logger.info("      → Signalstats: active area has violations")
            logger.info("      → BRNG analysis: high edge violation percentage")
            logger.info("      → Both content issues and border detection may need attention\n")
        elif signalstats_says_border and brng_says_content:
            logger.info("    ⚠ Methods show mixed results:")
            logger.info("      → Signalstats: border areas have more violations")
            logger.info("      → BRNG analysis: violations spread throughout frame")
            logger.info("      → Review thumbnails to determine actual issue location\n")
        else:
            # Default case
            if edge_pct > 50:
                logger.info("    → High edge violation percentage suggests border issues")
            elif edge_pct < 20:
                logger.info("    → Low edge percentage suggests content-based violations")
            else:
                logger.info("    → Mixed violation distribution - review thumbnails for details")
            logger.info("")
    
    def _build_upstream_context(self, signalstats_results: SignalstatsResult,
                                border_results: BorderDetectionResult) -> UpstreamAnalysisContext:
        """
        Package findings from border detection and signalstats into a context
        object that informs BRNG analyzer decisions.
        """
        period_diagnoses = {}
        period_active_brng = {}
        period_full_brng = {}
        
        for comp in (signalstats_results.comparison_results or []):
            idx = comp.get('period', 1) - 1  # 0-indexed
            period_diagnoses[idx] = comp.get('diagnosis', '')
            
            ff_data = comp.get('ffprobe_active_area', {})
            qc_data = comp.get('qctools_full_frame', {})
            
            period_active_brng[idx] = {
                'max_brng': ff_data.get('max_brng', 0),
                'violation_pct': ff_data.get('violations_pct', 0)
            }
            period_full_brng[idx] = {
                'max_brng': qc_data.get('max_brng', 0),
                'violation_pct': qc_data.get('violations_pct', 0)
            }
        
        # Calculate border violation fraction from full-frame vs active-area delta
        total_full = sum(d.get('violation_pct', 0) for d in period_full_brng.values())
        total_active = sum(d.get('violation_pct', 0) for d in period_active_brng.values())
        border_fraction = ((total_full - total_active) / total_full) if total_full > 0 else 0.0
        
        # Extract border widths from active area
        active = border_results.active_area  # (x, y, w, h)
        border_widths = {
            'left': active[0],
            'right': max(0, self.border_detector.width - active[0] - active[2]),
            'top': active[1],
            'bottom': max(0, self.border_detector.height - active[1] - active[3])
        }
        
        context = UpstreamAnalysisContext(
            period_diagnoses=period_diagnoses,
            period_active_area_brng=period_active_brng,
            period_full_frame_brng=period_full_brng,
            avg_active_area_brng=signalstats_results.avg_brng,
            overall_diagnosis=signalstats_results.diagnosis,
            head_switching=border_results.head_switching_artifacts,
            border_widths=border_widths,
            border_violation_fraction=max(0.0, min(1.0, border_fraction))
        )
        
        logger.debug(f"  Built upstream context for BRNG analyzer:")
        logger.debug(f"    Period diagnoses: {period_diagnoses}")
        logger.debug(f"    Border violation fraction: {border_fraction:.2f}")
        if border_results.head_switching_artifacts:
            logger.debug(f"    Head switching: detected")
        
        return context
    
    def _refine_periods_from_signalstats(self, current_periods, signalstats_results,
                                          qctools_candidate_periods, black_segments,
                                          period_duration, color_bars_end_time):
        """
        Replace low-value periods with better candidates based on signalstats findings.
        
        A period is "low-value" for BRNG analysis if signalstats diagnosed it as 
        'minimal_violations' with near-zero active-area BRNG — meaning there's 
        nothing meaningful for the differential detector to find there.
        """
        comparison_results = signalstats_results.comparison_results or []
        
        if not comparison_results:
            return current_periods
        
        # Score each current period by how useful it is for BRNG analysis
        period_scores = []
        for i, (start, dur) in enumerate(current_periods):
            comp = comparison_results[i] if i < len(comparison_results) else {}
            diagnosis = comp.get('diagnosis', '')
            
            ff_data = comp.get('ffprobe_active_area', {})
            active_pct = ff_data.get('violations_pct', 0)
            
            # Score: content_violations > border_violations > minimal
            if diagnosis == 'content_violations':
                score = 100 + active_pct
            elif diagnosis == 'border_violations':
                score = 50 + active_pct
            else:
                score = active_pct
            
            period_scores.append((i, score, start, dur))
        
        # Find periods worth replacing (score < 5 means essentially no active-area signal)
        replaceable = [(i, s, start, dur) for i, s, start, dur in period_scores if s < 5]
        
        if not replaceable:
            return current_periods
        
        # Find candidate replacement periods from QCTools that aren't already selected
        current_ranges = [(start, start + dur) for start, dur in current_periods]
        
        candidates = []
        for qct_start, qct_dur in qctools_candidate_periods:
            overlaps = False
            for cs, ce in current_ranges:
                if not (qct_start + qct_dur < cs or qct_start > ce):
                    overlaps = True
                    break
            if not overlaps:
                candidates.append((qct_start, qct_dur))
        
        if not candidates:
            return current_periods
        
        # Replace low-value periods with candidates
        refined = list(current_periods)
        replacements_made = 0
        for (idx, _, old_start, _), candidate in zip(replaceable, candidates):
            refined[idx] = candidate
            replacements_made += 1
            logger.info(f"  Replaced period {idx+1} ({old_start:.1f}s, minimal active-area BRNG) "
                       f"with QCTools candidate at {candidate[0]:.1f}s")
        
        # Validate replacements against black segments
        if black_segments and replacements_made > 0:
            refined = self.signalstats_analyzer._validate_periods_against_black_segments(
                refined, black_segments,
                effective_start=(color_bars_end_time or 0) + 10,
                period_duration=period_duration
            )
        
        if replacements_made > 0:
            logger.info(f"  Refined {replacements_made} analysis period(s) based on signalstats findings\n")
        
        return refined

    def _save_results(self, results: Dict):
        """Save analysis results to JSON"""
        import copy
        output_file = self.output_dir / f"{self.video_id}_enhanced_frame_analysis.json"
        
        # Deep copy to avoid modifying the original
        results_copy = copy.deepcopy(results)
        
        # Convert any remaining non-serializable objects
        def clean_for_json(obj, seen=None):
            if seen is None:
                seen = set()
            
            # Check for circular reference
            obj_id = id(obj)
            if obj_id in seen:
                return None  # or return a placeholder like "<circular reference>"
            
            if isinstance(obj, (str, int, float, bool, type(None))):
                return obj
            
            seen.add(obj_id)
            
            if isinstance(obj, dict):
                return {k: clean_for_json(v, seen.copy()) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return [clean_for_json(item, seen.copy()) for item in obj]
            elif hasattr(obj, '__dict__'):
                return clean_for_json(obj.__dict__, seen.copy())
            else:
                return str(obj)
        
        cleaned_results = clean_for_json(results_copy)
        
        with open(output_file, 'w') as f:
            json.dump(cleaned_results, f, indent=2)
        
        logger.info(f"Results saved to: {output_file}\n")

    def _analyze_qctools_violation_distribution(self, violations: List[FrameViolation],
                                                num_periods: int = 3,
                                                period_duration: int = 60,
                                                video_duration: float = None,
                                                black_segments: List[Tuple[float, float]] = None) -> List[Tuple[float, int]]:
        """
        Analyze the temporal distribution of QCTools violations and suggest analysis periods.

        Args:
            violations: List of frame violations to analyze
            num_periods: Number of analysis periods to generate
            period_duration: Duration of each period in seconds
            video_duration: Total video duration; periods are clamped so they
                never extend past the end of the file
            black_segments: Known all-black segments; violations inside them
                (noise spikes that escape the per-frame black classifier) are
                excluded so period selection doesn't target unwatchable content

        Returns:
            List of (start_time, duration) tuples for suggested periods
        """
        if black_segments and violations:
            filtered = [
                v for v in violations
                if not any(bs <= v.timestamp <= be for bs, be in black_segments)
            ]
            excluded = len(violations) - len(filtered)
            if excluded:
                logger.debug(f"  Excluded {excluded} violations inside black segments")
            violations = filtered

        if not violations:
            logger.info("  No QCTools violations to analyze distribution")
            return []

        # Never suggest a period longer than the video itself
        if video_duration and period_duration > video_duration:
            period_duration = max(1, int(video_duration))
        
        # Get all violation timestamps
        timestamps = [v.timestamp for v in violations]
        timestamps.sort()
        
        # Log the overall distribution
        logger.info(f"\n  === QCTools Violation Distribution ===")
        logger.debug(f"  Total violations found: {len(violations)}")
        logger.debug(f"  Time range: {timestamps[0]:.1f}s - {timestamps[-1]:.1f}s")
        
        # Create a histogram of violations over time (10-second bins)
        bin_size = 10.0
        num_bins = int((timestamps[-1] - timestamps[0]) / bin_size) + 1
        bins = [0] * num_bins
        
        for ts in timestamps:
            bin_idx = int((ts - timestamps[0]) / bin_size)
            if 0 <= bin_idx < num_bins:
                bins[bin_idx] += 1
        
        # Find the top periods with most violations
        bin_scores = []
        for i, count in enumerate(bins):
            if count > 0:
                start_time = timestamps[0] + i * bin_size
                bin_scores.append((start_time, count))
        
        # Sort by violation count
        bin_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Log the top bins
        logger.debug(f"  Top 10-second bins with violations:")
        for i, (start_time, count) in enumerate(bin_scores[:10]):
            logger.debug(f"    {i+1}. {start_time:.1f}s - {start_time+bin_size:.1f}s: {count} violations")
        
        # Select up to num_periods non-overlapping periods, ranked by violation density
        suggested_periods = []
        used_ranges = []
        
        for start_time, count in bin_scores:
            # Clamp so the period ends at or before the end of the video
            if video_duration:
                start_time = max(0.0, min(start_time, video_duration - period_duration))

            # Check if this period overlaps with any already selected
            overlaps = False
            for used_start, used_end in used_ranges:
                if not (start_time + period_duration < used_start or start_time > used_end):
                    overlaps = True
                    break
            
            if not overlaps:
                suggested_periods.append((start_time, period_duration))
                used_ranges.append((start_time, start_time + period_duration))
                
                if len(suggested_periods) >= num_periods:
                    break
        
        logger.debug(f"\n  QCTools-based analysis periods ({len(suggested_periods)} of {num_periods} requested):")
        for i, (start, duration) in enumerate(suggested_periods):
            logger.debug(f"    Period {i+1}: {start:.1f}s - {start+duration:.1f}s")
        
        return suggested_periods


def analyze_frame_quality(video_path: str, 
                         border_data_path: str = None,
                         output_dir: str = None,
                         frame_config: 'FrameAnalysisConfig' = None,
                         color_bars_end_time: float = None,
                         signals = None,
                         check_cancelled = None) -> Dict:
    """
    Main entry point for frame analysis from processing_mgmt.
    
    Args:
        video_path: Path to video file
        border_data_path: Optional path to existing border data JSON
        output_dir: Output directory for results
        frame_config: FrameAnalysisConfig dataclass with analysis parameters
        color_bars_end_time: End time of color bars if detected
        signals: Optional signals object for emitting step completion events
        check_cancelled: Optional callable returning True if processing should stop
    
    Returns:
        Complete analysis results dictionary
    """
    check_cancelled = check_cancelled or (lambda: False)
    
    # Use config dataclass directly
    if frame_config is None:
        # Use defaults if no config provided
        from AV_Spex.utils.config_setup import FrameAnalysisConfig
        frame_config = FrameAnalysisConfig()
    
    # Extract parameters directly from dataclass
    method = frame_config.border_detection_mode
    duration_limit = frame_config.brng_duration_limit
    skip_color_bars = bool(frame_config.brng_skip_color_bars)
    max_refinements = frame_config.max_border_retries
    
    if check_cancelled():
        return None
    
    # Run enhanced analysis
    analyzer = EnhancedFrameAnalysis(video_path, output_dir, signals=signals,
                                     check_cancelled_fn=check_cancelled)
    
    # Load existing border data if provided
    if border_data_path and Path(border_data_path).exists():
        with open(border_data_path, 'r') as f:
            border_data = json.load(f)
            # Would need to convert this to BorderDetectionResult
            # For now, proceed with fresh analysis
    
    if check_cancelled():
        return None
    
    results = analyzer.analyze(
        method=method,
        duration_limit=duration_limit,
        skip_color_bars=skip_color_bars,
        max_refinement_iterations=max_refinements,
        color_bars_end_time=color_bars_end_time,
        signals=signals
    )
    
    return results


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python enhanced_frame_analysis.py <video_file> [output_dir]")
        sys.exit(1)
    
    video_file = sys.argv[1]
    output_directory = sys.argv[2] if len(sys.argv) > 2 else None
    
    results = analyze_frame_quality(video_file, output_dir=output_directory)
    print(f"\nAnalysis complete for {video_file}")