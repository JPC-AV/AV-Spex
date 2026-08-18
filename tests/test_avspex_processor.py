"""Tests for processing.avspex_processor.

Covers:
* log_overall_time + check_directory smoke (preserved from original test)
* AVSpexProcessor.cancel / check_cancelled lifecycle (flag + single emit)
* AVSpexProcessor.__init__ refresh-on-init contract
* process_directories — empty list, success path, early-cancel, signal emissions
* process_single_directory — per-file logging try/finally even on exception
* _process_directory_contents — fixity / mediaconch / metadata / outputs branching
  driven by checks_config flags, including the new clams_detection +
  frame_analysis sub-step triggers for outputs.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.processing import avspex_processor as ap
from AV_Spex.utils import dir_setup


# ===========================================================================
# Pre-existing tests (preserved)
# ===========================================================================

def test_log_overall_time():
    assert ap.log_overall_time(1733854413.191993, 1733854426.615125) == '00:00:13'


def test_log_overall_time_past_24_hours_does_not_wrap():
    """A 36:23:40 batch must not report 12:23:40 (the old gmtime behavior)."""
    start = 1733854413.0
    assert ap.log_overall_time(start, start + 131020) == '36:23:40'


def test_check_directory_matches(tmp_path):
    """Use a tmp dir matching the video_id rather than a hard-coded user path."""
    d = tmp_path / "JPC_AV_01709"
    d.mkdir()
    assert dir_setup.check_directory(str(d), "JPC_AV_01709") is True


# ===========================================================================
# Helpers
# ===========================================================================

def _make_checks_config(
    *,
    fixity_flags=None,
    mediaconch=False,
    metadata_tools=None,
    qctools=False,
    qct_parse=False,
    clams_detection=False,
    access_file=False,
    report=False,
    frame_flags=None,
):
    """Build a fake ChecksConfig where each named flag can be toggled on."""
    fixity_flags = fixity_flags or {}
    metadata_tools = metadata_tools or {}
    frame_flags = frame_flags or {}

    cfg = MagicMock()
    # fixity
    cfg.fixity.check_fixity = fixity_flags.get("check_fixity", False)
    cfg.fixity.validate_stream_fixity = fixity_flags.get("validate_stream_fixity", False)
    cfg.fixity.embed_stream_fixity = fixity_flags.get("embed_stream_fixity", False)
    cfg.fixity.output_fixity = fixity_flags.get("output_fixity", False)
    # mediaconch
    cfg.tools.mediaconch.run_mediaconch = mediaconch
    # metadata tools
    for tool in ("mediainfo", "mediatrace", "exiftool", "ffprobe", "mkvalidator"):
        getattr(cfg.tools, tool).check_tool = metadata_tools.get(tool, False)
        getattr(cfg.tools, tool).run_tool = metadata_tools.get(tool, False)
    # outputs
    cfg.outputs.access_file = access_file
    cfg.outputs.report = report
    cfg.tools.qctools.run_tool = qctools
    cfg.tools.qct_parse.run_tool = qct_parse
    cfg.tools.clams_detection.run_tool = clams_detection
    # frame analysis sub-steps
    cfg.outputs.frame_analysis.enable_bitplane_check = frame_flags.get("bitplane", False)
    cfg.outputs.frame_analysis.enable_border_detection = frame_flags.get("border", False)
    cfg.outputs.frame_analysis.enable_brng_analysis = frame_flags.get("brng", False)
    cfg.outputs.frame_analysis.enable_signalstats = frame_flags.get("signalstats", False)
    cfg.outputs.frame_analysis.enable_dropped_sample_detection = frame_flags.get("dropped_sample", False)
    cfg.outputs.frame_analysis.enable_duplicate_frame_detection = frame_flags.get("duplicate_frame", False)
    return cfg


def _make_processor_with_config(checks_cfg, signals=None):
    """Construct an AVSpexProcessor and replace its checks_config with a fake."""
    proc = ap.AVSpexProcessor(signals=signals)
    proc.checks_config = checks_cfg
    return proc


def _stub_directory_init(monkeypatch, video_id="vid_x", dest_dir="/tmp/dest"):
    """Patch dir_setup.initialize_directory to return canned values (no FS work)."""
    monkeypatch.setattr(
        ap.dir_setup, "initialize_directory",
        lambda _src: ("/v/in.mkv", video_id, dest_dir, None),
    )


def _stub_per_file_logging(monkeypatch):
    """Make start/stop file log no-ops so tests don't write to disk."""
    monkeypatch.setattr(ap, "start_file_log", lambda *a, **kw: "/tmp/file.log")
    monkeypatch.setattr(ap, "stop_file_log", MagicMock())


