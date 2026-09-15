"""Tests for checks.frame_analysis.

Module is large (6037 LOC). This file targets the pure-logic surfaces and
small subprocess wrappers; the heavy cv2/ffmpeg paths are exercised
indirectly through orchestrator-level tests with everything mocked.

Coverage:
* All 8 dataclasses (FrameViolation, BorderDetectionResult, BRNGAnalysisResult,
  SignalstatsResult, DroppedSampleResult, DuplicateFrameRun, DuplicateFrameResult,
  UpstreamAnalysisContext)
* QCToolsParser
  - _detect_bit_depth (gz + plain, success + crash-fallback)
  - _extract_frame_violations (BRNG threshold, black-frame skip, missing tags)
  - _process_violation_buffer
  - parse_brng_period (time-window, every-frame share, black-frame skip)
  - detect_black_segments (min_duration + gap_tolerance + end-of-file flush)
  - find_duplicate_frame_candidates (min_run_length + color-bars/black exclusions)
* IntegratedSignalstatsAnalyzer pure-logic helpers
  - _seconds_to_timecode
  - _should_use_qctools (None + dict)
  - _validate_periods_against_black_segments (overlap thresholds)
  - _shift_period_away_from_black (search bounds)
* EnhancedFrameAnalysis pure-logic helpers
  - _is_step_enabled (bool / yes-no / unknown fallback)
* analyze_frame_quality entry point (default config + cancel + delegation)
"""

import gzip
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from types import SimpleNamespace

import numpy as np
import pytest

from AV_Spex.checks import frame_analysis as fa


# ===========================================================================
# Test fixtures: synthetic QCTools XML
# ===========================================================================

def _qctools_xml(frames):
    """Build a minimal qctools-shaped XML document.

    `frames` is a list of dicts. Each dict can contain:
      pkt_pts_time (str): timestamp; default str(idx)
      tags (dict[str, str]): {key_suffix → attribute value}, e.g.
                              {"YMAX": "940", "BRNG": "0.05"}
        Tag elements use BOTH attribute style (`value="..."`) and text content
        so they exercise both `.get('value')` and `findtext()` consumers.
    """
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<ffprobe>',
           '  <frames>']
    for idx, f in enumerate(frames):
        ts = f.get("pkt_pts_time", str(idx))
        out.append(f'    <frame media_type="video" pkt_pts_time="{ts}" n="{idx}">')
        for key, value in f.get("tags", {}).items():
            full_key = f"lavfi.signalstats.{key}"
            out.append(f'      <tag key="{full_key}" value="{value}">{value}</tag>')
        out.append('    </frame>')
    out.append('  </frames>')
    out.append('</ffprobe>')
    return "\n".join(out)


def _write_qctools(tmp_path, frames, *, gz=False, name="report.xml"):
    """Materialize a qctools XML file (optionally gzipped) for parser tests."""
    text = _qctools_xml(frames)
    path = tmp_path / (name + (".gz" if gz else ""))
    if gz:
        with gzip.open(path, "wt") as f:
            f.write(text)
    else:
        path.write_text(text)
    return str(path)


# Black frame thresholds used by _extract_frame_violations:
#   YMAX < 300, YHIGH < 115, YLOW < 97, YMIN < 6.5
_BLACK_TAGS = {"YMAX": "200", "YHIGH": "100", "YLOW": "50", "YMIN": "5"}
_NORMAL_TAGS = {"YMAX": "940", "YHIGH": "180", "YLOW": "100", "YMIN": "30"}


# ===========================================================================
# Section 1 — Dataclasses
# ===========================================================================

def test_frame_violation_defaults():
    fv = fa.FrameViolation(frame_num=10, timestamp=0.5, brng_value=12.5, violation_score=0.125)
    assert fv.frame_num == 10
    assert fv.timestamp == 0.5
    assert fv.brng_value == 12.5
    assert fv.violation_score == 0.125
    assert fv.violation_pixels == 0
    assert fv.violation_percentage == 0.0
    assert fv.diagnostics is None
    assert fv.pattern_analysis is None


def test_border_detection_result_minimal():
    r = fa.BorderDetectionResult(
        active_area=(0, 3, 720, 480),
        border_regions={},
        detection_method="simple",
        quality_frame_hints=[],
    )
    assert r.head_switching_artifacts is None
    assert r.requires_refinement is False
    assert r.expansion_recommendations is None


def test_brng_analysis_result_minimal():
    r = fa.BRNGAnalysisResult(
        violations=[], aggregate_patterns={}, actionable_report={},
        thumbnails=[], requires_border_adjustment=False,
    )
    assert r.refinement_recommendations is None
    assert r.analysis_periods is None
    assert r.period_summaries is None


def test_signalstats_result_minimal():
    r = fa.SignalstatsResult(
        violation_percentage=1.0, max_brng=0.5, avg_brng=0.1,
        analysis_periods=[], diagnosis="ok", used_qctools=True,
    )
    assert r.comparison_results is None


def test_dropped_sample_result_defaults():
    r = fa.DroppedSampleResult(
        status="clean", message="", spike_count=0, duration_diff_ms=0.0,
        audio_duration=10.0, video_duration=10.0, combined_score=0.0,
    )
    assert r.estimated_loss_ms == 0.0
    assert r.sample_rate == 0
    assert r.spectrogram_path is None
    assert r.spike_timestamps is None


def test_duplicate_frame_run_required_fields():
    run = fa.DuplicateFrameRun(
        start_time=10.0, end_time=10.5, duplicate_count=15, frozen_frames=16,
        estimated_loss_seconds=0.5, avg_ydif=0.1, max_ydif=0.5, avg_udif=0.1,
        avg_vdif=0.1, avg_vrep=0.1, cv_mse=None, cv_verified=False,
    )
    assert run.first_frame_thumbnail is None
    assert run.last_frame_thumbnail is None


def test_duplicate_frame_result_optional_runs():
    r = fa.DuplicateFrameResult(
        status="clean", message="", total_runs=0, total_duplicate_frames=0,
        estimated_loss_seconds=0.0, bit_depth_10=False,
        ydif_threshold=1.0, udif_threshold=1.0, vdif_threshold=1.0,
        min_run_length=2,
    )
    assert r.runs is None


def test_upstream_analysis_context_defaults():
    ctx = fa.UpstreamAnalysisContext(
        period_diagnoses={0: "minimal_violations"},
        period_active_area_brng={0: {"max_brng": 0.0, "violation_pct": 0.0}},
        period_full_frame_brng={0: {"max_brng": 0.0, "violation_pct": 0.0}},
        avg_active_area_brng=0.0,
        overall_diagnosis="minimal_violations",
    )
    assert ctx.head_switching is None
    assert ctx.border_widths is None
    assert ctx.border_violation_fraction == 0.0


# ===========================================================================
# Section 2 — QCToolsParser
# ===========================================================================

# ---- _detect_bit_depth ----------------------------------------------------

def test_detect_bit_depth_10bit_ymax_high(tmp_path):
    """YMAX > 250 in first frame → bit_depth_10 = True."""
    path = _write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}])
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is True


def test_detect_bit_depth_8bit_low_ymax(tmp_path):
    """First-100 frames all have YMAX < 250 → bit_depth_10 = False."""
    frames = [{"tags": {"YMAX": "200"}}] * 5
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is False


def test_detect_bit_depth_handles_gzipped_report(tmp_path):
    path = _write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}], gz=True)
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is True


def test_detect_bit_depth_unreadable_returns_false():
    parser = fa.QCToolsParser("/no/such/path.xml")
    assert parser.bit_depth_10 is False


def test_detect_bit_depth_chroma_midpoint_overrides_dark_ymax(tmp_path):
    """UAVG ~512 → 10-bit scale, even when the file opens with black leader
    (YMAX never exceeds 250 in the scanned frames)."""
    frames = [{"tags": {"YMAX": "64", "UAVG": "512.3"}}] * 5
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is True


def test_detect_bit_depth_chroma_midpoint_8bit(tmp_path):
    """UAVG ~128 → 8-bit scale, decided from the first frame."""
    path = _write_qctools(tmp_path, [{"tags": {"YMAX": "200", "UAVG": "128.1"}}])
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is False


# ---- _extract_frame_violations -------------------------------------------

def _frame_elem(frame_dict):
    """Build an ElementTree <frame> from a dict in the same shape as fixtures."""
    import xml.etree.ElementTree as ET
    xml = ['<frame pkt_pts_time="{ts}" n="{n}">'.format(
        ts=frame_dict.get("pkt_pts_time", "0.0"),
        n=frame_dict.get("n", "0"))]
    for key, value in frame_dict.get("tags", {}).items():
        full_key = f"lavfi.signalstats.{key}"
        xml.append(f'  <tag key="{full_key}" value="{value}"/>')
    xml.append('</frame>')
    return ET.fromstring("\n".join(xml))


def test_extract_frame_violations_above_threshold_returns_violation(tmp_path):
    """BRNG > 0.01 produces a FrameViolation with brng_value scaled to %."""
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    elem = _frame_elem({"pkt_pts_time": "1.5", "tags": dict(_NORMAL_TAGS, BRNG="0.05")})
    fv = parser._extract_frame_violations(elem, frame_num=42)
    assert fv is not None
    assert fv.frame_num == 42
    assert fv.timestamp == 1.5
    assert fv.brng_value == pytest.approx(5.0)  # 0.05 * 100
    assert fv.violation_score == pytest.approx(0.05)


def test_extract_frame_violations_below_threshold_returns_none(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    elem = _frame_elem({"tags": dict(_NORMAL_TAGS, BRNG="0.005")})  # 0.5% — below 1%
    assert parser._extract_frame_violations(elem, frame_num=1) is None


def test_extract_frame_violations_skips_all_black_frames(tmp_path):
    """All-black frame (luma all below thresholds) returns None even if BRNG > 0.01."""
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    elem = _frame_elem({"tags": dict(_BLACK_TAGS, BRNG="0.5")})  # huge BRNG, but black
    assert parser._extract_frame_violations(elem, frame_num=5) is None


def test_extract_frame_violations_no_brng_tag_returns_none(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    elem = _frame_elem({"tags": _NORMAL_TAGS})  # no BRNG
    assert parser._extract_frame_violations(elem, frame_num=1) is None


def test_extract_frame_violations_falls_back_to_attribute_frame_num(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    elem = _frame_elem({"n": "99", "pkt_pts_time": "3.3", "tags": dict(_NORMAL_TAGS, BRNG="0.1")})
    fv = parser._extract_frame_violations(elem)
    assert fv is not None
    assert fv.frame_num == 99


def test_extract_frame_violations_uses_fps_when_no_pkt_pts_time(tmp_path):
    """Without pkt_pts_time, timestamp is computed from frame_num / fps."""
    import xml.etree.ElementTree as ET
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]), fps=30.0)
    # Manually build frame WITHOUT pkt_pts_time
    elem = ET.fromstring(
        '<frame n="60">'
        '  <tag key="lavfi.signalstats.YMAX" value="940"/>'
        '  <tag key="lavfi.signalstats.YHIGH" value="180"/>'
        '  <tag key="lavfi.signalstats.YLOW" value="100"/>'
        '  <tag key="lavfi.signalstats.YMIN" value="30"/>'
        '  <tag key="lavfi.signalstats.BRNG" value="0.05"/>'
        '</frame>'
    )
    fv = parser._extract_frame_violations(elem, frame_num=60)
    assert fv is not None
    assert fv.timestamp == pytest.approx(60 / 30.0)


