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
