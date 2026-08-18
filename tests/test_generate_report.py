"""Tests for utils.generate_report.

Module is large (4993 LOC) and most of it is HTML/CSV scaffolding for the
final report. This file covers the pure-logic helpers and small subprocess
wrappers; the big section-renderers (make_*_html, generate_*_html) are not
directly tested because they're long string-builders that depend on heavy
fixture inputs and are exercised end-to-end in real runs.

Coverage:
* csv_to_html_table — header + body, mismatch styling, fail/pass styling, error fallback
* read_text_file — UTF-8 + latin-1 fallback + missing-file
* prepare_file_section — empty-path branch + delegation
* image_to_data_uri — base64 encoding + missing-file empty fallback
* parse_timestamp — valid + malformed → placeholder tuple
* parse_profile — prefix mapping + default
* find_qct_thumbs — filename parsing into sorted dict
* find_report_csvs — directory scanning into the 22-tuple
* read_xml_file — UTF-8 + latin-1 fallback + missing-file
* _get_video_duration / _get_audio_channel_count — ffprobe wrappers
* _build_waveform_filter — mono / stereo / multi-channel filter graph
* _imbalance_status_colors — characterization → (text, bg, border)
* _parse_bars_durations_csv — qct-parse + CLAMS row schemas
* _parse_tone_detection_csv — present + empty + malformed
* _seconds_to_display — sub-hour + multi-hour
* _extract_frame_at — ffmpeg seek-then-decode command construction
"""

import csv
import os
import subprocess
from base64 import b64decode
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.utils import generate_report as gr


# ===========================================================================
# Section 1 — csv_to_html_table
# ===========================================================================

def _write_csv(path, rows):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)


def test_csv_to_html_table_renders_header_and_body(tmp_path):
    csv_path = tmp_path / "data.csv"
    _write_csv(csv_path, [
        ["A", "B"],
        ["1", "2"],
        ["3", "4"],
    ])
    html = gr.csv_to_html_table(str(csv_path))
    assert "<table>" in html and "</table>" in html
    assert "<th>A</th>" in html and "<th>B</th>" in html
    assert "<td>1</td>" in html and "<td>2</td>" in html


def test_csv_to_html_table_check_fail_styles_pass_and_fail(tmp_path):
    csv_path = tmp_path / "data.csv"
    _write_csv(csv_path, [
        ["Field", "Result"],
        ["x", "pass"],
        ["y", "fail"],
    ])
    html = gr.csv_to_html_table(str(csv_path), check_fail=True)
    assert 'class="cell-match">pass' in html
    assert 'class="cell-mismatch">fail' in html


def test_csv_to_html_table_style_mismatched_marks_diff_columns(tmp_path):
    """When cols 1 and 2 differ, col 2 gets cell-match and col 3 gets cell-mismatch."""
    csv_path = tmp_path / "data.csv"
    _write_csv(csv_path, [
        ["Field", "Expected", "Actual", "Note"],
        ["fps",   "29.97",   "29.97",  "ok"],   # match → no styling on cols 2/3
        ["fmt",   "FFV1",    "H264",   "diff"], # mismatch → col 2 is match-color, col 3 is mismatch-color
    ])
    html = gr.csv_to_html_table(str(csv_path), style_mismatched=True)
    # The "ok" row should have plain cells
    assert "<td>29.97</td>" in html
    # The mismatched row uses the match/mismatch CSS classes
    assert 'class="cell-match">H264' in html
    assert 'class="cell-mismatch">diff' in html


def test_csv_to_html_table_empty_value_in_actual_skips_mismatch_styling(tmp_path):
    """If row[2] is empty, the row is treated as 'no actual value' and skips the diff styling."""
    csv_path = tmp_path / "data.csv"
    _write_csv(csv_path, [
        ["Field", "Expected", "Actual", "Note"],
        ["fmt",   "FFV1",    "",       "missing"],
    ])
    html = gr.csv_to_html_table(str(csv_path), style_mismatched=True)
    # Plain <td> for the missing-actual row
    assert 'class="cell-mismatch"' not in html
    assert 'class="cell-match"' not in html


def test_csv_to_html_table_falls_back_to_latin1_on_decode_error(tmp_path):
    """latin-1 fallback should be used when UTF-8 fails."""
    csv_path = tmp_path / "data.csv"
    csv_path.write_bytes(b"Header\n\xe9\n")  # \xe9 is invalid UTF-8 start byte for a single byte
    html = gr.csv_to_html_table(str(csv_path))
    # Header rendered; é-character (latin-1 0xe9) shows up in the body
    assert "<th>Header</th>" in html


def test_csv_to_html_table_missing_file_returns_error_paragraph():
    html = gr.csv_to_html_table("/no/such/file.csv")
    assert "<p>Error processing CSV file:" in html


# ===========================================================================
# Section 1b — make_mkvalidator_html / _read_mkvalidator_summary
# ===========================================================================

def _write_mkvalidator_summary(path, verdict, error_count=0, wrn0c2=0):
    path.write_text(
        f"{verdict}\n\n"
        f"Errors (ERR): {error_count}\n"
        f"WRN0C2 non-incrementing cluster timecode warnings: {wrn0c2}\n"
        f"Other warnings: 0\n"
    )


def test_read_mkvalidator_summary_valid(tmp_path):
    p = tmp_path / "s.txt"
    _write_mkvalidator_summary(p, "mkvalidator 0.6.0: the file appears to be valid")
    verdict, is_valid, errors = gr._read_mkvalidator_summary(str(p))
    assert is_valid is True and errors == 0
    assert "appears to be valid" in verdict


def test_read_mkvalidator_summary_invalid_by_verdict(tmp_path):
    p = tmp_path / "s.txt"
    _write_mkvalidator_summary(p, "mkvalidator 0.6.0: the file is not valid")
    _, is_valid, _ = gr._read_mkvalidator_summary(str(p))
    assert is_valid is False


def test_read_mkvalidator_summary_invalid_by_error_count(tmp_path):
    p = tmp_path / "s.txt"
    # Verdict reads valid but ERR errors were reported -> not valid.
    _write_mkvalidator_summary(p, "mkvalidator 0.6.0: the file appears to be valid", error_count=2)
    _, is_valid, errors = gr._read_mkvalidator_summary(str(p))
    assert is_valid is False and errors == 2