# ---- _process_violation_buffer -------------------------------------------

def test_process_violation_buffer_filters_none(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [{"tags": {"YMAX": "940"}}]))
    fv = fa.FrameViolation(frame_num=1, timestamp=0.0, brng_value=2.0, violation_score=0.02)
    out = parser._process_violation_buffer([fv, None, fv])
    assert out == [fv, fv]


# ---- parse_brng_period ----------------------------------------------------

def test_parse_period_filters_by_time_window(tmp_path):
    """Only frames within [start_time, end_time] should be considered."""
    frames = [
        {"pkt_pts_time": "0.0", "tags": dict(_NORMAL_TAGS, BRNG="0.5")},   # before window
        {"pkt_pts_time": "10.0", "tags": dict(_NORMAL_TAGS, BRNG="0.1")},  # in window
        {"pkt_pts_time": "12.0", "tags": dict(_NORMAL_TAGS, BRNG="0.2")},  # in window
        {"pkt_pts_time": "30.0", "tags": dict(_NORMAL_TAGS, BRNG="0.3")},  # after window
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    out = parser.parse_brng_period(start_time=5.0, end_time=20.0, period_num=1)
    assert out['brng_frames'] == [(10.0, 0.1), (12.0, 0.2)]


def test_parse_period_share_counts_every_frame_not_just_violations(tmp_path):
    """The bug: only flagged frames were returned, and the share was computed
    over that list — so it read 100% whenever anything was flagged."""
    frames = (
        [{"pkt_pts_time": str(t), "tags": dict(_NORMAL_TAGS, BRNG="0.05")} for t in range(0, 2)] +
        [{"pkt_pts_time": str(t), "tags": dict(_NORMAL_TAGS, BRNG="0")} for t in range(2, 10)]
    )
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    out = parser.parse_brng_period(0.0, 100.0, period_num=1)
    assert out['frames_analyzed'] == 10
    assert out['frames_with_violations'] == 2
    assert len(out['brng_values']) == 10


def test_parse_period_flags_any_out_of_range_pixel(tmp_path):
    """Same rule as the active-area ffprobe pass (BRNG > 0), so the shares compare."""
    frames = [
        {"pkt_pts_time": "1.0", "tags": dict(_NORMAL_TAGS, BRNG="0.0001")},  # below the old 1% cutoff
        {"pkt_pts_time": "2.0", "tags": dict(_NORMAL_TAGS, BRNG="0")},
    ]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    assert parser.parse_brng_period(0.0, 100.0, period_num=1)['frames_with_violations'] == 1


def test_parse_period_skips_black_frames(tmp_path):
    frames = [
        {"pkt_pts_time": "1.0", "tags": dict(_BLACK_TAGS, BRNG="0.5")},
        {"pkt_pts_time": "2.0", "tags": dict(_NORMAL_TAGS, BRNG="0")},
    ]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    out = parser.parse_brng_period(0.0, 100.0, period_num=1)
    assert out['frames_analyzed'] == 1
    assert out['frames_with_violations'] == 0
    assert out['black_frames_skipped'] == 1


def test_parse_period_clean_period_is_data_not_none(tmp_path):
    """A period with zero violations is a measurement, not a missing result."""
    frames = [{"pkt_pts_time": str(t), "tags": dict(_NORMAL_TAGS, BRNG="0")} for t in range(5)]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    out = parser.parse_brng_period(0.0, 100.0, period_num=1)
    assert out['frames_analyzed'] == 5 and out['frames_with_violations'] == 0


def test_parse_period_without_frames_returns_none(tmp_path):
    frames = [{"pkt_pts_time": "50.0", "tags": dict(_NORMAL_TAGS, BRNG="0.1")}]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    assert parser.parse_brng_period(0.0, 10.0, period_num=1) is None


def test_parse_period_handles_missing_pkt_pts_time(tmp_path):
    """A frame without pkt_pts_time should be skipped, not crash the loop."""
    frames = [
        {"pkt_pts_time": "5.0", "tags": dict(_NORMAL_TAGS, BRNG="0.1")},
    ]
    path = _write_qctools(tmp_path, frames)
    # Hand-write one extra frame missing the attribute
    raw = Path(path).read_text().replace(
        '</frames>',
        '    <frame media_type="video" n="99"/>\n  </frames>'
    )
    Path(path).write_text(raw)

    parser = fa.QCToolsParser(path)
    out = parser.parse_brng_period(0.0, 100.0, period_num=1)
    assert out['frames_analyzed'] == 1


# ---- detect_black_segments -----------------------------------------------

def test_detect_black_segments_finds_long_segment(tmp_path):
    """3 seconds of contiguous black at 1fps should be detected with min_duration=2.0."""
    frames = (
        [{"pkt_pts_time": str(t), "tags": _NORMAL_TAGS} for t in range(0, 5)] +
        [{"pkt_pts_time": str(t), "tags": _BLACK_TAGS}  for t in range(5, 9)] +  # 4s of black
        [{"pkt_pts_time": str(t), "tags": _NORMAL_TAGS} for t in range(9, 12)]
    )
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    segs = parser.detect_black_segments(min_duration=2.0)
    assert len(segs) == 1
    start, end = segs[0]
    assert start == pytest.approx(5.0)
    assert end == pytest.approx(8.0)


def test_detect_black_segments_filters_short_blips(tmp_path):
    """A single black frame is below min_duration=2.0 and is dropped."""
    frames = (
        [{"pkt_pts_time": str(t), "tags": _NORMAL_TAGS} for t in range(0, 5)] +
        [{"pkt_pts_time": "5", "tags": _BLACK_TAGS}] +  # 1 frame of black
        [{"pkt_pts_time": str(t), "tags": _NORMAL_TAGS} for t in range(6, 10)]
    )
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    assert parser.detect_black_segments(min_duration=2.0) == []


def test_detect_black_segments_flushes_run_at_eof(tmp_path):
    """An open run at end-of-file should still be reported if it meets duration."""
    frames = (
        [{"pkt_pts_time": str(t), "tags": _NORMAL_TAGS} for t in range(0, 3)] +
        [{"pkt_pts_time": str(t), "tags": _BLACK_TAGS}  for t in range(3, 8)]  # 5s, runs to EOF
    )
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    segs = parser.detect_black_segments(min_duration=2.0)
    assert len(segs) == 1
    assert segs[0] == (3.0, 7.0)


# ---- find_duplicate_frame_candidates -------------------------------------

def test_find_duplicate_runs_groups_consecutive_low_diff_frames(tmp_path):
    # Force 8-bit thresholds (0.25) by explicitly setting YMAX < 250 on every frame
    # (otherwise _detect_bit_depth's missing-YMAX default of 255 triggers 10-bit).
    frames = (
        [{"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "5", "UDIF": "5", "VDIF": "5"}}
            for t in range(0, 3)] +
        [{"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "0.1", "UDIF": "0.1", "VDIF": "0.1", "VREP": "0.5"}}
            for t in range(3, 7)] +  # 4 consecutive low-diff frames → run of 4
        [{"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "5", "UDIF": "5", "VDIF": "5"}}
            for t in range(7, 10)]
    )
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, thresholds = parser.find_duplicate_frame_candidates(min_run_length=2)
    assert thresholds == {"ydif": 0.25, "udif": 0.25, "vdif": 0.25}
    assert len(runs) == 1
    assert runs[0]["start_time"] == pytest.approx(3.0)
    assert runs[0]["end_time"] == pytest.approx(6.0)
    assert runs[0]["duplicate_count"] == 4


def test_find_duplicate_runs_filters_below_min_run_length(tmp_path):
    frames = [
        {"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "5", "UDIF": "5", "VDIF": "5"}}
        for t in range(0, 3)
    ] + [
        {"pkt_pts_time": "3", "tags": {"YMAX": "200", "YDIF": "0.1", "UDIF": "0.1", "VDIF": "0.1"}}
    ] + [
        {"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "5", "UDIF": "5", "VDIF": "5"}}
        for t in range(4, 7)
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, _ = parser.find_duplicate_frame_candidates(min_run_length=2)
    assert runs == []  # only 1 low-diff frame, below min_run_length=2


def test_find_duplicate_runs_excludes_color_bars_window(tmp_path):
    """Frames at or before color_bars_end_time should be excluded."""
    frames = [
        {"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "0.1", "UDIF": "0.1", "VDIF": "0.1"}}
        for t in range(0, 5)  # all below threshold, but in color-bars window
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, _ = parser.find_duplicate_frame_candidates(color_bars_end_time=10.0)
    assert runs == []


def test_find_duplicate_runs_excludes_black_segments(tmp_path):
    """Frames inside known black segments are not counted as duplicates."""
    frames = [
        {"pkt_pts_time": str(t), "tags": {"YMAX": "200", "YDIF": "0.1", "UDIF": "0.1", "VDIF": "0.1"}}
        for t in range(5, 10)
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, _ = parser.find_duplicate_frame_candidates(black_segments=[(0.0, 20.0)])
    assert runs == []


def test_find_duplicate_runs_excludes_flat_field_frames(tmp_path):
    """Zero-diff frames with no spatial variation (YMIN == YMAX) are the
    deck's signal-loss black/mute output, not a freeze — no run reported."""
    frames = [
        {"pkt_pts_time": str(t),
         "tags": {"YDIF": "0", "UDIF": "0", "VDIF": "0", "VREP": "0.99",
                  "YMIN": "64", "YMAX": "64"}}
        for t in range(0, 5)
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, _ = parser.find_duplicate_frame_candidates(min_run_length=2)
    assert runs == []


def test_find_duplicate_runs_keeps_low_diff_frames_with_spatial_structure(tmp_path):
    """Near-zero diff frames that still have luma spread (real frozen
    picture) are reported. UAVG ~512 marks the report as 10-bit scale,
    mirroring the real vendor-tape freeze this is modeled on."""
    frames = [
        {"pkt_pts_time": str(t),
         "tags": {"YDIF": "0.8", "UDIF": "0.8", "VDIF": "0.8", "VREP": "0.07",
                  "YMIN": "4", "YMAX": "200", "UAVG": "512"}}
        for t in range(0, 5)
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    runs, _ = parser.find_duplicate_frame_candidates(min_run_length=2)
    assert len(runs) == 1
    assert runs[0]["duplicate_count"] == 5


def test_find_duplicate_runs_uses_10bit_thresholds_when_detected(tmp_path):
    """10-bit fixture (YMAX > 250) bumps thresholds to 1.0 each."""
    frames = (
        [{"pkt_pts_time": "0", "tags": {"YMAX": "940"}}] +  # triggers 10-bit detection
        [{"pkt_pts_time": str(t), "tags": {"YDIF": "5", "UDIF": "5", "VDIF": "5"}}
            for t in range(1, 4)] +
        [{"pkt_pts_time": str(t), "tags": {"YDIF": "0.5", "UDIF": "0.5", "VDIF": "0.5"}}
            for t in range(4, 8)]  # below 1.0 → would NOT be candidates with 8-bit thresholds
    )
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    assert parser.bit_depth_10 is True
    _, thresholds = parser.find_duplicate_frame_candidates(min_run_length=2)
    assert thresholds == {"ydif": 1.0, "udif": 1.0, "vdif": 1.0}


# ===========================================================================
# Section 3 — IntegratedSignalstatsAnalyzer pure-logic helpers
# ===========================================================================

ISA = fa.IntegratedSignalstatsAnalyzer  # alias


def test_seconds_to_timecode_formatting():
    fake_self = MagicMock()
    assert ISA._seconds_to_timecode(fake_self, 0.0) == "00:00.000"
    assert ISA._seconds_to_timecode(fake_self, 65.5) == "01:05.500"
    assert ISA._seconds_to_timecode(fake_self, 3661.001) == "61:01.001"


def test_should_use_qctools_returns_false_when_no_data():
    fake_self = MagicMock()
    assert ISA._should_use_qctools(fake_self, None) is False
    assert ISA._should_use_qctools(fake_self, {}) is False


def test_should_use_qctools_returns_true_when_data_present():
    fake_self = MagicMock()
    qctools_result = {
        "frames_analyzed": 100,
        "frames_with_violations": 5,
        "brng_values": [0.01, 0.02],
        "period_num": 1,
    }
    assert ISA._should_use_qctools(fake_self, qctools_result) is True


def test_validate_periods_keeps_periods_below_overlap_threshold():
    """Periods with ≤25% black-segment overlap are kept unchanged."""
    fake_self = MagicMock()
    fake_self.duration = 1000.0
    # Period (start=10, dur=60) overlaps black (50, 60) → 10s/60s ≈ 16.7% → keep
    out = ISA._validate_periods_against_black_segments(
        fake_self,
        periods=[(10.0, 60)],
        black_segments=[(50.0, 60.0)],
        effective_start=0.0,
        period_duration=60,
    )
    assert out == [(10.0, 60)]


def test_validate_periods_shifts_when_overlap_too_high():
    """Periods overlapping >25% with black should be shifted to a clean spot."""
    fake_self = MagicMock()
    fake_self.duration = 1000.0
    # _validate_... calls self._shift_period_away_from_black recursively, so we must
    # bind the real implementation to fake_self instead of letting MagicMock invent one.
    fake_self._shift_period_away_from_black = ISA._shift_period_away_from_black.__get__(fake_self)
    # Period (10, 60) overlaps black (10, 50) → 40s/60s ≈ 66% → must shift
    out = ISA._validate_periods_against_black_segments(
        fake_self,
        periods=[(10.0, 60)],
        black_segments=[(10.0, 50.0)],
        effective_start=0.0,
        period_duration=60,
    )
    # Should have produced a shifted period — non-empty output, with start outside the black
    assert len(out) == 1
    new_start, dur = out[0]
    assert dur == 60
    # Shifted away from black: ≤10% overlap is the shifter's allowed budget
    overlap_with_black = max(0, min(new_start + dur, 50.0) - max(new_start, 10.0))
    assert overlap_with_black / dur <= 0.1


def test_shift_period_finds_clean_position_after_black():
    """Standard search should find a position whose black-overlap stays below the
    function's 10%-of-duration budget."""
    fake_self = MagicMock()
    fake_self.duration = 500.0
    new_start = ISA._shift_period_away_from_black(
        fake_self,
        original_start=10.0,
        duration=60,
        black_segments=[(0.0, 100.0)],
        effective_start=0.0,
        used_starts=[],
    )
    assert new_start is not None
    # Verify the function's own contract: ≤10% overlap with any black segment
    end = new_start + 60
    overlap = max(0, min(end, 100.0) - max(new_start, 0.0))
    assert overlap / 60 <= 0.1


def test_shift_period_returns_none_when_no_room():
    """When the entire video is black + no room outside, function returns None."""
    fake_self = MagicMock()
    fake_self.duration = 100.0
    new_start = ISA._shift_period_away_from_black(
        fake_self,
        original_start=10.0,
        duration=60,
        black_segments=[(0.0, 100.0)],  # all-black video
        effective_start=0.0,
        used_starts=[],
    )
    assert new_start is None


def test_shift_period_avoids_already_used_starts():
    """The shifter should also avoid positions too close to other selected periods."""
    fake_self = MagicMock()
    fake_self.duration = 1000.0
    # Black at start; force shift past 100s. used_starts at 110 should push us further.
    new_start = ISA._shift_period_away_from_black(
        fake_self,
        original_start=10.0,
        duration=60,
        black_segments=[(0.0, 100.0)],
        effective_start=0.0,
        used_starts=[120.0],  # too close to candidate starts near 110
    )
    if new_start is not None:
        # Distance from used_start should be ≥ duration
        assert abs(new_start - 120.0) >= 60


# ===========================================================================
# Section 4 — EnhancedFrameAnalysis pure-logic helpers
# ===========================================================================

EFA = fa.EnhancedFrameAnalysis  # alias


@pytest.mark.parametrize("flag,expected", [
    (True, True),
    (False, False),
    ("yes", True),
    ("Yes", True),
    ("YES", True),
    ("no", False),
    ("true", True),
    ("1", True),
    ("0", False),
    ("anything-else", False),
])
def test_is_step_enabled_handles_bool_and_string(flag, expected):
    fake_self = MagicMock()
    assert EFA._is_step_enabled(fake_self, flag) is expected


def test_is_step_enabled_unknown_type_defaults_to_true():
    """Backward-compat fallback: unknown types → True."""
    fake_self = MagicMock()
    assert EFA._is_step_enabled(fake_self, 42) is True
    assert EFA._is_step_enabled(fake_self, [1, 2, 3]) is True
    assert EFA._is_step_enabled(fake_self, None) is True


# ---- _find_qctools_report ------------------------------------------------

def _build_efa_self(tmp_path, video_id="JPC_AV_X"):
    """Build a stand-in object with the attributes _find_qctools_report needs."""
    fake_self = MagicMock()
    video_path = tmp_path / f"{video_id}.mkv"
    video_path.write_text("")
    fake_self.video_path = video_path
    fake_self.video_id = video_id
    return fake_self


def test_find_qctools_report_finds_in_qc_metadata_subdir(tmp_path):
    fake_self = _build_efa_self(tmp_path)
    qc_dir = tmp_path / f"{fake_self.video_id}_qc_metadata"
    qc_dir.mkdir()
    report = qc_dir / f"{fake_self.video_path.name}.qctools.xml.gz"
    report.write_text("")

    found = EFA._find_qctools_report(fake_self)
    assert found == str(report)


def test_find_qctools_report_finds_in_vrecord_metadata_subdir(tmp_path):
    fake_self = _build_efa_self(tmp_path)
    vrec_dir = tmp_path / f"{fake_self.video_id}_vrecord_metadata"
    vrec_dir.mkdir()
    # Use the without-extension naming variant
    report = vrec_dir / f"{fake_self.video_id}.qctools.xml.gz"
    report.write_text("")

    found = EFA._find_qctools_report(fake_self)
    assert found == str(report)


def test_find_qctools_report_returns_none_when_missing(tmp_path):
    fake_self = _build_efa_self(tmp_path)
    assert EFA._find_qctools_report(fake_self) is None


def test_find_qctools_report_prefers_full_filename_variant(tmp_path):
    """Both naming variants present → the function checks 'with extension' first."""
    fake_self = _build_efa_self(tmp_path)
    # without-extension variant in the parent
    short = tmp_path / f"{fake_self.video_id}.qctools.xml.gz"
    short.write_text("")
    # with-extension variant in the parent (checked first)
    full = tmp_path / f"{fake_self.video_path.name}.qctools.xml.gz"
    full.write_text("")
    assert EFA._find_qctools_report(fake_self) == str(full)


# ===========================================================================
# Section 5 — analyze_frame_quality entry point
# ===========================================================================

def test_analyze_frame_quality_cancelled_before_start_returns_none():
    """If cancellation fires immediately, function bails before instantiating analyzer."""
    cancelled = MagicMock(return_value=True)
    out = fa.analyze_frame_quality("/v/in.mkv", check_cancelled=cancelled)
    assert out is None


def test_analyze_frame_quality_uses_default_config_when_none(monkeypatch):
    """When frame_config=None, function loads defaults from FrameAnalysisConfig."""
    fake_analyzer = MagicMock()
    fake_analyzer.analyze.return_value = {"status": "ok"}
    monkeypatch.setattr(fa, "EnhancedFrameAnalysis", lambda *a, **kw: fake_analyzer)

    result = fa.analyze_frame_quality("/v/in.mkv")

    assert result == {"status": "ok"}
    # Default border_detection_mode is 'simple' (per FrameAnalysisConfig)
    fake_analyzer.analyze.assert_called_once()
    call = fake_analyzer.analyze.call_args
    assert call.kwargs.get("method") == "simple"


def test_analyze_frame_quality_passes_config_fields_through(monkeypatch):
    """Config values should be forwarded to analyzer.analyze() unchanged."""
    fake_analyzer = MagicMock()
    fake_analyzer.analyze.return_value = {"x": 1}
    monkeypatch.setattr(fa, "EnhancedFrameAnalysis", lambda *a, **kw: fake_analyzer)

    from AV_Spex.utils.config_setup import FrameAnalysisConfig
    cfg = FrameAnalysisConfig(
        border_detection_mode="sophisticated",
        brng_skip_color_bars=False,
        max_border_retries=5,
    )

    fa.analyze_frame_quality(
        "/v/in.mkv", frame_config=cfg, color_bars_end_time=4.5,
    )

    call = fake_analyzer.analyze.call_args
    assert call.kwargs["method"] == "sophisticated"
    assert "duration_limit" not in call.kwargs
    assert call.kwargs["skip_color_bars"] is False
    assert call.kwargs["max_refinement_iterations"] == 5
    assert call.kwargs["color_bars_end_time"] == 4.5


def test_analyze_frame_quality_returns_none_when_cancelled_after_init(monkeypatch):
    """Cancellation between analyzer instantiation and analyze() bails with None."""
    fake_analyzer = MagicMock()
    monkeypatch.setattr(fa, "EnhancedFrameAnalysis", lambda *a, **kw: fake_analyzer)

    # Cancel returns False on first call (before analyzer init), True on the second
    cancelled = MagicMock(side_effect=[False, True])
    result = fa.analyze_frame_quality("/v/in.mkv", check_cancelled=cancelled)

    assert result is None
    fake_analyzer.analyze.assert_not_called()


# ===========================================================================
# probe_video_properties — OpenCV-without-FFmpeg fallback
# ===========================================================================

class _DeadCapture:
    """An OpenCV build with no usable backend: open fails, every property is -1."""

    def isOpened(self):
        return False

    def get(self, prop):
        return -1.0

    def release(self):
        pass


class _LiveCapture:
    def __init__(self, props):
        self._props = props

    def isOpened(self):
        return True

    def get(self, prop):
        return self._props[prop]

    def release(self):
        pass


def _live_720x486():
    import cv2
    return _LiveCapture({
        cv2.CAP_PROP_FRAME_WIDTH: 720.0,
        cv2.CAP_PROP_FRAME_HEIGHT: 486.0,
        cv2.CAP_PROP_FPS: 30000 / 1001,
        cv2.CAP_PROP_FRAME_COUNT: 53489.0,
    })


def test_probe_video_properties_uses_opencv_when_it_opens(monkeypatch):
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _live_720x486())
    props = fa.probe_video_properties("/v/in.mkv")

    assert (props["width"], props["height"]) == (720, 486)
    assert props["total_frames"] == 53489
    assert props["opencv_usable"] is True


def test_probe_video_properties_falls_back_to_ffprobe(monkeypatch):
    """The George Blood failure: cv2 can't open FFV1/MKV, ffprobe still can."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _DeadCapture())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: {
        "width": 720, "height": 486, "fps": 30000 / 1001,
        "total_frames": 53488, "duration": 1784.747,
    })

    props = fa.probe_video_properties("/v/in.mkv")

    assert (props["width"], props["height"]) == (720, 486)
    assert props["opencv_usable"] is False


def test_probe_video_properties_never_returns_minus_one(monkeypatch):
    """-1 dimensions became crop=-1:-1:0:0; zeros are what guards can catch."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _DeadCapture())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: None)

    props = fa.probe_video_properties("/v/in.mkv")

    assert props["width"] == 0
    assert props["height"] == 0
    assert props["width"] != -1 and props["height"] != -1
    assert props["opencv_usable"] is False


def test_simple_borders_stay_positive_when_opencv_is_broken(monkeypatch):
    """End-to-end guard: the ffprobe fallback keeps the crop filter valid."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _DeadCapture())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: {
        "width": 720, "height": 486, "fps": 30000 / 1001,
        "total_frames": 53488, "duration": 1784.747,
    })

    det = fa.SophisticatedBorderDetector.__new__(fa.SophisticatedBorderDetector)
    det.video_path = "/v/in.mkv"
    det.signals = None
    det.check_cancelled = lambda: False
    det._init_video_properties()

    x, y, w, h = det._detect_simple_borders().active_area

    assert (x, y, w, h) == (25, 25, 670, 436)
    assert w > 0 and h > 0, "a non-positive crop reaches ffmpeg as crop=-1:-1:0:0"


def _bare_border_detector(width=720, height=486):
    det = fa.SophisticatedBorderDetector.__new__(fa.SophisticatedBorderDetector)
    det.video_path = "/v/in.mkv"
    det.signals = None
    det.check_cancelled = lambda: False
    det.width, det.height = width, height
    det.fps, det.total_frames, det.duration = 30000 / 1001, 1000, 33.4
    det.opencv_usable = True
    return det


def test_simple_mode_uses_configured_border_pixels():
    """simple_border_pixels / --frame-border-pixels used to be ignored (always 25)."""
    det = _bare_border_detector()

    result = det.detect_borders_with_quality_assessment(
        method='simple', simple_border_pixels=10)

    assert result.active_area == (10, 10, 700, 466)
    assert result.detection_method == 'simple'


def test_sophisticated_fallback_uses_configured_border_pixels(monkeypatch):
    """When sophisticated detection can't decode frames it falls back to simple
    mode, which should honour the configured crop as well."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _DeadCapture())
    det = _bare_border_detector()

    result = det.detect_borders_with_quality_assessment(
        method='sophisticated', simple_border_pixels=40)

    assert result.active_area == (40, 40, 640, 406)


def _bordered_frame(width=720, height=486, left=30, right=20, top=8, border_level=30):
    """Striped picture (mean 120, high contrast) inside dim, flat borders.

    border_level 30 sits between the default threshold (10) and the vertical
    blanking test (mean < 20), so only sophisticated_threshold decides whether
    the borders read as picture.
    """
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    stripes = np.where((np.arange(width) // 4) % 2 == 0, 60, 180).astype(np.uint8)
    frame[:, :, :] = stripes[None, :, None]
    frame[:, :left] = border_level
    frame[:, width - right:] = border_level
    frame[:top, :] = border_level
    return frame


class _FrameCapture:
    """A capture that opens and returns the same frame for every position."""

    def __init__(self, frame):
        self.frame = frame

    def isOpened(self):
        return True

    def set(self, prop, value):
        return True

    def read(self):
        return True, self.frame.copy()

    def get(self, prop):
        return 0.0

    def release(self):
        pass


def _sophisticated_area(monkeypatch, **params):
    frame = _bordered_frame()
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _FrameCapture(frame))
    det = _bare_border_detector()
    result = det.detect_borders_with_quality_assessment(method='sophisticated', **params)
    return result.active_area, result.detection_method


def test_sophisticated_threshold_decides_what_counts_as_border(monkeypatch):
    # Borders at brightness 30: above threshold 10 they read as picture...
    area, method = _sophisticated_area(monkeypatch, sophisticated_threshold=10)
    assert method == 'sophisticated'
    assert area == (5, 5, 710, 476)

    # ...below threshold 40 they are measured as borders (L30 R20 T8, +5px padding)
    area, _ = _sophisticated_area(monkeypatch, sophisticated_threshold=40)
    assert area == (35, 13, 660, 468)


def test_sophisticated_edge_sample_width_limits_left_right_search(monkeypatch):
    # A 10px search never reaches the end of the 30px/20px side borders, so no
    # left/right border is found; the top border (row scan) is unaffected.
    area, _ = _sophisticated_area(
        monkeypatch, sophisticated_threshold=40, sophisticated_edge_sample_width=10)
    assert area == (5, 13, 710, 468)


def test_sophisticated_padding_is_applied(monkeypatch):
    area, _ = _sophisticated_area(
        monkeypatch, sophisticated_threshold=40, sophisticated_padding=0)
    assert area == (30, 8, 670, 478)


def test_sophisticated_padding_that_consumes_the_frame_falls_back_to_simple(monkeypatch):
    area, method = _sophisticated_area(
        monkeypatch, sophisticated_threshold=40, sophisticated_padding=400,
        simple_border_pixels=25)
    assert method == 'simple'
    assert area == (25, 25, 670, 436)


@pytest.mark.parametrize("configured, expected", [(12, 12), (80, 80), (3, 5), (0, 5)])
def test_sophisticated_sample_frames_sets_quality_frame_count(monkeypatch, configured, expected):
    frame = _bordered_frame()
    det = _bare_border_detector()
    det.sophisticated_sample_frames = fa._config_int(
        configured, 30, det.MIN_QUALITY_FRAMES, "sophisticated_sample_frames")

    frames = det._select_quality_frames(_FrameCapture(frame), violations=None)

    assert len(frames) == expected


@pytest.mark.parametrize("pixels, expected", [
    (0, (0, 0, 720, 486)),
    (-5, (0, 0, 720, 486)),       # negative is clamped to no crop
    (400, (0, 0, 720, 486)),      # crop larger than the frame falls back to full frame
])
def test_simple_border_pixels_edge_values(pixels, expected):
    det = _bare_border_detector()

    result = det.detect_borders_with_quality_assessment(
        method='simple', simple_border_pixels=pixels)

    assert result.active_area == expected


def test_ffprobe_video_properties_derives_frame_count_from_duration(monkeypatch):
    """Matroska omits nb_frames, so it has to come from duration * fps."""
    completed = MagicMock()
    completed.stdout = (
        '{"streams": [{"width": 720, "height": 486, '
        '"avg_frame_rate": "30000/1001", "duration": "100.0"}], '
        '"format": {"duration": "100.0"}}'
    )
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **kw: completed)

    props = fa._ffprobe_video_properties("/v/in.mkv")

    assert (props["width"], props["height"]) == (720, 486)
    assert props["fps"] == pytest.approx(29.97, abs=0.01)
    assert props["total_frames"] == 2997


# OpenCV derives CAP_PROP_FRAME_COUNT from the container duration; when the
# container has none, it hands back AV_NOPTS_VALUE scaled to seconds instead of
# failing. This is the exact value JPC_AV_03569 produced.
_NOPTS_SECONDS = -9223372036854775.808


def _live_no_duration():
    """cv2 opens the file and geometry is right, but the timing is the sentinel."""
    import cv2
    return _LiveCapture({
        cv2.CAP_PROP_FRAME_WIDTH: 720.0,
        cv2.CAP_PROP_FRAME_HEIGHT: 486.0,
        cv2.CAP_PROP_FPS: 30000 / 1001,
        cv2.CAP_PROP_FRAME_COUNT: _NOPTS_SECONDS * (30000 / 1001),
    })


def test_probe_video_properties_rejects_opencv_nopts_frame_count(monkeypatch):
    """A missing container duration must not become a negative duration.

    Unchecked, this reached _select_analysis_periods as
    `available_duration = -9223372036854856.0` and then ffmpeg as
    `-t -9223372036854856.0`, which exits 222.
    """
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _live_no_duration())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: None)

    props = fa.probe_video_properties("/v/in.mkv")

    assert props["duration"] == 0, "0 is the 'unknown' value guards already check"
    assert props["total_frames"] == 0
    assert props["duration"] >= 0 and props["total_frames"] >= 0


def test_probe_video_properties_keeps_opencv_geometry_when_timing_is_bad(monkeypatch):
    """cv2 can still decode frames, so border detection must not be given up."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _live_no_duration())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: None)

    props = fa.probe_video_properties("/v/in.mkv")

    assert (props["width"], props["height"]) == (720, 486)
    assert props["opencv_usable"] is True


def test_probe_video_properties_recovers_timing_from_ffprobe(monkeypatch):
    """Geometry from cv2, timing from ffprobe — the file stays fully analyzable."""
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: _live_no_duration())
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: {
        "width": 720, "height": 486, "fps": 30000 / 1001,
        "total_frames": 11307, "duration": 377.31,
    })

    props = fa.probe_video_properties("/v/in.mkv")

    assert props["duration"] == pytest.approx(377.31)
    assert props["total_frames"] == 11307
    assert props["fps"] == pytest.approx(29.97, abs=0.01)
    assert props["opencv_usable"] is True


def test_probe_video_properties_rejects_nan_fps(monkeypatch):
    """A non-finite fps poisons the same arithmetic a negative one does."""
    import cv2 as _cv2
    cap = _LiveCapture({
        _cv2.CAP_PROP_FRAME_WIDTH: 720.0,
        _cv2.CAP_PROP_FRAME_HEIGHT: 486.0,
        _cv2.CAP_PROP_FPS: float("nan"),
        _cv2.CAP_PROP_FRAME_COUNT: 53489.0,
    })
    monkeypatch.setattr(fa.cv2, "VideoCapture", lambda *a, **kw: cap)
    monkeypatch.setattr(fa, "_ffprobe_video_properties", lambda p: None)

    props = fa.probe_video_properties("/v/in.mkv")

    assert props["fps"] == 0.0
    assert props["duration"] == 0


def test_ffprobe_video_properties_rejects_nopts_duration(monkeypatch):
    """ffprobe can report the sentinel too; a failed candidate must not survive."""
    completed = MagicMock()
    completed.stdout = (
        '{"streams": [{"width": 720, "height": 486, '
        '"avg_frame_rate": "30000/1001", "duration": "-9223372036854775.808"}], '
        '"format": {"duration": "-9223372036854775.808"}}'
    )
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **kw: completed)

    props = fa._ffprobe_video_properties("/v/in.mkv")

    assert props["duration"] == 0
    assert props["total_frames"] == 0


def test_ffprobe_video_properties_ignores_na_nb_frames(monkeypatch):
    """nb_frames of 'N/A' falls through to the duration-derived count."""
    completed = MagicMock()
    completed.stdout = (
        '{"streams": [{"width": 720, "height": 486, "nb_frames": "N/A", '
        '"avg_frame_rate": "30000/1001", "duration": "100.0"}], '
        '"format": {"duration": "100.0"}}'
    )
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **kw: completed)

    props = fa._ffprobe_video_properties("/v/in.mkv")

    assert props["total_frames"] == 2997


# ---------------------------------------------------------------------------
# Unknown duration: signalstats/BRNG must skip, not report a clean measurement
# ---------------------------------------------------------------------------

def _signalstats_analyzer(duration):
    """An analyzer with only the attributes period selection touches."""
    a = fa.IntegratedSignalstatsAnalyzer.__new__(fa.IntegratedSignalstatsAnalyzer)
    a.duration = duration
    a.fps = 30000 / 1001
    a.width, a.height = 720, 486
    a.qctools_report = None
    a.check_cancelled = lambda: False
    a.signals = None
    a.last_resort_period_note = None
    return a


def test_find_analysis_periods_refuses_unknown_duration():
    """A 0 duration yielded (50.03, -80.03) — a negative ffmpeg -t."""
    a = _signalstats_analyzer(duration=0.0)

    periods = a._find_analysis_periods(content_start=40.03, color_bars_end=30.03,
                                       duration=60, num_periods=3)

    assert periods == []
    assert all(length > 0 for _, length in periods), "a negative -t reaches ffmpeg"


def test_find_analysis_periods_uses_qctools_periods_without_a_duration():
    """QCTools periods are absolute, so they need no duration to be placed."""
    a = _signalstats_analyzer(duration=0.0)

    periods = a._find_analysis_periods(content_start=40.03, color_bars_end=30.03,
                                       duration=60, num_periods=3,
                                       qctools_periods=[(120.0, 60), (300.0, 60)])

    assert periods == [(120.0, 60), (300.0, 60)]


def test_signalstats_reports_unknown_duration_not_zero_percent(monkeypatch):
    """The false-clean this fixes: 0.0% violations on a file nothing was read from."""
    a = _signalstats_analyzer(duration=0.0)

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.03,
                                        color_bars_end_time=30.03,
                                        analysis_duration=60, num_periods=3)

    assert result.violation_percentage is None, "0.0 would render as a clean result"
    assert result.max_brng is None and result.avg_brng is None
    assert result.severity == "warning"
    assert "duration unknown" in result.diagnosis
    assert result.analysis_periods == []


def test_signalstats_keeps_the_black_content_reason(monkeypatch):
    """The other empty-period cause must not be relabelled as a duration problem."""
    a = _signalstats_analyzer(duration=1784.7)
    monkeypatch.setattr(a, "_find_analysis_periods", lambda *args, **kw: [])

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.03,
                                        color_bars_end_time=30.03,
                                        analysis_duration=60, num_periods=3)

    assert result.violation_percentage is None
    assert "black content" in result.diagnosis
    assert "duration unknown" not in result.diagnosis


def test_brng_returns_none_and_names_the_reason(caplog):
    """BRNG's could-not-run convention is None; the reason has to be the right one."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.check_cancelled = lambda: False
    analyzer.signals = None
    analyzer.upstream_context = None

    with caplog.at_level("WARNING"):
        result = analyzer.analyze_with_differential_detection(
            output_dir=Path(tempfile.mkdtemp()),
            analysis_periods=[],
            no_periods_reason=fa.DURATION_UNKNOWN_REASON,
        )

    assert result is None, "an empty result would render as 'no violations detected'"
    assert "duration unknown" in caplog.text
    assert "black content" not in caplog.text


def _analyzer_with_periods(monkeypatch, periods, qctools_report=None):
    a = _signalstats_analyzer(duration=1784.7)
    a.qctools_report = qctools_report
    monkeypatch.setattr(a, "_find_analysis_periods", lambda *args, **kw: periods)
    # Every source comes back empty: this is the "attempted but measured nothing"
    # case, not the "no periods" case.
    monkeypatch.setattr(a, "_parse_qctools_brng_period", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_analyze_with_ffprobe_period", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_seconds_to_timecode", lambda t: "00:00:00.000")
    return a


def _analyzer_measuring(monkeypatch, periods, measured_indices):
    """An analyzer whose ffprobe pass returns data only for some periods."""
    a = _signalstats_analyzer(duration=1784.7)
    a.qctools_report = None
    monkeypatch.setattr(a, "_find_analysis_periods", lambda *args, **kw: periods)
    monkeypatch.setattr(a, "_parse_qctools_brng_period", lambda *args, **kw: None)
    monkeypatch.setattr(a, "_seconds_to_timecode", lambda t: "00:00:00.000")

    def fake_ffprobe(active_area, start_time, duration, period_num, **kw):
        if (period_num - 1) not in measured_indices:
            return None
        return {'frames_analyzed': 100, 'frames_with_violations': 2,
                'brng_values': [0.001, 0.002], 'brng_frames': [(start_time, 0.001)]}

    monkeypatch.setattr(a, "_analyze_with_ffprobe_period", fake_ffprobe)
    return a


def _period_frames(start_time, n_frames, n_bad, brng=0.05):
    """A period's per-frame BRNG: the first n_bad frames out of range, the rest clean."""
    vals = [brng] * n_bad + [0.0] * (n_frames - n_bad)
    return [(start_time + i / 30, v) for i, v in enumerate(vals)]


def test_content_violations_with_clean_borders_are_not_labeled_border(tmp_path, monkeypatch):
    """25% of frames bad inside the picture, borders clean: full frame and active
    area agree, so every period is content_violations. The QCTools share used to
    read 100% (flagged frames divided by flagged frames), which labeled these
    periods border_violations and the file 'broadcast-safe'."""
    periods = [(100.0, 10), (300.0, 10)]
    frames = []
    for start, _ in periods:
        frames += [{"pkt_pts_time": str(ts), "tags": dict(_NORMAL_TAGS, BRNG=str(b))}
                   for ts, b in _period_frames(start, 100, 25)]
    frames.sort(key=lambda f: float(f["pkt_pts_time"]))

    a = _signalstats_analyzer(duration=1784.7)
    a.qctools_report = _write_qctools(tmp_path, frames)
    monkeypatch.setattr(a, "_find_analysis_periods", lambda *args, **kw: periods)
    monkeypatch.setattr(a, "_seconds_to_timecode", lambda t: "00:00:00.000")

    def fake_ffprobe(active_area, start_time, duration, period_num, **kw):
        pf = _period_frames(start_time, 100, 25)
        vals = [b for _, b in pf]
        return {'frames_analyzed': 100, 'frames_with_violations': 25,
                'brng_values': vals, 'brng_frames': pf}

    monkeypatch.setattr(a, "_analyze_with_ffprobe_period", fake_ffprobe)
    border = fa.BorderDetectionResult(active_area=(10, 10, 700, 466), border_regions={},
                                      detection_method="simple", quality_frame_hints=[])

    result = a.analyze_with_signalstats(border_data=border, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=10, num_periods=2)

    for cmp in result.comparison_results:
        assert cmp['qctools_full_frame']['violations_pct'] == pytest.approx(25.0)
        assert cmp['diagnosis'] == 'content_violations'
    assert result.severity != 'ok'
    assert "active picture area" in result.diagnosis


def test_signalstats_reports_partial_coverage(monkeypatch):
    """Aggregates from one period must not pass as the full intended sample."""
    a = _analyzer_measuring(monkeypatch, [(100.0, 60), (300.0, 60), (900.0, 60)],
                            measured_indices={0})

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=60, num_periods=3)

    assert result.violation_percentage is not None, "the measured period is still valid"
    assert result.periods_attempted == 3
    assert result.periods_measured == 1
    assert "1 of 3" in result.coverage_note


def test_signalstats_full_coverage_sets_no_note(monkeypatch):
    a = _analyzer_measuring(monkeypatch, [(100.0, 60), (300.0, 60)],
                            measured_indices={0, 1})

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=60, num_periods=2)

    assert result.periods_measured == result.periods_attempted == 2
    assert result.coverage_note is None


def test_signalstats_cancellation_is_reported_as_coverage_not_failure(monkeypatch):
    """Cancelling after some periods leaves valid numbers over a short sample."""
    a = _analyzer_measuring(monkeypatch, [(100.0, 60), (300.0, 60), (900.0, 60)],
                            measured_indices={0, 1, 2})
    calls = {'n': 0}

    def cancel_after_two():
        calls['n'] += 1
        return calls['n'] > 2   # checked once per period, at the top of the loop

    a.check_cancelled = cancel_after_two

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=60, num_periods=3)

    assert result.periods_measured == 2
    assert "Cancelled" in result.coverage_note


def test_signalstats_periods_that_return_nothing_are_not_zero_percent(monkeypatch):
    """The old 'No data available' path returned 0.0% — a clean bill of health."""
    a = _analyzer_with_periods(monkeypatch, [(100.0, 60), (300.0, 60)])

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=60, num_periods=2)

    assert result.violation_percentage is None
    assert result.max_brng is None and result.avg_brng is None
    assert result.severity == "warning"
    assert "2 analysis period(s) were attempted" in result.diagnosis
    assert result.diagnosis != "No data available"


def test_signalstats_names_only_the_sources_it_had(monkeypatch):
    """Blaming QCTools parsing when there was no QCTools report would mislead."""
    without = _analyzer_with_periods(monkeypatch, [(100.0, 60)], qctools_report=None)
    r1 = without.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                          color_bars_end_time=30.0,
                                          analysis_duration=60, num_periods=1)
    assert "QCTools" not in r1.diagnosis

    with_qct = _analyzer_with_periods(monkeypatch, [(100.0, 60)],
                                      qctools_report="/qc/report.qctools.xml.gz")
    r2 = with_qct.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                           color_bars_end_time=30.0,
                                           analysis_duration=60, num_periods=1)
    assert "QCTools" in r2.diagnosis


def test_signalstats_cancellation_is_not_reported_as_a_failure(monkeypatch):
    """Cancelling is the operator's doing, not a file or tool problem."""
    a = _analyzer_with_periods(monkeypatch, [(100.0, 60)])
    a.check_cancelled = lambda: True

    result = a.analyze_with_signalstats(border_data=None, content_start_time=40.0,
                                        color_bars_end_time=30.0,
                                        analysis_duration=60, num_periods=1)

    assert result.violation_percentage is None, "still not a clean result"
    assert "cancelled" in result.diagnosis
    assert "could not run" not in result.diagnosis


def test_brng_records_why_it_could_not_run():
    """The reason has to leave the analyzer; None alone tells the report nothing."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.check_cancelled = lambda: False
    analyzer.signals = None

    result = analyzer.analyze_with_differential_detection(
        output_dir=Path(tempfile.mkdtemp()),
        analysis_periods=[],
        no_periods_reason=fa.DURATION_UNKNOWN_REASON,
    )

    assert result is None
    assert "duration unknown" in analyzer.could_not_run_reason


def test_brng_could_not_run_reason_does_not_outlive_its_run():
    """A stale reason would put a could-not-run banner on a measured result."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.check_cancelled = lambda: False
    analyzer.signals = None

    analyzer.analyze_with_differential_detection(
        output_dir=Path(tempfile.mkdtemp()), analysis_periods=[],
        no_periods_reason=fa.DURATION_UNKNOWN_REASON)
    assert analyzer.could_not_run_reason is not None

    # A second run that gets as far as period handling clears it first.
    analyzer.analyze_with_differential_detection(
        output_dir=Path(tempfile.mkdtemp()), analysis_periods=[])
    assert "duration unknown" not in analyzer.could_not_run_reason


def test_summary_does_not_print_zero_percent_when_signalstats_could_not_run():
    """generate_summary is what the operator reads in the console."""
    from AV_Spex.checks import frame_analysis_report

    text = frame_analysis_report.generate_summary(
        {'qctools_report_available': True,
         'signalstats': {'violation_percentage': None, 'max_brng': None,
                         'avg_brng': None, 'analysis_periods': [],
                         'diagnosis': 'Signalstats could not run: video duration unknown'}},
        'JPC_AV_03569')

    assert "0.0%" not in text and "0.00%" not in text
    assert "could not run" in text.lower()


def test_ffprobe_video_properties_returns_none_on_bad_dimensions(monkeypatch):
    completed = MagicMock()
    completed.stdout = '{"streams": [{"width": 0, "height": 0}]}'
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **kw: completed)

    assert fa._ffprobe_video_properties("/v/in.mkv") is None


# ===========================================================================
# Active-area validation — a truthy tuple is not a valid crop
# ===========================================================================

@pytest.mark.parametrize("area", [
    (0, 0, -1, -1),   # what OpenCV's -1 dimensions produced
    (0, 0, 0, 0),     # what they produce now that -1 is gone
    (25, 25, 0, 436),
    (25, 25, 670, -5),
    (-1, 0, 670, 436),
    None,
    (),
    (0, 0, 720),      # wrong arity
])
def test_is_valid_active_area_rejects_unusable(area):
    assert fa.is_valid_active_area(area) is False


def test_is_valid_active_area_accepts_real_rectangle():
    assert fa.is_valid_active_area((25, 25, 670, 436)) is True
    assert fa.is_valid_active_area((0, 0, 720, 486)) is True


def test_build_crop_filter_returns_empty_for_degenerate_area():
    """crop=-1:-1:0:0 is what ffmpeg rejected with exit status 234."""
    assert fa.build_crop_filter((0, 0, -1, -1)) == ""
    assert fa.build_crop_filter((0, 0, 0, 0)) == ""
    assert fa.build_crop_filter(None) == ""


def test_build_crop_filter_formats_valid_area():
    assert fa.build_crop_filter((25, 25, 670, 436)) == "crop=670:436:25:25,"
    assert fa.build_crop_filter((25, 25, 670, 436), trailing_comma=False) == "crop=670:436:25:25"


def test_sanitize_active_area_nulls_degenerate_and_keeps_good():
    assert fa.sanitize_active_area((0, 0, -1, -1), "BRNG") is None
    assert fa.sanitize_active_area(None) is None
    assert fa.sanitize_active_area((25, 25, 670, 436)) == (25, 25, 670, 436)


def test_comparison_video_command_omits_crop_when_area_degenerate(monkeypatch):
    """The end-to-end guard: no `crop=-1:-1:0:0` can reach ffmpeg."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = (0, 0, -1, -1)   # bypasses __init__ sanitizing
    analyzer.signals = None

    captured = []
    monkeypatch.setattr(fa.subprocess, "run",
                        lambda cmd, **kw: captured.append(cmd) or MagicMock())

    analyzer._create_comparison_videos_for_period(
        Path("/tmp/h.mp4"), Path("/tmp/o.mp4"), start_time=0.0, duration=60)

    assert captured, "no ffmpeg command was built"
    for cmd in captured:
        joined = " ".join(cmd)
        assert "crop=-1" not in joined
        assert "crop=" not in joined, f"degenerate area still produced a crop: {joined}"


def test_brng_analyzer_init_sanitizes_degenerate_border_data():
    border = fa.BorderDetectionResult(
        active_area=(0, 0, -1, -1), border_regions={},
        detection_method="simple", quality_frame_hints=[],
    )
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.border_data = border
    analyzer.active_area = fa.sanitize_active_area(border.active_area, "BRNG analysis")

    assert analyzer.active_area is None, "degenerate area must not survive into analysis"


# ===========================================================================
# "Clean" must mean "examined and found nothing", not "never ran"
# ===========================================================================

def _brng_analyzer_for_periods(tmp_path, monkeypatch, create_ok):
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = None
    analyzer.signals = None
    analyzer.check_cancelled = lambda: False
    analyzer.width, analyzer.height = 720, 486
    analyzer.fps, analyzer.total_frames, analyzer.duration = 29.97, 1000, 33.4
    analyzer.opencv_usable = True
    monkeypatch.setattr(
        fa.DifferentialBRNGAnalyzer, "_create_comparison_videos_for_period",
        lambda self, *a, **kw: create_ok)
    return analyzer


def test_all_periods_failing_returns_none_not_a_clean_result(tmp_path, monkeypatch):
    """The George Blood false negative: 0 frames examined was reported as clean."""
    analyzer = _brng_analyzer_for_periods(tmp_path, monkeypatch, create_ok=False)

    result = analyzer.analyze_with_differential_detection(
        output_dir=tmp_path,
        analysis_periods=[(0.0, 60), (115.0, 60), (215.0, 60)],
    )

    assert result is None, (
        "every period failed, so returning a BRNGAnalysisResult with no "
        "violations would be reported as 'No BRNG violations detected'"
    )


def test_partial_period_failure_still_returns_a_result(tmp_path, monkeypatch):
    """One bad period out of three degrades; it must not discard the good ones."""
    calls = {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        return calls["n"] != 1        # first period fails, rest succeed

    analyzer = _brng_analyzer_for_periods(tmp_path, monkeypatch, create_ok=True)
    monkeypatch.setattr(
        fa.DifferentialBRNGAnalyzer, "_create_comparison_videos_for_period", flaky)
    empty_stats = {
        "qctools_frames_targeted": 0, "frames_mapped_to_period": 0,
        "total_samples_analyzed": 0, "frames_checked": 0, "violations_found": 0,
    }
    monkeypatch.setattr(
        fa.DifferentialBRNGAnalyzer, "_analyze_differential_violations",
        lambda self, *a, **kw: ([], empty_stats))

    result = analyzer.analyze_with_differential_detection(
        output_dir=tmp_path,
        analysis_periods=[(0.0, 60), (115.0, 60), (215.0, 60)],
    )

    assert result is not None


# ===========================================================================
# Empty analysis periods: never silently, never a crash (fixes B + D)
# ===========================================================================

def test_validate_periods_never_returns_empty_keeps_least_black():
    """D: dropping every candidate would strand BRNG analysis with nothing to do."""
    fake_self = MagicMock()
    fake_self.duration = 1800.0
    fake_self._shift_period_away_from_black = ISA._shift_period_away_from_black.__get__(fake_self)
    fake_self._fit_period_in_content_gap = lambda *a, **kw: None   # no gap anywhere

    out = ISA._validate_periods_against_black_segments(
        fake_self,
        # 100%, 100%, then 57% black — the last is the least-bad candidate
        periods=[(1435.0, 60), (1565.0, 60), (1765.0, 60)],
        black_segments=[(0.0, 1799.0)],
        effective_start=10.0,
        period_duration=60,
    )

    assert out, "validation must never hand back an empty period list"
    assert out == [(1765.0, 60)], "should keep the candidate with the least black"


def test_validate_periods_empty_input_stays_empty():
    """No candidates in means no candidates out — the fallback needs something to pick."""
    fake_self = MagicMock()
    fake_self.duration = 1800.0
    out = ISA._validate_periods_against_black_segments(
        fake_self, periods=[], black_segments=[(0.0, 100.0)],
        effective_start=0.0, period_duration=60,
    )
    assert out == []


def test_no_analysis_periods_returns_none_rather_than_crashing(tmp_path):
    """B: the removed branch called a method that hasn't existed since Sept 2025."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = None
    analyzer.signals = None
    analyzer.check_cancelled = lambda: False

    result = analyzer.analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=None,
    )

    assert result is None


def test_no_analysis_periods_does_not_call_the_renamed_method(tmp_path, monkeypatch):
    """Guards against 'fixing' this by renaming the call — the args were reordered too."""
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = None
    analyzer.signals = None
    analyzer.check_cancelled = lambda: False

    monkeypatch.setattr(
        fa.DifferentialBRNGAnalyzer, "_create_comparison_videos_for_period",
        lambda self, *a, **kw: pytest.fail(
            "an empty period list must not fall back to an arbitrary whole-file window"))

    assert analyzer.analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=[]) is None


