#!/usr/bin/env python3
"""
inject.py: measure what a detector misses, by putting known faults into clips of real tapes.

Review sheets measure how much of what a detector reports is real. They cannot measure what it
misses, and the detectors that almost never fire (dropped samples, tone leak, duplicate
frames) give a sheet nothing to judge. Here the faults are made, so where they are is known:

    avspex-dev inject dropped-samples [RUN]     splices and digital-silence gaps
    avspex-dev inject tone-leak [RUN]           a leaked 1 kHz reference tone, at a range of levels
    avspex-dev inject duplicate-frames [RUN]    freezes of 1 to 15 repeated frames (needs qcli; slow)

For each tape, a clip of its programme audio is read off the drive and the detector is run on
it three ways: untouched (the control: anything reported there was already on the tape), and
with faults injected at recorded positions and sizes. The result is how many of the injected
faults were found, broken down by how severe they were, and whether injecting them made the
detector report anything else.

    --tapes ID ...     these tapes (default: -n tapes picked from the run by --seed)
    -n N               how many tapes (default 8)
    --at SECONDS       where the clip starts (default: 40% of the way through the tape)
    --seconds S        clip length (default 120; tone leak 128 and needs at least 64;
                       duplicate frames 45, and takes about three times that per tape)
    --keep DIR         keep the clean and injected clips there, to listen to

What it does and does not show. An injected fault is the fault's defining feature in its
plainest form: an exact butt joint, exact zeros, a steady sine. A miss here is a real miss. A
hit says less: real faults are messier (a real leak wobbles, a real splice can land in a
fade), so recall on real tapes will be at or below these figures, not above them.

Tapes held out for the detector in the ledger are never used. Running the detector on a clip
is looking at that tape's results for the detector, so the tapes used are listed with the
`avspex-dev ledger add` command that records them as dev.

Results go to out/inject/<fault>-<code>-s<seed>/ in the corpus (not tracked): faults.csv has
one row per injected fault with its size and whether it was found.
"""
import argparse
import csv
import json
import os
import random
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from multiprocessing import get_context
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _inject  # noqa: E402
import ledger  # noqa: E402
import scorecard as sc  # noqa: E402
from corpus import CORPUS, code_state, resolve_run  # noqa: E402


def recall_table(rows, key, order=None, label=""):
    """Lines of a recall table over faults grouped by key(row)."""
    groups = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r["found"])
    names = [n for n in (order or sorted(groups)) if n in groups]
    out = [f"  {label:<28} {'injected':>8} {'found':>6} {'recall':>7}"]
    for n in names:
        found = sum(groups[n])
        out.append(f"  {str(n):<28} {len(groups[n]):>8} {found:>6} {found / len(groups[n]):>6.0%}")
    return out


STAND_OUT = ("under 6 dB", "6 to 12 dB", "12 to 20 dB", "20 to 30 dB", "30 dB or more")


def stand_out_bin(db):
    return STAND_OUT[sum(db >= edge for edge in (6, 12, 20, 30))]


LEVELS = ("under -50 dBFS (quiet)", "-50 to -30 dBFS", "-30 dBFS or louder")


def level_bin(db):
    return LEVELS[sum(db >= edge for edge in (-50, -30))]


def report_dropped_samples(faults):
    splices = [f for f in faults if f["kind"] == "splice"]
    gaps = [f for f in faults if f["kind"] == "gap"]
    lines = ["", "Splices: audio cut out, the two ends butted together", ""]
    lines += recall_table(splices, lambda f: stand_out_bin(f["stands_out_db"]), STAND_OUT,
                          "step over the programme")
    lines += [""] + recall_table(splices, lambda f: level_bin(f["level_dbfs"]), LEVELS, "programme level at the cut")
    lines += [""] + recall_table(splices, lambda f: f"{f['cut_frames']:g} frame(s)",
                                 [f"{n:g} frame(s)" for n in _inject.SPLICE_FRAMES], "audio removed")
    lines += ["", "Silence gaps: every channel exact zero", ""]
    lines += recall_table(gaps, lambda f: f"{f['gap_ms']} ms", [f"{ms} ms" for ms in _inject.GAP_MS], "gap length")
    lines += [""] + recall_table(gaps, lambda f: level_bin(f["level_dbfs"]), LEVELS, "programme level before it")
    as_other = sum(f["found"] and f["found_as"] != f["kind"] for f in faults)
    if as_other:
        lines += ["", f"  {as_other} fault(s) were found, but reported as the other kind (a gap as a splice, or the reverse)"]
    return lines