def test_make_mkvalidator_html_valid_with_warnings(tmp_path):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file appears to be valid", wrn0c2=2)
    csv_path = tmp_path / "JPC_AV_TEST_mkvalidator_clusters.csv"
    _write_csv(csv_path, [
        ["cluster_index", "byte_offset"],
        ["1", "710818"],
        ["2", "74886584"],
    ])
    html = gr.make_mkvalidator_html(str(summary), str(csv_path))
    assert html is not None
    # Verdict is the focus: a green "Valid" status.
    assert "Valid" in html and "#1f7a4d" in html
    # WRN0C2 warnings table follows, with the byte offsets and a narrow wrapper.
    assert "<strong>2</strong>" in html and "WRN0C2" in html
    assert "710818" in html and "74886584" in html
    assert "max-width: 320px" in html


def test_make_mkvalidator_html_adds_approx_timecode_column(tmp_path, monkeypatch):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file appears to be valid", wrn0c2=2)
    csv_path = tmp_path / "JPC_AV_TEST_mkvalidator_clusters.csv"
    _write_csv(csv_path, [
        ["cluster_index", "byte_offset"],
        ["1", "500"],
        ["2", "1000"],
    ])
    video = tmp_path / "v.mkv"
    video.write_bytes(b"x")  # must exist for the mapper's isfile check

    # file_size=1000, duration=100s -> offset 500 = 50.0s, offset 1000 = 100.0s.
    monkeypatch.setattr(gr.os.path, "getsize", lambda p: 1000)
    monkeypatch.setattr(gr, "_get_video_duration", lambda p: 100.0)
    from AV_Spex.checks import qct_parse as qp
    monkeypatch.setattr(qp, "_get_video_frame_rate", lambda p: 30.0)
    monkeypatch.setattr(qp, "_get_video_start_timecode", lambda p: None)

    html = gr.make_mkvalidator_html(str(summary), str(csv_path), str(video))
    assert "Approx. timecode" in html
    # 50.0s @ 30fps NDF, no start offset -> 00:00:50:00 ; 100.0s -> 00:01:40:00
    assert "00:00:50:00" in html
    assert "00:01:40:00" in html
    # Wider table to fit the third column.
    assert "max-width: 460px" in html


def test_make_mkvalidator_html_no_video_path_omits_timecode_column(tmp_path):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file appears to be valid", wrn0c2=1)
    csv_path = tmp_path / "JPC_AV_TEST_mkvalidator_clusters.csv"
    _write_csv(csv_path, [["cluster_index", "byte_offset"], ["1", "500"]])

    # No video_path -> no timecode column header (the methodology blurb still
    # mentions the column by name, so check for the <th> specifically), narrow table.
    html = gr.make_mkvalidator_html(str(summary), str(csv_path), None)
    assert ">Approx. timecode</th>" not in html
    assert "max-width: 320px" in html


def test_make_mkvalidator_html_valid_no_warnings(tmp_path):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file appears to be valid")
    # No clusters CSV -> section still renders, focused on the verdict.
    html = gr.make_mkvalidator_html(str(summary), None)
    assert html is not None
    assert "Valid" in html
    assert "No timecode warnings." in html


def test_make_mkvalidator_html_invalid(tmp_path):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file is not valid", error_count=1)
    html = gr.make_mkvalidator_html(str(summary), None)
    assert html is not None
    assert "Invalid" in html and "#b3261e" in html


def test_make_mkvalidator_html_includes_methodology_explainer(tmp_path):
    summary = tmp_path / "JPC_AV_TEST_mkvalidator_summary.txt"
    _write_mkvalidator_summary(summary, "mkvalidator 0.6.0: the file appears to be valid")
    html = gr.make_mkvalidator_html(str(summary), None)
    # Collapsible explainer present and wired to the shared toggleContent() helper.
    assert "What is mkvalidator?" in html
    assert "toggleContent('mkvalidator_methodology'" in html
    assert 'id="mkvalidator_methodology"' in html
    # Touches the three topics it should explain.
    assert "WRN0C2" in html
    assert "Byte offset" in html and "timecode" in html


def test_make_mkvalidator_html_missing_summary_returns_none():
    assert gr.make_mkvalidator_html("/no/such/summary.txt", None) is None
    assert gr.make_mkvalidator_html(None, None) is None


# ===========================================================================
# Section 2 — read_text_file / prepare_file_section / image_to_data_uri
# ===========================================================================

def test_read_text_file_utf8(tmp_path):
    p = tmp_path / "x.txt"
    p.write_text("hello world")
    assert gr.read_text_file(str(p)) == "hello world"


def test_read_text_file_falls_back_to_latin1(tmp_path):
    """Non-UTF-8 byte should be decoded with latin-1 fallback."""
    p = tmp_path / "x.txt"
    p.write_bytes(b"\xe9 caf\xe9")  # latin-1 for "é café"
    text = gr.read_text_file(str(p))
    assert "é" in text


def test_read_text_file_missing_file_raises(tmp_path):
    """Function only catches UnicodeDecodeError; FileNotFoundError bubbles up."""
    with pytest.raises(FileNotFoundError):
        gr.read_text_file(str(tmp_path / "no_such.txt"))


def test_prepare_file_section_with_path(tmp_path):
    p = tmp_path / "doc.txt"
    p.write_text("body")
    content, name = gr.prepare_file_section(str(p))
    assert content == "body"
    assert name == "doc.txt"


def test_prepare_file_section_no_path_returns_empty_pair():
    assert gr.prepare_file_section(None) == ("", "")
    assert gr.prepare_file_section("") == ("", "")


def test_prepare_file_section_with_custom_processor(tmp_path):
    """The processor takes precedence over read_text_file."""
    p = tmp_path / "doc.xml"
    p.write_text("<root/>")
    content, name = gr.prepare_file_section(str(p), process_function=lambda _: "processed")
    assert content == "processed"
    assert name == "doc.xml"


def test_find_qc_metadata_picks_up_fixity_summary_json(tmp_path):
    dest = tmp_path / "JPC_AV_00001_qc_metadata"
    dest.mkdir()
    summary = dest / "JPC_AV_00001_fixity_summary.json"
    summary.write_text("{}")

    out = gr.find_qc_metadata(str(dest))
    assert len(out) == 8
    (exiftool_path, ffprobe_path, mediainfo_path, mediaconch_csv,
     fixity_sidecar, fixity_summary_json, policy_content, policy_name) = out
    assert fixity_summary_json == str(summary)
    assert fixity_sidecar is None
    assert mediaconch_csv is None


