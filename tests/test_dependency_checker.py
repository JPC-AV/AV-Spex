"""Tests for utils.dependency_checker.

Covers the non-GUI surface area:
* DependencyInfo / DependencyStatus dataclasses + enum
* check_external_dependency — wraps shutil.which
* check_py_version — sys.exit on too-old Python
* cli_deps_check — orchestrator that bails when any required tool missing
* DependencyManager.check_dependencies_cli — prints per-tool status
* DependencyManager._get_cli_dependencies — required-tools list
* DependencyCheckDialog._get_required_dependencies — GUI version of the list
  (called as an unbound method to avoid building the actual QDialog)
* DependencyCheckWorker.run — sequential checks, per-tool status assignment,
  emits dependency_checked / all_checks_complete signals
* DependencyCheckWorker._check_version — subprocess wrapper for version probe
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.utils import dependency_checker as dc


# ---------------------------------------------------------------------------
# DependencyInfo / DependencyStatus
# ---------------------------------------------------------------------------

def test_dependency_info_defaults():
    """All fields except name + command should have sensible defaults."""
    info = dc.DependencyInfo(name="FFmpeg", command="ffmpeg")
    assert info.name == "FFmpeg"
    assert info.command == "ffmpeg"
    assert info.version_command is None
    assert info.min_version is None
    assert info.description == ""
    assert info.install_hint == ""
    assert info.status == dc.DependencyStatus.CHECKING
    assert info.version_found is None
    assert info.error_message is None


def test_dependency_status_enum_values():
    assert dc.DependencyStatus.FOUND.value == "found"
    assert dc.DependencyStatus.NOT_FOUND.value == "not_found"
    assert dc.DependencyStatus.VERSION_ISSUE.value == "version_issue"
    assert dc.DependencyStatus.CHECKING.value == "checking"


# ---------------------------------------------------------------------------
# check_external_dependency
# ---------------------------------------------------------------------------

def test_check_external_dependency_present(monkeypatch):
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/usr/bin/" + name)
    assert dc.check_external_dependency("ffmpeg") is True


def test_check_external_dependency_missing(monkeypatch):
    monkeypatch.setattr(dc.shutil, "which", lambda _name: None)
    assert dc.check_external_dependency("nope") is False


# ---------------------------------------------------------------------------
# check_py_version
# ---------------------------------------------------------------------------

def test_check_py_version_modern_python_passes(monkeypatch):
    """Real Python is 3.10+ — function should be a no-op."""
    # No patching needed; simply ensure no SystemExit
    dc.check_py_version()


def test_check_py_version_old_python_exits(monkeypatch):
    fake_version = (3, 9, 0, "final", 0)
    monkeypatch.setattr(dc.sys, "version_info", fake_version)
    with pytest.raises(SystemExit) as exc:
        dc.check_py_version()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# cli_deps_check
# ---------------------------------------------------------------------------

def test_cli_deps_check_all_present_returns_true(monkeypatch):
    monkeypatch.setattr(dc, "check_external_dependency", lambda _c: True)
    # check_py_version is a real call; for current interpreter (3.10+) it's a no-op.
    assert dc.cli_deps_check() is True


def test_cli_deps_check_one_missing_returns_false(monkeypatch):
    """As soon as a single required command is missing, return False."""
    seen = []

    def fake_external(cmd):
        seen.append(cmd)
        return cmd != "mediaconch"  # mediaconch is the missing one

    monkeypatch.setattr(dc, "check_external_dependency", fake_external)
    result = dc.cli_deps_check()
    assert result is False
    # Function should have early-returned at the missing tool, not iterated all
    assert "mediaconch" in seen
    # Tools after mediaconch in the canonical order shouldn't be checked
    canonical = ['ffmpeg', 'mediainfo', 'exiftool', 'mediaconch', 'qcli', 'mkvmerge']
    cutoff = canonical.index("mediaconch")
    for after in canonical[cutoff + 1:]:
        assert after not in seen


def test_cli_deps_check_propagates_old_python_exit(monkeypatch):
    """check_py_version raises SystemExit; cli_deps_check should not swallow it."""
    monkeypatch.setattr(dc.sys, "version_info", (3, 8, 0, "final", 0))
    with pytest.raises(SystemExit):
        dc.cli_deps_check()


# ---------------------------------------------------------------------------
# DependencyManager.check_dependencies_cli
# ---------------------------------------------------------------------------

def test_check_dependencies_cli_all_present(monkeypatch, capsys):
    monkeypatch.setattr(dc.shutil, "which", lambda name: "/usr/bin/" + name)
    ok = dc.DependencyManager.check_dependencies_cli()
    assert ok is True
    out = capsys.readouterr().out
    assert "FFmpeg: Found" in out
    assert "MediaInfo: Found" in out
    assert "QCTools: Found" in out
    assert "MKVToolNix: Found" in out


def test_check_dependencies_cli_missing_logs_install_hint(monkeypatch, capsys):
    """When a tool is missing, its install hint appears in output and result is False."""
    def fake_which(name):
        return None if name == "exiftool" else "/usr/bin/" + name
    monkeypatch.setattr(dc.shutil, "which", fake_which)

    ok = dc.DependencyManager.check_dependencies_cli()
    assert ok is False
    out = capsys.readouterr().out
    assert "ExifTool: Not found" in out
    assert "brew install exiftool" in out  # install hint surfaced


# ---------------------------------------------------------------------------
# Required-dependency lists
# ---------------------------------------------------------------------------

def test_get_cli_dependencies_includes_all_expected_tools():
    deps = dc.DependencyManager._get_cli_dependencies()
    names = {d.name for d in deps}
    assert names == {"FFmpeg", "MediaInfo", "ExifTool", "MediaConch", "QCTools",
                     "MKVToolNix", "mkvalidator"}
    # Each entry must have a real command + install hint to be useful in CLI output
    for d in deps:
        assert d.command
        assert d.install_hint


def test_gui_required_dependencies_have_version_commands():
    """GUI list also defines version_command for each tool (used by _check_version)."""
    # Call the unbound method on a stand-in `self` to avoid building an actual QDialog
    deps = dc.DependencyCheckDialog._get_required_dependencies(self=MagicMock())
    names = {d.name for d in deps}
    assert names == {"FFmpeg", "MediaInfo", "ExifTool", "MediaConch", "QCTools",
                     "MKVToolNix", "mkvalidator"}
    for d in deps:
        assert d.version_command, f"{d.name} should declare a version_command for the GUI checker"


# ---------------------------------------------------------------------------
# DependencyCheckWorker.run
# ---------------------------------------------------------------------------

def _spy_signals(worker):
    """Replace the worker's emit signals with MagicMocks so we can inspect calls."""
    worker.dependency_checked = MagicMock()
    worker.all_checks_complete = MagicMock()


