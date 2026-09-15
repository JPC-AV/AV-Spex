import json
import pytest
from dataclasses import dataclass, field
from typing import Dict, List

from AV_Spex.utils.config_manager import ConfigManager


# ---------------------------------------------------------------------------
# Minimal dataclasses used in isolation tests (decoupled from the app's real
# SpexConfig/ChecksConfig so these tests don't depend on bundled JSON shape).
# ---------------------------------------------------------------------------

@dataclass
class NestedData:
    name: str
    count: int = 0


@dataclass
class SimpleConfig:
    title: str
    enabled: bool
    nested: NestedData
    items: List[NestedData] = field(default_factory=list)
    options: Dict[str, NestedData] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _reset_singleton():
    ConfigManager._instance = None
    ConfigManager._configs = {}
    ConfigManager._config_classes = {}


@pytest.fixture
def config_mgr(tmp_path, monkeypatch):
    """Fresh ConfigManager with user_config_dir redirected to tmp_path.

    Bundle dir is left at the real src/AV_Spex location because __new__ verifies
    that the bundled config/ directory exists.
    """
    user_dir = tmp_path / "user_config"
    monkeypatch.setattr(
        "AV_Spex.utils.config_manager.appdirs.user_config_dir",
        lambda appname=None, appauthor=None: str(user_dir),
    )
    _reset_singleton()
    try:
        yield ConfigManager()
    finally:
        _reset_singleton()


@pytest.fixture
def sandbox_mgr(tmp_path, monkeypatch):
    """ConfigManager pointed at a sandbox bundle dir + user dir.

    Lets tests inject arbitrary bundled configs without relying on the real
    checks_config.json / spex_config.json files.
    """
    user_dir = tmp_path / "user_config"
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "config").mkdir(parents=True)

    monkeypatch.setattr(
        "AV_Spex.utils.config_manager.appdirs.user_config_dir",
        lambda appname=None, appauthor=None: str(user_dir),
    )
    _reset_singleton()
    try:
        mgr = ConfigManager()
        # Redirect paths derived from _bundle_dir onto the sandbox bundle
        mgr._bundle_dir = str(bundle_dir)
        mgr._logo_files_dir = str(bundle_dir / "logo_image_files")
        mgr._bundled_policies_dir = str(bundle_dir / "config" / "mediaconch_policies")
        yield mgr, bundle_dir, user_dir
    finally:
        _reset_singleton()


def _write_bundled_config(bundle_dir, name, data):
    path = bundle_dir / "config" / f"{name}_config.json"
    path.write_text(json.dumps(data))
    return path


def _write_last_used_config(user_dir, name, data):
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / f"last_used_{name}_config.json"
    path.write_text(json.dumps(data))
    return path


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_migrate_yes_no_to_bool(config_mgr):
    assert config_mgr._migrate_yes_no_to_bool("yes") is True
    assert config_mgr._migrate_yes_no_to_bool("YES") is True
    assert config_mgr._migrate_yes_no_to_bool("no") is False
    assert config_mgr._migrate_yes_no_to_bool("No") is False
    # Non yes/no values pass through unchanged
    assert config_mgr._migrate_yes_no_to_bool("maybe") == "maybe"
    assert config_mgr._migrate_yes_no_to_bool(True) is True
    assert config_mgr._migrate_yes_no_to_bool(42) == 42


def test_migrate_bool_to_yes_no(config_mgr):
    assert config_mgr._migrate_bool_to_yes_no(True) == "yes"
    assert config_mgr._migrate_bool_to_yes_no(False) == "no"
    assert config_mgr._migrate_bool_to_yes_no("something") == "something"
    assert config_mgr._migrate_bool_to_yes_no(42) == 42


def test_update_dict_recursively_deep_merge(config_mgr):
    target = {"a": 1, "b": {"c": 2, "d": 3}, "e": [1, 2]}
    source = {"b": {"c": 20}, "e": [99]}
    config_mgr._update_dict_recursively(target, source)
    assert target == {"a": 1, "b": {"c": 20, "d": 3}, "e": [99]}