def test_find_qc_metadata_empty_dir_returns_all_none(tmp_path):
    dest = tmp_path / "JPC_AV_00001_qc_metadata"
    dest.mkdir()
    out = gr.find_qc_metadata(str(dest))
    assert len(out) == 8
    assert all(v is None for v in out)


def _write_fixity_summary(tmp_path, summary_dict):
    import json
    path = tmp_path / "JPC_AV_00001_fixity_summary.json"
    path.write_text(json.dumps(summary_dict))
    return str(path)


def test_make_fixity_section_html_whole_file_passed(tmp_path):
    computed = "a" * 32
    path = _write_fixity_summary(tmp_path, {
        "video_id": "JPC_AV_00001",
        "whole_file": {
            "result": "passed",
            "algorithm": "md5",
            "computed_checksum": computed,
            "validated_at": "2026-07-16 10:00:00",
            "validated_against": [
                {"file": "JPC_AV_00001_2026_04_19_fixity.md5",
                 "date": "2026-04-19", "checksum": computed},
            ],
        },
    })
    html = gr.make_fixity_section_html(path, "")
    assert "Whole-file checksum" in html
    assert "Passed" in html
    assert computed in html
    assert "JPC_AV_00001_2026_04_19_fixity.md5" in html
    assert "2026-04-19" in html
    assert "2026-07-16 10:00:00" in html
    assert 'class="cell-match"' in html
    assert 'class="cell-mismatch"' not in html


def test_make_fixity_section_html_whole_file_failed_marks_mismatch(tmp_path):
    path = _write_fixity_summary(tmp_path, {
        "whole_file": {
            "result": "failed",
            "algorithm": "md5",
            "computed_checksum": "a" * 32,
            "validated_at": "2026-07-16 10:00:00",
            "validated_against": [
                {"file": "old_fixity.md5", "date": "2025-01-01", "checksum": "b" * 32},
            ],
        },
    })
    html = gr.make_fixity_section_html(path, "")
    assert "Failed" in html
    assert 'class="cell-mismatch"' in html


def test_make_fixity_section_html_stream_fixity(tmp_path):
    path = _write_fixity_summary(tmp_path, {
        "stream_fixity": {
            "result": "passed",
            "algorithm": "md5",
            "validated_at": "2026-07-16 10:00:00",
            "video": {"embedded_hash": "v" * 32, "computed_hash": "v" * 32, "match": True},
            "audio": {"embedded_hash": "a" * 32, "computed_hash": "a" * 32, "match": True},
        },
    })
    html = gr.make_fixity_section_html(path, "")
    assert "Embedded stream fixity" in html
    assert "v" * 32 in html
    assert "a" * 32 in html
    assert html.count('class="cell-match"') == 2


def test_make_fixity_section_html_checksum_output_only(tmp_path):
    path = _write_fixity_summary(tmp_path, {
        "checksum_output": {
            "algorithm": "md5",
            "checksum": "c" * 32,
            "created_at": "2026-07-16 09:00:00",
            "checksum_file": "JPC_AV_00001_2026_07_16_09_00_fixity.md5",
        },
    })
    html = gr.make_fixity_section_html(path, "")
    assert "c" * 32 in html
    assert "2026-07-16 09:00:00" in html
    assert "<tr><th>Checksum file</th><th>Created</th><th>Checksum</th></tr>" in html
    assert "JPC_AV_00001_2026_07_16_09_00_fixity.md5" in html
    assert "No validation against a previously stored checksum" in html


def test_make_fixity_section_html_falls_back_to_sidecar_content():
    html = gr.make_fixity_section_html(None, "abc123  JPC_AV_00001.mkv")
    assert html == "<pre>abc123  JPC_AV_00001.mkv</pre>"


def test_make_fixity_section_html_nothing_to_show_returns_none():
    assert gr.make_fixity_section_html(None, "") is None


def test_image_to_data_uri_encodes_bytes(tmp_path):
    img = tmp_path / "img.png"
    img.write_bytes(b"PNGDATA")
    uri = gr.image_to_data_uri(str(img))
    assert uri.startswith("data:image/png;base64,")
    decoded = b64decode(uri.split(",", 1)[1])
    assert decoded == b"PNGDATA"


def test_image_to_data_uri_custom_mime_type(tmp_path):
    img = tmp_path / "img.jpg"
    img.write_bytes(b"JPEG")
    uri = gr.image_to_data_uri(str(img), mime_type="image/jpeg")
    assert uri.startswith("data:image/jpeg;base64,")


def test_image_to_data_uri_missing_file_returns_empty():
    assert gr.image_to_data_uri("/no/such/file.png") == ""


# ===========================================================================
# Section 3 — parse_timestamp / parse_profile
# ===========================================================================

def test_parse_timestamp_valid_format():
    assert gr.parse_timestamp("01:02:03.4500") == (1, 2, 3, 4500)


def test_parse_timestamp_no_milliseconds_defaults_to_zero():
    assert gr.parse_timestamp("00:00:05") == (0, 0, 5, 0)


def test_parse_timestamp_pads_short_milliseconds_to_four_digits():
    """'.5' → 5000 (left-justified to 4 digits) for sortability."""
    assert gr.parse_timestamp("00:00:05.5") == (0, 0, 5, 5000)


def test_parse_timestamp_truncates_long_milliseconds():
    assert gr.parse_timestamp("00:00:05.12345") == (0, 0, 5, 1234)


@pytest.mark.parametrize("bad", ["", None, "5", "1:2", "abc", "00:00:00:abc"])
def test_parse_timestamp_invalid_returns_placeholder_tuple(bad):
    assert gr.parse_timestamp(bad) == (9999, 99, 99, 99, 9999)


@pytest.mark.parametrize("name,expected", [
    ("color_bars_detection.YMAX.940", 0),
    ("color_bars_evaluation.YMIN.28", 1),
    ("threshold_profile.BRNG.0.5", 2),
    ("tag_check.YMAX.940", 3),
    ("anything_else", 99),
    ("", 99),
])
def test_parse_profile_prefix_mapping(name, expected):
    assert gr.parse_profile(name) == expected


