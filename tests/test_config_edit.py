"""Tests for AV_Spex.utils.config_edit.

Covers:
- format_config_value: pure formatting (bool/str/list/dict/nested)
- validate_config_spec: pure input validation
- update_tool_setting / toggle_on / toggle_off: CLI --on/--off routing
- apply_profile: predefined profile application
- resolve_config: trivial mapping lookup
- apply_signalflow_profile: dict + dataclass paths
- apply_exiftool_profile, apply_mediainfo_profile, apply_ffprobe_profile: replace-section shape
- Custom-profile CRUD (save / delete / get / apply_custom_profile)
- ExifTool / MediaInfo / FFprobe profile CRUD
"""

from dataclasses import dataclass, field, asdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.utils import config_edit


# ---------------------------------------------------------------------------
# format_config_value
# ---------------------------------------------------------------------------

def test_format_config_value_bool_true():
    assert config_edit.format_config_value(True) == "✅"


def test_format_config_value_bool_false():
    assert config_edit.format_config_value(False) == "❌"


def test_format_config_value_legacy_yes_no():
    assert config_edit.format_config_value("yes") == "✅"
    assert config_edit.format_config_value("no") == "❌"


def test_format_config_value_list():
    assert config_edit.format_config_value(["a", "b", "c"]) == "a, b, c"


def test_format_config_value_empty_list():
    assert config_edit.format_config_value([]) == ""


def test_format_config_value_plain_string():
    assert config_edit.format_config_value("hello") == "hello"


def test_format_config_value_int():
    assert config_edit.format_config_value(42) == "42"


def test_format_config_value_dict_contains_keys():
    result = config_edit.format_config_value({"foo": True, "bar": False}, indent=2)
    # Format includes key names and the bool glyphs.
    assert "foo" in result
    assert "bar" in result
    assert "✅" in result
    assert "❌" in result


def test_format_config_value_nested_dict_newline_before_nested():
    """A dict value inside a dict should be preceded by a newline (is_nested=True)."""
    result = config_edit.format_config_value(
        {"outer": {"inner": True}}, indent=0
    )
    assert "\n" in result
    assert "inner" in result
    assert "outer" in result


# ---------------------------------------------------------------------------
# validate_config_spec
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["all", "spex", "checks", "exiftool",
                                   "mediainfo", "ffprobe", "signalflow"])
def test_validate_config_spec_base_types_ok(spec):
    assert config_edit.validate_config_spec(spec) is True


def test_validate_config_spec_empty_string_invalid():
    assert config_edit.validate_config_spec("") is False


def test_validate_config_spec_unknown_type_invalid():
    assert config_edit.validate_config_spec("garbage") is False


def test_validate_config_spec_valid_checks_subsection():
    assert config_edit.validate_config_spec("checks,tools") is True


def test_validate_config_spec_invalid_checks_subsection():
    assert config_edit.validate_config_spec("checks,bogus") is False


def test_validate_config_spec_valid_spex_subsection():
    assert config_edit.validate_config_spec("spex,filename_values") is True


def test_validate_config_spec_invalid_spex_subsection():
    assert config_edit.validate_config_spec("spex,not_a_section") is False


def test_validate_config_spec_profile_configs_reject_subsections():
    """exiftool/mediainfo/ffprobe don't have named subsections."""
    assert config_edit.validate_config_spec("exiftool,anything") is False
    assert config_edit.validate_config_spec("mediainfo,anything") is False
    assert config_edit.validate_config_spec("ffprobe,anything") is False


def test_validate_config_spec_all_ignores_subsection_validity():
    """`all,<anything>` is considered valid — subsection validation is skipped."""
    assert config_edit.validate_config_spec("all,garbage") is True


# ---------------------------------------------------------------------------
# resolve_config
# ---------------------------------------------------------------------------

