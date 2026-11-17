"""
avspex-dev: one front door for the developer tools.

Everyday commands:

    avspex-dev setup                   point the tools at a corpus (clone or create one) and say who you are
    avspex-dev doctor                  check the install, the corpus and the saved AV Spex config
    avspex-dev collect SOURCE...       run AV Spex over the tapes in SOURCE and keep the results as a run
    avspex-dev runs                    list runs, and whether each one's sidecars are on this machine
    avspex-dev sheet [RUN]             write a review workbook of everything nobody has judged yet
    avspex-dev import-sheet XLSX       file the verdicts you filled in
    avspex-dev score [RUN]             score each detector against the confirmed cases

RUN is a run ID, a unique prefix of one, or `latest` (the default).

Also:

    avspex-dev table RUN...            one row per tape, or --diff between two runs   (aggregate.py)
    avspex-dev ledger ...              which tape was used for what; held-out draws    (ledger.py)
    avspex-dev labels                  validate the bin label sheets                   (labels.py)
    avspex-dev label-sheet RUN ...     build a bin label sheet                         (label_sheet.py)
    avspex-dev replay detector [RUN]   re-run the QCTools-report detectors from a run's sidecars and
                                       score them: minutes, no tapes needed            (replays/detector.py)
    avspex-dev replay NAME ...         harness | period-grid | bin-table | tag-weights (replays/)
    avspex-dev dropout NAME ...        scan | sheet | export                           (dropout_*.py, event_labels.py)
    avspex-dev corpus ...              run-id, import, and what collect calls          (corpus.py)

`avspex-dev COMMAND -h` shows that command's options. Each command runs the script named on
the right with the same arguments, so `python3 dev_tools/<script>.py ...` still works.
"""
import argparse
import json
import os
import runpy
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TEMPLATE = HERE / "corpus_template"
for _p in (HERE / "replays", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import settings  # noqa: E402

# command -> script, for the commands that hand their arguments straight on
SCRIPTS = {
    "runs": ("corpus.py", ["list"]),
    "score": ("scorecard.py", []),
    "table": ("aggregate.py", []),
    "ledger": ("ledger.py", []),
    "labels": ("labels.py", []),
    "label-sheet": ("label_sheet.py", []),
    "corpus": ("corpus.py", []),
}
GROUPS = {
    "replay": {"detector": "replays/detector.py", "harness": "replays/harness.py",
               "period-grid": "replays/period_grid.py", "bin-table": "replays/bin_table.py",
               "tag-weights": "replays/tag_weights.py"},
    "dropout": {"scan": "dropout_scan.py", "sheet": "dropout_sheet.py", "export": "event_labels.py"},
}
TOOLS = ("av-spex", "ffmpeg", "ffprobe", "qcli", "mediainfo", "exiftool", "mediaconch")
SKELETON = ("runs", "truth", "labels", "configs", "notes", "studies")


def script_call(command, argv):
    """(script path, arguments) for a command that maps onto an existing script, else None."""
    if command in SCRIPTS:
        script, prefix = SCRIPTS[command]
        return HERE / script, [*prefix, *argv]
    if command in GROUPS:
        names = GROUPS[command]
        if not argv or argv[0] not in names:
            sys.exit(f"usage: avspex-dev {command} {{{' | '.join(names)}}} ...")
        return HERE / names[argv[0]], list(argv[1:])
    if command == "import-sheet":
        if len(argv) != 1 or argv[0].startswith("-"):
            sys.exit("usage: avspex-dev import-sheet XLSX")
        return HERE / "scorecard.py", ["--import-sheet", argv[0]]
    if command == "sheet":
        ap = argparse.ArgumentParser(prog="avspex-dev sheet", description=(
            "Write a review workbook: every event a detector reported that nobody has judged yet. "
            "Fill in the verdict column, then run avspex-dev import-sheet on it."))
        ap.add_argument("run", nargs="?", help="run ID, prefix or 'latest' (default)")
        ap.add_argument("-o", "--out", metavar="XLSX",
                        help="default: truth/review_<run>_<reviewer>_<date>.xlsx in the corpus")
        ap.add_argument("--detector", action="append", help="only this detector (repeatable)")
        ap.add_argument("--heldout", action="store_true",
                        help="the held-out test form: only tapes the ledger reserves for the detector")
        ap.add_argument("--max-per-tape", type=int, metavar="N",
                        help="at most N events from any one tape, picked at random by --seed")
        ap.add_argument("--seed", help="with --max-per-tape: default is today's date as YYYYMMDD")
        a = ap.parse_args(argv)
        out = [a.run] if a.run else []
        for d in a.detector or []:
            out += ["--detector", d]
        if a.heldout:
            out.append("--heldout")
        if a.max_per_tape:
            out += ["--max-per-tape", str(a.max_per_tape)]
        if a.seed:
            out += ["--seed", a.seed]
        return HERE / "scorecard.py", [*out, "--sheet", *([a.out] if a.out else [])]
    return None


def run_script(script, argv, prog):
    sys.argv = [prog, *argv]
    runpy.run_path(str(script), run_name="__main__")


def require_corpus():
    path = settings.corpus()
    if not path.is_dir():
        sys.exit(f"no corpus at {path} ({settings.corpus_source()}). Run: avspex-dev setup")
    return path


# ---------------------------------------------------------------------- setup ---

def _ask(prompt, default=None):
    if not sys.stdin.isatty():
        return default
    answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def _git(cwd, *args, timeout=60):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)


