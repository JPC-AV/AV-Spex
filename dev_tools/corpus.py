#!/usr/bin/env python3
"""
corpus.py: the developer results corpus that collect.sh fills and aggregate.py / replays read.

Each AV Spex batch run gets one run directory, keyed by the code and config that produced it:

    $AVSPEX_CORPUS/runs/<run_id>/
        run.json                  git commit, src/ diff hash, config hashes, versions, label
        checks_config.json        effective checks config at the start of the run
        spex_config.json          effective spex config at the start of the run
        src.diff                  uncommitted src/ changes, when there are any
        packages/<FOLDER>/
            package.json          written last: its presence means the package is collected
            <ID>_qc_metadata/...  sidecars the run wrote (+ the QCTools report it read),
            <ID>_report_csvs/...  same relative paths as in the package
            ground_truth/active_signalstats.txt.gz   optional whole-file pass

run_id = <commit>[-d<src diff hash>]-c<config hash>. The same code and config always map to
the same run directory, so re-running a batch resumes it, and any code or config change
starts a new one rather than overwriting the old results.

Subcommands (collect.sh calls most of these; you will mostly use `list`):

    corpus.py list                              runs, labels and package counts
    corpus.py init-run [--label L]              write run.json for the current code + config
    corpus.py check-run RUN_ID                  fail if code or config drifted from RUN_ID
    corpus.py done RUN_ID                       folder names already collected without error
    corpus.py collect-package RUN_ID PKG ...    copy one package's sidecars into the run
    corpus.py import SRC... --run-id ID         adopt outputs already on the drives as a run
"""
import argparse
import dataclasses
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CORPUS = Path(os.environ.get("AVSPEX_CORPUS", Path.home() / "git/JPC_AV/avspex_corpus")).expanduser()
RUNS = CORPUS / "runs"

# Sidecars worth keeping. Media, HTML reports and images are excluded by default: they are
# most of the bytes and nothing downstream reads them (pass --images to keep pictures).
KEEP_EXT = {".json", ".csv", ".txt", ".tsv", ".xml", ".gz", ".log"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}
MAX_BYTES = 100 * 1024 * 1024
# Inputs the run read rather than wrote. They are kept whatever their age because the
# replays need them, and a re-run reuses them instead of regenerating them.
INPUT_PATTERNS = ("*.qctools.xml.gz", "*.qctools.mkv", "*.audio_stats.xml.gz")


# ------------------------------------------------------------------ run identity ---

def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, check=True).stdout


def _short(data):
    return hashlib.sha1(data).hexdigest()[:7]


def code_state():
    """Commit plus a hash of uncommitted src/ changes (tracked diff + untracked files).
    Only src/ counts, so editing docs or dev_tools does not start a new run."""
    import AV_Spex
    installed = Path(AV_Spex.__file__).resolve()
    if REPO / "src" not in installed.parents:
        sys.exit(f"AV_Spex is imported from {installed}, not this repo's src/. "
                 f"Use the editable install (pip install -e {REPO}) so runs are tied to this code.")
    commit = _git("rev-parse", "HEAD").decode().strip()
    diff = _git("diff", "HEAD", "--binary", "--", "src")
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z", "--", "src").split(b"\0")
    for rel in sorted(u for u in untracked if u):
        diff += b"\0" + rel + b"\0" + (REPO / rel.decode()).read_bytes()
    return {
        "commit": commit,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
        "src_dirty": bool(diff),
        "src_diff_hash": _short(diff) if diff else None,
        "_diff": diff,
    }


def config_state():
    from AV_Spex.utils.config_manager import ConfigManager
    from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig
    mgr = ConfigManager()
    mgr.refresh_configs()
    configs = {name: dataclasses.asdict(mgr.get_config(name, cls))
               for name, cls in (("checks", ChecksConfig), ("spex", SpexConfig))}
    blob = json.dumps(configs, sort_keys=True).encode()
    return {"config_hash": _short(blob), "_configs": configs}


def current_run_id(code=None, config=None):
    code = code or code_state()
    config = config or config_state()
    rid = code["commit"][:7]
    if code["src_dirty"]:
        rid += f"-d{code['src_diff_hash']}"
    return f"{rid}-c{config['config_hash']}"