def test_differential_analyzer_has_no_stale_method_reference():
    """The Sept 2025 rename left one call site behind; keep it from coming back."""
    import inspect
    src = inspect.getsource(fa.DifferentialBRNGAnalyzer)
    assert "self._create_comparison_videos(" not in src, (
        "_create_comparison_videos was renamed to _create_comparison_videos_for_period "
        "(and its start/duration args swapped) in commit 90aa139"
    )


# ===========================================================================
# Last-resort period confidence, validator → BRNGAnalysisResult
# ===========================================================================

def test_brng_result_defaults_to_normal_confidence():
    result = fa.BRNGAnalysisResult(
        violations=[], aggregate_patterns={}, actionable_report={},
        thumbnails=[], requires_border_adjustment=False,
    )
    assert result.period_confidence == "normal"
    assert result.period_confidence_note is None


def test_validator_records_note_when_it_keeps_a_black_period():
    fake_self = MagicMock()
    fake_self.duration = 1800.0
    fake_self.last_resort_period_note = None
    fake_self._shift_period_away_from_black = ISA._shift_period_away_from_black.__get__(fake_self)
    fake_self._fit_period_in_content_gap = lambda *a, **kw: None

    ISA._validate_periods_against_black_segments(
        fake_self, periods=[(1435.0, 60), (1765.0, 60)],
        black_segments=[(0.0, 1799.0)], effective_start=10.0, period_duration=60,
    )

    note = fake_self.last_resort_period_note
    assert note, "the last-resort fallback must record why confidence is reduced"
    assert "black" in note.lower()


