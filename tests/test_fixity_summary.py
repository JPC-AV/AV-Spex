"""
Tests for utils/fixity_summary.py — the read-modify-write JSON summary
consumed by the HTML report's Fixity section — and for the stale-summary
wipe at the start of ProcessingManager.process_fixity().
"""

import json
import os
from unittest.mock import MagicMock

import pytest

from AV_Spex.utils import fixity_summary as fs


VIDEO_ID = "JPC_AV_00001"


def _summary_path(root):
    return os.path.join(root, f"{VIDEO_ID}_qc_metadata", f"{VIDEO_ID}_fixity_summary.json")


def _read(root):
    with open(_summary_path(root)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# update_fixity_summary
# ---------------------------------------------------------------------------

def test_update_creates_summary_and_qc_dir(tmp_path):
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "passed"})
    summary = _read(str(tmp_path))
    assert summary["video_id"] == VIDEO_ID
    assert summary["whole_file"] == {"result": "passed"}


def test_update_preserves_other_sections(tmp_path):
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "stream_fixity", {"result": "passed"})
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "failed"})
    summary = _read(str(tmp_path))
    assert summary["stream_fixity"] == {"result": "passed"}
    assert summary["whole_file"] == {"result": "failed"}


def test_update_overwrites_same_section(tmp_path):
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "failed"})
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "passed"})
    assert _read(str(tmp_path))["whole_file"] == {"result": "passed"}


def test_update_recovers_from_corrupt_summary(tmp_path):
    os.makedirs(os.path.dirname(_summary_path(str(tmp_path))))
    with open(_summary_path(str(tmp_path)), "w") as fh:
        fh.write("not json{")
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "passed"})
    assert _read(str(tmp_path))["whole_file"] == {"result": "passed"}


# ---------------------------------------------------------------------------
# clear_fixity_summary
# ---------------------------------------------------------------------------

def test_clear_removes_summary(tmp_path):
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "passed"})
    assert os.path.isfile(_summary_path(str(tmp_path)))
    fs.clear_fixity_summary(str(tmp_path), VIDEO_ID)
    assert not os.path.isfile(_summary_path(str(tmp_path)))


def test_clear_is_noop_when_summary_missing(tmp_path):
    # Should not raise.
    fs.clear_fixity_summary(str(tmp_path), VIDEO_ID)


# ---------------------------------------------------------------------------
# process_fixity wipes stale sections from previous runs
# ---------------------------------------------------------------------------

def test_process_fixity_clears_stale_summary(tmp_path, monkeypatch):
    """
    A summary section left by a previous run (e.g. whole-file validation) must
    not survive into a run that doesn't perform that step, or the report would
    present it as a current result.
    """
    from AV_Spex.processing import processing_mgmt as pm

    class _FakeFixity:
        check_fixity = False
        validate_stream_fixity = False
        embed_stream_fixity = False
        output_fixity = False
        overwrite_stream_fixity = False

    fake_checks = MagicMock()
    fake_checks.fixity = _FakeFixity()
    fake_checks.video_file_extension = "mkv"
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = fake_checks
    monkeypatch.setattr(pm, "ConfigManager", lambda: mock_mgr)

    # Stale summary from a previous validation run.
    fs.update_fixity_summary(str(tmp_path), VIDEO_ID, "whole_file", {"result": "passed"})

    manager = pm.ProcessingManager()
    manager.process_fixity(str(tmp_path), str(tmp_path / f"{VIDEO_ID}.mkv"), VIDEO_ID)

    assert not os.path.isfile(_summary_path(str(tmp_path)))