def test_resolve_config_known_key():
    mapping = {"step1": {"x": 1}, "step2": {"x": 2}}
    assert config_edit.resolve_config("step1", mapping) == {"x": 1}


def test_resolve_config_unknown_key_returns_none():
    assert config_edit.resolve_config("nope", {"a": 1}) is None


# ---------------------------------------------------------------------------
# update_tool_setting / toggle_on / toggle_off
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cfg(monkeypatch):
    """Replace the module-level config_mgr singleton with a MagicMock.

    Defaults the loaded config's video_file_extension to 'mkv' so the
    non-MKV guardrail in apply_profile is a no-op; tests that exercise the
    guardrail set it to a non-MKV value explicitly.
    """
    mock = MagicMock()
    mock.get_config.return_value.video_file_extension = "mkv"
    monkeypatch.setattr(config_edit, "config_mgr", mock)
    return mock


def _last_updates(mock_cfg):
    """Return the `updates` dict from the last update_config call."""
    assert mock_cfg.update_config.called, "update_config was never called"
    args = mock_cfg.update_config.call_args.args
    return args[1]  # (config_name, updates_dict)


def test_toggle_on_standard_tool(mock_cfg):
    config_edit.toggle_on(["exiftool.run_tool"])
    updates = _last_updates(mock_cfg)
    assert updates == {"tools": {"exiftool": {"run_tool": True}}}


def test_toggle_off_standard_tool(mock_cfg):
    config_edit.toggle_off(["mediainfo.check_tool"])
    updates = _last_updates(mock_cfg)
    assert updates == {"tools": {"mediainfo": {"check_tool": False}}}


def test_update_tool_setting_fixity_routes_to_fixity(mock_cfg):
    config_edit.update_tool_setting(["fixity.check_fixity"], True)
    updates = _last_updates(mock_cfg)
    assert "fixity" in updates
    assert updates["fixity"] == {"check_fixity": True}
    assert "tools" not in updates


def test_update_tool_setting_fixity_invalid_field_warns(mock_cfg):
    config_edit.update_tool_setting(["fixity.unknown"], True)
    # No valid fields → no update.
    assert mock_cfg.update_config.called is False


def test_update_tool_setting_mediaconch_run_mediaconch(mock_cfg):
    config_edit.update_tool_setting(["mediaconch.run_mediaconch"], True)
    updates = _last_updates(mock_cfg)
    assert updates == {"tools": {"mediaconch": {"run_mediaconch": True}}}


def test_update_tool_setting_mediaconch_rejects_other_fields(mock_cfg):
    config_edit.update_tool_setting(["mediaconch.check_tool"], True)
    assert mock_cfg.update_config.called is False


def test_update_tool_setting_qctools_only_accepts_run_tool(mock_cfg):
    config_edit.update_tool_setting(["qctools.run_tool"], False)
    updates = _last_updates(mock_cfg)
    assert updates == {"tools": {"qctools": {"run_tool": False}}}


def test_update_tool_setting_qctools_rejects_check_tool(mock_cfg):
    config_edit.update_tool_setting(["qctools.check_tool"], True)
    assert mock_cfg.update_config.called is False


@pytest.mark.parametrize("field", [
    "run_tool", "barsDetection", "evaluateBars", "thumbExport",
    "audio_analysis", "detect_clamped_levels",
])
def test_update_tool_setting_qct_parse_valid_fields(mock_cfg, field):
    config_edit.update_tool_setting([f"qct_parse.{field}"], True)
    updates = _last_updates(mock_cfg)
    assert updates == {"tools": {"qct_parse": {field: True}}}


def test_update_tool_setting_qct_parse_rejects_unknown_field(mock_cfg):
    config_edit.update_tool_setting(["qct_parse.not_a_field"], True)
    assert mock_cfg.update_config.called is False


def test_update_tool_setting_standard_tool_rejects_unknown_field(mock_cfg):
    config_edit.update_tool_setting(["exiftool.garbage"], True)
    assert mock_cfg.update_config.called is False


