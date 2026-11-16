"""dev_tools/settings.py, the multi-machine parts of corpus.py, and the avspex-dev dispatcher.

What is pinned here is what keeps a second person's machine honest: settings resolve in a
fixed order, a run whose sidecars are on another machine is never scored as "nothing found",
a tape's start timecode travels with its package record, and a verdict records who gave it.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev_tools"))
import cli  # noqa: E402
import corpus  # noqa: E402
import ledger  # noqa: E402
import scorecard  # noqa: E402
import settings  # noqa: E402

A, B = "JPC_AV_00001", "JPC_AV_00002"


@pytest.fixture
def clean_settings(tmp_path, monkeypatch):
    """An empty settings directory and no settings in the environment."""
    monkeypatch.setenv("AVSPEX_DEV_SETTINGS_DIR", str(tmp_path / "settings"))
    for var in settings.KEYS.values():
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "settings"


@pytest.fixture
def corp(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS", tmp_path)
    monkeypatch.setattr(corpus, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(scorecard, "CORPUS", tmp_path)
    monkeypatch.setattr(scorecard, "TRUTH", tmp_path / "truth")
    monkeypatch.setattr(scorecard.Timing, "_cache", None)
    monkeypatch.setattr(ledger, "CORPUS", tmp_path)
    monkeypatch.setattr(ledger, "LEDGER", tmp_path / "ledger.csv")
    monkeypatch.setattr(ledger, "TRUTH", tmp_path / "truth")
    for d in ("runs", "truth", "labels"):
        (tmp_path / d).mkdir()
    return tmp_path


def add_package(corp, run, vid, collected, files=("x_qc_metadata/x.json",), present=True, **info):
    """A package record in a run; `present` says whether the sidecars it lists are on disk."""
    run_dir = corp / "runs" / run
    pkg = run_dir / "packages" / vid
    pkg.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": run, "host": "run-host"}))
    (pkg / "package.json").write_text(json.dumps(
        {"folder": vid, "video_id": vid, "collected": collected, "files": list(files), "avspex_exit": 0, **info}))
    if present:
        for rel in files:
            (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
            (pkg / rel).write_text("{}")
    return run_dir


# ------------------------------------------------------------------ settings ---

def test_settings_come_from_the_environment_then_the_file_then_the_old_default(clean_settings, monkeypatch):
    assert settings.corpus() == settings.LEGACY_CORPUS
    assert settings.corpus_source() == "default location"
    assert settings.staging() is None

    settings.save(corpus="/data/corpus", staging="/data/stage", reviewer="Ada")
    assert settings.corpus() == Path("/data/corpus")
    assert settings.corpus_source() == "settings file"
    assert settings.staging() == Path("/data/stage")
    assert settings.reviewer() == "Ada"

    monkeypatch.setenv("AVSPEX_CORPUS", "/env/corpus")
    monkeypatch.setenv("AVSPEX_REVIEWER", "Grace")
    assert settings.corpus() == Path("/env/corpus")
    assert settings.corpus_source() == "environment"
    assert settings.reviewer() == "Grace"


def test_saving_one_setting_keeps_the_others(clean_settings):
    settings.save(corpus="/data/corpus", reviewer="Ada")
    settings.save(staging="/data/stage")
    assert settings.load() == {"corpus": "/data/corpus", "reviewer": "Ada", "staging": "/data/stage"}
    settings.save(staging=None)
    assert "staging" not in settings.load()
    with pytest.raises(KeyError):
        settings.save(nonsense="x")


def test_an_unreadable_settings_file_reads_as_no_settings(clean_settings):
    clean_settings.mkdir()
    (clean_settings / "dev_tools.json").write_text("{not json")
    assert settings.load() == {}
    assert settings.corpus() == settings.LEGACY_CORPUS


# ------------------------------------------------------- records-only runs ---

def test_a_run_without_its_sidecars_is_records_only(corp):
    here = add_package(corp, "here", A, "2026-11-01T10:00:00")
    elsewhere = add_package(corp, "elsewhere", A, "2026-11-10T10:00:00", present=False, host="other-mac")
    assert corpus.has_sidecars(here)
    assert not corpus.has_sidecars(elsewhere)
    assert corpus.run_hosts(elsewhere) == ["other-mac"]
    with pytest.raises(SystemExit) as e:
        corpus.require_sidecars(elsewhere)
    assert "other-mac" in str(e.value) and "not on this machine" in str(e.value)
    corpus.require_sidecars(here)   # no exit


def test_records_that_list_no_files_are_not_taken_for_missing_sidecars(corp):
    """Older records and hand-built test corpora have no file list; there is nothing to check."""
    run = add_package(corp, "old", A, "2026-10-01T10:00:00", files=())
    assert corpus.has_sidecars(run)
    assert corpus.run_hosts(run) == ["run-host"]   # falls back to run.json


def test_one_package_with_sidecars_makes_the_run_local(corp):
    add_package(corp, "mixed", A, "2026-11-01T10:00:00", present=False)
    run = add_package(corp, "mixed", B, "2026-11-02T10:00:00")
    assert corpus.has_sidecars(run)


def test_latest_is_the_newest_run_that_can_be_scored_here(corp):
    add_package(corp, "older-local", A, "2026-11-01T10:00:00")
    add_package(corp, "newer-local", A, "2026-11-05T10:00:00")
    add_package(corp, "newest-elsewhere", A, "2026-11-10T10:00:00", present=False)
    assert corpus.resolve_run("latest").name == "newer-local"
    assert corpus.resolve_run("older").name == "older-local"          # prefixes still work
    assert corpus.resolve_run("newest").name == "newest-elsewhere"    # by name, records-only resolves


def test_latest_with_nothing_local_says_to_collect(corp):
    add_package(corp, "elsewhere", A, "2026-11-10T10:00:00", present=False)
    with pytest.raises(SystemExit) as e:
        corpus.resolve_run("latest")
    assert "collect" in str(e.value)


def test_find_packages_refuses_a_records_only_run(corp):
    add_package(corp, "elsewhere", A, "2026-11-10T10:00:00", present=False)
    with pytest.raises(SystemExit):
        corpus.find_packages(["elsewhere"])
    run = add_package(corp, "here", A, "2026-11-01T10:00:00")
    assert corpus.find_packages(["here"]) == [run / "packages" / A]


def test_the_run_list_says_where_the_sidecars_are(corp, capsys):
    add_package(corp, "here", A, "2026-11-01T10:00:00")
    add_package(corp, "elsewhere", A, "2026-11-10T10:00:00", present=False)
    corpus.list_runs()
    lines = {l.split()[0]: l for l in capsys.readouterr().out.splitlines()}
    assert "local" in lines["here"] and "records only" not in lines["here"]
    assert "records only" in lines["elsewhere"]


# -------------------------------------------------------- package discovery ---

def test_packages_are_found_by_name_under_a_source_or_as_the_source(tmp_path):
    for rel in ("vendor/JPC_AV_00001", "vendor/deep/JPC_AV_00002", "vendor/JPC_AV_00003_notes",
                "vendor/.Trashes/JPC_AV_00009", "other"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / "vendor/JPC_AV_00004").write_text("a file, not a package")
    found = corpus.discover_packages([tmp_path / "vendor"])
    assert [p.name for p in found] == ["JPC_AV_00001", "JPC_AV_00002"]
    assert corpus.discover_packages([tmp_path / "vendor/JPC_AV_00001"]) == [tmp_path / "vendor/JPC_AV_00001"]
    assert corpus.discover_packages([tmp_path / "other"]) == []
    with pytest.raises(SystemExit):
        corpus.discover_packages([tmp_path / "not_mounted"])


# ------------------------------------------------------------ start timecode ---

def make_pkg(corp, vid, start_timecode):
    run = add_package(corp, "r1", vid, "2026-11-01T10:00:00", files=(),
                      video={"fps": 30000 / 1001, "start_timecode": start_timecode})
    return scorecard.Package(run / "packages" / vid)


def test_a_tape_start_timecode_comes_from_its_package_record(corp):
    t = scorecard.Timing.for_package(make_pkg(corp, A, "01:00:00:00"))
    assert not t.known
    assert t.seconds("01:00:10:00") == pytest.approx(10.01, abs=0.001)
    zero = scorecard.Timing.for_package(make_pkg(corp, B, None))
    assert zero.seconds("00:00:10:00") == pytest.approx(10.01, abs=0.001)


def test_timecode_json_overrides_the_package_record(corp):
    """labels/timecode.json is where a correction by hand goes, including 'starts at zero'."""
    (corp / "labels" / "timecode.json").write_text(json.dumps(
        {A: {"fps": 29.97, "start_timecode": None}, B: {"fps": 29.97, "start_timecode": "00:00:10:00"}}))
    a = scorecard.Timing.for_package(make_pkg(corp, A, "01:00:00:00"))
    assert a.known and a.seconds("00:00:10:00") == pytest.approx(10.01, abs=0.001)
    b = scorecard.Timing.for_package(make_pkg(corp, B, "01:00:00:00"))
    assert b.seconds("00:00:20:00") == pytest.approx(10.01, abs=0.001)


# ------------------------------------------------------------- review sheets ---

def test_an_imported_verdict_records_who_gave_it(corp, clean_settings, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    monkeypatch.setenv("AVSPEX_REVIEWER", "Ada Lovelace")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "unjudged"
    ws.append(scorecard.SHEET_COLUMNS)
    ws.append(("bars", A, "00:00:01:00", "00:00:30:00", 29.0, "present", "clean bars", ""))
    ws.append(("bars", A, "00:10:00:00", "00:10:05:00", 5.0, "", "", ""))
    src = corp / "review.xlsx"
    wb.save(src)
    scorecard.import_sheet(src, {})
    truth, problems = scorecard.load_truth()
    assert not problems
    (row,) = truth["bars"]
    assert row["source"].startswith("Ada Lovelace review ") and row["source"].endswith("review.xlsx")
    assert row["verdict"] == "present" and row["note"] == "clean bars"


def test_the_default_sheet_name_carries_run_and_reviewer_and_never_overwrites(corp, clean_settings, monkeypatch):
    monkeypatch.setenv("AVSPEX_REVIEWER", "Ada Lovelace")
    run = add_package(corp, "abc1234-c0000000", A, "2026-11-01T10:00:00")
    first = scorecard.default_sheet_path(run)
    assert first.parent == corp / "truth"
    assert first.name.startswith("review_abc1234-c0000000_Ada-Lovelace_") and first.suffix == ".xlsx"
    first.write_text("")
    second = scorecard.default_sheet_path(run)
    assert second != first and second.stem == first.stem + "_2"


def test_a_review_sheet_explains_itself(corp, clean_settings):
    openpyxl = pytest.importorskip("openpyxl")
    run = add_package(corp, "r1", A, "2026-11-01T10:00:00", files=(), video={"fps": 29.97})
    dest = corp / "out" / "sheet.xlsx"            # the directory does not exist yet
    scorecard.write_sheet(run, {}, 1.0, ["bars"], dest)
    wb = openpyxl.load_workbook(dest)
    assert wb.sheetnames == ["unjudged", "instructions"]
    text = " ".join(str(c.value) for row in wb["instructions"].iter_rows() for c in row)
    for word in ("present", "absent", "blank", "import-sheet"):
        assert word in text


# ---------------------------------------------------------------- dispatcher ---

@pytest.mark.parametrize("command, argv, script, expected", [
    ("runs", [], "corpus.py", ["list"]),
    ("score", ["latest", "--detector", "bars"], "scorecard.py", ["latest", "--detector", "bars"]),
    ("score", [], "scorecard.py", []),
    ("import-sheet", ["r.xlsx"], "scorecard.py", ["--import-sheet", "r.xlsx"]),
    ("sheet", [], "scorecard.py", ["--sheet"]),
    ("sheet", ["abc"], "scorecard.py", ["abc", "--sheet"]),
    ("sheet", ["abc", "-o", "r.xlsx", "--detector", "bars", "--heldout"], "scorecard.py",
     ["abc", "--detector", "bars", "--heldout", "--sheet", "r.xlsx"]),
    ("table", ["--diff", "a", "b"], "aggregate.py", ["--diff", "a", "b"]),
    ("ledger", ["draw", "bars", "15"], "ledger.py", ["draw", "bars", "15"]),
    ("labels", [], "labels.py", []),
    ("label-sheet", ["r", "--probe"], "label_sheet.py", ["r", "--probe"]),
    ("replay", ["period-grid", "r", "--counts", "3,6"], "replays/period_grid.py", ["r", "--counts", "3,6"]),
    ("replay", ["harness", "r"], "replays/harness.py", ["r"]),
    ("dropout", ["export"], "event_labels.py", []),
    ("dropout", ["scan", "r", "v.mkv"], "dropout_scan.py", ["r", "v.mkv"]),
    ("corpus", ["run-id"], "corpus.py", ["run-id"]),
])
def test_each_command_runs_the_script_it_names(command, argv, script, expected):
    path, args = cli.script_call(command, argv)
    assert path == cli.HERE / script and path.is_file()
    assert args == expected


def test_the_sheet_arguments_parse_the_way_scorecard_reads_them(corp, monkeypatch):
    """`--sheet` takes an optional value, so the run has to come before it."""
    from AV_Spex.utils.log_setup import logger
    monkeypatch.setattr(logger, "level", logger.level)   # scorecard.main() quiets the app logger
    seen = {}
    monkeypatch.setattr(scorecard, "write_sheet", lambda run, truth, tol, names, dest, heldout=False:
                        seen.update(run=run.name, names=names, dest=dest, heldout=heldout))
    add_package(corp, "abc1234", A, "2026-11-01T10:00:00")
    _, args = cli.script_call("sheet", ["abc", "--detector", "bars", "-o", "r.xlsx"])
    monkeypatch.setattr(sys, "argv", ["scorecard.py", *args])
    scorecard.main()
    assert seen == {"run": "abc1234", "names": ["bars"], "dest": "r.xlsx", "heldout": False}


def test_unknown_commands_and_bad_group_names_are_refused():
    assert cli.script_call("nonsense", []) is None
    with pytest.raises(SystemExit):
        cli.script_call("replay", ["nonsense"])
    with pytest.raises(SystemExit):
        cli.script_call("import-sheet", [])


def test_baseline_comparison_ignores_keys_this_version_dropped(corp, monkeypatch):
    (corp / "configs").mkdir()
    assert cli.baseline_differences(corp) is None
    (corp / "configs" / "baseline_checks.json").write_text(json.dumps(
        {"checks": {"tools": {"qctools": {"run_tool": True}, "gone": {"x": 1}}, "validate_filename": True}}))
    saved = {"checks": {"tools": {"qctools": {"run_tool": True}, "new_tool": {"run_tool": False}},
                        "validate_filename": True}, "spex": {}}
    monkeypatch.setattr(corpus, "config_state", lambda: {"_configs": saved})
    assert cli.baseline_differences(corp) == []
    saved["checks"]["tools"]["qctools"]["run_tool"] = False
    (diff,) = cli.baseline_differences(corp)
    assert diff.startswith("checks.tools.qctools.run_tool")


def test_setup_template_adds_what_is_missing_and_overwrites_nothing(tmp_path):
    (tmp_path / ".gitignore").write_text("mine\n")
    added = cli.install_template(tmp_path)
    assert added == [".gitattributes", "README.md"]
    assert (tmp_path / ".gitignore").read_text() == "mine\n"
    assert "merge=union" in (tmp_path / ".gitattributes").read_text()
    assert all((tmp_path / d).is_dir() for d in cli.SKELETON)
    assert cli.install_template(tmp_path) == []