def install_template(corpus):
    """Create the corpus directories and copy in the template files that are missing.
    Nothing that exists is overwritten."""
    added = []
    for name in SKELETON:
        (corpus / name).mkdir(parents=True, exist_ok=True)
    for src, dest in (("gitignore", ".gitignore"), ("gitattributes", ".gitattributes"), ("README.md", "README.md")):
        if not (corpus / dest).exists():
            shutil.copyfile(TEMPLATE / src, corpus / dest)
            added.append(dest)
    return added


def cmd_setup(argv):
    ap = argparse.ArgumentParser(prog="avspex-dev setup", description=(
        "Point the dev tools at a results corpus and record who you are. With --clone, the shared "
        "corpus is cloned; otherwise an existing directory is adopted or an empty corpus is created. "
        "Anything not given is asked for. Safe to run again: nothing existing is overwritten."))
    ap.add_argument("--clone", metavar="URL", help="git URL of the shared corpus repository")
    ap.add_argument("--corpus", metavar="DIR", help="where the corpus is, or should go")
    ap.add_argument("--reviewer", metavar="NAME", help="your name, recorded on the verdicts you import")
    ap.add_argument("--staging", metavar="DIR", help="local directory for staged collection (optional)")
    ap.add_argument("--link", action="store_true", help="put avspex-dev on PATH, next to python")
    ap.add_argument("--no-doctor", action="store_true", help="do not run the checks afterwards")
    a = ap.parse_args(argv)

    saved = settings.load()
    corpus = a.corpus or _ask("Corpus directory", saved.get("corpus") or (
        str(settings.LEGACY_CORPUS) if settings.LEGACY_CORPUS.is_dir() else str(Path.home() / "avspex_corpus")))
    corpus = Path(corpus).expanduser().resolve()
    populated = corpus.is_dir() and any(corpus.iterdir())
    if a.clone:
        if populated:
            sys.exit(f"{corpus} already has files in it; choose another --corpus for the clone, "
                     f"or leave --clone out to use what is there")
        corpus.parent.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["git", "clone", a.clone, str(corpus)]).returncode:
            sys.exit("git clone failed")
        print(f"cloned {a.clone} -> {corpus}")
    elif populated:
        print(f"using the corpus already at {corpus}")
    else:
        corpus.mkdir(parents=True, exist_ok=True)
        print(f"created an empty corpus at {corpus}")
    added = install_template(corpus)
    if added:
        print(f"added to the corpus: {', '.join(added)}")
    if not (corpus / ".git").exists():
        _git(corpus, "init", "-q")
        print("initialised it as a git repository (no remote yet: git remote add origin URL)")

    reviewer = a.reviewer or _ask("Your name, as it should appear on verdicts",
                                  saved.get("reviewer") or settings.reviewer())
    values = {"corpus": corpus, "reviewer": reviewer}
    if a.staging:
        values["staging"] = Path(a.staging).expanduser().resolve()
    path = settings.save(**values)
    print(f"settings saved to {path}")
    for key, var in settings.KEYS.items():
        if os.environ.get(var) and key in values and os.environ[var] != str(values[key]):
            print(f"! {var} is set in your environment to {os.environ[var]} and overrides the saved {key}")

    bin_link = Path(sys.executable).parent / "avspex-dev"
    if bin_link.exists() or bin_link.is_symlink():
        pass
    elif a.link or (_ask(f"Put avspex-dev on your PATH ({bin_link})? y/n", "y") or "").lower().startswith("y") \
            and sys.stdin.isatty():
        try:
            bin_link.symlink_to(HERE / "avspex-dev")
            print(f"linked {bin_link}")
        except OSError as e:
            print(f"! could not link {bin_link} ({e}); call {HERE / 'avspex-dev'} by path instead")
    if not (bin_link.exists() and shutil.which("avspex-dev")):
        print(f"avspex-dev is not on your PATH; call it as {HERE / 'avspex-dev'} (or run setup --link)")

    if not a.no_doctor:
        print(flush=True)
        return subprocess.run([sys.executable, str(HERE / "avspex-dev"), "doctor"]).returncode
    return 0