def test_validator_leaves_note_unset_when_periods_are_clean():
    fake_self = MagicMock()
    fake_self.duration = 1800.0
    fake_self.last_resort_period_note = None

    ISA._validate_periods_against_black_segments(
        fake_self, periods=[(100.0, 60)], black_segments=[(0.0, 10.0)],
        effective_start=0.0, period_duration=60,
    )

    assert fake_self.last_resort_period_note is None


def test_validator_note_is_sticky_across_calls():
    """Validation runs at several points; one fallback taints the whole run."""
    fake_self = MagicMock()
    fake_self.duration = 1800.0
    fake_self.last_resort_period_note = None
    fake_self._shift_period_away_from_black = ISA._shift_period_away_from_black.__get__(fake_self)
    fake_self._fit_period_in_content_gap = lambda *a, **kw: None

    # First call trips the fallback
    ISA._validate_periods_against_black_segments(
        fake_self, [(1765.0, 60)], [(0.0, 1799.0)], 10.0, 60)
    first = fake_self.last_resort_period_note
    assert first

    # A later clean call must not erase it
    ISA._validate_periods_against_black_segments(
        fake_self, [(100.0, 60)], [(0.0, 10.0)], 0.0, 60)
    assert fake_self.last_resort_period_note == first


def _stub_brng_analyzer():
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = None
    analyzer.signals = None
    analyzer.check_cancelled = lambda: False
    analyzer._create_comparison_videos_for_period = lambda *a, **kw: True
    analyzer._analyze_differential_violations = lambda *a, **kw: ([], {
        "qctools_frames_targeted": 0, "frames_mapped_to_period": 0,
        "total_samples_analyzed": 0, "frames_checked": 0, "violations_found": 0,
    })
    return analyzer