def test_worker_run_marks_present_command_as_found(monkeypatch):
    info = dc.DependencyInfo(name="X", command="ffmpeg")
    monkeypatch.setattr(dc.shutil, "which", lambda c: "/usr/bin/" + c if c == "ffmpeg" else None)

    worker = dc.DependencyCheckWorker([info])
    _spy_signals(worker)
    worker.run()

    assert info.status == dc.DependencyStatus.FOUND
    worker.dependency_checked.emit.assert_called_once_with("X", info)
    worker.all_checks_complete.emit.assert_called_once_with(True)


def test_worker_run_marks_missing_command_as_not_found(monkeypatch):
    info = dc.DependencyInfo(name="X", command="not-a-tool")
    monkeypatch.setattr(dc.shutil, "which", lambda _c: None)

    worker = dc.DependencyCheckWorker([info])
    _spy_signals(worker)
    worker.run()

    assert info.status == dc.DependencyStatus.NOT_FOUND
    assert "not found in PATH" in info.error_message
    worker.all_checks_complete.emit.assert_called_once_with(False)


def test_worker_run_aggregates_mixed_results(monkeypatch):
    """When some are found and some are missing, all_checks_complete=False."""
    a = dc.DependencyInfo(name="A", command="present")
    b = dc.DependencyInfo(name="B", command="absent")
    monkeypatch.setattr(dc.shutil, "which", lambda c: "/usr/bin/" + c if c == "present" else None)

    worker = dc.DependencyCheckWorker([a, b])
    _spy_signals(worker)
    worker.run()

    assert a.status == dc.DependencyStatus.FOUND
    assert b.status == dc.DependencyStatus.NOT_FOUND
    assert worker.dependency_checked.emit.call_count == 2
    worker.all_checks_complete.emit.assert_called_once_with(False)