def _tool_version(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
        return out.splitlines()[0].strip() if out else None
    except Exception:
        return None


def init_run(label=None):
    code, config = code_state(), config_state()
    run_id = current_run_id(code, config)
    run_dir = RUNS / run_id
    (run_dir / "packages").mkdir(parents=True, exist_ok=True)
    run_json = run_dir / "run.json"
    if run_json.exists():
        info = json.loads(run_json.read_text())
        if label and label not in info.setdefault("labels", []):
            info["labels"].append(label)
        info.setdefault("sessions", []).append(_now())
    else:
        try:
            from importlib.metadata import version
            avspex_version = version("AV_Spex")
        except Exception:
            avspex_version = None
        info = {
            "run_id": run_id,
            "labels": [label] if label else [],
            "created": _now(),
            "sessions": [_now()],
            **{k: v for k, v in code.items() if not k.startswith("_")},
            "config_hash": config["config_hash"],
            "avspex_version": avspex_version,
            "python": platform.python_version(),
            "ffmpeg": _tool_version(["ffmpeg", "-version"]),
            "qcli": _tool_version(["qcli", "-version"]),
            "host": platform.node(),
        }
        for name, cfg in config["_configs"].items():
            (run_dir / f"{name}_config.json").write_text(json.dumps(cfg, indent=2))
        if code["_diff"]:
            (run_dir / "src.diff").write_bytes(code["_diff"])
    _write_json(run_json, info)
    return run_id


def check_run(run_id):
    """Code or config edited mid-batch would mix two runs under one ID."""
    now = current_run_id()
    if now != run_id:
        sys.exit(f"code or config changed since the run started: now {now}, run is {run_id}")


# ----------------------------------------------------------------- packages ---

def package_id(pkg):
    """The ID the package's files carry. Usually the folder name, but not always
    (JPC_AV_20241/ holds JPC_AV_02041_* files)."""
    pkg = Path(pkg)
    if (pkg / f"{pkg.name}.mkv").exists():
        return pkg.name
    vids = sorted(p for p in pkg.glob("JPC_AV_*.mkv") if not p.name.startswith("._")
                  and "_access" not in p.name)
    if vids:
        return vids[0].stem
    qcs = [q for q in pkg.glob("JPC_AV_*_qc_metadata") if q.is_dir()]
    return qcs[0].name[:-len("_qc_metadata")] if len(qcs) == 1 else pkg.name


def find_video(pkg, vid):
    pkg = Path(pkg)
    if (pkg / f"{vid}.mkv").exists():
        return pkg / f"{vid}.mkv"
    for ext in ("mkv", "mov", "mxf"):
        for p in sorted(pkg.glob(f"*.{ext}")):
            if not p.name.startswith("._") and "_access" not in p.name:
                return p
    return None


def video_properties(video):
    """As frame analysis saw them (probe_video_properties), so replays need no video."""
    from AV_Spex.checks.frame_analysis import probe_video_properties
    props = probe_video_properties(str(video)) or {}
    return {k: props.get(k) for k in ("width", "height", "fps", "total_frames", "duration")}


def qctools_report(pkg_dir, vid):
    """Relative path of the QCTools report, the way find_qctools_report() looks for it."""
    for sub in (f"{vid}_qc_metadata", f"{vid}_vrecord_metadata"):
        for pattern in ("*.qctools.xml.gz", "*.qctools.mkv"):
            hits = sorted(p for p in (Path(pkg_dir) / sub).glob(pattern) if not p.name.startswith("._"))
            if hits:
                return str(hits[0].relative_to(pkg_dir))
    return None


def _wanted(path, rel, since, images):
    if path.name.startswith("._") or path.name == ".DS_Store":
        return False
    if any(path.match(p) for p in INPUT_PATTERNS):
        return True
    if since is not None and path.stat().st_mtime < since:
        return False
    ext = path.suffix.lower()
    return (ext in KEEP_EXT or (images and ext in IMAGE_EXT)) and path.stat().st_size <= MAX_BYTES


def collect_package(run_id, pkg, started=None, log_offset=0, exit_code=None, avspex_seconds=None,
                    source=None, images=False, ground_truth=None):
    """Copy what this run wrote (mtime >= started) plus the inputs replays need.
    package.json goes last, so a half-copied package is never mistaken for a done one."""
    pkg = Path(pkg)
    vid = package_id(pkg)
    out = RUNS / run_id / "packages" / pkg.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "package.json").unlink(missing_ok=True)
    # A retry replaces an earlier attempt's sidecars. The ground truth was just written by
    # collect_one.sh for this attempt, so it stays.
    for old in out.iterdir():
        if old.name != "ground_truth":
            shutil.rmtree(old) if old.is_dir() else old.unlink()

    since = started - 1 if started else None
    log_rel = f"{vid}_qc_metadata/{vid}_avspex_processing.log"
    files = []
    for sub in sorted(p for p in pkg.iterdir() if p.is_dir() and p.name.startswith(vid)):
        for path in sorted(sub.rglob("*")):
            rel = str(path.relative_to(pkg))
            if not path.is_file() or rel == log_rel or not _wanted(path, rel, since, images):
                continue
            dest = out / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            files.append(rel)

    # The processing log appends across runs; keep only this run's part of it
    log = pkg / log_rel
    if log.exists():
        dest = out / log_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "rb") as src, open(dest, "wb") as dst:
            src.seek(log_offset if log_offset <= log.stat().st_size else 0)
            shutil.copyfileobj(src, dst)
        files.append(log_rel)

    video = find_video(pkg, vid)
    info = {
        "folder": pkg.name,
        "video_id": vid,
        "run_id": run_id,
        "source": source,
        "video_file": video.name if video else None,
        "video": video_properties(video) if video else None,
        "qctools_report": qctools_report(out, vid),
        "avspex_exit": exit_code,
        "avspex_seconds": avspex_seconds,
        "frame_analysis_seconds": frame_analysis_seconds(out / log_rel),
        "ground_truth": ground_truth,
        "collected": _now(),
        "files": sorted(files),
    }
    _write_json(out / "package.json", info)
    return out