def test_update_tool_setting_invalid_format_skipped(mock_cfg):
    """A spec missing a dot should be skipped silently (warning only)."""
    config_edit.update_tool_setting(["not_a_valid_spec"], True)
    assert mock_cfg.update_config.called is False


def test_update_tool_setting_multiple_specs_combined(mock_cfg):
    config_edit.update_tool_setting([
        "exiftool.check_tool",
        "ffprobe.run_tool",
        "fixity.output_fixity",
    ], True)
    updates = _last_updates(mock_cfg)
    assert updates["tools"]["exiftool"] == {"check_tool": True}
    assert updates["tools"]["ffprobe"] == {"run_tool": True}
    assert updates["fixity"] == {"output_fixity": True}


def test_update_tool_setting_no_valid_specs_does_not_call_update(mock_cfg):
    config_edit.update_tool_setting([
        "garbage",
        "mediaconch.check_tool",  # invalid for mediaconch
        "qctools.check_tool",     # invalid for qctools
    ], True)
    assert mock_cfg.update_config.called is False


# ---------------------------------------------------------------------------
# apply_profile
# ---------------------------------------------------------------------------

def test_apply_profile_applies_all_sections(mock_cfg):
    profile = {
        "validate_filename": True,
        "outputs": {"report": True},
        "fixity": {"check_fixity": True},
        "tools": {"exiftool": {"run_tool": True}},
    }
    config_edit.apply_profile(profile)
    updates = _last_updates(mock_cfg)
    assert updates["validate_filename"] is True
    assert updates["outputs"] == {"report": True}
    assert updates["fixity"] == {"check_fixity": True}
    assert updates["tools"] == {"exiftool": {"run_tool": True}}


def test_apply_profile_partial_only_includes_provided_sections(mock_cfg):
    """If a section isn't in the profile, it shouldn't appear in updates."""
    config_edit.apply_profile({"validate_filename": False})
    updates = _last_updates(mock_cfg)
    assert updates == {"validate_filename": False}


def test_apply_profile_empty_profile_no_update(mock_cfg):
    config_edit.apply_profile({})
    assert mock_cfg.update_config.called is False


def test_apply_profile_builtin_step1(mock_cfg):
    """The built-in step1 profile should apply all four sections."""
    config_edit.apply_profile(config_edit.profile_step1)
    updates = _last_updates(mock_cfg)
    assert set(updates.keys()) == {"validate_filename", "outputs", "fixity", "tools"}
    # Spot-check a key value.
    assert updates["fixity"]["embed_stream_fixity"] is True
    assert updates["tools"]["qctools"]["run_tool"] is False


def test_apply_profile_builtin_step2(mock_cfg):
    """The built-in step2 profile should apply all four sections."""
    config_edit.apply_profile(config_edit.profile_step2)
    updates = _last_updates(mock_cfg)
    assert set(updates.keys()) == {"validate_filename", "outputs", "fixity", "tools"}
    # qctools/qct-parse drive the step2 analysis pass.
    assert updates["tools"]["qctools"]["run_tool"] is True
    assert updates["tools"]["qct_parse"]["run_tool"] is True
    # CLAMS detection and the bit-plane check are part of step2 (regression
    # guard — these were added to the profile and are easy to silently drop).
    assert updates["tools"]["clams_detection"]["run_tool"] is True
    assert updates["outputs"]["frame_analysis"]["enable_bitplane_check"] is True
    # The previously-present frame analysis sub-steps remain on.
    assert updates["outputs"]["frame_analysis"]["enable_border_detection"] is True
    assert updates["outputs"]["frame_analysis"]["enable_brng_analysis"] is True
    assert updates["outputs"]["frame_analysis"]["enable_signalstats"] is True


