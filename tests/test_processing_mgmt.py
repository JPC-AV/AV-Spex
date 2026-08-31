"""Tests for AV_Spex.processing.processing_mgmt.

The focus is the head-bars consensus: qct-parse and CLAMS both detect colour
bars, they disagree in known ways, and the resolution was calibrated against
real tapes (see the JPC_AV_02212 / JPC_AV_01056 notes). Its output —
``color_bars_end_time`` — drives access-file trimming, the BRNG skip window and
chroma-phase detection, so a silent regression here would surface only as a
subtly wrong report.

Also covered: the CSV/SSIM helpers the consensus reads, and the small pure
utilities around them.
"""

import csv
import os

import pytest

from AV_Spex.processing import processing_mgmt as pm
from AV_Spex.processing.processing_mgmt import (
    HEAD_BARS_START_THRESHOLD,
    HEAD_END_AGREEMENT_SLACK,
    CLAMS_SSIM_CORROBORATION_MIN,
    merge_head_bars_consensus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def consensus(qct_start=None, qct_end=None, qct_regions=None,
              clams_start=None, clams_end=None, clams_regions=None,
              clams_ran=True, verdict=None):
    """Run the consensus with a fixed SSIM verdict."""
    calls = []

    def arbiter(lo, hi):
        calls.append((lo, hi))
        return verdict

    merged_end, demoted, regions = merge_head_bars_consensus(
        qctparse_head_start=qct_start,
        qctparse_head_end=qct_end,
        qctparse_all_regions=qct_regions or [],
        clams_head_start=clams_start,
        clams_head_end_raw=clams_end,
        clams_regions=clams_regions or [],
        clams_ran=clams_ran,
        region_is_bars=arbiter,
    )
    return merged_end, demoted, regions, calls


# ---------------------------------------------------------------------------
# Consensus: the two detectors agree
# ---------------------------------------------------------------------------

def test_agreement_takes_the_later_end_and_asks_no_questions():
    """Within the slack the detectors are treated as agreeing, and the later
    end wins so a trim never cuts into remaining bars."""
    end, demoted, _, calls = consensus(
        qct_start=0.0, qct_end=60.0, clams_start=0.0, clams_end=62.0)
    assert end == 62.0
    assert demoted is False
    assert calls == [], "no SSIM arbitration needed when they agree"


def test_agreement_slack_boundary_is_inclusive():
    end, _, _, calls = consensus(
        qct_start=0.0, qct_end=60.0,
        clams_start=0.0, clams_end=60.0 + HEAD_END_AGREEMENT_SLACK)
    assert end == 60.0 + HEAD_END_AGREEMENT_SLACK
    assert calls == []


def test_just_outside_the_slack_triggers_arbitration():
    _, _, _, calls = consensus(
        qct_start=0.0, qct_end=60.0,
        clams_start=0.0, clams_end=60.0 + HEAD_END_AGREEMENT_SLACK + 0.1,
        verdict=True)
    assert calls, "disagreement beyond the slack must consult the SSIM evidence"


# ---------------------------------------------------------------------------
# Consensus: the detectors disagree — SSIM arbitrates
# ---------------------------------------------------------------------------

def test_disputed_span_rejected_by_ssim_uses_the_earlier_end():
    """Luma thresholds cannot tell bars from a saturated slate; when SSIM says
    the disputed span is not bars, the earlier end is the honest answer."""
    end, demoted, _, calls = consensus(
        qct_start=0.0, qct_end=20.0, clams_start=0.0, clams_end=62.0,
        verdict=False)
    assert end == 20.0
    assert demoted is False
    assert calls == [(20.0, 62.0)], "arbitration covers exactly the disputed span"


def test_disputed_span_confirmed_by_ssim_uses_the_later_end():
    end, _, _, _ = consensus(
        qct_start=0.0, qct_end=20.0, clams_start=0.0, clams_end=62.0,
        verdict=True)
    assert end == 62.0


def test_inconclusive_ssim_keeps_the_later_end():
    """None means 'not enough evidence' — that must not read as rejection."""
    end, demoted, _, _ = consensus(
        qct_start=0.0, qct_end=20.0, clams_start=0.0, clams_end=62.0,
        verdict=None)
    assert end == 62.0
    assert demoted is False


def test_arbitration_span_is_ordered_low_to_high():
    """qct-parse may claim the later end; the span is still asked low-to-high."""
    _, _, _, calls = consensus(
        qct_start=0.0, qct_end=62.0, clams_start=0.0, clams_end=20.0,
        verdict=True)
    assert calls == [(20.0, 62.0)]


# ---------------------------------------------------------------------------
# Consensus: only one detector has a claim
# ---------------------------------------------------------------------------

def test_lone_qctparse_claim_contradicted_by_clams_is_demoted():
    """A head claim CLAMS decisively contradicts must not drive trimming."""
    end, demoted, _, calls = consensus(
        qct_start=0.0, qct_end=60.0, clams_ran=True, verdict=False)
    assert end is None, "a demoted claim sets no head-bars end time"
    assert demoted is True
    assert calls == [(0.0, 60.0)]


def test_lone_qctparse_claim_stands_when_clams_did_not_run():
    end, demoted, _, calls = consensus(
        qct_start=0.0, qct_end=60.0, clams_ran=False, verdict=False)
    assert end == 60.0
    assert demoted is False
    assert calls == [], "no CLAMS run means no evidence to demote with"


def test_lone_qctparse_claim_stands_on_inconclusive_evidence():
    end, demoted, _, _ = consensus(
        qct_start=0.0, qct_end=60.0, clams_ran=True, verdict=None)
    assert end == 60.0
    assert demoted is False


def test_lone_qctparse_claim_without_a_start_is_not_arbitrated():
    """Without a start there is no span to check, so the claim stands."""
    end, demoted, _, calls = consensus(
        qct_start=None, qct_end=60.0, clams_ran=True, verdict=False)
    assert end == 60.0 and demoted is False and calls == []


def test_lone_clams_claim_is_used():
    end, demoted, _, _ = consensus(clams_start=0.0, clams_end=62.0)
    assert end == 62.0 and demoted is False


def test_neither_detector_found_head_bars():
    end, demoted, regions, _ = consensus()
    assert end is None and demoted is False and regions is None


# ---------------------------------------------------------------------------
# Consensus: what counts as *head* bars
# ---------------------------------------------------------------------------

def test_late_clams_span_is_not_head_bars():
    """Bars starting well into the file are mid-file bars, not head bars."""
    end, _, _, _ = consensus(
        clams_start=HEAD_BARS_START_THRESHOLD + 1, clams_end=200.0)
    assert end is None


def test_clams_span_just_inside_the_head_window_counts():
    end, _, _, _ = consensus(
        clams_start=HEAD_BARS_START_THRESHOLD - 0.1, clams_end=200.0)
    assert end == 200.0


def test_late_clams_span_does_not_arbitrate_a_qctparse_claim():
    """A mid-file CLAMS span is irrelevant to the head claim."""
    end, demoted, _, calls = consensus(
        qct_start=0.0, qct_end=60.0,
        clams_start=500.0, clams_end=520.0, clams_ran=True, verdict=False)
    assert demoted is True, "still demoted — CLAMS ran and saw no head bars"
    assert calls == [(0.0, 60.0)]


# ---------------------------------------------------------------------------
# Exclusion spans: what downstream analysis skips
# ---------------------------------------------------------------------------

def test_exclusion_spans_merge_both_detectors():
    _, _, regions, _ = consensus(
        qct_start=0.0, qct_end=60.0,
        qct_regions=[("head", 0.0, 60.0), ("additional-1", 300.0, 310.0)],
        clams_start=0.0, clams_end=62.0,
        clams_regions=[(0.0, 62.0, "bars")])
    assert regions == [(0.0, 62.0), (300.0, 310.0)]


def test_tone_spans_are_not_excluded():
    """Only bars are skipped; a tone span is audio, not picture."""
    _, _, regions, _ = consensus(
        clams_start=0.0, clams_end=62.0,
        clams_regions=[(0.0, 62.0, "bars"), (500.0, 505.0, "tone")])
    assert regions == [(0.0, 62.0)]


def test_demoted_head_region_is_dropped_from_exclusions():
    """If the consensus decided it isn't bars, don't skip it either."""
    _, demoted, regions, _ = consensus(
        qct_start=0.0, qct_end=60.0,
        qct_regions=[("head", 0.0, 60.0), ("additional-1", 300.0, 310.0)],
        clams_ran=True, verdict=False)
    assert demoted is True
    assert regions == [(300.0, 310.0)], "the demoted head span is not excluded"


def test_head_relaxed_label_is_also_dropped_when_demoted():
    _, _, regions, _ = consensus(
        qct_start=0.0, qct_end=60.0,
        qct_regions=[("head-relaxed", 0.0, 60.0)],
        clams_ran=True, verdict=False)
    assert regions is None


def test_adjacent_spans_within_a_second_are_coalesced():
    """A detector can report one run as two fragments a fraction apart."""
    _, _, regions, _ = consensus(
        qct_regions=[("additional-1", 10.0, 20.0), ("additional-2", 20.5, 30.0)])
    assert regions == [(10.0, 30.0)]


def test_spans_more_than_a_second_apart_stay_separate():
    _, _, regions, _ = consensus(
        qct_regions=[("additional-1", 10.0, 20.0), ("additional-2", 21.5, 30.0)])
    assert regions == [(10.0, 20.0), (21.5, 30.0)]


def test_nested_span_does_not_shorten_the_enclosing_one():
    _, _, regions, _ = consensus(
        qct_regions=[("a", 10.0, 40.0), ("b", 15.0, 20.0)])
    assert regions == [(10.0, 40.0)]


def test_regions_with_missing_bounds_are_ignored():
    _, _, regions, _ = consensus(
        qct_regions=[("head", None, 60.0), ("additional-1", 300.0, None),
                     ("additional-2", 400.0, 410.0)])
    assert regions == [(400.0, 410.0)]


# ---------------------------------------------------------------------------
# parse_colorbars_duration_csv
# ---------------------------------------------------------------------------

def _write(path, rows):
    with open(path, 'w', newline='') as f:
        csv.writer(f).writerows(rows)
    return str(path)


def test_parse_durations_labeled_three_column_format(tmp_path):
    p = _write(tmp_path / "d.csv", [
        ["qct-parse color bars found:"],
        ["head", "00:00:03.000", "00:01:02.500"],
        ["additional-1", "00:05:00.000", "00:05:10.000"],
    ])
    start, end, regions = pm.parse_colorbars_duration_csv(p)
    assert start == 3.0
    assert end == 62.5
    assert regions == [("head", 3.0, 62.5), ("additional-1", 300.0, 310.0)]


def test_parse_durations_legacy_two_column_format(tmp_path):
    """The older CSV had no labels; the single row is the head region."""
    p = _write(tmp_path / "d.csv", [
        ["qct-parse color bars found:"],
        ["00:00:03.000", "00:01:02.500"],
    ])
    start, end, regions = pm.parse_colorbars_duration_csv(p)
    assert (start, end) == (3.0, 62.5)
    assert regions == [("head", 3.0, 62.5)]


def test_parse_durations_head_relaxed_counts_as_head(tmp_path):
    p = _write(tmp_path / "d.csv", [
        ["qct-parse color bars found:"],
        ["additional-1", "00:05:00.000", "00:05:10.000"],
        ["head-relaxed", "00:00:01.000", "00:00:55.000"],
    ])
    start, end, _ = pm.parse_colorbars_duration_csv(p)
    assert (start, end) == (1.0, 55.0), "the relaxed head pass is still the head"


def test_parse_durations_falls_back_to_the_first_region(tmp_path):
    p = _write(tmp_path / "d.csv", [
        ["qct-parse color bars found:"],
        ["additional-1", "00:05:00.000", "00:05:10.000"],
    ])
    start, end, _ = pm.parse_colorbars_duration_csv(p)
    assert (start, end) == (300.0, 310.0)


def test_parse_durations_no_bars_marker(tmp_path):
    p = _write(tmp_path / "d.csv", [["qct-parse found no color bars"]])
    assert pm.parse_colorbars_duration_csv(p) == (None, None, [])


def test_parse_durations_missing_file(tmp_path):
    assert pm.parse_colorbars_duration_csv(str(tmp_path / "nope.csv")) == (None, None, [])


def test_parse_durations_empty_file(tmp_path):
    p = tmp_path / "d.csv"; p.write_text("")
    assert pm.parse_colorbars_duration_csv(str(p)) == (None, None, [])


# ---------------------------------------------------------------------------
# _clams_max_ssim_in_span
# ---------------------------------------------------------------------------

def _ssim_csv(tmp_path, video_id, rows):
    from AV_Spex.checks import bars_detection_clams
    path = tmp_path / f"{video_id}{bars_detection_clams.SSIM_SCORES_CSV_SUFFIX}"
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(["pass", "frame", "timestamp", "ssim_score", "exceeds_threshold"])
        w.writerows(rows)
    return path


def test_max_ssim_only_counts_samples_inside_the_span(tmp_path):
    _ssim_csv(tmp_path, "V1", [
        ["primary", 1, "00:00:05.000", "0.30", "False"],
        ["primary", 2, "00:00:15.000", "0.80", "True"],
        ["primary", 3, "00:00:25.000", "0.90", "True"],
        ["primary", 4, "00:01:00.000", "0.99", "True"],
    ])
    score, count = pm._clams_max_ssim_in_span(str(tmp_path), "V1", 10.0, 30.0)
    assert count == 2
    assert score == pytest.approx(0.90)


def test_max_ssim_span_bounds_are_inclusive(tmp_path):
    _ssim_csv(tmp_path, "V1", [["p", 1, "00:00:10.000", "0.42", "False"]])
    score, count = pm._clams_max_ssim_in_span(str(tmp_path), "V1", 10.0, 10.0)
    assert (score, count) == (pytest.approx(0.42), 1)


def test_max_ssim_missing_csv(tmp_path):
    assert pm._clams_max_ssim_in_span(str(tmp_path), "V1", 0.0, 10.0) == (None, 0)


def test_max_ssim_skips_malformed_rows(tmp_path):
    _ssim_csv(tmp_path, "V1", [
        ["p", 1, "not-a-timestamp", "0.9", "True"],
        ["p", 2, "00:00:05.000", "not-a-number", "True"],
        ["p", 3],
        ["p", 4, "00:00:06.000", "0.75", "True"],
    ])
    score, count = pm._clams_max_ssim_in_span(str(tmp_path), "V1", 0.0, 10.0)
    assert (score, count) == (pytest.approx(0.75), 1)


# ---------------------------------------------------------------------------
# _clams_region_is_bars
# ---------------------------------------------------------------------------

def test_region_is_bars_uses_recorded_scores_when_they_cover_the_span(tmp_path):
    """Three or more recorded samples are enough — no rescan needed."""
    _ssim_csv(tmp_path, "V1", [
        ["p", 1, "00:00:01.000", "0.80", "True"],
        ["p", 2, "00:00:02.000", "0.70", "True"],
        ["p", 3, "00:00:03.000", "0.60", "True"],
    ])
    assert pm._clams_region_is_bars("/v.mkv", str(tmp_path), "V1", 0.0, 10.0) is True


def test_region_is_not_bars_when_scores_stay_below_the_floor(tmp_path):
    _ssim_csv(tmp_path, "V1", [
        ["p", 1, "00:00:01.000", "0.20", "False"],
        ["p", 2, "00:00:02.000", "0.30", "False"],
        ["p", 3, "00:00:03.000", "0.40", "False"],
    ])
    assert pm._clams_region_is_bars("/v.mkv", str(tmp_path), "V1", 0.0, 10.0) is False


def test_region_is_bars_at_the_corroboration_floor(tmp_path):
    _ssim_csv(tmp_path, "V1", [
        ["p", i, f"00:00:0{i}.000", str(CLAMS_SSIM_CORROBORATION_MIN), "True"]
        for i in (1, 2, 3)
    ])
    assert pm._clams_region_is_bars("/v.mkv", str(tmp_path), "V1", 0.0, 10.0) is True


@pytest.mark.parametrize("start,end", [(None, 10.0), (0.0, None), (10.0, 10.0), (10.0, 5.0)])
def test_region_is_bars_rejects_a_degenerate_span(tmp_path, start, end):
    assert pm._clams_region_is_bars("/v.mkv", str(tmp_path), "V1", start, end) is None


def test_region_is_bars_without_a_report_directory():
    assert pm._clams_region_is_bars("/v.mkv", None, "V1", 0.0, 10.0) is None


def test_region_is_bars_returns_none_when_cancelled(tmp_path):
    """Too few recorded samples would mean a rescan; cancellation stops that."""
    _ssim_csv(tmp_path, "V1", [["p", 1, "00:00:01.000", "0.80", "True"]])
    assert pm._clams_region_is_bars(
        "/v.mkv", str(tmp_path), "V1", 0.0, 10.0,
        check_cancelled=lambda: True) is None


# ---------------------------------------------------------------------------
# find_qctools_report
# ---------------------------------------------------------------------------

def test_find_qctools_report_prefers_qc_metadata(tmp_path):
    qc = tmp_path / "V1_qc_metadata"; qc.mkdir()
    vr = tmp_path / "V1_vrecord_metadata"; vr.mkdir()
    (qc / "V1.qctools.xml.gz").write_text("")
    (vr / "V1.qctools.xml.gz").write_text("")
    assert pm.find_qctools_report(str(tmp_path), "V1").endswith(
        os.path.join("V1_qc_metadata", "V1.qctools.xml.gz"))


def test_find_qctools_report_falls_back_to_vrecord(tmp_path):
    (tmp_path / "V1_qc_metadata").mkdir()
    vr = tmp_path / "V1_vrecord_metadata"; vr.mkdir()
    (vr / "V1.qctools.xml.gz").write_text("")
    assert "V1_vrecord_metadata" in pm.find_qctools_report(str(tmp_path), "V1")


def test_find_qctools_report_accepts_the_mkv_form(tmp_path):
    qc = tmp_path / "V1_qc_metadata"; qc.mkdir()
    (qc / "V1.qctools.mkv").write_text("")
    assert pm.find_qctools_report(str(tmp_path), "V1").endswith(".qctools.mkv")


def test_find_qctools_report_none_when_absent(tmp_path):
    (tmp_path / "V1_qc_metadata").mkdir()
    assert pm.find_qctools_report(str(tmp_path), "V1") is None


def test_find_qctools_report_ignores_hidden_files(tmp_path):
    qc = tmp_path / "V1_qc_metadata"; qc.mkdir()
    (qc / "._V1.qctools.xml.gz").write_text("")
    assert pm.find_qctools_report(str(tmp_path), "V1") is None


# ---------------------------------------------------------------------------
# calculate_border_regions
# ---------------------------------------------------------------------------

def test_border_regions_all_four_sides():
    r = pm.calculate_border_regions(10, 6, 700, 474, 720, 486)
    assert r['left_border'] == (0, 0, 10, 486)
    assert r['right_border'] == (710, 0, 10, 486)
    assert r['top_border'] == (0, 0, 720, 6)
    assert r['bottom_border'] == (0, 480, 720, 6)


def test_border_regions_none_when_active_area_fills_the_frame():
    r = pm.calculate_border_regions(0, 0, 720, 486, 720, 486)
    assert all(v is None for v in r.values())


# ---------------------------------------------------------------------------
# extract_percentage
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line,expected", [
    ("......    42 of 100 %", 42),          # the dotted form qcli emits
    ("...............  100 of 100 %", 100),
    ("1 of 100 %", 1),                      # early progress, before any dots
])
def test_extract_percentage_reads_qctools_progress(line, expected):
    """qcli reports as 'dots + N of 100 %', not a bare percentage."""
    assert pm.extract_percentage(line, signals=object()) == expected


@pytest.mark.parametrize("line", [
    "no digits here", "", "frame= 100 fps=25", "Processing: 42%",
])
def test_extract_percentage_ignores_other_output(line):
    assert pm.extract_percentage(line, signals=object()) is None