# ===========================================================================
# Section 4 — find_qct_thumbs
# ===========================================================================

def test_find_qct_thumbs_returns_empty_dict_when_no_thumbexports(tmp_path):
    assert gr.find_qct_thumbs(str(tmp_path)) == {}


def test_find_qct_thumbs_parses_color_bars_detection_filename(tmp_path):
    thumbs = tmp_path / "ThumbExports"
    thumbs.mkdir()
    (thumbs / "JPC_AV_05000.color_bars_detection.bars_found.first.0.00.00.03.1030.png").write_text("x")
    out = gr.find_qct_thumbs(str(tmp_path))
    assert len(out) == 1
    key = next(iter(out))
    # First-frame-of-color-bars uses the special label
    assert key.startswith("First frame of color bars")
    path, tag, ts = out[key]
    assert path.endswith(".png")
    assert tag == "bars_found"


def test_find_qct_thumbs_parses_failure_filename(tmp_path):
    thumbs = tmp_path / "ThumbExports"
    thumbs.mkdir()
    (thumbs / "JPC_AV_05000.color_bars_evaluation.YMAX.940.0.00.00.53.7870.jpg").write_text("x")
    out = gr.find_qct_thumbs(str(tmp_path))
    assert len(out) == 1
    key = next(iter(out))
    assert "Failed frame" in key
    assert "YMAX" in key
    _, tag, _ = out[key]
    assert tag == "YMAX"


def test_find_qct_thumbs_skips_dotfiles(tmp_path):
    thumbs = tmp_path / "ThumbExports"
    thumbs.mkdir()
    (thumbs / ".DS_Store").write_text("x")
    assert gr.find_qct_thumbs(str(tmp_path)) == {}


def test_find_qct_thumbs_sorts_by_timestamp_when_tag_names_unknown(tmp_path):
    """Sort key is (parse_profile(tag_name), parse_timestamp(timestamp)). Since
    tag_name is YMAX/bars (not a profile name), parse_profile returns 99 for
    each and the secondary timestamp sort takes over."""
    thumbs = tmp_path / "ThumbExports"
    thumbs.mkdir()
    (thumbs / "v.color_bars_evaluation.YMAX.940.0.00.00.50.0000.png").write_text("x")
    (thumbs / "v.color_bars_detection.bars.first.0.00.00.10.0000.png").write_text("x")

    out = gr.find_qct_thumbs(str(tmp_path))
    keys = list(out.keys())
    # Earlier timestamp (00:00:10) sorts first
    assert "00:00:10" in keys[0]
    assert "00:00:50" in keys[1]


# ===========================================================================
# Section 5 — find_report_csvs
# ===========================================================================

def test_find_report_csvs_empty_directory_returns_all_none(tmp_path):
    out = gr.find_report_csvs(str(tmp_path))
    # 22-tuple: all None except 2 list fields (content_check_outputs, windowed_colorbars_values)
    assert isinstance(out, tuple)
    assert len(out) == 22
    none_count = sum(1 for x in out if x is None)
    list_count = sum(1 for x in out if x == [])
    assert none_count == 20
    assert list_count == 2


def test_find_report_csvs_picks_up_known_filenames(tmp_path):
    """Each well-named CSV lands in the right slot in the tuple."""
    files = {
        "qct-parse_colorbars_durations.csv":   "colorbars_duration_output",
        "qct-parse_colorbars_eval_summary.csv": "bars_eval_check_output",
        "qct-parse_colorbars_values.csv":       "colorbars_values_output",
        "qct-parse_profile_summary.csv":        "profile_check_output",
        "qct-parse_profile_failures.csv":       "profile_fails_csv",
        "qct-parse_tags_summary.csv":           "tags_check_output",
        "qct-parse_tags_failures.csv":          "tag_fails_csv",
        "qct-parse_audio_clipping.csv":         "audio_clipping_csv",
        "qct-parse_channel_imbalance.csv":      "channel_imbalance_csv",
        "qct-parse_identical_channels.csv":     "identical_channels_csv",
        "qct-parse_audible_timecode.csv":       "audible_timecode_csv",
        "qct-parse_audio_dropout.csv":          "audio_dropout_csv",
        "qct-parse_clamped_levels.csv":         "clamped_levels_csv",
        "qct-parse_clamped_traces.csv":         "clamped_traces_csv",
        "qct-parse_chroma_phase_summary.csv":   "chroma_phase_summary_csv",
        "qct-parse_chroma_phase_events.csv":    "chroma_phase_events_csv",
        "qct-parse_tone_leak_summary.csv":      "tone_leak_summary_csv",
        "qct-parse_tone_leak_events.csv":       "tone_leak_events_csv",
        "JPC_AV_metadata_difference.csv":       "difference_csv",
    }
    for name in files:
        (tmp_path / name).write_text("")

    out = gr.find_report_csvs(str(tmp_path))
    # Unpack the 22-tuple in the order the function returns it
    (
        colorbars_duration_output, bars_eval_check_output, colorbars_values_output,
        windowed_colorbars_values, content_check_outputs, profile_check_output,
        profile_fails_csv, tags_check_output, tag_fails_csv, colorbars_eval_fails_csv,
        audio_clipping_csv, channel_imbalance_csv, identical_channels_csv,
        audible_timecode_csv,
        audio_dropout_csv, clamped_levels_csv, clamped_traces_csv,
        chroma_phase_summary_csv, chroma_phase_events_csv,
        tone_leak_summary_csv, tone_leak_events_csv, difference_csv,
    ) = out
    assert colorbars_duration_output.endswith("qct-parse_colorbars_durations.csv")
    assert bars_eval_check_output.endswith("qct-parse_colorbars_eval_summary.csv")
    assert colorbars_values_output.endswith("qct-parse_colorbars_values.csv")
    assert profile_check_output.endswith("qct-parse_profile_summary.csv")
    assert profile_fails_csv.endswith("qct-parse_profile_failures.csv")
    assert tags_check_output.endswith("qct-parse_tags_summary.csv")
    assert tag_fails_csv.endswith("qct-parse_tags_failures.csv")
    assert audio_clipping_csv.endswith("qct-parse_audio_clipping.csv")
    assert channel_imbalance_csv.endswith("qct-parse_channel_imbalance.csv")
    assert identical_channels_csv.endswith("qct-parse_identical_channels.csv")
    assert audible_timecode_csv.endswith("qct-parse_audible_timecode.csv")
    assert audio_dropout_csv.endswith("qct-parse_audio_dropout.csv")
    assert clamped_levels_csv.endswith("qct-parse_clamped_levels.csv")
    assert clamped_traces_csv.endswith("qct-parse_clamped_traces.csv")
    assert chroma_phase_summary_csv.endswith("qct-parse_chroma_phase_summary.csv")
    assert chroma_phase_events_csv.endswith("qct-parse_chroma_phase_events.csv")
    assert tone_leak_summary_csv.endswith("qct-parse_tone_leak_summary.csv")
    assert tone_leak_events_csv.endswith("qct-parse_tone_leak_events.csv")
    assert difference_csv.endswith("metadata_difference.csv")