# --------------------------------------------------------------------- doctor ---

def baseline_differences(corpus):
    """Where the saved AV Spex config differs from the corpus's configs/baseline_checks.json:
    one line per setting ([] when it matches), or None when the corpus has no baseline. Only
    settings the baseline names are compared."""
    path = corpus / "configs" / "baseline_checks.json"
    if not path.exists():
        return None
    import corpus as corpus_mod
    current = corpus_mod.config_state()["_configs"]
    diffs = []

    def walk(want, have, where):
        if isinstance(want, dict) and isinstance(have, dict):
            for key, value in want.items():
                # A key this version of AV Spex no longer has is ignored on import; not a difference
                if key in have:
                    walk(value, have[key], f"{where}{key}.")
        elif want != have:
            diffs.append(f"{where.rstrip('.')}: saved {have!r}, baseline {want!r}")

    walk(json.loads(path.read_text()), current, "")
    return diffs


class Report:
    def __init__(self):
        self.failed = 0

    def line(self, mark, what, detail="", fix=""):
        print(f"  {mark:<4} {what}{f': {detail}' if detail else ''}")
        if fix:
            print(f"       -> {fix}")
        self.failed += mark == "FAIL"

    def ok(self, what, detail=""):
        self.line("ok", what, detail)

    def warn(self, what, detail="", fix=""):
        self.line("warn", what, detail, fix)

    def fail(self, what, detail="", fix=""):
        self.line("FAIL", what, detail, fix)


def _quiet_avspex():
    import logging
    from AV_Spex.utils.log_setup import logger
    logger.setLevel(logging.ERROR)