def test_confidence_note_reaches_the_result(tmp_path):
    result = _stub_brng_analyzer().analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=[(1765.0, 60)],
        period_confidence_note="mostly black",
    )
    assert result.period_confidence == "last_resort"
    assert result.period_confidence_note == "mostly black"


def test_no_note_means_normal_confidence(tmp_path):
    result = _stub_brng_analyzer().analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=[(100.0, 60)],
    )
    assert result.period_confidence == "normal"
    assert result.period_confidence_note is None


def test_confidence_survives_asdict_for_the_report(tmp_path):
    """The report reads results['brng_analysis'], which is asdict() output."""
    from dataclasses import asdict
    result = _stub_brng_analyzer().analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=[(1765.0, 60)],
        period_confidence_note="mostly black",
    )
    data = asdict(result)
    assert data["period_confidence"] == "last_resort"
    assert data["period_confidence_note"] == "mostly black"


# ===========================================================================
# resolve_period_confidence — precedence between the two signals
# ===========================================================================

def test_resolve_period_confidence_normal_when_nothing_is_wrong():
    level, note = fa.resolve_period_confidence()
    assert level == "normal"
    assert note is None


def test_resolve_period_confidence_partial_coverage_only():
    level, note = fa.resolve_period_confidence(coverage_note="2 of 3 examined")
    assert level == "partial_coverage"
    assert note == "2 of 3 examined"


