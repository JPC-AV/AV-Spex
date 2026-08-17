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
  - parse_for_violations_streaming_period (time-window + max_frames cap)
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
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---- parse_for_violations_streaming_period -------------------------------

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
    out = parser.parse_for_violations_streaming_period(start_time=5.0, end_time=20.0, period_num=1)
    times = [v.timestamp for v in out]
    assert times == [12.0, 10.0]  # sorted by violation_score desc


def test_parse_period_caps_results_to_max_frames(tmp_path):
    frames = [
        {"pkt_pts_time": str(t), "tags": dict(_NORMAL_TAGS, BRNG=f"0.{t:02d}")}
        for t in range(1, 10)  # 9 violation candidates
    ]
    path = _write_qctools(tmp_path, frames)
    parser = fa.QCToolsParser(path)
    out = parser.parse_for_violations_streaming_period(0.0, 100.0, period_num=1, max_frames=3)
    assert len(out) == 3
    # Top 3 by violation_score should be the highest-BRNG frames
    assert out[0].brng_value > out[1].brng_value > out[2].brng_value


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
    out = parser.parse_for_violations_streaming_period(0.0, 100.0, period_num=1)
    assert len(out) == 1


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
        brng_duration_limit=120,
        brng_skip_color_bars=False,
        max_border_retries=5,
    )

    fa.analyze_frame_quality(
        "/v/in.mkv", frame_config=cfg, color_bars_end_time=4.5,
    )

    call = fake_analyzer.analyze.call_args
    assert call.kwargs["method"] == "sophisticated"
    assert call.kwargs["duration_limit"] == 120
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


def test_ffprobe_video_properties_returns_none_on_bad_dimensions(monkeypatch):
    completed = MagicMock()
    completed.stdout = '{"streams": [{"width": 0, "height": 0}]}'
    monkeypatch.setattr(fa.subprocess, "run", lambda *a, **kw: completed)

    assert fa._ffprobe_video_properties("/v/in.mkv") is None