def cmd_doctor(argv):
    ap = argparse.ArgumentParser(prog="avspex-dev doctor", description=(
        "Check that this machine is ready: the install, the external tools, the corpus and its git "
        "state, the saved AV Spex config against the shared baseline, and the truth files. Each "
        "problem comes with what to do about it. Exits 1 if anything is marked FAIL."))
    ap.add_argument("--offline", action="store_true", help="do not contact the corpus remote")
    a = ap.parse_args(argv)
    r = Report()

    print("Install")
    try:
        import AV_Spex
        installed = Path(AV_Spex.__file__).resolve()
        if REPO / "src" in installed.parents:
            r.ok("AV Spex runs from this repository", str(REPO))
            _quiet_avspex()
        else:
            r.fail("AV Spex is imported from somewhere else", str(installed),
                   f'pip install -e "{REPO}[dev]"   (runs are tied to this repository\'s code)')
    except ImportError as e:
        r.fail("AV Spex is not installed", str(e), f'pip install -e "{REPO}[dev]"')
    try:
        import openpyxl  # noqa: F401
        r.ok("openpyxl (review workbooks)")
    except ImportError:
        r.fail("openpyxl is missing", fix=f'pip install -e "{REPO}[dev]"')
    missing = [t for t in TOOLS if not shutil.which(t)]
    if missing:
        r.fail("not on PATH", ", ".join(missing), "brew install the missing tools (see the AV Spex README)")
    else:
        r.ok("external tools", ", ".join(TOOLS))
    if shutil.which("avspex-dev"):
        r.ok("avspex-dev is on PATH")
    else:
        r.warn("avspex-dev is not on PATH", fix=f"avspex-dev setup --link, or call {HERE / 'avspex-dev'}")

    print("Settings")
    corpus = settings.corpus()
    r.ok("corpus", f"{corpus} ({settings.corpus_source()})")
    r.ok("reviewer", f"{settings.reviewer()}" + ("" if settings.get("reviewer")[0] else " (login name; set one with setup)"))
    staging = settings.staging()
    if staging:
        if staging.is_dir():
            free = shutil.disk_usage(staging).free / 1e9
            (r.ok if free >= 50 else r.warn)("staging", f"{staging}, {free:.0f} GB free")
        else:
            r.warn("staging directory does not exist", str(staging), "create it, or pass --stage DIR to collect")

    print("Corpus")
    if not corpus.is_dir():
        r.fail("no corpus here", str(corpus), "avspex-dev setup")
        print(f"\n{r.failed} problem(s)")
        return 1
    if _git(corpus, "rev-parse", "--git-dir").returncode:
        r.fail("the corpus is not a git repository", fix="avspex-dev setup")
    else:
        if not (corpus / ".gitattributes").exists():
            r.warn("no .gitattributes", "two people's verdicts can conflict on merge", "avspex-dev setup adds it")
        dirty = [l for l in _git(corpus, "status", "--porcelain").stdout.splitlines() if l.strip()]
        remote = _git(corpus, "remote").stdout.split()
        if not remote:
            r.warn("no git remote", "your work stays on this machine",
                   "git remote add origin URL, then git push -u origin HEAD")
        else:
            fetched = True
            if not a.offline:
                try:
                    fetched = _git(corpus, "fetch", "--quiet", timeout=30).returncode == 0
                except subprocess.TimeoutExpired:
                    fetched = False
            counts = _git(corpus, "rev-list", "--left-right", "--count", "@{u}...HEAD")
            if counts.returncode:
                r.warn("this branch has no upstream", fix="git push -u origin HEAD")
            else:
                behind, ahead = (int(n) for n in counts.stdout.split())
                note = "" if fetched else " (could not reach the remote; as of the last fetch)"
                if behind:
                    r.warn("behind the remote", f"{behind} commit(s){note}", "git pull, before reviewing anything")
                else:
                    r.ok("up to date with the remote" + note, f"{ahead} commit(s) to push" if ahead else "")
        if dirty:
            r.warn("uncommitted changes", f"{len(dirty)} file(s)", "git add -A && git commit, then git push")

    import corpus as corpus_mod
    runs = [d for d in sorted((corpus / "runs").glob("*")) if (d / "run.json").exists()]
    local = sum(corpus_mod.has_sidecars(d) for d in runs)
    r.ok("runs", f"{len(runs)}: {local} with sidecars here, {len(runs) - local} records only")
    if runs and not local:
        r.warn("no run can be scored on this machine yet", fix="avspex-dev collect --baseline /path/to/tapes")

    env = {**os.environ, "AVSPEX_CORPUS": str(corpus)}
    for what, args in (("truth files", ["scorecard.py", "--check"]), ("ledger", ["ledger.py", "check"]),
                       ("bin label sheets", ["labels.py"])):
        res = subprocess.run([sys.executable, str(HERE / args[0]), *args[1:]], capture_output=True, text=True, env=env)
        if res.returncode == 0:
            r.ok(what)
        else:
            tail = (res.stderr.strip() or res.stdout.strip()).splitlines()[-3:]
            r.fail(f"{what} have problems" if what.endswith("s") else f"{what} has problems",
                   " | ".join(tail), f"avspex-dev {'score --check' if args[0] == 'scorecard.py' else args[0][:-3] + ' ' + ' '.join(args[1:])}".strip())

    print("AV Spex config")
    try:
        diffs = baseline_differences(corpus)
        run_id = corpus_mod.current_run_id()
        if diffs is None:
            r.warn("the corpus has no configs/baseline_checks.json", "nothing to compare the saved config with")
        elif diffs:
            r.warn("the saved config differs from the baseline", f"{len(diffs)} setting(s)",
                   "avspex-dev collect --baseline imports the baseline (your config is backed up first)")
            for d in diffs[:8]:
                print(f"         {d}")
            if len(diffs) > 8:
                print(f"         ... and {len(diffs) - 8} more")
        else:
            r.ok("the saved config matches the baseline")
        r.ok("a collect now would file under run", run_id)
    except SystemExit as e:
        r.fail("could not read the code or config state", str(e))

    print(f"\n{'ready' if not r.failed else f'{r.failed} problem(s) to fix'}")
    return 1 if r.failed else 0