def test_resolve_period_confidence_last_resort_only():
    level, note = fa.resolve_period_confidence(last_resort_note="mostly black")
    assert level == "last_resort"
    assert note == "mostly black"


def test_resolve_period_confidence_last_resort_outranks_partial_coverage():
    """Wrong numbers are worse than incomplete ones — but keep both reasons."""
    level, note = fa.resolve_period_confidence(
        last_resort_note="mostly black", coverage_note="2 of 3 examined")
    assert level == "last_resort"
    assert "mostly black" in note and "2 of 3 examined" in note


def test_period_confidence_levels_are_ordered_least_to_most_compromised():
    assert fa.PERIOD_CONFIDENCE_LEVELS == ("normal", "partial_coverage", "last_resort")


def _analyzer_failing_periods(fail_indices):
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.video_path = Path("/v/in.mkv")
    analyzer.active_area = None
    analyzer.signals = None
    analyzer.check_cancelled = lambda: False
    calls = {"n": 0}

    def create(*a, **kw):
        calls["n"] += 1
        return (calls["n"] - 1) not in fail_indices

    analyzer._create_comparison_videos_for_period = create
    analyzer._analyze_differential_violations = lambda *a, **kw: ([], {
        "qctools_frames_targeted": 0, "frames_mapped_to_period": 0,
        "total_samples_analyzed": 0, "frames_checked": 0, "violations_found": 0,
    })
    return analyzer