def frame_analysis_seconds(log_path):
    """Seconds between the last frame-analysis start and its summary in the log."""
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return None
    stamp = lambda l: datetime.strptime(l[:23], "%Y-%m-%d %H:%M:%S,%f")
    starts = [i for i, l in enumerate(lines) if "Frame analysis configuration:" in l]
    if not starts:
        return None
    ends = [i for i, l in enumerate(lines) if i > starts[-1] and "Enhanced Frame Analysis Summary" in l]
    try:
        return round((stamp(lines[ends[0]]) - stamp(lines[starts[-1]])).total_seconds()) if ends else None
    except ValueError:
        return None


def done(run_id):
    pkgs = RUNS / run_id / "packages"
    for pj in sorted(pkgs.glob("*/package.json")):
        info = json.loads(pj.read_text())
        if info.get("avspex_exit") == 0:
            yield info["folder"]


# ------------------------------------------------------------------- import ---

def import_existing(sources, run_id, label, images=False):
    """Adopt outputs already sitting next to the videos (e.g. the October 2026 period study)
    as a run. The code and config that made them are not recoverable, so the run ID is
    given by hand and run.json says so. Everything in the output folders is taken, so stale
    sidecars from older runs come along too; prefer a fresh collect.sh run where possible."""
    run_dir = RUNS / run_id
    (run_dir / "packages").mkdir(parents=True, exist_ok=True)
    if not (run_dir / "run.json").exists():
        _write_json(run_dir / "run.json", {"run_id": run_id, "labels": [label] if label else [],
                                           "created": _now(), "imported": True,
                                           "commit": None, "config_hash": None})
    pkgs = []
    for src in map(Path, sources):
        pkgs += [src] if re.fullmatch(r"JPC_AV_\d+", src.name) else sorted(
            d for d in src.rglob("JPC_AV_*") if d.is_dir() and re.fullmatch(r"JPC_AV_\d+", d.name))
    for pkg in pkgs:
        vid = package_id(pkg)
        qc = pkg / f"{vid}_qc_metadata"
        if not (qc / f"{vid}_enhanced_frame_analysis.json").exists():
            print(f"{pkg.name}: no frame-analysis JSON, skipped")
            continue
        out = collect_package(run_id, pkg, exit_code=0, source=str(pkg), images=images)
        info = json.loads((out / "package.json").read_text())
        # The period study kept its extras next to the outputs; move them to corpus names.
        meta = qc / f"{vid}_period_study_meta.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            info["video"] = info["video"] or {k: m.get(k) for k in ("width", "height", "fps",
                                                                    "total_frames", "duration")}
            info["avspex_seconds"] = m.get("avspex_seconds")
            info["frame_analysis_seconds"] = m.get("frame_analysis_seconds")
            if (out / f"{vid}_qc_metadata/{vid}_gt_active_signalstats.txt.gz").exists():
                (out / "ground_truth").mkdir(exist_ok=True)
                os.replace(out / f"{vid}_qc_metadata/{vid}_gt_active_signalstats.txt.gz",
                           out / "ground_truth/active_signalstats.txt.gz")
                info["ground_truth"] = {"crop": m.get("crop"), "seconds": m.get("gt_seconds")}
        info["files"] = sorted(str(p.relative_to(out)) for p in out.rglob("*")
                               if p.is_file() and p.name != "package.json")
        _write_json(out / "package.json", info)
        print(f"{pkg.name}: imported ({len(info['files'])} files)")