def test_find_report_csvs_collects_content_filter_csvs_into_list(tmp_path):
    """contentFilter CSVs accumulate into a list (multiple per video)."""
    (tmp_path / "qct-parse_contentFilter_a.csv").write_text("")
    (tmp_path / "qct-parse_contentFilter_b.csv").write_text("")

    out = gr.find_report_csvs(str(tmp_path))
    content_check_outputs = out[4]
    assert len(content_check_outputs) == 2
    names = sorted(os.path.basename(p) for p in content_check_outputs)
    assert names == ["qct-parse_contentFilter_a.csv", "qct-parse_contentFilter_b.csv"]


def test_find_report_csvs_ignores_unknown_files(tmp_path):
    (tmp_path / ".DS_Store").write_text("")
    (tmp_path / "random_artifact.csv").write_text("")
    out = gr.find_report_csvs(str(tmp_path))
    assert all(x is None or x == [] for x in out)


# ===========================================================================
# Section 6 — read_xml_file
# ===========================================================================

def test_read_xml_file_utf8(tmp_path):
    p = tmp_path / "x.xml"
    p.write_text("<root/>")
    assert gr.read_xml_file(str(p)) == "<root/>"


def test_read_xml_file_falls_back_to_latin1(tmp_path):
    p = tmp_path / "x.xml"
    p.write_bytes(b"<root>\xe9</root>")
    out = gr.read_xml_file(str(p))
    assert "é" in out


def test_read_xml_file_missing_raises():
    """Function only catches UnicodeDecodeError; FileNotFoundError bubbles up."""
    with pytest.raises(FileNotFoundError):
        gr.read_xml_file("/no/such/file.xml")


# ===========================================================================
# Section 7 — ffprobe wrappers
# ===========================================================================

def test_get_video_duration_returns_float(monkeypatch):
    fake = MagicMock(returncode=0, stdout="3661.5\n", stderr="")
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **kw: fake)
    assert gr._get_video_duration("/v.mkv") == 3661.5


def test_get_video_duration_subprocess_error_returns_none(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")))
    assert gr._get_video_duration("/v.mkv") is None


def test_get_audio_channel_count_returns_int(monkeypatch):
    fake = MagicMock(returncode=0, stdout="2\n")
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **kw: fake)
    assert gr._get_audio_channel_count("/v.mkv") == 2


def test_get_audio_channel_count_failure_returns_none(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")))
    assert gr._get_audio_channel_count("/v.mkv") is None


def test_get_audio_stream_channels_single_stereo(monkeypatch):
    fake = MagicMock(returncode=0, stdout="2\n")
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **kw: fake)
    assert gr._get_audio_stream_channels("/v.mkv") == [2]


def test_get_audio_stream_channels_two_mono_streams(monkeypatch):
    fake = MagicMock(returncode=0, stdout="1\n1\n")
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **kw: fake)
    assert gr._get_audio_stream_channels("/v.mxf") == [1, 1]


def test_get_audio_stream_channels_failure_returns_none(monkeypatch):
    monkeypatch.setattr(gr.subprocess, "run", MagicMock(side_effect=subprocess.CalledProcessError(1, "ffprobe")))
    assert gr._get_audio_stream_channels("/v.mkv") is None


# ===========================================================================
# Section 8 — _build_waveform_filter
# ===========================================================================

def test_build_waveform_filter_mono_no_channelsplit():
    f = gr._build_waveform_filter([1], width=1200, height=80)
    assert "channelsplit" not in f
    assert "showwavespic" in f
    assert "1200x80" in f


def test_build_waveform_filter_stereo_uses_layout_stereo():
    f = gr._build_waveform_filter([2], width=1200, height=80)
    assert "channelsplit=channel_layout=stereo" in f
    # vstack inputs = 2 waveforms + 1 separator = 3
    assert "vstack=inputs=3" in f


def test_build_waveform_filter_quad_uses_layout_quad():
    f = gr._build_waveform_filter([4], width=800, height=60)
    assert "channelsplit=channel_layout=quad" in f
    # 4 channels: 4 waveforms + 3 separators = vstack=inputs=7
    assert "vstack=inputs=7" in f


def test_build_waveform_filter_unknown_count_uses_generic_layout():
    """Channel count not in the standard layout map → 'Nc' generic layout."""
    f = gr._build_waveform_filter([5], width=800, height=60)
    assert "channelsplit=channel_layout=5c" in f


def test_build_waveform_filter_two_mono_streams_no_channelsplit():
    """Two separate mono streams (MXF) draw both channels without channelsplit."""
    f = gr._build_waveform_filter([1, 1], width=1200, height=80)
    assert "channelsplit" not in f
    # Each mono stream is referenced directly.
    assert "[0:a:0]" in f
    assert "[0:a:1]" in f
    # 2 channels: 2 waveforms + 1 separator = vstack=inputs=3
    assert "vstack=inputs=3" in f


def test_build_waveform_filter_mono_and_stereo_streams_mixed():
    """A mono stream plus a stereo stream → 3 channels total."""
    f = gr._build_waveform_filter([1, 2], width=800, height=60)
    assert "[0:a:0]" in f  # mono stream used directly
    assert "[0:a:1]channelsplit=channel_layout=stereo" in f  # stereo stream split
    # 3 channels: 3 waveforms + 2 separators = vstack=inputs=5
    assert "vstack=inputs=5" in f


# ===========================================================================
# Section 9 — _imbalance_status_colors
# ===========================================================================

