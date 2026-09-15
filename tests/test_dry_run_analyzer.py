"""Tests for AV_Spex.processing.dry_run_analyzer.DryRunAnalyzer.

The dry run analyzer reports what *would* happen during a real run without
executing tools. Its value depends on staying faithful to the real pipeline's
gating logic, so these tests pin the logic-heavy, drift-prone parts:

- _analyze_step: the status-resolution engine (override / disabled /
  precondition / will-run)
- _analyze_access_file: the access-file pre-processing modifier gating, including
  the silent-drop preconditions the dry run exists to surface
- _analyze_fixity_steps: MKV gating + the embed-skips-validate rule
- _analyze_metadata_tools: mediatrace MKV gating + check-needs-output rule
- _analyze_qctools_steps / _analyze_clams_detection: report dependency + summary
- _initialize_for_analysis: video discovery by configured extension

The analyzer is instantiated via __new__ to bypass __init__ (which loads real
configs); config is supplied as SimpleNamespace stand-ins so we don't have to
build every nested dataclass.
"""

from types import SimpleNamespace

import pytest

from AV_Spex.processing.dry_run_analyzer import (
    DryRunAnalyzer,
    StepStatus,
)


# ---------------------------------------------------------------------------
# Config builders (SimpleNamespace stand-ins for the dataclasses)
# ---------------------------------------------------------------------------