def report_tone_leak(faults):
    lines = []
    for what in ("comb", "1 kHz only"):
        rows = [f for f in faults if f["kind"] == what]
        title = ("1 kHz with harmonics at 2, 3, 4 and 6 kHz (a leaked reference tone)" if what == "comb"
                 else "1 kHz alone, no harmonics")
        lines += ["", title, ""]
        lines += recall_table(rows, lambda f: f"{f['level_db']} dBFS", [f"{db} dBFS" for db in _inject.TONE_LEVELS_DB],
                              "level of each tone (peak)")
    return lines


MOTION = ("still (under 1 code)", "slow (1 to 3 codes)", "moving (3 codes or more)")


def report_duplicate_frames(faults):
    lines = []
    for kind, title in (("identical", "Bit-identical repeats (the capture repeated a frame)"),
                        ("re-digitized", "Re-digitized repeats (the deck froze: same picture, fresh noise)")):
        rows = [f for f in faults if f["kind"] == kind]
        lines += ["", title, ""]
        lines += recall_table(rows, lambda f: f"{f['repeats']} repeat(s)",
                              [f"{n} repeat(s)" for n in _inject.DUPLICATE_RUNS], "length of the freeze")
        lines += [""] + recall_table(rows, lambda f: MOTION[sum(f["motion"] >= e for e in (1, 3))], MOTION,
                                     "picture change it hides")
    if faults:
        lines += ["", f"  The detector was run with duplicate_min_run_length {faults[0]['min_run_length']}: "
                      f"it reports runs of that many repeats or more, and shorter freezes not at all."]
    return lines


REPORTS = {"dropped-samples": report_dropped_samples, "tone-leak": report_tone_leak,
           "duplicate-frames": report_duplicate_frames}