def _stub_processing_manager(monkeypatch):
    """Replace ProcessingManager with a MagicMock factory; return the factory
    so the test can inspect what was instantiated and called."""
    factory = MagicMock()
    instance = MagicMock()
    factory.return_value = instance
    monkeypatch.setattr(ap, "ProcessingManager", factory)
    return factory, instance


# ===========================================================================
# cancel / check_cancelled lifecycle
# ===========================================================================

def test_cancel_sets_flag_and_check_cancelled_returns_true():
    proc = ap.AVSpexProcessor()
    assert proc.check_cancelled() is False
    proc.cancel()
    assert proc.check_cancelled() is True


def test_check_cancelled_emits_once_with_signals():
    """When cancelled and signals present, cancelled.emit() fires only once."""
    signals = MagicMock()
    proc = ap.AVSpexProcessor(signals=signals)
    proc.cancel()

    proc.check_cancelled()
    proc.check_cancelled()
    proc.check_cancelled()

    # Despite 3 calls, only one emit
    signals.cancelled.emit.assert_called_once()


def test_check_cancelled_no_signal_no_emit_no_crash():
    """Without signals attached, check_cancelled still returns the flag."""
    proc = ap.AVSpexProcessor()
    proc.cancel()
    assert proc.check_cancelled() is True


# ===========================================================================
# __init__ — refresh on init
# ===========================================================================

def test_init_calls_refresh_configs():
    """AVSpexProcessor refreshes ConfigManager so it picks up GUI changes."""
    with patch.object(ap.ConfigManager, "refresh_configs") as refresh_mock:
        ap.AVSpexProcessor()
    refresh_mock.assert_called()


def test_init_loads_checks_and_spex_configs():
    proc = ap.AVSpexProcessor()
    # These attributes are stored on init
    assert proc.checks_config is not None
    assert proc.spex_config is not None


# ===========================================================================
# process_directories
# ===========================================================================

def test_process_directories_empty_list_returns_formatted_time():
    proc = ap.AVSpexProcessor()
    result = proc.process_directories([])
    # log_overall_time always returns "HH:MM:SS"
    assert isinstance(result, str)
    assert result.count(":") == 2


def test_process_directories_emits_file_started_per_directory(monkeypatch):
    signals = MagicMock()
    proc = ap.AVSpexProcessor(signals=signals)
    # No-op out per-directory work
    monkeypatch.setattr(proc, "process_single_directory", lambda *_a, **_kw: True)

    proc.process_directories(["/a", "/b", "/c"])

    # file_started emitted for each directory with (path, idx, total)
    calls = signals.file_started.emit.call_args_list
    assert len(calls) == 3
    assert calls[0].args == ("/a", 1, 3)
    assert calls[1].args == ("/b", 2, 3)
    assert calls[2].args == ("/c", 3, 3)
    # And step_completed("All Processing") fires once at the end
    signals.step_completed.emit.assert_any_call("All Processing")


def test_process_directories_returns_false_when_cancelled_before_start():
    proc = ap.AVSpexProcessor()
    proc.cancel()
    assert proc.process_directories(["/a"]) is False


def test_process_directories_stops_on_mid_loop_cancel(monkeypatch):
    """If cancel happens between directories, the loop exits and returns False."""
    proc = ap.AVSpexProcessor()
    seen = []

    def fake_single(src):
        seen.append(src)
        if src == "/b":
            proc.cancel()
        return True

    monkeypatch.setattr(proc, "process_single_directory", fake_single)
    result = proc.process_directories(["/a", "/b", "/c"])
    assert result is False
    # /a + /b ran, /c never started
    assert seen == ["/a", "/b"]


# ===========================================================================
# process_single_directory — per-file logging lifecycle
# ===========================================================================

def test_process_single_directory_returns_false_when_init_fails(monkeypatch):
    """If initialize_directory returns None, function bails before any work."""
    monkeypatch.setattr(ap.dir_setup, "initialize_directory", lambda _src: None)
    signals = MagicMock()
    proc = ap.AVSpexProcessor(signals=signals)

    result = proc.process_single_directory("/bad/dir")
    assert result is False
    signals.error.emit.assert_called_once()


def test_process_single_directory_starts_and_stops_file_log_on_success(monkeypatch):
    _stub_directory_init(monkeypatch)
    start_mock = MagicMock(return_value="/tmp/file.log")
    stop_mock = MagicMock()
    monkeypatch.setattr(ap, "start_file_log", start_mock)
    monkeypatch.setattr(ap, "stop_file_log", stop_mock)

    proc = ap.AVSpexProcessor()
    # Stub the inner content function so it returns quickly
    monkeypatch.setattr(proc, "_process_directory_contents", lambda *a, **kw: True)

    result = proc.process_single_directory("/some/dir")

    assert result is True
    start_mock.assert_called_once_with("/tmp/dest", "vid_x")
    stop_mock.assert_called_once()


