import hashlib
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from AV_Spex.checks import fixity_check as fc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_config_mgr(monkeypatch):
    """
    Patch ConfigManager in fixity_check so get_checksum_algorithm() returns the
    algorithm we want for the test.
    """
    state = {"algorithm": "md5"}

    def _configure(algorithm):
        state["algorithm"] = algorithm

    fake_fixity = MagicMock()
    # Make getattr(fake_fixity, 'checksum_algorithm', 'md5') look up the state
    # dynamically. Using property-like descriptor via side-effectful MagicMock
    # attribute isn't trivial, so we use a tiny proxy class instead.

    class _FakeFixity:
        @property
        def checksum_algorithm(self):
            return state["algorithm"]

    fake_checks = MagicMock()
    fake_checks.fixity = _FakeFixity()
    # Sample fixtures use .mkv; get_video_file_extension() reads this field.
    fake_checks.video_file_extension = "mkv"

    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = fake_checks

    monkeypatch.setattr(
        "AV_Spex.checks.fixity_check.ConfigManager", lambda: mock_mgr
    )
    return _configure


@pytest.fixture
def sample_video_file(tmp_path):
    """Create a tiny fake MKV file and return its path + known checksums."""
    content = b"fake video data for fixity testing"
    video_path = tmp_path / "JPC_AV_00001.mkv"
    video_path.write_bytes(content)

    return {
        "path": str(video_path),
        "dir": str(tmp_path),
        "video_id": "JPC_AV_00001",
        "md5": hashlib.md5(content).hexdigest(),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


# ---------------------------------------------------------------------------
# calculate_checksum
# ---------------------------------------------------------------------------

def test_calculate_checksum_md5(sample_video_file):
    result = fc.calculate_checksum(sample_video_file["path"], algorithm="md5")
    assert result == sample_video_file["md5"]


def test_calculate_checksum_sha256(sample_video_file):
    result = fc.calculate_checksum(sample_video_file["path"], algorithm="sha256")
    assert result == sample_video_file["sha256"]


def test_calculate_checksum_default_is_md5(sample_video_file):
    result = fc.calculate_checksum(sample_video_file["path"])
    assert result == sample_video_file["md5"]


def test_calculate_checksum_cancelled_before_start(sample_video_file):
    cancelled = lambda: True
    result = fc.calculate_checksum(
        sample_video_file["path"], algorithm="md5", check_cancelled=cancelled
    )
    assert result is None


def test_calculate_checksum_emits_progress_signal(sample_video_file):
    signals = MagicMock()
    fc.calculate_checksum(
        sample_video_file["path"], algorithm="md5", signals=signals
    )
    # For a small file we should still hit 100% once.
    assert signals.md5_progress.emit.called
    # The final emitted value should always be between 0 and 100.
    for call in signals.md5_progress.emit.call_args_list:
        (value,) = call.args
        assert 0 <= value <= 100


# ---------------------------------------------------------------------------
# read_checksum_from_file
# ---------------------------------------------------------------------------

def test_read_checksum_md5(tmp_path):
    path = tmp_path / "file_2026_04_20.md5"
    path.write_text("d41d8cd98f00b204e9800998ecf8427e  foo.mkv\n")
    checksum, algo = fc.read_checksum_from_file(str(path))
    assert checksum == "d41d8cd98f00b204e9800998ecf8427e"
    assert algo == "md5"


def test_read_checksum_sha256(tmp_path):
    hash_str = "a" * 64
    path = tmp_path / "file.sha256"
    path.write_text(f"{hash_str}  foo.mkv\n")
    checksum, algo = fc.read_checksum_from_file(str(path))
    assert checksum == hash_str
    assert algo == "sha256"


def test_read_checksum_no_checksum_returns_none(tmp_path):
    path = tmp_path / "empty.md5"
    path.write_text("no checksum here\n")
    checksum, algo = fc.read_checksum_from_file(str(path))
    assert checksum is None
    assert algo is None


def test_read_checksum_latin1_fallback(tmp_path):
    """File that isn't UTF-8 should fall back to latin-1 and still find the hash."""
    path = tmp_path / "weird.md5"
    # Byte 0xff is invalid UTF-8 but valid latin-1.
    content = b"\xff d41d8cd98f00b204e9800998ecf8427e  foo.mkv\n"
    path.write_bytes(content)
    checksum, algo = fc.read_checksum_from_file(str(path))
    assert checksum == "d41d8cd98f00b204e9800998ecf8427e"
    assert algo == "md5"


# ---------------------------------------------------------------------------
# output_fixity
# ---------------------------------------------------------------------------

def test_output_fixity_writes_md5_and_txt(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    checksum = fc.output_fixity(sample_video_file["dir"], sample_video_file["path"])
    assert checksum == sample_video_file["md5"]

    # Two output files should exist: *_fixity.txt and *_fixity.md5
    entries = os.listdir(sample_video_file["dir"])
    assert any(e.endswith("_fixity.txt") for e in entries)
    assert any(e.endswith("_fixity.md5") for e in entries)

    # Content of txt file should include the checksum and basename.
    txt_file = [e for e in entries if e.endswith("_fixity.txt")][0]
    with open(os.path.join(sample_video_file["dir"], txt_file)) as fh:
        body = fh.read()
    assert sample_video_file["md5"] in body
    assert "JPC_AV_00001.mkv" in body


def test_output_fixity_sha256_uses_sha256_extension(sample_video_file, patch_config_mgr):
    patch_config_mgr("sha256")
    checksum = fc.output_fixity(sample_video_file["dir"], sample_video_file["path"])
    assert checksum == sample_video_file["sha256"]

    entries = os.listdir(sample_video_file["dir"])
    assert any(e.endswith("_fixity.sha256") for e in entries)


def test_output_fixity_cancelled_returns_none(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    result = fc.output_fixity(
        sample_video_file["dir"],
        sample_video_file["path"],
        check_cancelled=lambda: True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# get_checksum_algorithm
# ---------------------------------------------------------------------------

def test_get_checksum_algorithm_reads_config(patch_config_mgr):
    patch_config_mgr("sha256")
    assert fc.get_checksum_algorithm() == "sha256"
    patch_config_mgr("MD5")  # stored as uppercase
    assert fc.get_checksum_algorithm() == "md5"


# ---------------------------------------------------------------------------
# check_fixity (integration-ish)
# ---------------------------------------------------------------------------

def _make_qc_dir(root, video_id):
    """Create the expected {video_id}_qc_metadata subdirectory."""
    qc_dir = os.path.join(root, f"{video_id}_qc_metadata")
    os.makedirs(qc_dir, exist_ok=True)
    return qc_dir


def test_check_fixity_passes_when_checksums_match(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    qc_dir = _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    # Write a stored checksum file with a valid date-suffix filename.
    stored_file = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2026_04_19_fixity.md5",
    )
    with open(stored_file, "w") as fh:
        fh.write(f"{sample_video_file['md5']}  {sample_video_file['video_id']}.mkv\n")

    fc.check_fixity(
        sample_video_file["dir"],
        sample_video_file["video_id"],
    )

    # Result file should be created in the qc_metadata dir with "passed" in it.
    results = [f for f in os.listdir(qc_dir) if f.endswith("_fixity_check.txt")]
    assert len(results) == 1
    with open(os.path.join(qc_dir, results[0])) as fh:
        body = fh.read()
    assert "Fixity check passed" in body


def test_check_fixity_fails_when_checksums_mismatch(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    qc_dir = _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    wrong_checksum = "0" * 32
    stored_file = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2026_04_19_fixity.md5",
    )
    with open(stored_file, "w") as fh:
        fh.write(f"{wrong_checksum}  {sample_video_file['video_id']}.mkv\n")

    fc.check_fixity(sample_video_file["dir"], sample_video_file["video_id"])

    results = [f for f in os.listdir(qc_dir) if f.endswith("_fixity_check.txt")]
    assert len(results) == 1
    with open(os.path.join(qc_dir, results[0])) as fh:
        body = fh.read()
    assert "Fixity check failed" in body
    assert wrong_checksum in body
    assert sample_video_file["md5"] in body


def test_check_fixity_creates_checksum_if_none_exists(sample_video_file, patch_config_mgr):
    """
    When there are no checksum files and no pre-calculated checksum, check_fixity
    should delegate to output_fixity to create one.
    """
    patch_config_mgr("md5")
    _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    # No stored checksum files yet.
    fc.check_fixity(sample_video_file["dir"], sample_video_file["video_id"])

    entries = os.listdir(sample_video_file["dir"])
    assert any(e.endswith("_fixity.md5") for e in entries)


def test_check_fixity_cancelled_returns_none(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])
    result = fc.check_fixity(
        sample_video_file["dir"],
        sample_video_file["video_id"],
        check_cancelled=lambda: True,
    )
    assert result is None


def test_check_fixity_missing_video_file(tmp_path, patch_config_mgr):
    """check_fixity should return gracefully when the video file is absent."""
    patch_config_mgr("md5")
    _make_qc_dir(str(tmp_path), "JPC_AV_99999")
    # No .mkv file exists.
    result = fc.check_fixity(str(tmp_path), "JPC_AV_99999")
    assert result is None


def _load_fixity_summary(root, video_id):
    import json
    summary_path = os.path.join(
        root, f"{video_id}_qc_metadata", f"{video_id}_fixity_summary.json"
    )
    with open(summary_path) as fh:
        return json.load(fh)


def test_output_fixity_writes_fixity_summary(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    fc.output_fixity(sample_video_file["dir"], sample_video_file["path"])

    summary = _load_fixity_summary(sample_video_file["dir"], sample_video_file["video_id"])
    section = summary["checksum_output"]
    assert section["algorithm"] == "md5"
    assert section["checksum"] == sample_video_file["md5"]
    assert section["checksum_file"].endswith("_fixity.md5")
    assert section["created_at"]


def test_check_fixity_pass_writes_fixity_summary(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    stored_file = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2026_04_19_fixity.md5",
    )
    with open(stored_file, "w") as fh:
        fh.write(f"{sample_video_file['md5']}  {sample_video_file['video_id']}.mkv\n")

    fc.check_fixity(sample_video_file["dir"], sample_video_file["video_id"])

    summary = _load_fixity_summary(sample_video_file["dir"], sample_video_file["video_id"])
    whole_file = summary["whole_file"]
    assert whole_file["result"] == "passed"
    assert whole_file["algorithm"] == "md5"
    assert whole_file["computed_checksum"] == sample_video_file["md5"]
    assert whole_file["validated_at"]
    assert whole_file["validated_against"] == [{
        "file": os.path.basename(stored_file),
        "date": "2026-04-19",
        "checksum": sample_video_file["md5"],
    }]


def test_check_fixity_fail_writes_fixity_summary(sample_video_file, patch_config_mgr):
    patch_config_mgr("md5")
    _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    wrong_checksum = "0" * 32
    stored_file = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2026_04_19_fixity.md5",
    )
    with open(stored_file, "w") as fh:
        fh.write(f"{wrong_checksum}  {sample_video_file['video_id']}.mkv\n")

    fc.check_fixity(sample_video_file["dir"], sample_video_file["video_id"])

    summary = _load_fixity_summary(sample_video_file["dir"], sample_video_file["video_id"])
    whole_file = summary["whole_file"]
    assert whole_file["result"] == "failed"
    assert whole_file["computed_checksum"] == sample_video_file["md5"]
    assert whole_file["validated_against"][0]["checksum"] == wrong_checksum


def test_check_fixity_handles_multiple_checksum_files(sample_video_file, patch_config_mgr):
    """
    Multiple stored checksum files with different dates — all are considered,
    and a mismatch against the calculated hash should still be detected.
    """
    patch_config_mgr("md5")
    qc_dir = _make_qc_dir(sample_video_file["dir"], sample_video_file["video_id"])

    older = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2025_01_01_fixity.md5",
    )
    newer = os.path.join(
        sample_video_file["dir"],
        f"{sample_video_file['video_id']}_2026_04_19_fixity.md5",
    )
    with open(older, "w") as fh:
        fh.write(f"{'1' * 32}  f.mkv\n")
    with open(newer, "w") as fh:
        fh.write(f"{'2' * 32}  f.mkv\n")

    fc.check_fixity(sample_video_file["dir"], sample_video_file["video_id"])

    results = [f for f in os.listdir(qc_dir) if f.endswith("_fixity_check.txt")]
    assert len(results) == 1
    with open(os.path.join(qc_dir, results[0])) as fh:
        body = fh.read()
    # Neither stored hash matches the real one, so this should be a failure.
    assert "Fixity check failed" in body
    assert sample_video_file["md5"] in body