def test_worker_run_invokes_version_check_when_specified(monkeypatch):
    info = dc.DependencyInfo(
        name="X", command="ffmpeg",
        version_command="ffmpeg -version",
        min_version="4.0",
    )
    monkeypatch.setattr(dc.shutil, "which", lambda _c: "/usr/bin/ffmpeg")
    fake_proc = MagicMock(returncode=0, stdout="ffmpeg version 5.0\n", stderr="")
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **kw: fake_proc)

    worker = dc.DependencyCheckWorker([info])
    _spy_signals(worker)
    worker.run()

    # Per the simplified _check_version implementation, any zero returncode is a pass
    assert info.status == dc.DependencyStatus.FOUND
    assert info.version_found == "ffmpeg version 5.0"
    worker.all_checks_complete.emit.assert_called_once_with(True)


def test_worker_run_cancelled_short_circuits(monkeypatch):
    """If cancel() is called before run(), iteration stops immediately."""
    a = dc.DependencyInfo(name="A", command="anything")
    b = dc.DependencyInfo(name="B", command="anything")
    monkeypatch.setattr(dc.shutil, "which", lambda _c: "/usr/bin/anything")

    worker = dc.DependencyCheckWorker([a, b])
    _spy_signals(worker)
    worker.cancel()
    worker.run()

    # No emits, no statuses changed
    worker.dependency_checked.emit.assert_not_called()
    worker.all_checks_complete.emit.assert_not_called()
    assert a.status == dc.DependencyStatus.CHECKING
    assert b.status == dc.DependencyStatus.CHECKING


# ---------------------------------------------------------------------------
# DependencyCheckWorker._check_version
# ---------------------------------------------------------------------------

def test_check_version_zero_returncode_returns_true(monkeypatch):
    info = dc.DependencyInfo(name="X", command="ffmpeg", version_command="ffmpeg -version", min_version="4.0")
    fake_proc = MagicMock(returncode=0, stdout="ffmpeg version 5.0\n", stderr="")
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **kw: fake_proc)

    worker = dc.DependencyCheckWorker([info])
    ok, version = worker._check_version(info)
    assert ok is True
    assert version == "ffmpeg version 5.0"


def test_check_version_nonzero_returncode_returns_false(monkeypatch):
    info = dc.DependencyInfo(name="X", command="x", version_command="x --version", min_version="1.0")
    fake_proc = MagicMock(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(dc.subprocess, "run", lambda *a, **kw: fake_proc)

    worker = dc.DependencyCheckWorker([info])
    ok, version = worker._check_version(info)
    assert ok is False
    assert version == "Unknown"


def test_check_version_subprocess_error_returns_false(monkeypatch):
    info = dc.DependencyInfo(name="X", command="x", version_command="x --version", min_version="1.0")
    monkeypatch.setattr(dc.subprocess, "run", MagicMock(side_effect=dc.subprocess.SubprocessError("oops")))

    worker = dc.DependencyCheckWorker([info])
    ok, version = worker._check_version(info)
    assert ok is False
    assert version == "Unknown"


def test_check_version_timeout_returns_false(monkeypatch):
    info = dc.DependencyInfo(name="X", command="x", version_command="x --version", min_version="1.0")
    monkeypatch.setattr(
        dc.subprocess, "run",
        MagicMock(side_effect=dc.subprocess.TimeoutExpired(cmd="x --version", timeout=10)),
    )

    worker = dc.DependencyCheckWorker([info])
    ok, version = worker._check_version(info)
    assert ok is False
    assert version == "Unknown"


def test_check_version_file_not_found_returns_false(monkeypatch):
    """If the version command itself is missing on PATH, FileNotFoundError → False."""
    info = dc.DependencyInfo(name="X", command="x", version_command="x --version", min_version="1.0")
    monkeypatch.setattr(dc.subprocess, "run", MagicMock(side_effect=FileNotFoundError("missing")))

    worker = dc.DependencyCheckWorker([info])
    ok, version = worker._check_version(info)
    assert ok is False
    assert version == "Unknown"


def test_check_version_uses_split_command_args(monkeypatch):
    """version_command is shell-string; _check_version splits it before passing to subprocess."""
    info = dc.DependencyInfo(name="X", command="x", version_command="ffmpeg -version", min_version="1.0")
    fake_proc = MagicMock(returncode=0, stdout="v5", stderr="")
    run_mock = MagicMock(return_value=fake_proc)
    monkeypatch.setattr(dc.subprocess, "run", run_mock)

    worker = dc.DependencyCheckWorker([info])
    worker._check_version(info)

    # First positional arg should be the split list
    assert run_mock.call_args.args[0] == ["ffmpeg", "-version"]
    # Should request capture + text + a sane timeout
    assert run_mock.call_args.kwargs["capture_output"] is True
    assert run_mock.call_args.kwargs["text"] is True
    assert run_mock.call_args.kwargs["timeout"] == 10