@pytest.mark.parametrize("characterization", ["Balanced", "Mono (single channel)"])
def test_imbalance_status_colors_balanced_green(characterization):
    text, bg, border = gr._imbalance_status_colors(characterization)
    # Green-family palette
    assert text == "#155724"
    assert bg == "#d4edda"
    assert border == "#c3e6cb"


@pytest.mark.parametrize("characterization", ["Slight imbalance", "Moderate imbalance"])
def test_imbalance_status_colors_warning_yellow(characterization):
    text, bg, border = gr._imbalance_status_colors(characterization)
    assert text == "#856404"
    assert bg == "#fff3cd"


def test_imbalance_status_colors_default_red_for_unknown():
    """Significant imbalance + anything unknown falls into red palette."""
    for c in ("Significant imbalance", "Unknown", "", "garbage"):
        text, bg, border = gr._imbalance_status_colors(c)
        assert text == "#721c24"
        assert bg == "#f8d7da"


# ===========================================================================
# Section 9b — identical channels section
# ===========================================================================

def _identical_csv_rows(verdict, characterization, pair_row, regions=()):
    rows = [
        ["Identical Channel Detection Results"],
        ["Total Audio Frames", "5394"],
        ["Number of Channels", "2"],
        ["Overall Verdict", verdict],
        ["Overall Characterization", characterization],
        [],
        ["Channel Pair Comparisons"],
        ["Channel A", "Channel B", "Active Frames", "Matching Frames", "Matching (%)",
         "Mean Phase Correlation", "Difference Peak (dBFS)", "Sum Peak (dBFS)",
         "Program Peak (dBFS)", "Compared Region", "Characterization"],
        pair_row,
    ]
    if regions:
        rows += [[], ["Matching Regions"],
                 ["Channel A", "Channel B", "Start", "End", "Duration (s)"]]
        rows += list(regions)
    return rows


def test_identical_status_colors_by_verdict():
    assert gr._identical_status_colors("Distinct channels")[1] == "#d4edda"          # green
    assert gr._identical_status_colors("Identical channels (dual mono)")[1] == "#fff3cd"  # yellow
    assert gr._identical_status_colors("Partially identical channels")[1] == "#fff3cd"
    assert gr._identical_status_colors("Polarity-inverted duplicate channels")[1] == "#f8d7da"  # red
    assert gr._identical_status_colors("Insufficient audio to compare")[1] == "#e2e3e5"


def test_make_identical_channels_html_renders_pair_and_regions(tmp_path):
    csv_path = tmp_path / "qct-parse_identical_channels.csv"
    _write_csv(csv_path, _identical_csv_rows(
        "Identical channels (dual mono)", "Bit-identical",
        ["Channel 1", "Channel 2", "5390", "5390", "100.00", "1.000",
         "-inf", "-12.0", "-18.1", "Whole file", "Bit-identical"],
        regions=[["Channel 1", "Channel 2", "00:00:00:00", "00:29:58:11", "1798.4"]],
    ))
    html = gr.make_identical_channels_html(str(csv_path))
    assert "Identical channels (dual mono) — Bit-identical" in html
    assert "Channel 1 / Channel 2" in html
    assert "5390 of 5390 (100.00%)" in html
    assert "Matching Regions" in html
    assert "00:29:58:11" in html


def test_make_identical_channels_html_shows_sum_residual_when_inverted(tmp_path):
    """An inverted duplicate cancels on the sum, so that's the residual shown."""
    csv_path = tmp_path / "qct-parse_identical_channels.csv"
    _write_csv(csv_path, _identical_csv_rows(
        "Polarity-inverted duplicate channels", "Polarity-inverted duplicate (bit-identical)",
        ["Channel 1", "Channel 2", "5390", "5390", "100.00", "-1.000",
         "-12.0", "-inf", "-18.1", "Whole file", "Polarity-inverted duplicate (bit-identical)"],
    ))
    html = gr.make_identical_channels_html(str(csv_path))
    # the -inf sum residual is displayed, not the program-level difference peak
    difference_cells = [line for line in html.splitlines() if "-inf" in line]
    assert difference_cells
    assert "Matching Regions" not in html


def test_make_identical_channels_html_missing_file_returns_none(tmp_path):
    assert gr.make_identical_channels_html(str(tmp_path / "nope.csv")) is None
    assert gr.make_identical_channels_html(None) is None


# ===========================================================================
# Section 10 — _parse_bars_durations_csv
# ===========================================================================

def test_parse_bars_durations_qct_parse_two_column_format(tmp_path):
    """Legacy qct-parse single-row format reports pass_label='primary'."""
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path, [
        ["qct-parse color bars found:"],
        ["00:00:00.000", "00:00:05.500"],
    ])
    runs = gr._parse_bars_durations_csv(str(csv_path))
    assert runs == [("primary", 0.0, 5.5)]


def test_parse_bars_durations_clams_three_column_format(tmp_path):
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path, [
        ["clams bars detection color bars found:"],
        ["primary",    "00:00:00.000", "00:00:04.000"],
        ["second_pass", "00:01:00.000", "00:01:30.000"],
    ])
    runs = gr._parse_bars_durations_csv(str(csv_path))
    assert runs == [
        ("primary", 0.0, 4.0),
        ("second_pass", 60.0, 90.0),
    ]


def test_parse_bars_durations_no_bars_returns_empty(tmp_path):
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path, [["clams bars detection found no color bars"]])
    assert gr._parse_bars_durations_csv(str(csv_path)) == []


def test_parse_bars_durations_missing_file_returns_empty():
    assert gr._parse_bars_durations_csv("/no/such/file.csv") == []
    assert gr._parse_bars_durations_csv(None) == []


def test_parse_bars_durations_skips_malformed_rows(tmp_path):
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path, [
        ["color bars found"],
        ["malformed-timestamp"],   # 1 col — skipped (need ≥2)
        ["00:00:00.000", "BAD"],   # invalid end time → skipped
        ["primary", "00:00:00.000", "00:00:01.000"],   # valid
    ])
    runs = gr._parse_bars_durations_csv(str(csv_path))
    assert runs == [("primary", 0.0, 1.0)]


def test_parse_bars_durations_qct_parse_three_column_labeled(tmp_path):
    """New qct-parse 3-column format with head + CLAMS-guided regions."""
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path, [
        ["qct-parse color bars found:"],
        ["head", "00:00:00.000", "00:01:30.000"],
        ["clams-guided-tone", "00:15:00.000", "00:15:45.000"],
    ])
    runs = gr._parse_bars_durations_csv(str(csv_path))
    assert runs == [
        ("head", 0.0, 90.0),
        ("clams-guided-tone", 900.0, 945.0),
    ]


