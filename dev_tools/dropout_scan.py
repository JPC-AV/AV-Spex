#!/usr/bin/env python3
"""
dropout_scan.py: run the video dropout streak scan over whole tapes and cache every loose run.

The scan (src/AV_Spex/checks/video_dropout_detection.py) has to read the picture, which is the
slow part (about 0.2x real time). It runs once per tape at deliberately loose thresholds, and
every run it finds lands in the tape's corpus package, so the operating point can be tuned
offline against operator verdicts as often as needed:

    <package>/dropout_scan/
        scan.json         params, code hash, video, fps, frames scanned, seconds, avoid spans
        streaks.csv.gz    one row per run: frame, time, line, x, length, polarity, spatial,
                          temporal, level, flatness
        frames.csv.gz     one row per frame with any candidate pixel: frame, time, n_runs,
                          candidate_pixels (n_runs counts every run, even past the per-frame cap)

The avoid spans are the ones period selection uses (black, bars, unanalyzable; rebuilt by
replays/_selection.py from the package's QCTools report), recorded so later steps can skip them
without loading the report again. A tape whose scan.json matches the current code and params is
skipped unless --force.

    python3 dev_tools/dropout_scan.py period-study-2026-10 \\
        ~/git/JPC_AV/sample_files/jpc/JPC_AV_02213/JPC_AV_02213.mkv ... [-j 6]

Videos are matched to packages by the video ID in their file name.
"""
import argparse
import csv
import gzip
import hashlib
import json
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "replays"))

from corpus import resolve_run  # noqa: E402
from AV_Spex.checks import video_dropout_detection as vdd  # noqa: E402

SCAN_DIR = "dropout_scan"
STREAK_COLS = ("frame", "time", "line", "x", "length", "polarity", "spatial", "temporal",
               "level", "flatness")
FRAME_COLS = ("frame", "time", "n_runs", "candidate_pixels")


def code_hash():
    return hashlib.sha256(Path(vdd.__file__).read_bytes()).hexdigest()[:12]


def package_for(run, video):
    vid = Path(video).stem
    for pj in (run / "packages").glob("*/package.json"):
        if json.loads(pj.read_text()).get("video_id") == vid:
            return pj.parent
    return None


def avoid_spans(pkg):
    try:
        import _selection
        return [list(map(float, s)) for s in _selection.load_package(pkg).avoid]
    except Exception as exc:     # no frame-analysis JSON, no report: scan anyway
        print(f"{pkg.name}: avoid spans unavailable ({exc}); none recorded", file=sys.stderr)
        return []


def scan_one(job):
    pkg, video, params, force = job
    out = pkg / SCAN_DIR
    meta_path = out / "scan.json"
    pdict = vdd.params_dict(params)
    if meta_path.exists() and not force:
        meta = json.loads(meta_path.read_text())
        if meta.get("code_hash") == code_hash() and meta.get("params") == pdict and meta.get("complete"):
            return f"{pkg.name}: up to date, skipped"
    v = json.loads((pkg / "package.json").read_text())["video"]
    out.mkdir(exist_ok=True)
    t0 = time.time()
    frames = runs = 0
    with gzip.open(out / "streaks.csv.gz", "wt", newline="") as sf, \
            gzip.open(out / "frames.csv.gz", "wt", newline="") as ff:
        sw, fw = csv.writer(sf), csv.writer(ff)
        sw.writerow(STREAK_COLS)
        fw.writerow(FRAME_COLS)
        for fs in vdd.scan_video(str(video), v["width"], v["height"], v["fps"], params):
            frames += 1
            if fs.candidate_pixels:
                fw.writerow((fs.index, f"{fs.time:.4f}", fs.n_runs, fs.candidate_pixels))
            for r in fs.runs:
                runs += 1
                sw.writerow((fs.index, f"{fs.time:.4f}", r.line, r.x, r.length, r.polarity,
                             f"{r.spatial:.1f}", f"{r.temporal:.1f}", f"{r.level:.1f}",
                             f"{r.flatness:.1f}"))
    meta = {
        "video": str(video), "video_id": Path(video).stem, "fps": v["fps"],
        "width": v["width"], "height": v["height"], "frames_scanned": frames,
        "runs_written": runs, "seconds": round(time.time() - t0, 1),
        "params": pdict, "code_hash": code_hash(), "avoid": avoid_spans(pkg),
        "scanned": time.strftime("%Y-%m-%dT%H:%M:%S"), "complete": True,
    }
    meta_path.write_text(json.dumps(meta, indent=1))
    return (f"{pkg.name}: {frames} frames, {runs} runs, {meta['seconds']:.0f}s "
            f"({meta['seconds'] / max(frames / v['fps'], 1):.2f}x real time)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("-j", "--jobs", type=int, default=4)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    run = resolve_run(args.run)
    jobs = []
    for video in args.videos:
        video = Path(video).expanduser()
        pkg = package_for(run, video)
        if not video.is_file() or pkg is None:
            print(f"{video}: {'not a file' if not video.is_file() else 'no package in ' + run.name}; skipped",
                  file=sys.stderr)
            continue
        jobs.append((pkg, video, vdd.StreakParams(), args.force))
    with Pool(min(args.jobs, len(jobs) or 1)) as pool:
        for msg in pool.imap_unordered(scan_one, jobs):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