def test_apply_profile_non_mkv_forces_mkv_only_checks_off(mock_cfg):
    """On a non-MKV config, applying a profile that enables MKV-only checks
    (step1) re-applies the extension guardrail, forcing them off."""
    mock_cfg.get_config.return_value.video_file_extension = "mov"
    config_edit.apply_profile(config_edit.profile_step1)
    # The last update_config call is the guardrail's correction.
    updates = _last_updates(mock_cfg)
    assert updates["fixity"]["embed_stream_fixity"] is False
    assert updates["fixity"]["validate_stream_fixity"] is False
    assert updates["fixity"]["overwrite_stream_fixity"] is False
    assert updates["tools"]["mediatrace"] == {"run_tool": False, "check_tool": False}
    assert updates["tools"]["mkvalidator"] == {"run_tool": False, "check_tool": False}


def test_enforce_extension_compatibility_forces_mkvalidator_off_alone():
    """mkvalidator validates Matroska conformance, so it is forced off on its own
    even when every other MKV-only setting is already off."""
    from types import SimpleNamespace
    off = SimpleNamespace(run_tool=False, check_tool=False)
    cfg = SimpleNamespace(
        video_file_extension="mp4",
        fixity=SimpleNamespace(embed_stream_fixity=False, validate_stream_fixity=False,
                               overwrite_stream_fixity=False),
        tools=SimpleNamespace(mediatrace=off,
                              mkvalidator=SimpleNamespace(run_tool=True, check_tool=False)),
    )
    mock = MagicMock()
    mock.get_config.return_value = cfg
    with patch.object(config_edit, "config_mgr", mock):
        assert config_edit.enforce_extension_compatibility() is True
    updates = mock.update_config.call_args.args[1]
    assert updates == {"tools": {"mkvalidator": {"run_tool": False, "check_tool": False}}}


def test_enforce_extension_compatibility_noop_for_mkv(mock_cfg):
    mock_cfg.get_config.return_value.video_file_extension = "mkv"
    assert config_edit.enforce_extension_compatibility() is False
    assert mock_cfg.update_config.called is False


# ---------------------------------------------------------------------------
# apply_signalflow_profile
# ---------------------------------------------------------------------------

def test_apply_signalflow_profile_from_dict(mock_cfg):
    """A hardcoded-style dict should route Source_VTR/TBC_Framesync/etc. into
    mediatrace ENCODER_SETTINGS."""
    # Provide a spex_config stub with mediatrace_values.ENCODER_SETTINGS and
    # no ffmpeg format tags (keeps the ffmpeg branch inactive).
    spex_stub = SimpleNamespace(
        mediatrace_values=SimpleNamespace(
            ENCODER_SETTINGS=SimpleNamespace()  # no existing attrs
        ),
        ffmpeg_values={},  # no 'format' key → ffmpeg branch skipped
    )
    mock_cfg.get_config.return_value = spex_stub

    profile = {
        "name": "my-profile",
        "Source_VTR": ["SVO-5800"],
        "TBC_Framesync": ["DPS-475"],
        "ADC": [],
        "Capture_Device": [],
        "Computer": [],
    }

    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        config_edit.apply_signalflow_profile(profile)

    # replace_config_section should be called with mediatrace path.
    paths = [c.args[1] for c in mock_cfg.replace_config_section.call_args_list]
    assert "mediatrace_values.ENCODER_SETTINGS" in paths


def test_apply_signalflow_profile_from_dataclass(mock_cfg):
    """A SignalflowProfile dataclass should be serialized via its attributes."""

    @dataclass
    class _Profile:
        Source_VTR: list = field(default_factory=lambda: ["A"])
        TBC_Framesync: list = field(default_factory=list)
        ADC: list = field(default_factory=list)
        Capture_Device: list = field(default_factory=list)
        Computer: list = field(default_factory=list)

    spex_stub = SimpleNamespace(
        mediatrace_values=SimpleNamespace(ENCODER_SETTINGS=SimpleNamespace()),
        ffmpeg_values={},
    )
    mock_cfg.get_config.return_value = spex_stub

    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        config_edit.apply_signalflow_profile(_Profile())

    # Find the mediatrace call and verify Source_VTR made it through.
    mediatrace_calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[1] == "mediatrace_values.ENCODER_SETTINGS"
    ]
    assert len(mediatrace_calls) == 1
    written = mediatrace_calls[0].args[2]
    assert written["Source_VTR"] == ["A"]