def test_update_dict_recursively_skips_unknown_keys(config_mgr):
    """Keys not already in target are silently ignored (current behavior)."""
    target = {"a": 1}
    config_mgr._update_dict_recursively(target, {"b": 2})
    assert target == {"a": 1}


# ---------------------------------------------------------------------------
# Checks-config migration
# ---------------------------------------------------------------------------

def test_migrate_adds_validate_filename_default(config_mgr):
    data = {"outputs": {}, "fixity": {}, "tools": {}}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["validate_filename"] is True


def test_migrate_preserves_existing_validate_filename(config_mgr):
    data = {"outputs": {}, "fixity": {}, "tools": {}, "validate_filename": False}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["validate_filename"] is False


def test_migrate_outputs_yes_no_to_bool(config_mgr):
    data = {"outputs": {"access_file": "yes", "report": "no"}}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["outputs"]["access_file"] is True
    assert result["outputs"]["report"] is False


def test_migrate_resets_invalid_qctools_ext(config_mgr):
    data = {"outputs": {"qctools_ext": "bogus.ext"}}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["outputs"]["qctools_ext"] == "qctools.xml.gz"


def test_migrate_preserves_valid_qctools_ext(config_mgr):
    data = {"outputs": {"qctools_ext": "qctools.mkv"}}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["outputs"]["qctools_ext"] == "qctools.mkv"


def test_migrate_renames_signalstats_fields(config_mgr):
    data = {"outputs": {"frame_analysis": {
        "signalstats_duration": 90,
        "signalstats_periods": 5,
    }}}
    result = config_mgr._migrate_config_data(data, "checks")
    fa = result["outputs"]["frame_analysis"]
    assert fa["analysis_period_duration"] == 90
    assert fa["analysis_period_count"] == 5
    assert "signalstats_duration" not in fa
    assert "signalstats_periods" not in fa


def test_migrate_skips_signalstats_rename_when_new_field_already_present(config_mgr):
    """If analysis_period_duration is already set, don't clobber it."""
    data = {"outputs": {"frame_analysis": {
        "signalstats_duration": 90,
        "analysis_period_duration": 120,
    }}}
    result = config_mgr._migrate_config_data(data, "checks")
    fa = result["outputs"]["frame_analysis"]
    assert fa["analysis_period_duration"] == 120
    # Old key was NOT popped because new key was already present
    assert fa["signalstats_duration"] == 90


def test_migrate_audio_analysis_merge_with_yes_no_strings(config_mgr):
    data = {"tools": {"qct_parse": {
        "detect_audio_clipping": "no",
        "detect_channel_imbalance": "yes",
    }}}
    result = config_mgr._migrate_config_data(data, "checks")
    qp = result["tools"]["qct_parse"]
    assert qp["audio_analysis"] is True
    assert "detect_audio_clipping" not in qp
    assert "detect_channel_imbalance" not in qp


def test_migrate_audio_analysis_merge_both_false(config_mgr):
    data = {"tools": {"qct_parse": {
        "detect_audio_clipping": False,
        "detect_channel_imbalance": False,
    }}}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["tools"]["qct_parse"]["audio_analysis"] is False


def test_migrate_audio_analysis_respects_existing_value(config_mgr):
    """If audio_analysis is already set, legacy fields should still be removed."""
    data = {"tools": {"qct_parse": {
        "audio_analysis": True,
        "detect_audio_clipping": False,
        "detect_channel_imbalance": False,
    }}}
    result = config_mgr._migrate_config_data(data, "checks")
    qp = result["tools"]["qct_parse"]
    assert qp["audio_analysis"] is True
    assert "detect_audio_clipping" not in qp
    assert "detect_channel_imbalance" not in qp


