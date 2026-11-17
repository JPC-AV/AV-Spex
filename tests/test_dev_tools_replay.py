"""dev_tools/replays/_detector.py and detector.py: what the detector replay freezes from a run.

The replay itself is checked against real runs (its "Reproduces the run?" count). What is
pinned here is the part that cannot be: how the CLAMS claims are rebuilt from a run's
sidecars, that a replay never switches on a step that needs the video, and that a replay is
found by name like a run.
"""
import json
import sys
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[1] / "dev_tools"
for p in (DEV, DEV / "replays"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import _detector  # noqa: E402
import corpus  # noqa: E402
import detector  # noqa: E402


def clams(tmp_path, bars=None, tones=None):
    if bars is not None:
        (tmp_path / _detector.CLAMS_BARS_CSV).write_text(
            "clams bars detection color bars found:\n" + "".join(f"{l},{a},{b}\n" for l, a, b in bars)
            if bars else "clams bars detection found no color bars\n")
    if tones is not None:
        (tmp_path / _detector.CLAMS_TONE_CSV).write_text(
            "clams tone detection tones found:\n" + "".join(f"{l},{a},{b}\n" for l, a, b in tones)
            if tones else "clams tone detection found no tones\n")
    return tmp_path


def test_no_clams_sidecars_means_clams_did_not_run(tmp_path):
    assert _detector.frozen_clams(tmp_path) == ([], None, None, False)


def test_clams_found_nothing_is_not_the_same_as_did_not_run(tmp_path):
    assert _detector.frozen_clams(clams(tmp_path, bars=[], tones=[])) == ([], None, None, True)


def test_the_first_primary_run_is_the_clams_head_claim(tmp_path):
    regions, start, end, ran = _detector.frozen_clams(clams(
        tmp_path,
        bars=[("primary", "00:00:02.002", "00:00:06.006"), ("second-pass", "00:10:00.000", "00:10:20.500")],
        tones=[("primary", "00:00:01.500", "00:00:03.750")]))
    assert ran and (start, end) == (pytest.approx(2.002), pytest.approx(6.006))
    assert [(round(s, 1), round(e, 1), k) for s, e, k in regions] == \
        [(1.5, 3.8, "tone"), (2.0, 6.0, "bars"), (600.0, 620.5, "bars")]


def test_a_relaxed_head_run_is_the_claim_only_inside_the_head_window(tmp_path):
    early = _detector.frozen_clams(clams(tmp_path, bars=[("head-second-pass", "00:00:07.000", "00:00:25.000")]))
    assert early[1:3] == (7.0, 25.0)
    late = _detector.frozen_clams(clams(tmp_path, bars=[("head-second-pass", "00:00:45.000", "00:00:55.000")]))
    assert late[1:3] == (None, None) and late[0] == [(45.0, 55.0, "bars")]


def test_the_runs_config_is_used_and_fields_added_since_take_their_default(tmp_path):
    (tmp_path / "checks_config.json").write_text(json.dumps(
        {"tools": {"clams_detection": {"run_tool": True}, "qct_parse": {"detect_tone_leak": True}}}))
    configs = _detector.run_configs(tmp_path)
    assert configs["checks"].tools.clams_detection.run_tool is True
    assert configs["checks"].tools.qct_parse.detect_tone_leak is True
    assert configs["checks"].outputs.frame_analysis.duplicate_min_run_length == 2      # not in the run's file


def test_a_replay_never_switches_on_a_step_that_needs_the_video(tmp_path, monkeypatch):
    from AV_Spex.utils.config_manager import ConfigManager
    monkeypatch.setattr(ConfigManager, "_configs", {})
    (tmp_path / "checks_config.json").write_text(json.dumps({"tools": {"qct_parse": {
        "thumbExport": True, "detect_tone_leak": True, "evaluateBars": True, "detect_clamped_levels": True}}}))
    q = _detector._install_config(tmp_path, ["chroma_phase"])["checks"].tools.qct_parse
    assert (q.barsDetection, q.detect_chroma_phase_errors, q.audio_analysis) == (True, True, False)
    assert not (q.thumbExport or q.detect_tone_leak or q.evaluateBars or q.detect_clamped_levels)
    q = _detector._install_config(tmp_path, ["audio_dropout", "bars"])["checks"].tools.qct_parse
    assert (q.detect_chroma_phase_errors, q.audio_analysis) == (False, True)


def test_detectors_that_read_the_video_are_not_replayable():
    assert not set(_detector.REPLAYABLE) & set(_detector.NEEDS_VIDEO)
    assert set(_detector.REPLAYABLE) | set(_detector.NEEDS_VIDEO) == set(detector.sc.DETECTORS)


def test_events_compare_by_value_and_not_run_only_matches_not_run():
    assert detector._same([(1.0, 2.0), (5.0, 5.0)], [(5.001, 5.004), (1.0, 2.0)])
    assert not detector._same([(1.0, 2.0)], [(1.0, 2.1)])
    assert detector._same(None, None) and not detector._same(None, []) and not detector._same([], None)
    assert detector._overlaps((10.0, 11.0), [(11.5, 12.0)], 1.0)
    assert not detector._overlaps((10.0, 11.0), [(12.5, 13.0)], 1.0)


def test_a_replay_is_found_by_name_but_a_run_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS", tmp_path)
    monkeypatch.setattr(corpus, "RUNS", tmp_path / "runs")
    for d in ("runs/abc1234-c1", "out/replays/replay-abc1234-c1-ffff"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "run.json").write_text("{}")
    assert corpus.resolve_run("replay-abc").name == "replay-abc1234-c1-ffff"
    assert corpus.resolve_run("abc").name == "abc1234-c1"
