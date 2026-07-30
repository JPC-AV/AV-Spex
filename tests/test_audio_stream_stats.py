"""Tests for checks/audio_stream_stats.py — the per-stream audio stats sidecar
generator used when a multi-stream input (e.g. broadcast MXF) means the QCTools
report's audio frames only describe a qcli downmix.

Covers:
* _filter_line — document/tags-wrapper stripping and "-inf" silence flooring
* build_lavfi_graph — amovie stream selection, amerge, path escaping
* probe_audio_streams — ffprobe output parsing (mocked subprocess)
* generate_audio_stats_sidecar — success, failure, cancellation, temp cleanup
  (mocked ffprobe process)
* _detect_audio_pkt — timestamp attribute detection on sidecar files
* analyzeAudio contract — a sidecar-shaped fixture parses into per-channel results
"""

import gzip
import os
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.checks import audio_stream_stats as ass
from AV_Spex.checks.audio_stream_stats import (
    AUDIO_FILTER,
    SILENCE_FLOOR_DB,
    audio_stats_path,
    build_lavfi_graph,
    generate_audio_stats_sidecar,
    probe_audio_streams,
    _filter_line,
)
from AV_Spex.checks.qct_parse import analyzeAudio, _detect_audio_pkt


# ---- _filter_line ---------------------------------------------------------

@pytest.mark.parametrize("line", [
    '<?xml version="1.0" encoding="UTF-8"?>\n',
    '<ffprobe>\n',
    '</ffprobe>\n',
    '    <frames>\n',
    '    </frames>\n',
    '            <tags>\n',
    '            </tags>\n',
])
def test_filter_line_drops_wrappers(line):
    assert _filter_line(line) is None


def test_filter_line_keeps_frame_and_tag_lines():
    frame = '        <frame media_type="audio" pkt_dts_time="0.000000">\n'
    tag = '            <tag key="lavfi.astats.1.RMS_level" value="-63.9"/>\n'
    assert _filter_line(frame) == frame
    assert _filter_line(tag) == tag


def test_filter_line_floors_negative_inf():
    line = '<tag key="lavfi.astats.2.RMS_level" value="-inf"/>\n'
    out = _filter_line(line)
    assert 'value="-inf"' not in out
    assert f'value="{SILENCE_FLOOR_DB:.6f}"' in out


# ---- build_lavfi_graph ----------------------------------------------------

def test_build_lavfi_graph_two_streams():
    graph = build_lavfi_graph("/path/to/file.mkv", [1, 2])
    assert graph == (
        "amovie='/path/to/file.mkv':s=1+2[a0][a1];"
        f"[a0][a1]amerge=inputs=2,{AUDIO_FILTER}"
    )


def test_build_lavfi_graph_four_streams():
    graph = build_lavfi_graph("/f.mxf", [1, 2, 3, 4])
    assert "s=1+2+3+4[a0][a1][a2][a3];" in graph
    assert "amerge=inputs=4" in graph
    # astats must precede aphasemeter/ebur128 (per-channel metadata is captured
    # before those filters collapse the signal to a stereo downmix)
    assert graph.index("astats=") < graph.index("aphasemeter=") < graph.index("ebur128=")


def test_build_lavfi_graph_single_stream_no_amerge():
    graph = build_lavfi_graph("/f.mkv", [1])
    assert "amerge" not in graph
    assert graph.startswith("amovie='/f.mkv':s=1,")


def test_build_lavfi_graph_escapes_special_characters():
    graph = build_lavfi_graph("/dir with 'quote'/C:file.mkv", [1, 2])
    assert r"\'" in graph
    assert r"C\:file" in graph


# ---- probe_audio_streams --------------------------------------------------