# -------------------------------------------------------------------- utils ---

def _now():
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path, data):
    tmp = Path(str(path) + ".partial")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def resolve_run(arg):
    p = Path(arg).expanduser()
    if (p / "run.json").exists():
        return p
    hits = [d for d in RUNS.glob(f"{arg}*") if (d / "run.json").exists()] if RUNS.exists() else []
    if not hits:
        sys.exit(f"{arg}: no such run (see corpus.py list)")
    if len(hits) > 1:
        sys.exit(f"{arg}: ambiguous, matches {', '.join(h.name for h in hits)}")
    return hits[0]


def list_runs():
    if not RUNS.exists():
        print(f"no runs in {CORPUS}")
        return
    for run_dir in sorted(RUNS.iterdir(), key=lambda p: p.stat().st_mtime):
        rj = run_dir / "run.json"
        if not rj.exists():
            continue
        info = json.loads(rj.read_text())
        pkgs = list((run_dir / "packages").glob("*/package.json"))
        ok = sum(json.loads(p.read_text()).get("avspex_exit") == 0 for p in pkgs)
        print(f"{info['run_id']:<32} {info.get('created', ''):<20} {ok:>3}/{len(pkgs):<3} packages  "
              f"{', '.join(info.get('labels') or [])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p = sub.add_parser("init-run")
    p.add_argument("--label")
    sub.add_parser("run-id")
    p = sub.add_parser("check-run")
    p.add_argument("run_id")
    p = sub.add_parser("done")
    p.add_argument("run_id")
    p = sub.add_parser("collect-package")
    p.add_argument("run_id")
    p.add_argument("pkg")
    p.add_argument("--started", type=float)
    p.add_argument("--log-offset", type=int, default=0)
    p.add_argument("--exit-code", type=int)
    p.add_argument("--avspex-seconds", type=int)
    p.add_argument("--source")
    p.add_argument("--images", action="store_true")
    p.add_argument("--ground-truth-json", help="JSON describing a ground-truth pass already written")
    p = sub.add_parser("import")
    p.add_argument("sources", nargs="+")
    p.add_argument("--run-id", required=True)
    p.add_argument("--label")
    p.add_argument("--images", action="store_true")
    args = ap.parse_args()

    if args.cmd in ("init-run", "run-id", "check-run", "collect-package", "import"):
        # Selection code and config loading log heavily; keep this tool's output readable
        import logging
        from AV_Spex.utils.log_setup import logger
        logger.setLevel(logging.ERROR)

    if args.cmd == "list":
        list_runs()
    elif args.cmd == "init-run":
        print(init_run(args.label))
    elif args.cmd == "run-id":
        print(current_run_id())
    elif args.cmd == "check-run":
        check_run(args.run_id)
    elif args.cmd == "done":
        print("\n".join(done(args.run_id)))
    elif args.cmd == "collect-package":
        gt = json.loads(args.ground_truth_json) if args.ground_truth_json else None
        out = collect_package(args.run_id, args.pkg, args.started, args.log_offset, args.exit_code,
                              args.avspex_seconds, args.source, args.images, gt)
        print(out)
    elif args.cmd == "import":
        import_existing(args.sources, args.run_id, args.label, args.images)


if __name__ == "__main__":
    main()