_THREE_PERIODS = [(0.0, 60), (115.0, 60), (215.0, 60)]


def test_one_failed_period_marks_partial_coverage(tmp_path):
    result = _analyzer_failing_periods({0}).analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=_THREE_PERIODS)

    assert result.period_confidence == "partial_coverage"
    assert "2 of 3" in result.period_confidence_note


def test_all_periods_succeeding_stays_normal(tmp_path):
    result = _analyzer_failing_periods(set()).analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=_THREE_PERIODS)

    assert result.period_confidence == "normal"
    assert result.period_confidence_note is None


def test_black_sample_with_a_failed_period_reports_last_resort(tmp_path):
    result = _analyzer_failing_periods({0}).analyze_with_differential_detection(
        output_dir=tmp_path, analysis_periods=_THREE_PERIODS,
        period_confidence_note="Least-black candidate used.")

    assert result.period_confidence == "last_resort"
    # Neither reason is lost to the precedence choice
    assert "Least-black candidate used." in result.period_confidence_note
    assert "2 of 3" in result.period_confidence_note


# ---------------------------------------------------------------------------
# Head-switching bottom-edge widening
#
# It used to read 'artifact_height' / 'affected_percentage', keys border
# detection never writes, so it never fired. It now widens only by how far
# head switching reaches past the bottom crop border detection already made.
# ---------------------------------------------------------------------------

def _brng_analyzer_with_head_switching(head_switching, bottom_crop):
    analyzer = fa.DifferentialBRNGAnalyzer.__new__(fa.DifferentialBRNGAnalyzer)
    analyzer.upstream_context = fa.UpstreamAnalysisContext(
        period_diagnoses={}, period_active_area_brng={}, period_full_frame_brng={},
        avg_active_area_brng=0.0, overall_diagnosis="",
        head_switching=head_switching,
        border_widths={'left': 14, 'right': 17, 'top': 5, 'bottom': bottom_crop},
    )
    return analyzer