def test_migrate_fixity_yes_no(config_mgr):
    data = {"fixity": {
        "check_fixity": "yes",
        "validate_stream_fixity": "no",
        "embed_stream_fixity": "yes",
        "output_fixity": "no",
        "overwrite_stream_fixity": "no",
    }}
    result = config_mgr._migrate_config_data(data, "checks")
    fx = result["fixity"]
    assert fx["check_fixity"] is True
    assert fx["validate_stream_fixity"] is False
    assert fx["embed_stream_fixity"] is True
    assert fx["output_fixity"] is False
    assert fx["overwrite_stream_fixity"] is False


def test_migrate_tools_yes_no(config_mgr):
    data = {"tools": {
        "exiftool": {"check_tool": "yes", "run_tool": "no"},
        "mediaconch": {"run_mediaconch": "yes"},
        "qctools": {"run_tool": "yes"},
        "qct_parse": {"run_tool": "no", "barsDetection": "yes", "evaluateBars": "no", "thumbExport": "yes"},
    }}
    result = config_mgr._migrate_config_data(data, "checks")
    assert result["tools"]["exiftool"]["check_tool"] is True
    assert result["tools"]["exiftool"]["run_tool"] is False
    assert result["tools"]["mediaconch"]["run_mediaconch"] is True
    assert result["tools"]["qctools"]["run_tool"] is True
    assert result["tools"]["qct_parse"]["barsDetection"] is True
    assert result["tools"]["qct_parse"]["evaluateBars"] is False


def test_migrate_non_checks_config_untouched(config_mgr):
    """Only `checks` config is migrated — other config names pass through."""
    data = {"tools": {"exiftool": {"check_tool": "yes"}}, "validate_filename": "yes"}
    result = config_mgr._migrate_config_data(data, "spex")
    assert result["tools"]["exiftool"]["check_tool"] == "yes"
    assert result["validate_filename"] == "yes"


# ---------------------------------------------------------------------------
# Dataclass deserialization
# ---------------------------------------------------------------------------

def test_deserialize_simple_dataclass(config_mgr):
    data = {"title": "hello", "enabled": True, "nested": {"name": "x", "count": 3}}
    result = config_mgr._deserialize_dataclass(SimpleConfig, data)
    assert isinstance(result, SimpleConfig)
    assert result.title == "hello"
    assert result.enabled is True
    assert isinstance(result.nested, NestedData)
    assert result.nested.name == "x"
    assert result.nested.count == 3


def test_deserialize_handles_list_of_dataclasses(config_mgr):
    data = {
        "title": "t", "enabled": False, "nested": {"name": "n"},
        "items": [{"name": "a", "count": 1}, {"name": "b", "count": 2}],
    }
    result = config_mgr._deserialize_dataclass(SimpleConfig, data)
    assert len(result.items) == 2
    assert all(isinstance(i, NestedData) for i in result.items)
    assert result.items[0].name == "a"
    assert result.items[1].count == 2


def test_deserialize_handles_dict_of_dataclasses(config_mgr):
    data = {
        "title": "t", "enabled": False, "nested": {"name": "n"},
        "options": {
            "first": {"name": "A", "count": 10},
            "second": {"name": "B", "count": 20},
        },
    }
    result = config_mgr._deserialize_dataclass(SimpleConfig, data)
    assert set(result.options.keys()) == {"first", "second"}
    assert isinstance(result.options["first"], NestedData)
    assert result.options["second"].count == 20


def test_deserialize_skips_unknown_fields(config_mgr):
    data = {
        "title": "t", "enabled": True, "nested": {"name": "n"},
        "extra_garbage_field": "ignored",
    }
    result = config_mgr._deserialize_dataclass(SimpleConfig, data)
    assert result.title == "t"
    assert not hasattr(result, "extra_garbage_field")


def test_deserialize_none_returns_none(config_mgr):
    assert config_mgr._deserialize_dataclass(SimpleConfig, None) is None


# ---------------------------------------------------------------------------
# Lifecycle: get / save / update / replace / reset / refresh (sandboxed)
# ---------------------------------------------------------------------------