def test_apply_signalflow_profile_updates_ffmpeg_when_present(mock_cfg):
    """When ffmpeg_values.format.tags exists, ENCODER_SETTINGS there is updated too."""
    spex_stub = SimpleNamespace(
        mediatrace_values=SimpleNamespace(ENCODER_SETTINGS=SimpleNamespace()),
        ffmpeg_values={
            "format": {
                "tags": {
                    "ENCODER_SETTINGS": {"OldKey": ["stale"]},
                }
            }
        },
    )
    mock_cfg.get_config.return_value = spex_stub

    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        config_edit.apply_signalflow_profile({
            "Source_VTR": ["new"],
            "TBC_Framesync": [],
            "ADC": [],
            "Capture_Device": [],
            "Computer": [],
        })

    ffmpeg_calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[1] == "ffmpeg_values.format.tags.ENCODER_SETTINGS"
    ]
    assert len(ffmpeg_calls) == 1
    written = ffmpeg_calls[0].args[2]
    # The new value should be present; old keys not in the new profile are kept.
    assert written["Source_VTR"] == ["new"]
    assert "OldKey" in written


# ---------------------------------------------------------------------------
# apply_exiftool_profile / apply_mediainfo_profile / apply_ffprobe_profile
# ---------------------------------------------------------------------------

def test_apply_exiftool_profile_replaces_section(mock_cfg):
    profile = {"FileType": "MKV", "ImageWidth": "720"}
    result = config_edit.apply_exiftool_profile(profile)
    assert result is True
    # Should call replace_config_section on 'spex' / 'exiftool_values'.
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[0] == "spex" and c.args[1] == "exiftool_values"
    ]
    assert len(calls) == 1
    assert calls[0].args[2] == profile


def test_apply_mediainfo_profile_remaps_keys(mock_cfg):
    """Profile uses 'general'/'video'/'audio' but spex uses 'expected_*'."""
    profile = {
        "general": {"Format": "Matroska"},
        "video": {"Width": "720"},
        "audio": {"Channels": "2"},
    }
    result = config_edit.apply_mediainfo_profile(profile)
    assert result is True
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[0] == "spex" and c.args[1] == "mediainfo_values"
    ]
    assert len(calls) == 1
    written = calls[0].args[2]
    assert written["expected_general"] == {"Format": "Matroska"}
    assert written["expected_video"] == {"Width": "720"}
    assert written["expected_audio"] == {"Channels": "2"}


def test_apply_ffprobe_profile_preserves_structure(mock_cfg):
    profile = {
        "video_stream": {"codec_name": "ffv1"},
        "audio_stream": {"codec_name": "flac"},
        "format": {"format_name": "matroska webm"},
    }
    result = config_edit.apply_ffprobe_profile(profile)
    assert result is True
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[0] == "spex" and c.args[1] == "ffmpeg_values"
    ]
    assert len(calls) == 1
    written = calls[0].args[2]
    assert written["video_stream"] == {"codec_name": "ffv1"}
    assert written["audio_stream"] == {"codec_name": "flac"}
    assert written["format"] == {"format_name": "matroska webm"}


def test_apply_exiftool_profile_converts_dataclass(mock_cfg):
    @dataclass
    class _Profile:
        FileType: str = "MKV"

    config_edit.apply_exiftool_profile(_Profile())
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[1] == "exiftool_values"
    ]
    assert calls[0].args[2] == {"FileType": "MKV"}