def test_process_single_directory_stops_file_log_on_exception(monkeypatch):
    """try/finally guarantees stop_file_log even if processing raises."""
    _stub_directory_init(monkeypatch)
    stop_mock = MagicMock()
    monkeypatch.setattr(ap, "start_file_log", lambda *a, **kw: "/tmp/file.log")
    monkeypatch.setattr(ap, "stop_file_log", stop_mock)

    proc = ap.AVSpexProcessor()

    def boom(*a, **kw):
        raise RuntimeError("processing exploded")

    monkeypatch.setattr(proc, "_process_directory_contents", boom)

    with pytest.raises(RuntimeError):
        proc.process_single_directory("/some/dir")

    stop_mock.assert_called_once()


def test_process_single_directory_emits_file_completed(monkeypatch):
    """file_completed carries (video_id, destination_directory) for the console PDF."""
    _stub_directory_init(monkeypatch)
    _stub_per_file_logging(monkeypatch)
    signals = MagicMock()

    proc = ap.AVSpexProcessor(signals=signals)
    monkeypatch.setattr(proc, "_process_directory_contents", lambda *a, **kw: True)

    proc.process_single_directory("/some/dir")

    signals.file_completed.emit.assert_called_once_with("vid_x", "/tmp/dest")


def test_process_single_directory_emits_file_completed_on_exception(monkeypatch):
    """The emit lives in the finally block, so a failed file still gets saved."""
    _stub_directory_init(monkeypatch)
    _stub_per_file_logging(monkeypatch)
    signals = MagicMock()

    proc = ap.AVSpexProcessor(signals=signals)

    def boom(*a, **kw):
        raise RuntimeError("processing exploded")

    monkeypatch.setattr(proc, "_process_directory_contents", boom)

    with pytest.raises(RuntimeError):
        proc.process_single_directory("/some/dir")

    signals.file_completed.emit.assert_called_once_with("vid_x", "/tmp/dest")


# ===========================================================================
# _process_directory_contents — branching
# ===========================================================================

def _run_contents(monkeypatch, checks_cfg, signals=None):
    """Helper that wires up stubs, builds a processor, and runs the inner method."""
    _stub_directory_init(monkeypatch)
    _stub_per_file_logging(monkeypatch)
    factory, instance = _stub_processing_manager(monkeypatch)
    # display_processing_banner just prints; let it run (it's harmless in tests)
    proc = _make_processor_with_config(checks_cfg, signals=signals)
    proc._process_directory_contents("/src", "/v/in.mkv", "vid_x", "/dest", None)
    return factory, instance


# ----- Fixity branch -----

def test_no_fixity_flags_skips_process_fixity(monkeypatch):
    cfg = _make_checks_config()  # all flags off
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_fixity.assert_not_called()


@pytest.mark.parametrize("flag", [
    "check_fixity", "validate_stream_fixity", "embed_stream_fixity", "output_fixity"
])
def test_any_fixity_flag_triggers_process_fixity(monkeypatch, flag):
    cfg = _make_checks_config(fixity_flags={flag: True})
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_fixity.assert_called_once_with("/src", "/v/in.mkv", "vid_x")


def test_fixity_emits_tool_started_and_completed(monkeypatch):
    cfg = _make_checks_config(fixity_flags={"check_fixity": True})
    signals = MagicMock()
    _run_contents(monkeypatch, cfg, signals=signals)

    # tool_started + tool_completed for fixity should both fire
    started = [c.args[0] for c in signals.tool_started.emit.call_args_list]
    completed = [c.args[0] for c in signals.tool_completed.emit.call_args_list]
    assert any("Fixity" in s for s in started)
    assert any("Fixity" in s for s in completed)


# ----- MediaConch branch -----

def test_mediaconch_disabled_skips_validate(monkeypatch):
    cfg = _make_checks_config(mediaconch=False)
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.validate_video_with_mediaconch.assert_not_called()


def test_mediaconch_enabled_runs_validate_and_emits_step_completed(monkeypatch):
    cfg = _make_checks_config(mediaconch=True)
    signals = MagicMock()
    _, mgr = _run_contents(monkeypatch, cfg, signals=signals)

    mgr.validate_video_with_mediaconch.assert_called_once_with("/v/in.mkv", "/dest", "vid_x")
    # step_completed("MediaConch Validation") emitted
    completed_steps = [c.args[0] for c in signals.step_completed.emit.call_args_list]
    assert "MediaConch Validation" in completed_steps