def test_get_config_loads_bundled_default(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "from-bundle", "enabled": False, "nested": {"name": "a"},
    })
    cfg = mgr.get_config("simple", SimpleConfig, use_last_used=False)
    assert cfg.title == "from-bundle"
    assert cfg.enabled is False
    assert cfg.nested.name == "a"


def test_get_config_returns_cached_instance(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "cached", "enabled": True, "nested": {"name": "a"},
    })
    first = mgr.get_config("simple", SimpleConfig)
    second = mgr.get_config("simple", SimpleConfig)
    assert first is second


def test_get_config_prefers_last_used_over_bundled(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "bundle", "enabled": False, "nested": {"name": "b"},
    })
    _write_last_used_config(user_dir, "simple", {
        "title": "last-used", "enabled": True, "nested": {"name": "u"},
    })
    cfg = mgr.get_config("simple", SimpleConfig, use_last_used=True)
    assert cfg.title == "last-used"
    assert cfg.enabled is True


def test_get_config_falls_back_when_last_used_corrupted(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "bundle", "enabled": False, "nested": {"name": "b"},
    })
    user_dir.mkdir(parents=True, exist_ok=True)
    corrupted = user_dir / "last_used_simple_config.json"
    corrupted.write_text("{not valid json")

    cfg = mgr.get_config("simple", SimpleConfig, use_last_used=True)
    assert cfg.title == "bundle"
    # _cleanup_corrupted_configs should have removed the bad last_used file
    assert not corrupted.exists()


def test_save_config_writes_last_used_json(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "t", "enabled": True, "nested": {"name": "n"},
    })
    mgr.get_config("simple", SimpleConfig)
    mgr.save_config("simple", is_last_used=True)

    saved_path = user_dir / "last_used_simple_config.json"
    assert saved_path.exists()
    saved = json.loads(saved_path.read_text())
    assert saved["title"] == "t"
    assert saved["enabled"] is True


def test_save_config_noop_when_not_cached(sandbox_mgr):
    mgr, _, user_dir = sandbox_mgr
    # No config loaded — save_config should do nothing rather than raise
    mgr.save_config("simple", is_last_used=True)
    assert not (user_dir / "last_used_simple_config.json").exists()


def test_update_config_deep_merges_and_persists(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "orig", "enabled": False, "nested": {"name": "n", "count": 1},
    })
    mgr.get_config("simple", SimpleConfig)

    mgr.update_config("simple", {"nested": {"count": 99}})
    result = mgr.get_config("simple", SimpleConfig)

    # Untouched fields preserved; only nested.count changed
    assert result.title == "orig"
    assert result.nested.name == "n"
    assert result.nested.count == 99

    # Written to last_used
    saved = json.loads((user_dir / "last_used_simple_config.json").read_text())
    assert saved["nested"]["count"] == 99
    assert saved["title"] == "orig"


def test_update_config_loads_uncached_via_registered_class(sandbox_mgr):
    """update_config should reload the config using the class registered by a
    previous get_config call when the cache has been cleared."""
    mgr, bundle_dir, _ = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "orig", "enabled": False, "nested": {"name": "n", "count": 1},
    })
    mgr.get_config("simple", SimpleConfig)  # registers class
    mgr.refresh_configs()                   # clears cache

    mgr.update_config("simple", {"title": "updated"})
    cfg = mgr.get_config("simple", SimpleConfig)
    assert cfg.title == "updated"


def test_replace_config_section_swaps_nested_value(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "orig", "enabled": False, "nested": {"name": "n", "count": 1},
    })
    mgr.get_config("simple", SimpleConfig)

    mgr.replace_config_section("simple", "nested", {"name": "replaced", "count": 42})
    result = mgr.get_config("simple", SimpleConfig)
    assert result.nested.name == "replaced"
    assert result.nested.count == 42

    # Persisted to last_used
    saved = json.loads((user_dir / "last_used_simple_config.json").read_text())
    assert saved["nested"] == {"name": "replaced", "count": 42}


