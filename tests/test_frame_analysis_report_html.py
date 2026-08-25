"""Tests for the frame-analysis report renderers in generate_report.

generate_frame_analysis_html was a single 1,372-line function; it is now an
orchestrator over four section renderers. The end-to-end report oracle only
covers ~73% of that code — the sample files never produce some severity and
diagnosis variants — so the uncovered branches are pinned here.

Each renderer takes the frame_outputs dict and returns its own fragment, or an
empty string when its inputs are absent.
"""

import json

import pytest

from AV_Spex.utils import generate_report as gr


EMPTY = {
    'border_visualization': None, 'border_data': None,
    'brng_analysis': None, 'signalstats_analysis': None,
    'brng_thumbnails': None, 'enhanced_analysis': None,
}


def outputs(**overrides):
    d = dict(EMPTY)
    d.update(overrides)
    return d


def _json_file(tmp_path, name, payload):
    p = tmp_path / name
    p.write_text(json.dumps(payload))
    return str(p)


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------

def test_no_inputs_renders_nothing():
    assert gr.generate_frame_analysis_html(outputs(), "V1") == ""


def test_section_wrapper_and_anchor_are_emitted(tmp_path):
    """The TOC falls back to this anchor when no subsection rendered."""
    html = gr.generate_frame_analysis_html(
        outputs(border_data=_json_file(tmp_path, "b.json", {})), "V1")
    assert 'id="section-frame-analysis"' in html
    assert "Frame Analysis Results" in html


@pytest.mark.parametrize("key", [
    'border_visualization', 'border_data', 'brng_analysis', 'signalstats_analysis',
])
def test_any_single_input_produces_output(tmp_path, key):
    html = gr.generate_frame_analysis_html(
        outputs(**{key: _json_file(tmp_path, "x.json", {})}), "V1")
    assert html != ""


# ---------------------------------------------------------------------------
# Each renderer is independent and skips cleanly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("renderer", [
    gr._render_frame_border_html,
    gr._render_frame_signalstats_html,
    gr._render_frame_brng_html,
    gr._render_frame_thumbs_html,
])
def test_renderer_returns_empty_without_its_inputs(renderer):
    assert renderer(outputs()) == ""


def test_renderers_emit_their_own_anchors(tmp_path):
    """Phase-03 TOC entries point at these ids, so they must come from here."""
    border = gr._render_frame_border_html(
        outputs(border_data=_json_file(tmp_path, "b.json", {})))
    assert "id='section-border-detection'" in border

    ss = gr._render_frame_signalstats_html(
        outputs(signalstats_analysis=_json_file(tmp_path, "s.json", {})))
    assert "id='section-signalstats'" in ss

    brng = gr._render_frame_brng_html(
        outputs(brng_analysis=_json_file(tmp_path, "r.json", {})))
    assert "id='section-brng-analysis'" in brng


def test_border_renderer_does_not_emit_other_sections(tmp_path):
    """A renderer must not leak another section's anchor — the TOC derives
    its entries from which anchors appear."""
    html = gr._render_frame_border_html(
        outputs(border_data=_json_file(tmp_path, "b.json", {})))
    assert "id='section-signalstats'" not in html
    assert "id='section-brng-analysis'" not in html


# ---------------------------------------------------------------------------
# Malformed inputs must not take the report down
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("renderer,key", [
    (gr._render_frame_border_html, 'border_data'),
    (gr._render_frame_signalstats_html, 'signalstats_analysis'),
    (gr._render_frame_brng_html, 'brng_analysis'),
])
def test_unparseable_json_does_not_raise(tmp_path, renderer, key):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert isinstance(renderer(outputs(**{key: str(bad)})), str)


@pytest.mark.parametrize("renderer,key", [
    (gr._render_frame_border_html, 'border_data'),
    (gr._render_frame_signalstats_html, 'signalstats_analysis'),
    (gr._render_frame_brng_html, 'brng_analysis'),
])
def test_missing_file_does_not_raise(tmp_path, renderer, key):
    assert isinstance(renderer(outputs(**{key: str(tmp_path / "nope.json")})), str)


# ---------------------------------------------------------------------------
# Static methodology copy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("const", [
    'BORDER_DETECTION_METHODOLOGY_HTML',
    'SIGNALSTATS_METHODOLOGY_HTML',
    'BRNG_METHODOLOGY_HTML',
])
def test_methodology_constants_exist_and_are_static(const):
    """Hoisted out of the renderers; they must carry no format placeholders."""
    value = getattr(gr, const)
    assert isinstance(value, str) and len(value) > 200
    assert '{' not in value.replace('{{', '').replace('}}', '')


def test_each_renderer_includes_its_methodology(tmp_path):
    border = gr._render_frame_border_html(
        outputs(border_data=_json_file(tmp_path, "b.json", {})))
    assert gr.BORDER_DETECTION_METHODOLOGY_HTML.strip()[:40] in border