@pytest.mark.parametrize("head_switching, bottom_crop, expected", [
    # JPC_AV_03801: reaches 30px, 11px cropped -> 19px residual -> 24px strip
    ({'detected': True, 'percentage': 55.0, 'avg_height_px': 6, 'max_height_px': 30}, 11, 24),
    # 21419511_noNoise: reaches 30px, 19px cropped -> 11px residual, strip covers it
    ({'detected': True, 'percentage': 70.0, 'avg_height_px': 8, 'max_height_px': 30}, 19, 15),
    # Seen in too few frames
    ({'detected': True, 'percentage': 25.0, 'avg_height_px': 6, 'max_height_px': 30}, 11, 15),
    # Very tall residual is capped
    ({'detected': True, 'percentage': 90.0, 'avg_height_px': 6, 'max_height_px': 80}, 11, 40),
    # Not detected / absent
    ({'detected': False, 'percentage': 90.0, 'max_height_px': 30}, 0, 15),
    (None, 0, 15),
])
def test_head_switching_bottom_edge_width(head_switching, bottom_crop, expected):
    analyzer = _brng_analyzer_with_head_switching(head_switching, bottom_crop)
    assert analyzer._head_switching_bottom_edge_width(15) == expected


def test_head_switching_residual_band_is_classified_as_bottom_edge():
    """A 35px band of violations at the bottom: without widening it spills past
    the 15px strip into the adjacent band and reads as content; with the
    head-switching residual covering it, it is a bottom edge artifact."""
    mask = np.zeros((436, 670), dtype=np.uint8)
    mask[-35:, :] = 255

    no_hs = _brng_analyzer_with_head_switching(None, 11)
    assert 'bottom' not in no_hs._detect_edge_violations_enhanced(mask)['edges_affected']

    # reaches 41px, 11px cropped -> 30px residual -> 35px strip
    hs = {'detected': True, 'percentage': 80.0, 'avg_height_px': 6, 'max_height_px': 41}
    with_hs = _brng_analyzer_with_head_switching(hs, 11)
    assert 'bottom' in with_hs._detect_edge_violations_enhanced(mask)['edges_affected']


# ---------------------------------------------------------------------------
# Border detection off: signalstats measures the full frame only
#
# analyze() hands a full-frame placeholder BorderDetectionResult (method
# 'disabled') downstream. Signalstats used to treat it as a detected active
# area and compare the full frame with a "crop" of the same full frame,
# producing border/content diagnoses instead of the full-frame-only result.
# ---------------------------------------------------------------------------

def _full_frame_ss_analyzer(monkeypatch, periods):
    analyzer = fa.IntegratedSignalstatsAnalyzer.__new__(fa.IntegratedSignalstatsAnalyzer)
    analyzer.video_path = "/v/in.mkv"
    analyzer.qctools_report = "/v/in.mkv.qctools.xml.gz"
    analyzer.check_cancelled = lambda: False
    analyzer.signals = None
    analyzer.width, analyzer.height, analyzer.duration = 720, 486, 600.0
    analyzer.last_resort_period_note = None
    monkeypatch.setattr(analyzer, "_find_analysis_periods", lambda *a, **kw: periods)

    def qctools_period(start, end, num):
        # 40% of non-black frames flagged, max 2% of pixels
        return {'frames_analyzed': 100, 'frames_with_violations': 40,
                'brng_values': [0.02] * 40 + [0.0] * 60,
                'brng_frames': [(start + i / 30, 0.02) for i in range(40)]}
    monkeypatch.setattr(analyzer, "_parse_qctools_brng_period", qctools_period)

    ffprobe_calls = []

    def ffprobe_period(active_area, start, duration, num, progress_range=None):
        ffprobe_calls.append(active_area)
        return {'frames_analyzed': 100, 'frames_with_violations': 5,
                'brng_values': [0.001] * 5 + [0.0] * 95,
                'brng_frames': [(start + i / 30, 0.001) for i in range(5)]}
    monkeypatch.setattr(analyzer, "_analyze_with_ffprobe_period", ffprobe_period)
    return analyzer, ffprobe_calls


def _placeholder_border(area, method):
    return fa.BorderDetectionResult(active_area=area, border_regions={},
                                    detection_method=method, quality_frame_hints=[])


def test_signalstats_with_border_detection_disabled_is_full_frame_only(monkeypatch):
    analyzer, ffprobe_calls = _full_frame_ss_analyzer(monkeypatch, [(10.0, 60), (200.0, 60)])

    result = analyzer.analyze_with_signalstats(border_data=_placeholder_border((0, 0, 720, 486), 'disabled'))

    assert ffprobe_calls == []                   # no self-comparison pass
    assert result.analyzed_region == 'full_frame'
    assert "borders were not excluded" in result.diagnosis
    assert all('diagnosis' not in c for c in result.comparison_results)


def test_signalstats_with_detected_borders_still_compares(monkeypatch):
    analyzer, ffprobe_calls = _full_frame_ss_analyzer(monkeypatch, [(10.0, 60), (200.0, 60)])

    result = analyzer.analyze_with_signalstats(border_data=_placeholder_border((25, 25, 670, 436), 'simple'))

    assert ffprobe_calls == [(25, 25, 670, 436)] * 2
    assert result.analyzed_region == 'active_area'
    assert [c['diagnosis'] for c in result.comparison_results] == ['border_violations'] * 2


def _context_builder():
    obj = fa.EnhancedFrameAnalysis.__new__(fa.EnhancedFrameAnalysis)
    obj.border_detector = SimpleNamespace(width=720, height=486)
    obj.signalstats_analyzer = SimpleNamespace(_validate_periods_against_black_segments=lambda p, *a, **kw: p)
    return obj


def _ss_comparison_result(comparisons):
    return fa.SignalstatsResult(violation_percentage=10.0, max_brng=2.0, avg_brng=1.0,
                                analysis_periods=[], diagnosis="", used_qctools=True,
                                comparison_results=comparisons)


def test_upstream_context_skips_periods_without_a_comparison():
    """An unmeasured period must not read as 0% active-area BRNG (light sampling)."""
    comparisons = [
        {'period': 1, 'qctools_full_frame': {'violations_pct': 40.0, 'max_brng': 2.0}},
        {'period': 2, 'diagnosis': 'content_violations',
         'qctools_full_frame': {'violations_pct': 40.0, 'max_brng': 2.0},
         'ffprobe_active_area': {'violations_pct': 35.0, 'max_brng': 1.5}},
    ]
    ctx = _context_builder()._build_upstream_context(
        _ss_comparison_result(comparisons), _placeholder_border((0, 0, 720, 486), 'disabled'))

    assert ctx.period_diagnoses == {1: 'content_violations'}
    assert 0 not in ctx.period_active_area_brng


def test_period_refinement_keeps_periods_without_a_comparison():
    comparisons = [
        {'period': 1, 'qctools_full_frame': {'violations_pct': 40.0}},
        {'period': 2, 'qctools_full_frame': {'violations_pct': 40.0}},
    ]
    current = [(10.0, 60), (200.0, 60)]
    refined = _context_builder()._refine_periods_from_signalstats(
        current_periods=current, signalstats_results=_ss_comparison_result(comparisons),
        qctools_candidate_periods=[(400.0, 60), (500.0, 60)], black_segments=[],
        period_duration=60, color_bars_end_time=0)

    assert refined == current


# ---------------------------------------------------------------------------
# Bars safety margin is applied once, the same on every pass
#
# The first signalstats pass passed content_start_time = bars_end + 10 and
# _find_analysis_periods added another 10 (bars + 20s); refinement re-runs and
# BRNG fallbacks used bars + 10s.
# ---------------------------------------------------------------------------

def _period_finder(duration=600.0):
    analyzer = fa.IntegratedSignalstatsAnalyzer.__new__(fa.IntegratedSignalstatsAnalyzer)
    analyzer.duration = duration
    analyzer.last_resort_period_note = None
    return analyzer


@pytest.mark.parametrize("bars_end, expected_first_start", [(52.0, 62.0), (None, 10.0), (0, 10.0)])
def test_even_periods_start_one_margin_after_bars(bars_end, expected_first_start):
    periods = _period_finder()._find_analysis_periods(
        0, bars_end, 60, 3, None, qctools_periods=None, black_segments=[])
    assert periods[0][0] == pytest.approx(expected_first_start)


def test_margin_helper_matches_constant():
    assert fa.BARS_SAFETY_MARGIN_SECONDS == 10
    assert fa.content_start_after_bars(52.0) == 62.0
    assert fa.content_start_after_bars(None) == 10.0


# ---------------------------------------------------------------------------
# Active-area signalstats probe output parsing
#
# The probe used `-of csv=p=0`. A frame carrying side data (e.g. HDR mastering
# metadata) is written as "0.000000,0.209591," — the trailing separator left
# an empty BRNG value after the last-comma split, so the frame was silently
# dropped. Captured from a real HEVC clip with mastering-display side data.
# ---------------------------------------------------------------------------

SIDE_DATA_PROBE_OUTPUT = """\
[FRAME]
pts_time=0.000000
TAG:lavfi.signalstats.BRNG=0.209591
[SIDE_DATA]
side_data_type=Mastering display metadata
[/SIDE_DATA]
[SIDE_DATA]
[/SIDE_DATA]
[/FRAME]
[FRAME]
pts_time=0.033000
TAG:lavfi.signalstats.BRNG=0.21153
[/FRAME]
[FRAME]
pts_time=N/A
TAG:lavfi.signalstats.BRNG=0
[/FRAME]
[FRAME]
pts_time=0.100000
[/FRAME]
"""


def test_signalstats_frame_parser_keeps_frames_with_side_data():
    frames = list(fa.iter_signalstats_brng_frames(SIDE_DATA_PROBE_OUTPUT.splitlines(keepends=True)))

    assert frames == [(0.0, 0.209591), (0.033, 0.21153), (None, 0.0)]


def test_active_area_probe_counts_every_frame(monkeypatch):
    """End to end through _analyze_with_ffprobe_period with a fake ffprobe."""
    import io

    captured = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            captured['cmd'] = cmd
            self.stdout = io.StringIO(SIDE_DATA_PROBE_OUTPUT)
            self.returncode = 0

        def communicate(self):
            return "", ""

    monkeypatch.setattr(fa.subprocess, "Popen", FakeProc)
    analyzer = fa.IntegratedSignalstatsAnalyzer.__new__(fa.IntegratedSignalstatsAnalyzer)
    analyzer.video_path = "/v/in.mkv"
    analyzer.check_cancelled = lambda: False
    analyzer.signals = None

    result = analyzer._analyze_with_ffprobe_period((10, 10, 300, 220), 0.0, 1, 1)

    assert 'csv=p=0' not in captured['cmd']
    assert result['frames_analyzed'] == 3
    assert result['frames_with_violations'] == 2
    assert result['brng_frames'] == [(0.0, 0.209591), (0.033, 0.21153)]   # N/A pts has no timestamp