# ----- Metadata-tools branch -----

@pytest.mark.parametrize("tool", ["mediainfo", "mediatrace", "exiftool", "ffprobe", "mkvalidator"])
def test_any_metadata_tool_triggers_process_video_metadata(monkeypatch, tool):
    cfg = _make_checks_config(metadata_tools={tool: True})
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_video_metadata.assert_called_once_with("/v/in.mkv", "/dest", "vid_x")


def test_no_metadata_tools_skips_process_video_metadata(monkeypatch):
    cfg = _make_checks_config()
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_video_metadata.assert_not_called()


def test_metadata_tools_emit_step_completed_for_each_enabled_tool(monkeypatch):
    cfg = _make_checks_config(metadata_tools={"mediainfo": True, "ffprobe": True})
    signals = MagicMock()
    _run_contents(monkeypatch, cfg, signals=signals)

    completed = [c.args[0] for c in signals.step_completed.emit.call_args_list]
    # Both Mediainfo and FFprobe should be marked done; Exiftool/Mediatrace should not
    assert "Mediainfo" in completed
    assert "FFprobe" in completed
    assert "Exiftool" not in completed
    assert "Mediatrace" not in completed
    assert "mkvalidator" not in completed


def test_mkvalidator_emits_step_completed_when_enabled(monkeypatch):
    cfg = _make_checks_config(metadata_tools={"mkvalidator": True})
    signals = MagicMock()
    _run_contents(monkeypatch, cfg, signals=signals)

    completed = [c.args[0] for c in signals.step_completed.emit.call_args_list]
    assert "mkvalidator" in completed


# ----- Outputs branch (each trigger) -----

def test_no_outputs_flags_skips_process_video_outputs(monkeypatch):
    cfg = _make_checks_config()
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_video_outputs.assert_not_called()


@pytest.mark.parametrize("kwargs", [
    {"access_file": True},
    {"report": True},
    {"qctools": True},
    {"qct_parse": True},
    {"clams_detection": True},
    {"frame_flags": {"bitplane": True}},
    {"frame_flags": {"border": True}},
    {"frame_flags": {"brng": True}},
    {"frame_flags": {"signalstats": True}},
    {"frame_flags": {"dropped_sample": True}},
    {"frame_flags": {"duplicate_frame": True}},
])
def test_any_output_trigger_runs_process_video_outputs(monkeypatch, kwargs):
    """Any of the 11 enable-flags should be enough to invoke output processing."""
    cfg = _make_checks_config(**kwargs)
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_video_outputs.assert_called_once()


def test_output_processing_passes_metadata_differences(monkeypatch):
    """metadata_differences from process_video_metadata should be forwarded to outputs."""
    cfg = _make_checks_config(metadata_tools={"mediainfo": True}, report=True)
    _, mgr = _run_contents(monkeypatch, cfg)
    mgr.process_video_metadata.return_value = {"some": "diff"}

    # Re-run after adjusting return value (our helper returned the mgr, but the
    # _process_directory_contents already executed). Run a fresh contents call:
    proc = _make_processor_with_config(cfg)
    factory, mgr2 = _stub_processing_manager(monkeypatch)
    mgr2.process_video_metadata.return_value = {"some": "diff"}
    proc._process_directory_contents("/src", "/v/in.mkv", "vid_x", "/dest", None)

    args = mgr2.process_video_outputs.call_args.args
    # last positional arg is metadata_differences
    assert args[-1] == {"some": "diff"}


# ----- Cancellation mid-pipeline -----

def test_cancelled_mid_pipeline_skips_subsequent_steps(monkeypatch):
    """If cancellation occurs after fixity but before mediaconch, mediaconch
    must not run."""
    _stub_directory_init(monkeypatch)
    _stub_per_file_logging(monkeypatch)
    factory, instance = _stub_processing_manager(monkeypatch)

    cfg = _make_checks_config(
        fixity_flags={"check_fixity": True},
        mediaconch=True,
        metadata_tools={"mediainfo": True},
    )
    proc = _make_processor_with_config(cfg)

    # Cancel after process_fixity is called
    def cancel_after_fixity(*a, **kw):
        proc.cancel()

    instance.process_fixity.side_effect = cancel_after_fixity

    proc._process_directory_contents("/src", "/v/in.mkv", "vid_x", "/dest", None)

    # fixity ran, but mediaconch + metadata didn't
    instance.process_fixity.assert_called_once()
    instance.validate_video_with_mediaconch.assert_not_called()
    instance.process_video_metadata.assert_not_called()