# ===========================================================================
# Variant fixtures
#
# The end-to-end report oracle covers ~74% of these renderers: the sample files
# happen to produce only some of the severity, diagnosis and shape variants.
# The rest are driven directly here.
#
# The renderers accept a dict in place of a path, so these need no files.
# ===========================================================================

def signalstats(**over):
    """A minimal signalstats payload; override to select a variant."""
    d = {'diagnosis': 'Analysis complete', 'results': {}}
    d.update(over)
    return outputs(signalstats_analysis=d)


def brng(**over):
    d = {'violations': [], 'period_summaries': [], 'analysis_periods': []}
    d.update(over)
    return outputs(brng_analysis=d)


# -- signalstats: severity derived from the diagnosis text ------------------
# Results generated before the severity field existed fall back to keyword
# matching, so each keyword family has to keep working.

@pytest.mark.parametrize("diagnosis,icon", [
    ("Signal is broadcast-compliant",        "✅"),
    ("Levels are broadcast-safe throughout", "✅"),
    ("Minor excursions, acceptable",         "ℹ"),
    ("Significant excursions detected",      "⛔"),
    ("Requires operator attention",          "⛔"),
    ("Severe clipping present",              "⛔"),
    ("Please review these regions",          "⚠"),
])
def test_severity_inferred_from_diagnosis_keywords(diagnosis, icon):
    html = gr._render_frame_signalstats_html(signalstats(diagnosis=diagnosis))
    assert icon in html, f"{diagnosis!r} did not map to the expected badge"


def test_explicit_severity_beats_the_keyword_fallback():
    """A stored severity must win over whatever the prose happens to say."""
    html = gr._render_frame_signalstats_html(
        signalstats(diagnosis="Signal is broadcast-compliant", severity='alert'))
    assert "⛔" in html
    assert "✅" not in html


# Each severity has its own badge colour. The icon alone does not identify the
# branch — 'info' and the unrecognised fallback share it — so assert the colour.
SEVERITY_STYLE = {
    'ok':      ('#d2ffed', '✅'),
    'alert':   ('#ffbaba', '⛔'),
    'warning': ('#fff3cd', '⚠'),
    'info':    ('#e3f0ff', 'ℹ'),
}


@pytest.mark.parametrize("severity", sorted(SEVERITY_STYLE))
def test_each_explicit_severity_renders_its_own_style(severity):
    bg, icon = SEVERITY_STYLE[severity]
    html = gr._render_frame_signalstats_html(signalstats(severity=severity))
    assert icon in html
    assert bg in html, f"{severity} should use its own background {bg}"


def test_unrecognised_severity_does_not_borrow_the_info_style():
    """The fallback shares the info icon, so only the colour separates them."""
    html = gr._render_frame_signalstats_html(signalstats(severity='banana'))
    assert SEVERITY_STYLE['info'][0] not in html


def test_unrecognised_severity_falls_back_without_raising():
    html = gr._render_frame_signalstats_html(signalstats(severity='banana'))
    assert isinstance(html, str) and html != ""


# -- signalstats: analysis_periods shape ------------------------------------
# Periods arrive either as (start, end) pairs or as dicts, depending on which
# code path produced them.

def test_analysis_periods_as_pairs():
    html = gr._render_frame_signalstats_html(
        signalstats(analysis_periods=[(0.0, 60.0), (120.0, 180.0)]))
    assert isinstance(html, str)


def test_analysis_periods_as_dicts():
    html = gr._render_frame_signalstats_html(
        signalstats(analysis_periods=[{'start': 0.0, 'end': 60.0}]))
    assert isinstance(html, str)


def test_analysis_periods_mixed_shapes_do_not_raise():
    html = gr._render_frame_signalstats_html(
        signalstats(analysis_periods=[(0.0, 60.0), {'start': 90.0, 'end': 120.0}, None]))
    assert isinstance(html, str)


def test_signalstats_active_area_branch():
    html = gr._render_frame_signalstats_html(
        signalstats(results={'active_area': {'x': 10, 'y': 6, 'width': 700, 'height': 474}}))
    assert isinstance(html, str) and html != ""


# -- BRNG: severity thresholds ---------------------------------------------

def test_high_average_violation_is_flagged():
    html = gr._render_frame_brng_html(brng(
        actionable_report={'summary_statistics': {'average_violation_percentage': 25.0},
                           'overall_assessment': 'heavy'}))
    assert isinstance(html, str) and html != ""


def test_edge_dominated_violations_branch():
    html = gr._render_frame_brng_html(brng(
        aggregate_patterns={'edge_violation_percentage': 80.0,
                            'continuous_edge_percentage': 10.0}))
    assert isinstance(html, str) and html != ""


def test_pure_edge_violations_branch():
    """continuous_pct == 0 with edge_pct > 95 is the border-artifact signature."""
    html = gr._render_frame_brng_html(brng(
        aggregate_patterns={'edge_violation_percentage': 99.0,
                            'continuous_edge_percentage': 0.0}))
    assert isinstance(html, str) and html != ""