# ===========================================================================
# Section 11 — _parse_tone_detection_csv
# ===========================================================================

def test_parse_tone_detection_with_three_column_rows(tmp_path):
    csv_path = tmp_path / "tones.csv"
    _write_csv(csv_path, [
        ["clams tone detection tones found:"],
        ["primary",    "00:00:00.000", "00:00:02.000"],
        ["second_pass", "00:00:10.500", "00:00:15.500"],
    ])
    out = gr._parse_tone_detection_csv(str(csv_path))
    assert out == [
        ("primary", 0.0, 2.0),
        ("second_pass", 10.5, 15.5),
    ]


def test_parse_tone_detection_legacy_two_column_rows(tmp_path):
    csv_path = tmp_path / "tones.csv"
    _write_csv(csv_path, [
        ["tones found:"],
        ["00:00:00.000", "00:00:02.000"],
    ])
    out = gr._parse_tone_detection_csv(str(csv_path))
    assert out == [("primary", 0.0, 2.0)]


def test_parse_tone_detection_no_tones_returns_empty(tmp_path):
    csv_path = tmp_path / "tones.csv"
    _write_csv(csv_path, [["clams tone detection found no tones"]])
    assert gr._parse_tone_detection_csv(str(csv_path)) == []


def test_parse_tone_detection_missing_file_returns_empty():
    assert gr._parse_tone_detection_csv(None) == []
    assert gr._parse_tone_detection_csv("/no/such.csv") == []


# ===========================================================================
# Section 12 — _seconds_to_display
# ===========================================================================

@pytest.mark.parametrize("seconds,expected", [
    (None, "N/A"),
    (0.0, "0:00.0"),
    (5.5, "0:05.5"),
    (65.0, "1:05.0"),
    (3725.5, "1:02:05.5"),  # over 1 hour switches format
])
def test_seconds_to_display(seconds, expected):
    assert gr._seconds_to_display(seconds) == expected


# ===========================================================================
# Section 13 — _extract_frame_at
# ===========================================================================

def test_extract_frame_at_seeks_before_input_for_keyframe_seek(monkeypatch):
    """-ss MUST appear before -i to trigger fast keyframe seek."""
    run_mock = MagicMock(returncode=0)
    monkeypatch.setattr(gr.subprocess, "run", run_mock)

    gr._extract_frame_at("/v.mkv", timestamp=12.5, output_path="/out/frame.jpg", height=80)

    cmd = run_mock.call_args[0][0]
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx, "-ss must appear before -i for fast keyframe seek"

    # Single frame, scaled to height
    assert "-frames:v" in cmd and cmd[cmd.index("-frames:v") + 1] == "1"
    vf_idx = cmd.index("-vf")
    assert cmd[vf_idx + 1] == "scale=-1:80"
    # Timestamp
    assert cmd[ss_idx + 1] == "12.5"


def test_extract_frame_at_propagates_subprocess_error(monkeypatch):
    """check=True means CalledProcessError bubbles up."""
    monkeypatch.setattr(
        gr.subprocess, "run",
        MagicMock(side_effect=subprocess.CalledProcessError(1, "ffmpeg")),
    )
    with pytest.raises(subprocess.CalledProcessError):
        gr._extract_frame_at("/v.mkv", 0.0, "/out.jpg", 80)


# ---------------------------------------------------------------------------
# Audible timecode report: consensus regions shown, per-method details hidden
# ---------------------------------------------------------------------------

def _write_audible_tc_csv(path):
    """Write a consensus-format audible-timecode CSV like qct_parse now emits:
    a consensus regions table (last column 'Detection Methods') followed by a
    'Per-Method Detections' forensic section."""
    rows = [
        ["Audible Timecode Detection Results"],
        ["Metric Type", "both"],
        ["Total Audio Frames", "35593"],
        ["Duration", "59:19.2"],
        ["Audible Timecode Detected", "Yes"],
        ["Regions Detected", "2"],
        [],
        ["Start Time", "End Time", "Duration", "Channel", "Confidence", "Detection Methods"],
        ["00:00:26:15", "00:47:03:21", "46:37.2", "Channel 1", "high", "R128 + astats (ch1)"],
        ["00:49:55:12", "00:59:08:15", "9:13.1", "Channel 1", "high", "R128 + astats (ch1)"],
        [],
        ["Per-Method Detections", "5"],
        [],
        ["Start Time", "End Time", "Criterion", "Channel", "Confidence", "Details"],
        ["00:00:26:15", "00:47:03:21", "R128-A (stable mix at TC level)", "n/a (mix-based)", "high", "secret-detail-A"],
        ["00:49:55:12", "00:51:20:11", "astats (ch1)", "ch1", "high", "secret-detail-B"],
    ]
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def test_make_audible_timecode_html_shows_consensus_regions(tmp_path):
    csv_path = tmp_path / "qct-parse_audible_timecode.csv"
    _write_audible_tc_csv(str(csv_path))
    html = gr.make_audible_timecode_html(str(csv_path))
    assert html is not None
    # Consensus regions are rendered with their span, channel, and methods.
    assert "Audible timecode detected in 2 region(s)" in html
    assert "R128 + astats (ch1)" in html
    assert "Channel 1" in html
    assert "Detection Methods" in html
    # Positions are shown as the NDF timecodes from the CSV, span first→last.
    assert "00:00:26:15" in html and "00:59:08:15" in html


def test_make_audible_timecode_html_hides_per_method_details(tmp_path):
    csv_path = tmp_path / "qct-parse_audible_timecode.csv"
    _write_audible_tc_csv(str(csv_path))
    html = gr.make_audible_timecode_html(str(csv_path))
    # The per-method forensic rows (criteria + details) must not leak into the
    # report — only the consensus table is shown.
    assert "secret-detail-A" not in html
    assert "secret-detail-B" not in html
    assert "stable mix at TC level" not in html


# ===========================================================================
# make_tone_leak_html
# ===========================================================================