def _mock_run(stdout, returncode=0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_probe_audio_streams_parses_index_and_channels():
    with patch.object(ass.subprocess, 'run', return_value=_mock_run("1,1\n2,1\n")):
        assert probe_audio_streams("f.mkv") == [(1, 1), (2, 1)]


def test_probe_audio_streams_stereo_single_stream():
    with patch.object(ass.subprocess, 'run', return_value=_mock_run("1,2\n")):
        assert probe_audio_streams("f.mkv") == [(1, 2)]


def test_probe_audio_streams_failure_returns_empty():
    with patch.object(ass.subprocess, 'run', return_value=_mock_run("", returncode=1)):
        assert probe_audio_streams("f.mkv") == []
    with patch.object(ass.subprocess, 'run', side_effect=OSError("no ffprobe")):
        assert probe_audio_streams("f.mkv") == []


# ---- generate_audio_stats_sidecar -----------------------------------------

FFPROBE_XML_LINES = [
    '<?xml version="1.0" encoding="UTF-8"?>\n',
    '<ffprobe>\n',
    '    <frames>\n',
    '        <frame media_type="audio" pkt_dts_time="0.000000" channels="2">\n',
    '            <tags>\n',
    '                <tag key="lavfi.astats.1.RMS_level" value="-30.000000"/>\n',
    '                <tag key="lavfi.astats.2.RMS_level" value="-inf"/>\n',
    '            </tags>\n',
    '        </frame>\n',
    '        <frame media_type="audio" pkt_dts_time="0.100000" channels="2">\n',
    '            <tags>\n',
    '                <tag key="lavfi.astats.1.RMS_level" value="-31.000000"/>\n',
    '                <tag key="lavfi.astats.2.RMS_level" value="-80.000000"/>\n',
    '            </tags>\n',
    '        </frame>\n',
    '    </frames>\n',
    '</ffprobe>\n',
]


class _FakeProc:
    """Stand-in for the ffprobe Popen object."""

    def __init__(self, lines, returncode=0, stderr_text=""):
        self.stdout = iter(lines)
        self.stderr = MagicMock()
        self.stderr.read.return_value = stderr_text
        self.returncode = returncode
        self.killed = False

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patched_generate(tmp_path, proc, streams=((1, 1), (2, 1)), check_cancelled=None):
    with patch.object(ass, 'probe_audio_streams', return_value=list(streams)), \
         patch.object(ass.subprocess, 'Popen', return_value=proc):
        return generate_audio_stats_sidecar(
            "input.mkv", str(tmp_path), "VID", check_cancelled=check_cancelled
        )


def test_generate_success_writes_flattened_gzip(tmp_path):
    out = _patched_generate(tmp_path, _FakeProc(FFPROBE_XML_LINES))
    assert out == audio_stats_path(str(tmp_path), "VID")
    assert os.path.isfile(out)
    assert not os.path.exists(out + '.part')
    with gzip.open(out, 'rt', encoding='utf-8') as f:
        content = f.read()
    # exactly one document wrapper, no <tags> wrapper, frames + tags kept
    assert content.count('<ffprobe>') == 1
    assert content.count('<frames>') == 1
    assert '<tags>' not in content
    assert content.count('<frame ') == 2
    assert content.count('<tag key=') == 4
    # digital-silence flooring applied
    assert 'value="-inf"' not in content
    assert f'value="{SILENCE_FLOOR_DB:.6f}"' in content


def test_generate_ffprobe_failure_returns_none_and_cleans_up(tmp_path):
    proc = _FakeProc(FFPROBE_XML_LINES, returncode=1, stderr_text="boom")
    assert _patched_generate(tmp_path, proc) is None
    assert os.listdir(tmp_path) == []


def test_generate_no_frames_returns_none(tmp_path):
    lines = ['<?xml version="1.0"?>\n', '<ffprobe>\n', '</ffprobe>\n']
    assert _patched_generate(tmp_path, _FakeProc(lines)) is None
    assert os.listdir(tmp_path) == []


def test_generate_cancellation_kills_and_cleans_up(tmp_path):
    # Enough lines to cross the cancellation poll interval
    lines = FFPROBE_XML_LINES[:3] + FFPROBE_XML_LINES[3:9] * 200
    proc = _FakeProc(lines)
    result = _patched_generate(tmp_path, proc, check_cancelled=lambda: True)
    assert result is None
    assert proc.killed
    assert os.listdir(tmp_path) == []


def test_generate_no_streams_returns_none(tmp_path):
    with patch.object(ass, 'probe_audio_streams', return_value=[]):
        assert generate_audio_stats_sidecar("input.mkv", str(tmp_path), "VID") is None
    assert os.listdir(tmp_path) == []


# ---- _detect_audio_pkt ----------------------------------------------------

def _write_sidecar(path, frame_attrs):
    with gzip.open(path, 'wt', encoding='utf-8') as f:
        f.write('<?xml version="1.0"?>\n<ffprobe>\n<frames>\n')
        f.write(f'<frame media_type="audio" {frame_attrs}>\n</frame>\n')
        f.write('</frames>\n</ffprobe>\n')


def test_detect_audio_pkt_dts(tmp_path):
    p = str(tmp_path / "a.xml.gz")
    _write_sidecar(p, 'pts_time="0.000000" pkt_dts_time="0.000000"')
    assert _detect_audio_pkt(p) == 'pkt_dts_time'


def test_detect_audio_pkt_pts(tmp_path):
    p = str(tmp_path / "b.xml.gz")
    _write_sidecar(p, 'pkt_pts_time="0.000000"')
    assert _detect_audio_pkt(p) == 'pkt_pts_time'


def test_detect_audio_pkt_defaults_on_missing_file(tmp_path):
    assert _detect_audio_pkt(str(tmp_path / "missing.xml.gz")) == 'pkt_dts_time'


# ---- analyzeAudio contract -------------------------------------------------

def test_analyze_audio_reads_generated_sidecar_shape(tmp_path):
    """A sidecar in the generated shape (flat <tag> children, per-channel astats)
    must parse through analyzeAudio into per-channel imbalance results."""
    sidecar = str(tmp_path / "VID.audio_stats.xml.gz")
    report_dir = tmp_path / "csvs"
    report_dir.mkdir()

    with gzip.open(sidecar, 'wt', encoding='utf-8') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<ffprobe>\n<frames>\n')
        for n in range(20):
            t = n * 0.1
            f.write(f'<frame media_type="audio" pkt_dts_time="{t:.6f}">\n')
            f.write('<tag key="lavfi.astats.1.RMS_level" value="-20.000000"/>\n')
            f.write(f'<tag key="lavfi.astats.2.RMS_level" value="{SILENCE_FLOOR_DB:.6f}"/>\n')
            f.write('<tag key="lavfi.astats.Overall.Peak_level" value="-10.000000"/>\n')
            f.write('<tag key="lavfi.astats.Overall.Flat_factor" value="0.000000"/>\n')
            f.write('</frame>\n')
        f.write('</frames>\n</ffprobe>\n')

    clipping, imbalance, _, _, _ = analyzeAudio(
        sidecar, 'pkt_dts_time', str(report_dir),
        detect_clipping=True, detect_imbalance=True,
    )
    assert clipping['total_audio_frames'] == 20
    assert clipping['clipping_detected'] is False
    assert imbalance['num_channels'] == 2
    # channel 2 sits at the digital-silence floor -> flagged silent
    assert imbalance['silent_channels'] == [2]