def test_replace_config_section_invalid_path_is_noop(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "t", "enabled": False, "nested": {"name": "n"},
    })
    mgr.get_config("simple", SimpleConfig)

    # Should not raise — just logs an error and leaves the config untouched
    mgr.replace_config_section("simple", "nonexistent.path", "x")
    cfg = mgr.get_config("simple", SimpleConfig)
    assert cfg.title == "t"
    assert cfg.nested.name == "n"


def test_reset_config_removes_last_used_and_reloads_default(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "bundle", "enabled": False, "nested": {"name": "n"},
    })
    _write_last_used_config(user_dir, "simple", {
        "title": "user-edited", "enabled": True, "nested": {"name": "u"},
    })
    mgr.get_config("simple", SimpleConfig)  # picks up last_used

    cfg = mgr.reset_config("simple", SimpleConfig)
    assert cfg.title == "bundle"
    assert not (user_dir / "last_used_simple_config.json").exists()


def test_refresh_configs_forces_reload_from_disk(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    _write_bundled_config(bundle_dir, "simple", {
        "title": "v1", "enabled": False, "nested": {"name": "n"},
    })
    mgr.get_config("simple", SimpleConfig)

    _write_bundled_config(bundle_dir, "simple", {
        "title": "v2", "enabled": False, "nested": {"name": "n"},
    })
    # Without refresh the cached config is returned
    assert mgr.get_config("simple", SimpleConfig).title == "v1"

    mgr.refresh_configs()
    assert mgr.get_config("simple", SimpleConfig).title == "v2"


def test_cleanup_corrupted_configs_only_removes_last_used_files(sandbox_mgr):
    mgr, _, user_dir = sandbox_mgr
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "last_used_checks_config.json").write_text("{}")
    (user_dir / "last_used_spex_config.json").write_text("{}")
    (user_dir / "some_other.json").write_text("keep-me")

    mgr._cleanup_corrupted_configs()

    assert not (user_dir / "last_used_checks_config.json").exists()
    assert not (user_dir / "last_used_spex_config.json").exists()
    assert (user_dir / "some_other.json").exists()


def test_remove_all_last_used_configs_covers_every_config_type(sandbox_mgr):
    """Backs the CLI --use-default-config reset: it must clear last_used files
    for *all* configs the app persists, not just checks/spex/filename."""
    mgr, _, user_dir = sandbox_mgr
    user_dir.mkdir(parents=True, exist_ok=True)
    names = [
        "checks", "spex", "filename", "signalflow",
        "exiftool", "ffprobe", "mediainfo", "profiles_checks",
    ]
    for name in names:
        (user_dir / f"last_used_{name}_config.json").write_text("{}")
    # A bundled (non-last_used) config and an unrelated file must survive.
    (user_dir / "checks_config.json").write_text("{}")
    (user_dir / "notes.txt").write_text("keep-me")

    removed = mgr.remove_all_last_used_configs()

    assert removed == len(names)
    for name in names:
        assert not (user_dir / f"last_used_{name}_config.json").exists()
    assert (user_dir / "checks_config.json").exists()
    assert (user_dir / "notes.txt").exists()


def test_remove_all_last_used_configs_clears_cache(sandbox_mgr):
    mgr, _, user_dir = sandbox_mgr
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "last_used_checks_config.json").write_text("{}")
    mgr._configs["checks"] = object()

    mgr.remove_all_last_used_configs()

    assert mgr._configs == {}


def test_remove_all_last_used_configs_returns_zero_when_none_present(sandbox_mgr):
    mgr, _, user_dir = sandbox_mgr
    user_dir.mkdir(parents=True, exist_ok=True)

    assert mgr.remove_all_last_used_configs() == 0