# ---------------------------------------------------------------------------
# Custom profile CRUD (ChecksProfile)
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_profiles_config(mock_cfg):
    """Install a fake profiles_checks config with a few existing profiles."""
    from AV_Spex.utils.config_setup import ChecksProfile

    @dataclass
    class _FakeProfilesConfig:
        custom_profiles: dict = field(default_factory=dict)

    config = _FakeProfilesConfig()
    mock_cfg._configs = {"profiles_checks": config}
    mock_cfg.get_config.return_value = config
    return config


def _make_checks_profile(name, description=""):
    """ChecksProfile has default_factory for outputs/fixity/tools, so name +
    description is enough."""
    from AV_Spex.utils.config_setup import ChecksProfile
    return ChecksProfile(name=name, description=description)


def test_get_available_custom_profiles_empty(fake_profiles_config, mock_cfg):
    assert config_edit.get_available_custom_profiles() == []


def test_save_and_get_custom_profile(fake_profiles_config, mock_cfg):
    """save_custom_profile calls replace_config_section with the new profile included."""
    new_profile = _make_checks_profile("My Test Profile", "desc")
    config_edit.save_custom_profile(new_profile)

    # replace_config_section should have been called with the full custom_profiles dict.
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[1] == "custom_profiles"
    ]
    assert len(calls) >= 1
    written = calls[-1].args[2]
    assert "My Test Profile" in written
    assert written["My Test Profile"]["description"] == "desc"


def test_delete_custom_profile_not_found(fake_profiles_config, mock_cfg):
    result = config_edit.delete_custom_profile("does-not-exist")
    assert result is False
    assert mock_cfg.replace_config_section.called is False


def test_delete_custom_profile_removes_entry(fake_profiles_config, mock_cfg):
    fake_profiles_config.custom_profiles = {
        "keep": _make_checks_profile("keep"),
        "drop": _make_checks_profile("drop"),
    }
    result = config_edit.delete_custom_profile("drop")
    assert result is True
    calls = [
        c for c in mock_cfg.replace_config_section.call_args_list
        if c.args[1] == "custom_profiles"
    ]
    written = calls[-1].args[2]
    assert "keep" in written
    assert "drop" not in written


def test_apply_custom_profile_missing_returns_false(fake_profiles_config, mock_cfg):
    result = config_edit.apply_custom_profile("no-such-profile")
    assert result is False


def test_apply_custom_profile_routes_to_apply_profile(fake_profiles_config, mock_cfg):
    fake_profiles_config.custom_profiles = {"keep": _make_checks_profile("keep")}
    result = config_edit.apply_custom_profile("keep")
    assert result is True
    # apply_profile → config_mgr.update_config('checks', ...)
    checks_updates = [
        c for c in mock_cfg.update_config.call_args_list
        if c.args[0] == "checks"
    ]
    assert len(checks_updates) >= 1


def test_create_profile_from_current_config(mock_cfg):
    """Using a SimpleNamespace as the current-config stand-in so we don't have
    to construct every nested dataclass with required positional args."""
    cur = SimpleNamespace(
        validate_filename=True,
        video_file_extension="mkv",
        outputs=SimpleNamespace(),
        fixity=SimpleNamespace(),
        tools=SimpleNamespace(),
    )
    mock_cfg.get_config.return_value = cur
    profile = config_edit.create_profile_from_current_config("Snapshot", "d")
    assert profile.name == "Snapshot"
    assert profile.description == "d"
    assert profile.validate_filename is True
    assert profile.video_file_extension == "mkv"


# ---------------------------------------------------------------------------
# Profile CRUD: ExifTool / MediaInfo / FFprobe
# ---------------------------------------------------------------------------

def test_get_available_exiftool_profiles(mock_cfg):
    cfg = SimpleNamespace(exiftool_profiles={"A": {}, "B": {}})
    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        mock_cfg.get_config.return_value = cfg
        names = config_edit.get_available_exiftool_profiles()
    assert set(names) == {"A", "B"}