def choose_tapes(run, topic, wanted, n, seed):
    """{video_id: video path} for the tapes to use: never held-out ones, only ones whose video
    is on a mounted drive."""
    held = ledger.reserved(topic)
    pool = {}
    for vid, path in sc.packages(run).items():
        info = json.loads((path / "package.json").read_text())
        video = Path(info.get("source") or "") / (info.get("video_file") or "")
        duration = (info.get("video") or {}).get("duration")
        if video.is_file() and duration:
            pool[vid] = (video, duration)
    if wanted:
        bad = [t for t in wanted if t in held]
        if bad:
            sys.exit(f"held out for {topic} in the ledger, not to be looked at: {', '.join(bad)}")
        missing = [t for t in wanted if t not in pool]
        if missing:
            sys.exit(f"not in {run.name}, or the video is not on a mounted drive: {', '.join(missing)}")
        return {t: pool[t] for t in wanted}
    usable = sorted(v for v in pool if v not in held)
    if not usable:
        sys.exit(f"no tape of {run.name} has its video on a mounted drive (package.json records where each was)")
    return {v: pool[v] for v in sorted(random.Random(seed).sample(usable, min(n, len(usable))))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fault", choices=sorted(_inject.RUNNERS))
    ap.add_argument("run", nargs="?", help="run ID, prefix or 'latest' (default): where the tapes' paths come from")
    ap.add_argument("--tapes", nargs="+", metavar="ID")
    ap.add_argument("-n", type=int, default=8, help="tapes to use when --tapes is not given")
    ap.add_argument("--seed", type=int, default=int(date.today().strftime("%Y%m%d")),
                    help="picks the tapes and the fault positions (default: today's date)")
    ap.add_argument("--at", type=float, metavar="SECONDS", help="clip start (default: 40%% through the tape)")
    ap.add_argument("--seconds", type=float, help="clip length (default 120; tone leak 128; duplicate frames 45)")
    ap.add_argument("--keep", metavar="DIR", help="keep the clean and injected clips here")
    ap.add_argument("--min-run-length", type=int, metavar="N",
                    help="duplicate-frames only: try the detector with this duplicate_min_run_length "
                         "instead of the bundled default")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 2))
    args = ap.parse_args()

    import logging
    from AV_Spex.utils.log_setup import logger
    logger.setLevel(logging.ERROR)

    args.seconds = args.seconds or _inject.DEFAULT_SECONDS[args.fault]
    run = resolve_run(args.run or "latest")
    topic = _inject.TOPICS[args.fault]
    tapes = choose_tapes(run, topic, args.tapes, args.n, args.seed)
    code = code_state()
    tag = code["commit"][:7] + (f"-d{code['src_diff_hash']}" if code["src_dirty"] else "")
    dest = CORPUS / "out" / "inject" / f"{args.fault}-{tag}-s{args.seed}"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Injecting {args.fault} faults into {len(tapes)} tape(s) of {run.name}, code {tag}, seed {args.seed}")

    if args.min_run_length and args.fault != "duplicate-frames":
        sys.exit("--min-run-length only applies to duplicate-frames")
    if args.min_run_length:
        dest = dest.with_name(dest.name + f"-minrun{args.min_run_length}")
        dest.mkdir(parents=True, exist_ok=True)
    jobs = [{"video_id": vid, "video": str(video), "seed": args.seed, "seconds": args.seconds,
             "min_run_length": args.min_run_length,
             "start": args.at if args.at is not None else max(0.0, min(duration * 0.4, duration - args.seconds)),
             "keep": str(Path(args.keep).expanduser() / vid) if args.keep else None}
            for vid, (video, duration) in tapes.items()]
    faults, failed, control, false_alarms = [], [], 0, 0
    with ProcessPoolExecutor(max_workers=max(1, min(args.jobs, len(jobs))), mp_context=get_context("spawn")) as pool:
        for n, fut in enumerate(as_completed([pool.submit(_inject.RUNNERS[args.fault], j) for j in jobs]), start=1):
            r = fut.result()
            line = f"  [{n}/{len(jobs)}] {r['video_id']}  {r['seconds']:.0f}s"
            if r["ok"]:
                found = sum(f["found"] for f in r["faults"])
                line += f"  {found} of {len(r['faults'])} found; control {r['control']}, other reports {r['false_alarms']}"
                faults += r["faults"]
                control += r["control"]
                false_alarms += r["false_alarms"]
            else:
                failed.append(r["video_id"])
                line += f"  FAILED: {r['error']}"
            print(line, flush=True)
    if not faults:
        sys.exit("nothing was injected")

    for line in REPORTS[args.fault](faults):
        print(line)
    print(f"\nControl: {control} report(s) on the untouched clips (already on the tapes, not counted above)")
    print(f"Reports the injected faults do not account for: {false_alarms}")

    fields = list(dict.fromkeys(k for f in faults for k in f))
    fields.insert(0, fields.pop(fields.index("video_id")))
    with open(dest / "faults.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(faults)
    (dest / "run.json").write_text(json.dumps({
        "fault": args.fault, "run": run.name, "commit": code["commit"], "src_dirty": code["src_dirty"],
        "src_diff_hash": code["src_diff_hash"], "seed": args.seed, "seconds": args.seconds,
        "clips": {j["video_id"]: round(j["start"], 1) for j in jobs}, "failed": failed}, indent=2))
    print(f"\n{len(faults)} fault(s) -> {dest / 'faults.csv'}")

    uses = ledger.by_tape(ledger.read_rows())
    new = sorted(v for v in tapes if v not in failed and ledger.status(uses.get(v, {}).get(topic)) == "fresh")
    if new:
        print(f"\n{len(new)} of these tapes had never been looked at for {topic}. Record them:\n"
              f"  avspex-dev ledger add {topic} dev {' '.join(new)} --source \"inject {args.fault}, seed {args.seed}\"")
    if failed:
        print(f"\n{len(failed)} tape(s) failed: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