def test_load_json_config_raises_on_missing_file(sandbox_mgr):
    mgr, _, _ = sandbox_mgr
    with pytest.raises(FileNotFoundError):
        mgr._load_json_config("does_not_exist", use_last_used=False)


def test_load_json_config_raises_value_error_on_invalid_json(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    (bundle_dir / "config" / "bad_config.json").write_text("{not valid")
    with pytest.raises(ValueError):
        mgr._load_json_config("bad", use_last_used=False)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

def test_get_available_policies_combines_bundled_and_user(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    bundled = bundle_dir / "config" / "mediaconch_policies"
    bundled.mkdir(parents=True)
    (bundled / "A.xml").write_text("<x/>")
    (bundled / "B.xml").write_text("<x/>")
    (bundled / "README.txt").write_text("ignore non-xml")

    user_policies = user_dir / "mediaconch_policies"
    user_policies.mkdir(parents=True, exist_ok=True)
    (user_policies / "C.xml").write_text("<x/>")
    (user_policies / "A.xml").write_text("<x/>")  # duplicate name — de-duped

    assert mgr.get_available_policies() == ["A.xml", "B.xml", "C.xml"]


def test_get_policy_path_prefers_user_dir(sandbox_mgr):
    mgr, bundle_dir, user_dir = sandbox_mgr
    bundled = bundle_dir / "config" / "mediaconch_policies"
    bundled.mkdir(parents=True)
    (bundled / "same.xml").write_text("<bundle/>")

    user_policies = user_dir / "mediaconch_policies"
    user_policies.mkdir(parents=True, exist_ok=True)
    (user_policies / "same.xml").write_text("<user/>")

    assert mgr.get_policy_path("same.xml") == str(user_policies / "same.xml")


def test_get_policy_path_falls_back_to_bundled(sandbox_mgr):
    mgr, bundle_dir, _ = sandbox_mgr
    bundled = bundle_dir / "config" / "mediaconch_policies"
    bundled.mkdir(parents=True)
    (bundled / "only-bundled.xml").write_text("<bundle/>")

    assert mgr.get_policy_path("only-bundled.xml") == str(bundled / "only-bundled.xml")


def test_get_policy_path_missing_returns_none(sandbox_mgr):
    mgr, *_ = sandbox_mgr
    assert mgr.get_policy_path("nonexistent.xml") is None


# ---------------------------------------------------------------------------
# Smoke test against the real bundled checks_config.json
# ---------------------------------------------------------------------------

def test_real_bundled_checks_config_loads(config_mgr):
    """Catches regressions in the shipped checks_config.json schema."""
    from AV_Spex.utils.config_setup import ChecksConfig
    cfg = config_mgr.get_config("checks", ChecksConfig, use_last_used=False)
    assert cfg.outputs is not None
    assert cfg.fixity is not None
    assert cfg.tools is not None
    assert isinstance(cfg.validate_filename, bool)


def test_saved_frame_analysis_config_with_removed_brng_duration_limit_still_loads(sandbox_mgr):
    """brng_duration_limit was removed; last-used configs and exports written
    before then still carry it and must load (the key is simply dropped)."""
    from pathlib import Path
    from AV_Spex.utils.config_setup import ChecksConfig

    mgr, bundle_dir, user_dir = sandbox_mgr
    bundled = json.loads(
        (Path(__file__).parent.parent / "src" / "AV_Spex" / "config" / "checks_config.json").read_text())
    _write_bundled_config(bundle_dir, "checks", bundled)

    legacy = json.loads(json.dumps(bundled))
    legacy["outputs"]["frame_analysis"]["brng_duration_limit"] = 120
    legacy["outputs"]["frame_analysis"]["analysis_period_count"] = 4
    _write_last_used_config(user_dir, "checks", legacy)

    cfg = mgr.get_config("checks", ChecksConfig, use_last_used=True)

    assert cfg.outputs.frame_analysis.analysis_period_count == 4
    assert not hasattr(cfg.outputs.frame_analysis, "brng_duration_limit")