def _frame_analysis(**over):
    base = dict(
        enable_bitplane_check=False,
        enable_border_detection=False,
        enable_brng_analysis=False,
        enable_signalstats=False,
        border_detection_mode="simple",
        simple_border_pixels=25,
        auto_retry_borders=False,
        max_border_retries=3,
        brng_skip_color_bars=True,
        analysis_period_duration=60,
        analysis_period_count=3,
        enable_dropped_sample_detection=False,
        enable_duplicate_frame_detection=False,
        duplicate_min_run_length=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _outputs(**over):
    fa = over.pop("frame_analysis", None) or _frame_analysis()
    base = dict(
        access_file=True,
        report=False,
        qctools_ext="qctools.xml.gz",
        access_file_trim_color_bars=False,
        access_file_crop_borders=False,
        access_file_crop_to_480=False,
        access_file_exclude_flagged_audio=False,
    )
    base.update(over)
    return SimpleNamespace(frame_analysis=fa, **base)


def _basic_tool(run=False, check=False):
    return SimpleNamespace(run_tool=run, check_tool=check)


def _tools(**over):
    base = dict(
        mediainfo=_basic_tool(),
        mediatrace=_basic_tool(),
        exiftool=_basic_tool(),
        ffprobe=_basic_tool(),
        mediaconch=SimpleNamespace(run_mediaconch=False, mediaconch_policy=""),
        qctools=SimpleNamespace(run_tool=False),
        qct_parse=SimpleNamespace(run_tool=False, audio_analysis=False),
        clams_detection=SimpleNamespace(run_tool=False, bars=None, tone=None),
    )
    base.update(over)
    return SimpleNamespace(**base)


def _fixity(**over):
    base = dict(
        check_fixity=False,
        validate_stream_fixity=False,
        embed_stream_fixity=False,
        output_fixity=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _checks_config(**over):
    base = dict(
        validate_filename=False,
        video_file_extension="mkv",
        fixity=_fixity(),
        tools=_tools(),
        outputs=_outputs(),
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_analyzer(checks_config=None):
    a = DryRunAnalyzer.__new__(DryRunAnalyzer)
    a.checks_config = checks_config or _checks_config()
    a.spex_config = None
    a.signals = None
    a._cancelled = False
    return a


def by_name(analyses, name):
    return next(a for a in analyses if a.step_name == name)


# ---------------------------------------------------------------------------
# _analyze_step (status-resolution engine)
# ---------------------------------------------------------------------------

def test_analyze_step_disabled():
    a = make_analyzer()
    step = a._analyze_step("S", enabled=False, precondition_met=True)
    assert step.status == StepStatus.SKIPPED_DISABLED
    assert step.reason == "Disabled in configuration"


def test_analyze_step_precondition_not_met():
    a = make_analyzer()
    step = a._analyze_step("S", enabled=True, precondition_met=False,
                           precondition_reason="needs MKV")
    assert step.status == StepStatus.SKIPPED_PRECONDITION
    assert step.reason == "needs MKV"


def test_analyze_step_will_run():
    a = make_analyzer()
    step = a._analyze_step("S", enabled=True, precondition_met=True)
    assert step.status == StepStatus.WILL_RUN
    assert step.reason == "Ready to run"


def test_analyze_step_override_wins_over_enabled_and_met():
    a = make_analyzer()
    step = a._analyze_step("S", enabled=True, precondition_met=True,
                           status_override=StepStatus.SKIPPED_ALREADY_EXISTS,
                           precondition_reason="exists")
    assert step.status == StepStatus.SKIPPED_ALREADY_EXISTS
    assert step.reason == "exists"


def test_analyze_step_disabled_takes_precedence_over_failed_precondition():
    # When both disabled and precondition unmet, "disabled" is reported.
    a = make_analyzer()
    step = a._analyze_step("S", enabled=False, precondition_met=False)
    assert step.status == StepStatus.SKIPPED_DISABLED


# ---------------------------------------------------------------------------
# _analyze_access_file — already-exists short circuit
# ---------------------------------------------------------------------------

def test_access_file_already_exists():
    a = make_analyzer()
    step = a._analyze_access_file(access_file_found="proxy.mp4")[0]
    assert step.status == StepStatus.SKIPPED_ALREADY_EXISTS
    assert step.precondition_met is False
    assert "already exists" in step.reason.lower()


# ---------------------------------------------------------------------------
# _analyze_access_file — no modifiers
# ---------------------------------------------------------------------------

def test_access_file_no_modifiers():
    a = make_analyzer(_checks_config(outputs=_outputs()))
    step = a._analyze_access_file(access_file_found=None)[0]
    assert step.status == StepStatus.WILL_RUN
    assert step.reason == "No pre-processing modifiers enabled"
    assert step.warnings is None


def test_access_file_disabled_in_config():
    a = make_analyzer(_checks_config(outputs=_outputs(access_file=False)))
    step = a._analyze_access_file(access_file_found=None)[0]
    assert step.status == StepStatus.SKIPPED_DISABLED


# ---------------------------------------------------------------------------
# _analyze_access_file — exclude flagged audio (the recently-added modifier)
# ---------------------------------------------------------------------------

def test_access_exclude_audio_active_when_audio_analysis_on():
    cfg = _checks_config(
        outputs=_outputs(access_file_exclude_flagged_audio=True),
        tools=_tools(qct_parse=SimpleNamespace(run_tool=True, audio_analysis=True)),
    )
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.status == StepStatus.WILL_RUN
    assert "exclude flagged audio channel" in step.reason
    assert step.warnings is None


def test_access_exclude_audio_blocked_when_qct_parse_off():
    cfg = _checks_config(
        outputs=_outputs(access_file_exclude_flagged_audio=True),
        tools=_tools(qct_parse=SimpleNamespace(run_tool=False, audio_analysis=False)),
    )
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.warnings is not None
    assert any("exclude flagged audio channel" in w for w in step.warnings)
    assert any("silently included" in w for w in step.warnings)


def test_access_exclude_audio_blocked_when_audio_analysis_off():
    # qct-parse runs, but audio analysis specifically is off -> still blocked.
    cfg = _checks_config(
        outputs=_outputs(access_file_exclude_flagged_audio=True),
        tools=_tools(qct_parse=SimpleNamespace(run_tool=True, audio_analysis=False)),
    )
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.warnings is not None
    assert any("exclude flagged audio channel" in w for w in step.warnings)


# ---------------------------------------------------------------------------
# _analyze_access_file — trim color bars
# ---------------------------------------------------------------------------

def test_access_trim_color_bars_active_with_qct_parse():
    cfg = _checks_config(
        outputs=_outputs(access_file_trim_color_bars=True),
        tools=_tools(qct_parse=SimpleNamespace(run_tool=True, audio_analysis=False)),
    )
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert "trim color bars" in step.reason
    assert step.warnings is None


def test_access_trim_color_bars_blocked_without_qct_parse():
    cfg = _checks_config(outputs=_outputs(access_file_trim_color_bars=True))
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.warnings is not None
    assert any("trim color bars" in w for w in step.warnings)


# ---------------------------------------------------------------------------
# _analyze_access_file — crop borders (needs sophisticated border detection)
# ---------------------------------------------------------------------------

def test_access_crop_borders_active_with_sophisticated_detection():
    cfg = _checks_config(outputs=_outputs(
        access_file_crop_borders=True,
        frame_analysis=_frame_analysis(
            enable_border_detection=True, border_detection_mode="sophisticated"
        ),
    ))
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert "crop borders" in step.reason
    assert step.warnings is None


def test_access_crop_borders_blocked_when_border_detection_off():
    cfg = _checks_config(outputs=_outputs(
        access_file_crop_borders=True,
        frame_analysis=_frame_analysis(enable_border_detection=False),
    ))
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.warnings is not None
    assert any("border detection is disabled" in w for w in step.warnings)


def test_access_crop_borders_blocked_in_simple_mode():
    cfg = _checks_config(outputs=_outputs(
        access_file_crop_borders=True,
        frame_analysis=_frame_analysis(
            enable_border_detection=True, border_detection_mode="simple"
        ),
    ))
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.warnings is not None
    assert any("sophisticated" in w for w in step.warnings)


# ---------------------------------------------------------------------------
# _analyze_access_file — crop to 480 (no upstream dependency) + combos
# ---------------------------------------------------------------------------

def test_access_crop_to_480_unconditionally_active():
    cfg = _checks_config(outputs=_outputs(access_file_crop_to_480=True))
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert "crop to 480 lines" in step.reason
    assert step.warnings is None


def test_access_multiple_active_modifiers_listed_together():
    cfg = _checks_config(
        outputs=_outputs(
            access_file_trim_color_bars=True,
            access_file_crop_to_480=True,
        ),
        tools=_tools(qct_parse=SimpleNamespace(run_tool=True, audio_analysis=False)),
    )
    step = make_analyzer(cfg)._analyze_access_file(access_file_found=None)[0]
    assert step.reason.startswith("With: ")
    assert "trim color bars" in step.reason
    assert "crop to 480 lines" in step.reason


# ---------------------------------------------------------------------------
# _analyze_fixity_steps
# ---------------------------------------------------------------------------

def test_fixity_embed_requires_mkv(tmp_path):
    cfg = _checks_config(fixity=_fixity(embed_stream_fixity=True))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mov", "in")
    embed = by_name(analyses, "Embed Stream Fixity")
    assert embed.status == StepStatus.SKIPPED_PRECONDITION
    assert "MKV" in embed.reason


def test_fixity_embed_runs_on_mkv(tmp_path):
    cfg = _checks_config(fixity=_fixity(embed_stream_fixity=True))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mkv", "in")
    embed = by_name(analyses, "Embed Stream Fixity")
    assert embed.status == StepStatus.WILL_RUN


def test_fixity_validate_skipped_when_embed_enabled(tmp_path):
    # Real pipeline: validate-stream is skipped when embed-stream is enabled.
    cfg = _checks_config(fixity=_fixity(
        embed_stream_fixity=True, validate_stream_fixity=True
    ))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mkv", "in")
    validate = by_name(analyses, "Validate Stream Fixity")
    assert validate.precondition_met is False
    assert "Skipped when Embed Stream Fixity is enabled" in validate.reason


def test_fixity_validate_runs_when_embed_disabled_on_mkv(tmp_path):
    cfg = _checks_config(fixity=_fixity(
        embed_stream_fixity=False, validate_stream_fixity=True
    ))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mkv", "in")
    validate = by_name(analyses, "Validate Stream Fixity")
    assert validate.status == StepStatus.WILL_RUN


def test_fixity_check_precondition_finds_sidecar(tmp_path):
    (tmp_path / "in_checksums.md5").write_text("abc123  in.mkv\n")
    cfg = _checks_config(fixity=_fixity(check_fixity=True))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mkv", "in")
    check = by_name(analyses, "Validate Fixity (against stored checksum)")
    assert check.status == StepStatus.WILL_RUN
    assert "in_checksums.md5" in check.reason


def test_fixity_check_precondition_missing_sidecar(tmp_path):
    cfg = _checks_config(fixity=_fixity(check_fixity=True))
    a = make_analyzer(cfg)
    analyses = a._analyze_fixity_steps(str(tmp_path), "/v/in.mkv", "in")
    check = by_name(analyses, "Validate Fixity (against stored checksum)")
    assert check.status == StepStatus.SKIPPED_PRECONDITION
    assert "No checksum file found" in check.reason


# ---------------------------------------------------------------------------
# _analyze_metadata_tools
# ---------------------------------------------------------------------------

def test_metadata_mediatrace_requires_mkv(tmp_path):
    cfg = _checks_config(tools=_tools(mediatrace=_basic_tool(run=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_metadata_tools("/v/in.mov", str(tmp_path), "in")
    run_step = by_name(analyses, "MediaTrace (run)")
    assert run_step.status == StepStatus.SKIPPED_PRECONDITION
    assert "MKV" in run_step.reason


def test_metadata_mediatrace_runs_on_mkv(tmp_path):
    cfg = _checks_config(tools=_tools(mediatrace=_basic_tool(run=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_metadata_tools("/v/in.mkv", str(tmp_path), "in")
    run_step = by_name(analyses, "MediaTrace (run)")
    assert run_step.status == StepStatus.WILL_RUN


def test_metadata_check_blocked_without_run_or_existing_output(tmp_path):
    # check_tool on, run_tool off, no existing output -> cannot check.
    cfg = _checks_config(tools=_tools(mediainfo=_basic_tool(run=False, check=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_metadata_tools("/v/in.mkv", str(tmp_path), "in")
    check = by_name(analyses, "MediaInfo (check against expected values)")
    assert check.status == StepStatus.SKIPPED_PRECONDITION
    assert "No existing" in check.reason


def test_metadata_check_ok_when_run_enabled(tmp_path):
    cfg = _checks_config(tools=_tools(mediainfo=_basic_tool(run=True, check=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_metadata_tools("/v/in.mkv", str(tmp_path), "in")
    check = by_name(analyses, "MediaInfo (check against expected values)")
    assert check.status == StepStatus.WILL_RUN


def test_metadata_check_ok_with_existing_output(tmp_path):
    (tmp_path / "in_mediainfo_output.json").write_text("{}")
    cfg = _checks_config(tools=_tools(mediainfo=_basic_tool(run=False, check=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_metadata_tools("/v/in.mkv", str(tmp_path), "in")
    check = by_name(analyses, "MediaInfo (check against expected values)")
    assert check.status == StepStatus.WILL_RUN
    assert "existing output" in check.reason


# ---------------------------------------------------------------------------
# _analyze_qctools_steps
# ---------------------------------------------------------------------------

def test_qctparse_blocked_without_report(tmp_path):
    # qct-parse enabled but QCTools neither exists nor will run.
    cfg = _checks_config(tools=_tools(
        qctools=SimpleNamespace(run_tool=False),
        qct_parse=SimpleNamespace(run_tool=True, audio_analysis=False),
    ))
    a = make_analyzer(cfg)
    analyses = a._analyze_qctools_steps(str(tmp_path), str(tmp_path), "in")
    qctp = by_name(analyses, "QCT Parse (analyze QCTools report)")
    assert qctp.status == StepStatus.SKIPPED_PRECONDITION


def test_qctparse_ok_when_qctools_will_run(tmp_path):
    cfg = _checks_config(tools=_tools(
        qctools=SimpleNamespace(run_tool=True),
        qct_parse=SimpleNamespace(run_tool=True, audio_analysis=False),
    ))
    a = make_analyzer(cfg)
    analyses = a._analyze_qctools_steps(str(tmp_path), str(tmp_path), "in")
    qctp = by_name(analyses, "QCT Parse (analyze QCTools report)")
    assert qctp.status == StepStatus.WILL_RUN


def test_qctools_existing_report_marks_already_exists(tmp_path):
    meta = tmp_path / "in_qc_metadata"
    meta.mkdir()
    (meta / "in.qctools.xml.gz").write_text("x")
    cfg = _checks_config(tools=_tools(qctools=SimpleNamespace(run_tool=True)))
    a = make_analyzer(cfg)
    analyses = a._analyze_qctools_steps(str(tmp_path), str(meta), "in")
    qct = by_name(analyses, "QCTools (generate report)")
    assert qct.status == StepStatus.SKIPPED_ALREADY_EXISTS
    assert "Existing report found" in qct.reason


# ---------------------------------------------------------------------------
# _analyze_clams_detection
# ---------------------------------------------------------------------------

def test_clams_disabled():
    a = make_analyzer()
    step = a._analyze_clams_detection()[0]
    assert step.status == StepStatus.SKIPPED_DISABLED


def test_clams_enabled_includes_param_summary():
    bars = SimpleNamespace(threshold=0.7, sample_ratio=30, min_frame_count=10)
    tone = SimpleNamespace(tolerance=1.0, min_tone_duration_ms=2000)
    cfg = _checks_config(tools=_tools(
        clams_detection=SimpleNamespace(run_tool=True, bars=bars, tone=tone)
    ))
    step = make_analyzer(cfg)._analyze_clams_detection()[0]
    assert step.status == StepStatus.WILL_RUN
    assert "threshold=0.7" in step.reason
    assert "tolerance=1.0" in step.reason


def test_clams_missing_config_returns_empty():
    cfg = _checks_config(tools=_tools(clams_detection=None))
    # getattr returns None -> method returns []
    cfg.tools.clams_detection = None
    assert make_analyzer(cfg)._analyze_clams_detection() == []


# ---------------------------------------------------------------------------
# _initialize_for_analysis
# ---------------------------------------------------------------------------

def test_initialize_finds_single_video(tmp_path):
    (tmp_path / "JPC_AV_00001.mkv").write_text("x")
    a = make_analyzer()
    result = a._initialize_for_analysis(str(tmp_path))
    assert result is not None
    video_path, video_id, dest, access = result
    assert video_id == "JPC_AV_00001"
    assert dest.endswith("JPC_AV_00001_qc_metadata")
    assert access is None


def test_initialize_ignores_qctools_sidecar(tmp_path):
    (tmp_path / "JPC_AV_00001.mkv").write_text("x")
    (tmp_path / "JPC_AV_00001.qctools.mkv").write_text("x")
    a = make_analyzer()
    result = a._initialize_for_analysis(str(tmp_path))
    assert result is not None
    assert result[1] == "JPC_AV_00001"


def test_initialize_no_video_returns_none(tmp_path):
    a = make_analyzer()
    assert a._initialize_for_analysis(str(tmp_path)) is None


def test_initialize_multiple_videos_returns_none(tmp_path):
    (tmp_path / "a.mkv").write_text("x")
    (tmp_path / "b.mkv").write_text("x")
    a = make_analyzer()
    assert a._initialize_for_analysis(str(tmp_path)) is None


def test_initialize_respects_configured_extension(tmp_path):
    (tmp_path / "clip.mov").write_text("x")
    cfg = _checks_config(video_file_extension="mov")
    a = make_analyzer(cfg)
    result = a._initialize_for_analysis(str(tmp_path))
    assert result is not None
    assert result[1] == "clip"


def test_initialize_detects_existing_access_file(tmp_path):
    (tmp_path / "clip.mkv").write_text("x")
    (tmp_path / "clip.mp4").write_text("x")
    a = make_analyzer()
    result = a._initialize_for_analysis(str(tmp_path))
    assert result is not None
    assert result[3] == "clip.mp4"