def test_get_exiftool_profile_missing_returns_none(mock_cfg):
    cfg = SimpleNamespace(exiftool_profiles={})
    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        mock_cfg.get_config.return_value = cfg
        assert config_edit.get_exiftool_profile("nope") is None


def test_save_exiftool_profile_success(mock_cfg):
    # Simulate a config with one existing profile; after save, both are present.
    existing = SimpleNamespace(exiftool_profiles={"old": {"FileType": "MKV"}})
    after = SimpleNamespace(exiftool_profiles={
        "old": {"FileType": "MKV"},
        "new": {"FileType": "MP4"},
    })
    mock_cfg.get_config.side_effect = [existing, after]
    result = config_edit.save_exiftool_profile("new", {"FileType": "MP4"})
    assert result is True
    calls = [c for c in mock_cfg.replace_config_section.call_args_list
             if c.args[0] == "exiftool" and c.args[1] == "exiftool_profiles"]
    written = calls[-1].args[2]
    assert "new" in written
    assert "old" in written


def test_delete_exiftool_profile_missing_returns_false(mock_cfg):
    mock_cfg.get_config.return_value = SimpleNamespace(exiftool_profiles={})
    assert config_edit.delete_exiftool_profile("nope") is False


def test_delete_exiftool_profile_success(mock_cfg):
    mock_cfg.get_config.return_value = SimpleNamespace(exiftool_profiles={
        "keep": {"a": 1},
        "drop": {"b": 2},
    })
    result = config_edit.delete_exiftool_profile("drop")
    assert result is True
    calls = [c for c in mock_cfg.replace_config_section.call_args_list
             if c.args[1] == "exiftool_profiles"]
    written = calls[-1].args[2]
    assert "keep" in written and "drop" not in written


def test_save_mediainfo_profile_success(mock_cfg):
    existing = SimpleNamespace(mediainfo_profiles={})
    after = SimpleNamespace(mediainfo_profiles={"new": {"general": {}}})
    mock_cfg.get_config.side_effect = [existing, after]
    result = config_edit.save_mediainfo_profile("new", {"general": {}})
    assert result is True


def test_delete_mediainfo_profile_success(mock_cfg):
    mock_cfg.get_config.return_value = SimpleNamespace(mediainfo_profiles={
        "x": {"a": 1}, "y": {"b": 2},
    })
    assert config_edit.delete_mediainfo_profile("y") is True


def test_save_ffprobe_profile_success(mock_cfg):
    existing = SimpleNamespace(ffprobe_profiles={})
    after = SimpleNamespace(ffprobe_profiles={"new": {"video_stream": {}}})
    mock_cfg.get_config.side_effect = [existing, after]
    result = config_edit.save_ffprobe_profile("new", {"video_stream": {}})
    assert result is True


def test_delete_ffprobe_profile_success(mock_cfg):
    mock_cfg.get_config.return_value = SimpleNamespace(ffprobe_profiles={
        "x": {"a": 1}, "y": {"b": 2},
    })
    assert config_edit.delete_ffprobe_profile("x") is True


# ---------------------------------------------------------------------------
# get_signalflow_profile
# ---------------------------------------------------------------------------

def test_get_signalflow_profile_found(mock_cfg):
    cfg = SimpleNamespace(signalflow_profiles={"prof1": {"x": 1}})
    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        mock_cfg.get_config.return_value = cfg
        assert config_edit.get_signalflow_profile("prof1") == {"x": 1}


def test_get_signalflow_profile_missing(mock_cfg):
    cfg = SimpleNamespace(signalflow_profiles={})
    with patch("AV_Spex.utils.config_edit.ConfigManager", return_value=mock_cfg):
        mock_cfg.get_config.return_value = cfg
        assert config_edit.get_signalflow_profile("nope") is None
