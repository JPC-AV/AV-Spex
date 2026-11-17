"""Chroma-phase detection skips the bars the consensus confirmed.

Colour bars are saturated colour edge to edge, which is what both chroma-phase
rules look for. The detector used to skip only up to the end of whatever
qct-parse labelled "head", which on degraded head bars is often a short span of
saturated graphics minutes into the program (the real bars being found later as
"additional"), or nothing at all.
"""
import gzip

import pytest

from AV_Spex.checks import qct_parse
from AV_Spex.processing import processing_mgmt as pm

FPS = 30.0


def _report(path, flagged_seconds, total_seconds=60):
    """A QCTools report whose frames trip the envelope rule during flagged_seconds."""
    frames = []
    for n in range(int(total_seconds * FPS)):
        t = n / FPS
        wide = any(lo <= t < hi for lo, hi in flagged_seconds)
        umin, umax = (50, 950) if wide else (400, 600)
        tags = "".join(
            f'<tag key="lavfi.signalstats.{k}" value="{v}"/>'
            for k, v in (("UMIN", umin), ("UMAX", umax), ("VMIN", umin), ("VMAX", umax),
                         ("SATMAX", 300), ("HUEMED", 180), ("SATAVG", 50))
        )
        frames.append(f'<frame media_type="video" pkt_dts_time="{t:.4f}">{tags}</frame>')
    xml = f'<?xml version="1.0"?><ffprobe><frames>{"".join(frames)}</frames></ffprobe>'
    with gzip.open(path, "wb") as fh:
        fh.write(xml.encode())
    return str(path)


def _spans(results):
    return [(round(e["start_time"], 1), round(e["end_time"], 1)) for e in results["events"]]


@pytest.fixture
def report(tmp_path):
    # "bars" at 6-37 s (as on JPC_AV_02678) and a real event at 50-51 s
    return _report(tmp_path / "t.qctools.xml.gz", [(6, 37), (50, 51)])


def test_without_bars_everything_is_reported(report, tmp_path):
    results = qct_parse.analyzeChromaPhaseErrors(report, "pkt_dts_time", str(tmp_path), True)
    assert _spans(results) == [(6.0, 37.0), (50.0, 51.0)]
    assert results["bars_skipped_frames"] == 0


def test_bars_region_is_skipped_wherever_it_is(report, tmp_path):
    results = qct_parse.analyzeChromaPhaseErrors(
        report, "pkt_dts_time", str(tmp_path), True, bars_regions=[(6.0, 37.0)])
    assert _spans(results) == [(50.0, 51.0)]
    assert results["bars_skipped_frames"] > 900


def test_region_skip_leaves_what_precedes_the_bars(tmp_path):
    path = _report(tmp_path / "t.qctools.xml.gz", [(1, 2), (6, 37)])
    results = qct_parse.analyzeChromaPhaseErrors(
        path, "pkt_dts_time", str(tmp_path), True, bars_regions=[(6.0, 37.0)])
    assert _spans(results) == [(1.0, 2.0)]


def test_head_end_time_still_skips_from_the_top(tmp_path):
    path = _report(tmp_path / "t.qctools.xml.gz", [(1, 2), (6, 37), (50, 51)])
    results = qct_parse.analyzeChromaPhaseErrors(
        path, "pkt_dts_time", str(tmp_path), True, bars_end_time=37.1)
    assert _spans(results) == [(50.0, 51.0)]


def test_degenerate_regions_are_ignored(report, tmp_path):
    results = qct_parse.analyzeChromaPhaseErrors(
        report, "pkt_dts_time", str(tmp_path), True, bars_regions=[(None, 5.0), (40.0, 40.0)])
    assert _spans(results) == [(6.0, 37.0), (50.0, 51.0)]


# ---- consensus_after_qctparse ----

def _consensus_recorder(answer=(12.0, False, [(2.0, 12.0)])):
    calls = []

    def bars_consensus(head_start, head_end, all_regions):
        calls.append((head_start, head_end, list(all_regions)))
        return answer
    return bars_consensus, calls


def test_reuses_the_consensus_qctparse_computed():
    bars_consensus, calls = _consensus_recorder()
    result = {"head_bars_start": 2.0, "head_bars_end": 12.0, "all_bars_regions": [("head", 2.0, 12.0)],
              "bars_consensus": (12.5, False, [(2.0, 12.5)])}
    head_end, consensus = pm.consensus_after_qctparse(result, None, bars_consensus)
    assert head_end == 12.0
    assert consensus == (12.5, False, [(2.0, 12.5)])
    assert calls == []


def test_computes_it_when_qctparse_did_not():
    bars_consensus, calls = _consensus_recorder()
    result = {"head_bars_start": 2.0, "head_bars_end": 12.0, "all_bars_regions": [("head", 2.0, 12.0)],
              "bars_consensus": None}
    _, consensus = pm.consensus_after_qctparse(result, None, bars_consensus)
    assert consensus == (12.0, False, [(2.0, 12.0)])
    assert calls == [(2.0, 12.0, [("head", 2.0, 12.0)])]


def test_no_head_reads_the_csv_and_recomputes(tmp_path):
    # qct-parse found the bars only as "additional": the CSV's first region becomes the head claim
    (tmp_path / "qct-parse_colorbars_durations.csv").write_text(
        "qct-parse color bars found:\nadditional-1,00:00:06.7070,00:00:37.1040\n")
    bars_consensus, calls = _consensus_recorder((37.104, False, [(6.707, 37.104)]))
    result = {"head_bars_start": None, "head_bars_end": None,
              "all_bars_regions": [("additional-1", 6.707, 37.104)],
              "bars_consensus": (None, False, [(6.707, 37.104)])}
    head_end, consensus = pm.consensus_after_qctparse(result, str(tmp_path), bars_consensus)
    assert head_end == pytest.approx(37.104)
    assert consensus[0] == pytest.approx(37.104)
    assert len(calls) == 1 and calls[0][1] == pytest.approx(37.104)


def test_no_qctparse_at_all_still_gives_a_consensus():
    bars_consensus, calls = _consensus_recorder((8.0, False, [(1.0, 8.0)]))
    head_end, consensus = pm.consensus_after_qctparse(None, None, bars_consensus)
    assert head_end is None
    assert consensus == (8.0, False, [(1.0, 8.0)])
    assert calls == [(None, None, [])]