# -------------------------------------------------------------------- collect ---

def backup_and_import_config(config):
    """Import an AV Spex config, after exporting the current one so it can be put back."""
    backup = settings.settings_dir() / f"config_before_{datetime.now():%Y-%m-%d_%H%M%S}.json"
    backup.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(["av-spex", "--export-config", "all", "--export-file", str(backup)],
                         capture_output=True, text=True)
    if res.returncode or not backup.exists():
        sys.exit(f"could not back up the saved config; nothing imported\n{res.stderr.strip()}")
    print(f"Saved config backed up to {backup}")
    print(f"    (put it back with: av-spex --import-config '{backup}')")
    res = subprocess.run(["av-spex", "--import-config", str(config)], capture_output=True, text=True)
    if res.returncode:
        sys.exit(f"could not import {config}\n{res.stderr.strip()}")
    print(f"Imported {config}")


def cmd_collect(argv):
    ap = argparse.ArgumentParser(prog="avspex-dev collect", description=(
        "Run AV Spex over every tape package (a folder named JPC_AV_<digits>) under SOURCE and keep "
        "the sidecars in the corpus as a run. The run ID comes from the code and the saved AV Spex "
        "config, so running the same command again resumes, and changing either starts a new run. "
        "AV Spex runs on the packages where they are and writes its outputs into them as usual; "
        "use --stage for packages on a slow or read-only drive."))
    ap.add_argument("sources", nargs="+", metavar="SOURCE", help="a package, or a directory holding packages")
    ap.add_argument("-l", "--label", help="what this run is for, shown by avspex-dev runs")
    cfg = ap.add_mutually_exclusive_group()
    cfg.add_argument("--baseline", action="store_true",
                     help="import the corpus's configs/baseline_checks.json first (so everyone's runs compare)")
    cfg.add_argument("-c", "--config", metavar="FILE", help="import this AV Spex config first")
    ap.add_argument("-g", "--ground-truth", action="store_true",
                    help="also a whole-file active-area signalstats pass per tape (period replays need it)")
    ap.add_argument("-i", "--images", action="store_true", help="keep thumbnails and pictures too")
    ap.add_argument("-n", "--dry-run", action="store_true", help="list what would be collected and stop")
    st = ap.add_argument_group("staged collection (collect.sh / cycle_avspex_batches.sh)")
    st.add_argument("--stage", nargs="?", const="", metavar="DIR",
                    help="copy packages to this local directory in batches, run there, copy outputs back "
                         "(DIR defaults to the staging directory saved by setup)")
    st.add_argument("-b", metavar="GB", help="with --stage: largest batch")
    st.add_argument("-r", metavar="GB", help="with --stage: free space to keep in reserve")
    st.add_argument("-w", action="store_true", help="with --stage: leave the original packages untouched")
    a = ap.parse_args(argv)

    corpus = require_corpus()
    os.environ["AVSPEX_CORPUS"] = str(corpus)
    config = corpus / "configs" / "baseline_checks.json" if a.baseline else a.config
    if config and not Path(config).expanduser().is_file():
        sys.exit(f"no such config: {config}")
    if a.stage is None and (a.b or a.r or a.w):
        sys.exit("-b, -r and -w only apply with --stage")

    if a.stage is not None:
        stage = a.stage or settings.staging()
        if not stage:
            sys.exit("--stage needs a directory (or save one: avspex-dev setup --staging DIR)")
        if config and not a.dry_run:
            backup_and_import_config(Path(config).expanduser())
        cmd = [str(HERE / "collect.sh"), "-s", str(stage)]
        for flag, value in (("-l", a.label), ("-b", a.b), ("-r", a.r)):
            if value:
                cmd += [flag, value]
        cmd += [f for f, on in (("-g", a.ground_truth), ("-i", a.images), ("-w", a.w), ("-n", a.dry_run)) if on]
        return subprocess.run([*cmd, *a.sources]).returncode

    import corpus as corpus_mod
    _quiet_avspex()
    pkgs = corpus_mod.discover_packages(a.sources)
    names = [p.name for p in pkgs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        sys.exit(f"the same package name appears twice in the sources: {', '.join(dupes)}")
    if not pkgs:
        sys.exit(f"no JPC_AV_<digits> folders under: {', '.join(a.sources)}")

    if a.dry_run:
        run_id = corpus_mod.current_run_id()
        note = " (as the config is now; importing a config would change it)" if config else ""
        done = set(corpus_mod.done(run_id)) if (corpus_mod.RUNS / run_id).exists() else set()
        print(f"Run: {run_id}{note}")
        for p in pkgs:
            print(f"  {'done   ' if p.name in done else 'collect'}  {p}")
        print(f"{sum(p.name not in done for p in pkgs)} to collect, {len(done & set(names))} already done")
        return 0

    # Keep the Mac awake for the whole run, as the staged script does
    if sys.platform == "darwin" and shutil.which("caffeinate") and not os.environ.get("CYCLE_CAFFEINATED"):
        os.environ["CYCLE_CAFFEINATED"] = "1"
        os.execvp("caffeinate", ["caffeinate", "-ims", sys.executable, str(HERE / "avspex-dev"), "collect", *argv])

    read_only = [p for p in pkgs if not os.access(p, os.W_OK)]
    if read_only:
        sys.exit(f"{len(read_only)} package(s) are not writable (first: {read_only[0]}). AV Spex writes its "
                 f"outputs into the package; use --stage DIR -w to work on local copies instead.")
    if not shutil.which("av-spex"):
        sys.exit("av-spex is not on PATH (avspex-dev doctor)")
    if config:
        backup_and_import_config(Path(config).expanduser())

    run_id = corpus_mod.init_run(a.label)
    diffs = baseline_differences(corpus)
    which = ("no baseline in this corpus" if diffs is None else "baseline config" if not diffs
             else f"NOT the baseline config ({len(diffs)} setting(s) differ; avspex-dev doctor lists them)")
    done = set(corpus_mod.done(run_id))
    pending = [p for p in pkgs if p.name not in done]
    print(f"Run: {run_id}   corpus: {corpus}   {which}")
    print(f"{len(pkgs)} package(s) found, {len(pkgs) - len(pending)} already collected, {len(pending)} to do")

    env = {**os.environ, "COLLECT_RUN_ID": run_id, "COLLECT_PYTHON": sys.executable}
    if a.ground_truth:
        env["COLLECT_GROUND_TRUTH"] = "1"
    if a.images:
        env["COLLECT_IMAGES"] = "1"
    ok, failed = 0, []
    try:
        for n, pkg in enumerate(pending, start=1):
            print(f"\n=== [{n}/{len(pending)}] {pkg.name}  ({datetime.now():%H:%M})")
            rc = subprocess.run([str(HERE / "collect_one.sh"), str(pkg)], env={**env, "CYCLE_SOURCE": str(pkg)}).returncode
            if rc == 3:
                print("\nStopped: the code or the saved config changed since this run started. The remaining "
                      "packages would belong to a different run. Start collect again to begin that run.")
                failed.append(pkg.name)
                break
            if rc:
                failed.append(pkg.name)
            else:
                ok += 1
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.")
        return 130
    print(f"\nRun {run_id}: {ok} collected, {len(failed)} failed"
          + (f" ({', '.join(failed)}; they are retried on the next collect)" if failed else ""))
    print(f"Next: avspex-dev sheet {run_id}")
    return 1 if failed else 0


# ----------------------------------------------------------------------- main ---

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(line_buffering=True)   # keep our lines in order with av-spex's when piped
    except (AttributeError, ValueError):
        pass
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    command, rest = argv[0], argv[1:]
    if command == "setup":
        return cmd_setup(rest)
    if command == "doctor":
        return cmd_doctor(rest)
    if command == "collect":
        return cmd_collect(rest)
    call = script_call(command, rest)
    if call is None:
        sys.exit(f"avspex-dev: no command named {command!r} (avspex-dev -h lists them)")
    if not any(h in rest for h in ("-h", "--help")):
        require_corpus()
    try:
        run_script(*call, prog=f"avspex-dev {command}")
    except BrokenPipeError:           # output piped into head, less, ...
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    return 0