def _write_tone_leak_csvs(dirpath, flagged):
    """Write a summary (+ events, when flagged) CSV pair like tone_leak_check does."""
    summary = os.path.join(dirpath, "qct-parse_tone_leak_summary.csv")
    header = ["Stream", "Channel", "Total Windows", "Active Windows",
              "Flagged Windows", "Flagged %", "Median Comb (dB)",
              "Median Flagged Comb (dB)", "Median 1kHz Level (dBFS)",
              "Tone Leak Detected"]
    rows = [
        ["Tone Leak Detection Results"],
        ["Harmonics Scored (Hz)", "1000 2000 3000 4000 6000"],
        ["Window Size (seconds)", "8"],
        ["Comb Score Threshold (dB)", "12.0"],
        ["Min Flagged Fraction", "0.05"],
        ["Min Flagged Windows", "4"],
        ["Silence Floor (dBFS)", "-85.0"],
        [],
        header,
        ["0", "0", "375", "370", "365" if flagged else "0",
         "98.6" if flagged else "0.0", "22.3" if flagged else "4.6",
         "22.4" if flagged else "n/a", "-91.3" if flagged else "n/a",
         "Yes" if flagged else "No"],
        ["0", "1", "375", "370", "0", "0.0", "3.8", "n/a", "n/a", "No"],
    ]
    with open(summary, "w", newline="") as f:
        csv.writer(f).writerows(rows)

    events = os.path.join(dirpath, "qct-parse_tone_leak_events.csv")
    ev_rows = [["Timestamp Start", "Timestamp End", "Stream", "Channel",
                "Duration (s)", "Mean Comb (dB)", "Peak Comb (dB)",
                "Median 1kHz Level (dBFS)"]]
    if flagged:
        ev_rows.append(["00:00:00:00", "00:48:40:12", "0", "0", "2920", "22.3", "28.2", "-91.3"])
    with open(events, "w", newline="") as f:
        csv.writer(f).writerows(ev_rows)
    return summary, events


def test_make_tone_leak_html_flags_detected_channel(tmp_path):
    summary, events = _write_tone_leak_csvs(str(tmp_path), flagged=True)
    html = gr.make_tone_leak_html(summary, events)
    assert html is not None
    assert "Reference-Tone Leak Detected: stream 0 channel 0" in html
    # Per-channel table renders both channels with their verdicts.
    assert "98.6" in html and "22.3" in html
    # Events table renders the flagged region with file timecode.
    assert "00:48:40:12" in html
    assert "Show tone leak regions (1)" in html


def test_make_tone_leak_html_clean_shows_green_status(tmp_path):
    summary, events = _write_tone_leak_csvs(str(tmp_path), flagged=False)
    html = gr.make_tone_leak_html(summary, events)
    assert html is not None
    assert "No Reference-Tone Leak Detected" in html
    # No events -> no collapsible regions table.
    assert "Show tone leak regions" not in html


def test_make_tone_leak_html_missing_summary_returns_none(tmp_path):
    assert gr.make_tone_leak_html(str(tmp_path / "nope.csv"), None) is None


# ===========================================================================
# BRNG low-confidence caveat banner
# ===========================================================================

_FRAME_OUTPUTS_BASE = {
    'border_visualization': None, 'border_data': None, 'brng_analysis': None,
    'brng_thumbnails': [], 'signalstats_analysis': None,
    'enhanced_frame_analysis': None, 'dropped_sample_spectrogram': None,
    'dropped_sample_detection': None, 'duplicate_frame_detection': None,
    'initial_brng_analysis': None,
}


def _brng_payload(**extra):
    payload = {
        "actionable_report": {"overall_assessment": "No BRNG violations detected",
                              "summary_statistics": {}},
        "aggregate_patterns": {}, "violations": [],
        "period_summaries": [{"period_num": 1, "start_time": 1765.0,
                              "end_time": 1825.0, "violations_found": 0,
                              "frames_checked": 10}],
        "analysis_periods": [[1765.0, 60]],
    }
    payload.update(extra)
    return payload


def test_brng_last_resort_periods_render_a_low_confidence_caveat():
    """A mostly-black sample must not read like a normal clean result."""
    outputs = {**_FRAME_OUTPUTS_BASE, 'brng_analysis': _brng_payload(
        period_confidence="last_resort",
        period_confidence_note="No period free of black content could be found.",
    )}
    html = gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")

    assert "Low confidence:" in html
    assert "No period free of black content could be found." in html


def test_brng_normal_confidence_renders_no_caveat():
    outputs = {**_FRAME_OUTPUTS_BASE,
               'brng_analysis': _brng_payload(period_confidence="normal")}
    assert "Low confidence:" not in gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")


def test_brng_caveat_absent_for_results_predating_the_field():
    """Older sidecars have no period_confidence key at all."""
    outputs = {**_FRAME_OUTPUTS_BASE, 'brng_analysis': _brng_payload()}
    assert "Low confidence:" not in gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")


def test_brng_caveat_falls_back_to_generic_text_without_a_note():
    outputs = {**_FRAME_OUTPUTS_BASE,
               'brng_analysis': _brng_payload(period_confidence="last_resort")}
    html = gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")

    assert "Low confidence:" in html
    assert "could not be placed on picture content" in html


def test_brng_partial_coverage_renders_its_own_caveat():
    """Incomplete is a different claim from wrong — the wording must differ."""
    outputs = {**_FRAME_OUTPUTS_BASE, 'brng_analysis': _brng_payload(
        period_confidence="partial_coverage",
        period_confidence_note="Only 2 of 3 analysis periods could be examined.",
    )}
    html = gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")

    assert "Partial coverage:" in html
    assert "Only 2 of 3 analysis periods could be examined." in html
    # Must not claim the sample is unrepresentative — the analyzed periods are valid
    assert "Low confidence:" not in html
    assert "indicative only" not in html


def test_brng_partial_coverage_falls_back_to_generic_text():
    outputs = {**_FRAME_OUTPUTS_BASE,
               'brng_analysis': _brng_payload(period_confidence="partial_coverage")}
    html = gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")

    assert "Partial coverage:" in html
    assert "Some analysis periods could not be examined." in html


def test_brng_unknown_confidence_value_still_warns():
    """A level added later must not render as if nothing were wrong."""
    outputs = {**_FRAME_OUTPUTS_BASE,
               'brng_analysis': _brng_payload(period_confidence="something_new")}
    html = gr.generate_frame_analysis_html(outputs, "JPC_AV_02222")

    assert "Reduced confidence:" in html
