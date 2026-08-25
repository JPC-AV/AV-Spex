"""Tests for AV_Spex.checks.duplicate_frame_detection.

Focus: the file-aware timecode labelling. Neither sample video in the
regression set contains a freeze, so that block never runs there — and it is
the part that depends on the qct_parse timecode helpers being imported
correctly. A wrong import would fail silently behind the broad exception
handling, so it is pinned here instead.
"""

from types import SimpleNamespace

import pytest

from AV_Spex.checks import duplicate_frame_detection as dfd


def _candidate(start, end, count=3):
    return {
        'start_time': start,
        'end_time': end,
        'duplicate_count': count,
        'avg_ydif': 0.7, 'max_ydif': 0.9,
        'avg_udif': 0.4, 'avg_vdif': 0.4, 'avg_vrep': 0.2,
    }


def _parser(*candidates, fps=29.97):
    """Minimal stand-in for QCToolsParser."""
    return SimpleNamespace(
        fps=fps,
        bit_depth_10=True,
        find_duplicate_frame_candidates=lambda **kw: (
            list(candidates),
            {'ydif': 1.0, 'udif': 1.0, 'vdif': 1.0},
        ),
    )


def _run(tmp_path, *candidates, video='/nonexistent/v.mkv'):
    """Detect against an unopenable video, so candidates pass unverified.

    That is the documented behavior when OpenCV cannot open the file, and it
    is what lets these tests reach the timecode block without needing a real
    frozen-frame video.
    """
    return dfd.detect_duplicate_frames(
        video, str(tmp_path), qctools_parser=_parser(*candidates),
        check_cancelled=lambda: False,
    )


def test_no_parser_returns_none(tmp_path):
    assert dfd.detect_duplicate_frames('/v.mkv', str(tmp_path), qctools_parser=None) is None


def test_no_candidates_is_a_clean_result(tmp_path):
    result = _run(tmp_path)
    assert result.status == 'clean'
    assert result.runs == []


def test_run_is_labelled_with_file_timecode(tmp_path):
    """Positions must be the file's own timecode, not raw seconds."""
    result = _run(tmp_path, _candidate(2079.911, 2079.978))
    assert len(result.runs) == 1
    run = result.runs[0]
    assert run.start_timecode is not None, "timecode labelling did not run"
    # NDF at 29.97: HH:MM:SS:FF with colons, not wall-clock seconds.
    assert run.start_timecode.count(':') == 3
    assert run.end_timecode is not None


def test_timecode_matches_the_ndf_conversion(tmp_path):
    """Cross-check against the qct_parse helper the module delegates to."""
    from AV_Spex.checks.qct_parse import _tc_format_timecode
    start = 2079.911
    result = _run(tmp_path, _candidate(start, start + 0.067))
    expected = _tc_format_timecode(start, 29.97, 0, False)
    assert result.runs[0].start_timecode == expected


def test_ndf_timecode_runs_behind_wall_clock(tmp_path):
    """NDF drifts ~3.6 s/hour behind wall time — the reason for this whole path."""
    result = _run(tmp_path, _candidate(3600.0, 3600.1))
    # One wall-clock hour lands before 01:00:00:00 in NDF.
    assert result.runs[0].start_timecode < "01:00:00:00"


def test_counts_and_loss_are_summed(tmp_path):
    result = _run(tmp_path, _candidate(10.0, 10.1, count=3), _candidate(20.0, 20.2, count=6))
    assert result.total_duplicate_frames == 9
    assert result.estimated_loss_seconds == pytest.approx(9 / 29.97, abs=1e-3)


def test_status_escalates_with_findings(tmp_path):
    clean = _run(tmp_path)
    flagged = _run(tmp_path, _candidate(10.0, 10.1))
    assert clean.status == 'clean'
    assert flagged.status in ('warning', 'critical')