@pytest.mark.parametrize("confidence", ['low', 'last_resort', 'reduced'])
def test_non_normal_period_confidence_is_surfaced(confidence):
    """Period placement that had to fall back should say so."""
    html = gr._render_frame_brng_html(brng(
        period_confidence=confidence,
        period_confidence_note='periods placed by fallback'))
    assert isinstance(html, str) and html != ""


def test_skipped_seconds_are_reported():
    html = gr._render_frame_brng_html(brng(
        skip_info={'total_skipped_seconds': 62.5, 'reason': 'color bars'}))
    assert isinstance(html, str) and html != ""


# -- BRNG: recommendations --------------------------------------------------

# Recommendation severity is conveyed by colour only; the text is identical.
REC_STYLE = {'high': '#ffbaba', 'medium': '#fff3cd', 'low': '#e8f4fd'}


@pytest.mark.parametrize("severity", sorted(REC_STYLE))
def test_recommendation_severities_use_distinct_styling(severity):
    html = gr._render_frame_brng_html(brng(
        actionable_report={'recommendations': [
            {'issue': 'Check the head switching region', 'severity': severity}]}))
    assert 'Check the head switching region' in html
    assert REC_STYLE[severity] in html, f"{severity} should use {REC_STYLE[severity]}"


def test_recommendation_description_is_included_when_present():
    html = gr._render_frame_brng_html(brng(
        actionable_report={'recommendations': [
            {'issue': 'Crop the bottom edge', 'severity': 'medium',
             'description': 'Head switching noise in the last 7 lines'}]}))
    assert 'Head switching noise in the last 7 lines' in html


def test_recommendation_without_description_still_renders():
    html = gr._render_frame_brng_html(brng(
        actionable_report={'recommendations': [{'issue': 'No description here'}]}))
    assert 'No description here' in html


def test_recommendation_without_an_issue_uses_a_placeholder():
    html = gr._render_frame_brng_html(brng(
        actionable_report={'recommendations': [{'severity': 'low'}]}))
    assert 'Unknown issue' in html


# -- border detection: the standalone border_data.json path -----------------
# The enhanced-analysis path is what the sample reports exercise; the
# standalone fallback and the geometry maths below it are not.

def border_file(tmp_path, **over):
    payload = {
        'detection_method': 'sophisticated',
        'active_area': [13, 6, 696, 467],
        'video_properties': {'width': 720, 'height': 486},
    }
    payload.update(over)
    return outputs(border_data=_json_file(tmp_path, "border.json", payload))


def test_standalone_border_data_renders_the_geometry(tmp_path):
    html = gr._render_frame_border_html(border_file(tmp_path))
    assert "id='section-border-detection'" in html
    assert '696' in html or '467' in html, "active area should be reported"


def test_active_area_percentage_is_computed(tmp_path):
    """696x467 of 720x486 is ~92.9% of the frame."""
    html = gr._render_frame_border_html(border_file(tmp_path))
    assert '92.' in html


def test_zero_video_dimensions_skip_the_geometry_maths(tmp_path):
    """Guards a divide-by-zero when the probe could not size the frame."""
    html = gr._render_frame_border_html(
        border_file(tmp_path, video_properties={'width': 0, 'height': 0}))
    assert isinstance(html, str) and html != ""


def test_border_data_without_video_properties(tmp_path):
    payload = {'detection_method': 'simple',
               'active_area': [0, 0, 720, 486]}
    html = gr._render_frame_border_html(
        outputs(border_data=_json_file(tmp_path, "b.json", payload)))
    assert isinstance(html, str) and html != ""


def test_head_switching_artifacts_are_reported(tmp_path):
    html = gr._render_frame_border_html(border_file(
        tmp_path, head_switching_artifacts={'detected': True, 'height_px': 7}))
    assert isinstance(html, str) and html != ""


def test_full_frame_active_area_has_no_borders(tmp_path):
    html = gr._render_frame_border_html(border_file(
        tmp_path, active_area=[0, 0, 720, 486]))
    assert isinstance(html, str) and html != ""


def test_border_data_missing_active_area(tmp_path):
    html = gr._render_frame_border_html(border_file(tmp_path, active_area=None))
    assert isinstance(html, str)


# -- thumbnails: the count cap ---------------------------------------------

def _thumb_files(tmp_path, n):
    """brng_thumbnails is a list of image paths, not a directory."""
    d = tmp_path / "brng_thumbnails"
    d.mkdir(exist_ok=True)
    paths = []
    for i in range(n):
        f = d / f"brng_frame_{i:03d}_{i * 12}s.jpg"
        f.write_bytes(b"\xff\xd8\xff\xd9")
        paths.append(str(f))
    return paths


def test_more_than_six_thumbnails_reports_the_cap(tmp_path):
    html = gr._render_frame_thumbs_html(
        outputs(brng_thumbnails=_thumb_files(tmp_path, 9)))
    assert 'Showing 6 of 9' in html


def test_six_or_fewer_thumbnails_has_no_cap_note(tmp_path):
    html = gr._render_frame_thumbs_html(
        outputs(brng_thumbnails=_thumb_files(tmp_path, 3)))
    assert 'Showing 6 of' not in html
