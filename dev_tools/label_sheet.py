#!/usr/bin/env python3
"""
label_sheet.py: a blind labeling sheet for weighing QCTools tags in period selection.

The corpus ground truth (active-area BRNG/TOUT/VREP) is the same signal period selection
already scores, so it cannot credit a tag that is not scored yet. An operator's eye can. This
picks ~12 bins per tape, each 10 s long:

  - random analyzable bins as controls (the base rate each tag's hit rate is compared against)
  - the bin where each candidate tag peaks (CANDIDATES, below: new tags and today's scored ones)
  - the evidence bins of today's selection (replayed at 6 x 30, the app default)
  - the 2nd-ranked bin of each new candidate, while room remains

and writes them to $AVSPEX_CORPUS/labels/ as

  sheet_<name>.csv   video_id, tc_in, tc_out (the file's own NDF/DF timecode, matching the NLE),
                     blind_id, and empty defect / severity / notes columns to fill in
  key_<name>.json    which selector(s) put each bin on the sheet. Don't open it while labeling.

Rows are sorted by tape and timecode so each tape is one pass through the NLE; nothing on the
sheet says why a bin is there. Bins already labeled in an earlier sheet (or seed_labels.csv)
are skipped, so a second round only asks about new bins.

    python3 dev_tools/label_sheet.py period-study-2026-10 --probe          # first time; drives mounted
    python3 dev_tools/label_sheet.py period-study-2026-10 --name round1

--round legality builds a smaller sheet aimed at one question: should the legal-floor luma
measures (_selection.add_legal_floor_columns: how far a bin falls below legal black or rises
above legal white) be scored? Per tape, up to --per-tape 6:

  - 1 random control
  - the top bin of under_black, over_white and BRNG (for comparison)
  - bins from --regions, a CSV of video_id,tc_in,tc_out an operator flagged as missed
  - the unlabeled evidence bins of the legality scoring configurations in tag_weights.CONFIGS
  - the 2nd and 3rd bins of under_black and over_white, while room remains

    python3 dev_tools/label_sheet.py period-study-2026-10 --round legality --name round2 \
        --regions ~/git/JPC_AV/avspex_corpus/labels/missed_regions.csv

--probe reads each package's start timecode from its source video (package.json "source") and
caches it in labels/timecode.json. Without a cached start timecode a tape's rows are written
from 00:00:00:00 and flagged in the notes column.

Filling in: defect is one of DEFECTS (separate several with '+', e.g. dropout+noise), severity 0
(nothing worth a period), 1 (visible) or 2 (obvious/serious), and origin one of ORIGINS (where the
problem comes from; leave it blank for none). A row left blank counts as unlabeled.
"""
import argparse
import csv
import json
import logging
import random
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "replays"))
from bin_table import find_packages, package_rows  # noqa: E402
from _selection import load_package  # noqa: E402

from AV_Spex.checks.qct_parse import _tc_format_timecode, _tc_parse_start_timecode
from AV_Spex.utils import ffprobe_probe
from AV_Spex.utils.log_setup import logger

import os
LABELS = Path(os.environ.get("AVSPEX_CORPUS", "~/git/JPC_AV/avspex_corpus")).expanduser() / "labels"
BIN = 10.0

DEFECTS = ("none", "dropout", "head_switching_tracking", "sync_loss_roll", "illegal_levels",
           "chroma_error", "noise", "signal_loss_freeze", "graphics_slate_only", "other")
# tape: this transfer/playback (dropout, tracking, sync). content: in the program as made (sub-black
# clothing, a still photo, a graphic with illegal levels). source: baked into dubbed or archival
# material (generation loss, head switching recorded in). Join with '+' when a bin has more than one.
ORIGINS = ("tape", "content", "source")

# (column in bin_table's output, direction): '+' means high is suspicious, '-' low.
# New tags chosen from the `bin_table.py --screen` of period-study-2026-10 (present in every
# report, not flat, not redundant with a scored metric, not a luma/motion proxy):
NEW_CANDIDATES = (
    ("udif_max", "+"),          # chroma temporal spike, U   (rho vs YDIF 0.44)
    ("vdif_max", "+"),          # chroma temporal spike, V   (0.45)
    ("ydif_max", "+"),          # single-frame luma jump: sync loss / roll, but also hard cuts
    ("psnr_psnr_y_min", "-"),   # worst inter-field PSNR: fields disagreeing
    ("satlow_mean", "+"),       # raised chroma floor (noise/chroma error); ~independent of luma/motion
    ("tout_max", "+"),          # TOUT spike, beside the scored mean
    ("brng_min", "+"),          # illegal levels sustained through the whole bin
)
# Today's scored metrics, so their weights can be judged on the same labels.
SCORED_CANDIDATES = (
    ("bp_brng_mean", "+"), ("bp_satmax_max", "+"), ("bp_tout_mean", "+"),
    ("bp_vrep_mean", "+"), ("bp_deflicker_absmax", "+"),
)
CANDIDATES = NEW_CANDIDATES + SCORED_CANDIDATES

# Round 2 (--round legality): the legal-floor luma columns, with BRNG as the yardstick.
LEGALITY_CANDIDATES = (("under_black", "+"), ("over_white", "+"), ("bp_brng_mean", "+"))
LEGALITY_CONFIGS = ("+under_black(leg)", "+over_white(leg)", "+legal_floor(leg)")


def round1_depth(col, d):
    return 2 if (col, d) in NEW_CANDIDATES else 1


def legality_depth(col, d):
    return 1 if col == "bp_brng_mean" else 3


def selector(col, direction):
    return f"{col}{direction}"


def ranked_bins(rows, col, direction):
    """Analyzable bins best-first for one tag; empty when the tag has no spread on this tape."""
    vals = [(r[col], r["bin_start"]) for r in rows if r.get(col) is not None]
    if len(vals) < 2 or min(v for v, _ in vals) == max(v for v, _ in vals):
        return []
    sign = -1 if direction == "+" else 1
    return [b for _, b in sorted(vals, key=lambda vb: (sign * vb[0], vb[1]))]


RANK_NAMES = {1: "2nd", 2: "3rd"}


def choose_bins(rows, per_tape, n_controls, rng, skip, candidates=CANDIDATES, depth=round1_depth,
                evidence=True, forced=()):
    """{bin_start: [selectors]} for one tape. Every selector a chosen bin qualifies for is
    recorded, so a bin picked as a control that is also a tag's peak counts for both.

    Fill order: controls, each candidate's top bin, then `forced` ((bin_start, selector) pairs:
    missed regions, configuration picks), today's evidence bins, and the deeper-ranked bins
    (`depth(col, d)` of them per candidate) while room remains."""
    rows = [r for r in rows if r["analyzable"]]
    pool = [r["bin_start"] for r in rows if r["bin_start"] not in skip]
    qualifies = {}
    tops, deeper = [], []
    for col, d in candidates:
        ranked = [b for b in ranked_bins(rows, col, d) if b not in skip]
        if ranked:
            qualifies.setdefault(ranked[0], []).append(f"top:{selector(col, d)}")
            tops.append(ranked[0])
        for i in range(1, min(depth(col, d), len(ranked))):
            qualifies.setdefault(ranked[i], []).append(f"{RANK_NAMES.get(i, f'{i + 1}th')}:{selector(col, d)}")
            deeper.append(ranked[i])
    ev = [r["bin_start"] for r in rows if r["evidence"] and r["bin_start"] not in skip] if evidence else []
    for b in ev:
        qualifies.setdefault(b, []).append("evidence")
    analyzable = {r["bin_start"] for r in rows}
    forced = [(b, sel) for b, sel in forced if b in analyzable and b not in skip]
    for b, sel in forced:
        qualifies.setdefault(b, []).append(sel)

    chosen = {}
    for b in rng.sample(pool, min(n_controls, len(pool))):
        chosen[b] = ["control"]
    for b in tops + [b for b, _ in forced] + ev + deeper:
        if len(chosen) >= per_tape:
            break
        chosen.setdefault(b, [])
    for b in chosen:
        chosen[b] = sorted(set(chosen[b] + qualifies.get(b, [])))
    return chosen


def probe_timecodes(pkgs, cache):
    for pkg in pkgs:
        info = json.loads((pkg / "package.json").read_text())
        vid = info["video_id"]
        if vid in cache:
            continue
        video = Path(info.get("source") or "") / (info.get("video_file") or "")
        if not video.is_file():
            print(f"{vid}: source video not reachable ({video}); start timecode not probed", file=sys.stderr)
            continue
        tc = ffprobe_probe.start_timecode(str(video))
        cache[vid] = {"start_timecode": tc, "fps": ffprobe_probe.frame_rate(str(video))}
        print(f"{vid}: start timecode {tc or 'none (00:00:00:00)'}", file=sys.stderr)
    return cache


def tc_to_seconds(tc, fps, start_frames=0, drop=None):
    """An NLE timecode (HH:MM:SS:FF NDF, HH:MM:SS;FF DF) as elapsed seconds into the file:
    the inverse of qct_parse._tc_format_timecode. `drop` is the tape's own drop-frame flag; it
    wins over the separator, since a hand-typed DF timecode often uses ':' throughout."""
    from AV_Spex.checks.qct_parse import _tc_df_to_frames
    nominal = max(1, round(fps or 30))
    if drop is None:
        drop = ";" in tc
    h, m, sec, f = (int(x) for x in tc.strip().replace(";", ":").split(":"))
    frames = (_tc_df_to_frames(h, m, sec, f, nominal) if drop
              else ((h * 60 + m) * 60 + sec) * nominal + f)
    return (frames - start_frames) / (fps or 29.97)


def region_bins(path, rows_by_tape, timing, candidates, per_region=2):
    """{video_id: [(bin_start, 'region')]}: from each flagged region, the bins the candidate
    tags rank worst (an operator flagged the whole span; these are the bins most worth a label)."""
    out = {}
    for reg in csv.DictReader(open(Path(path).expanduser())):
        vid = reg["video_id"].strip()
        if vid not in rows_by_tape:
            print(f"regions: {vid} not in this run; skipped", file=sys.stderr)
            continue
        fps, start_frames, drop = timing[vid]
        lo = tc_to_seconds(reg["tc_in"], fps, start_frames, drop)
        hi = tc_to_seconds(reg["tc_out"], fps, start_frames, drop)
        inside = [r for r in rows_by_tape[vid] if r["analyzable"] and r["bin_start"] + BIN > lo and r["bin_start"] < hi]
        # Round-robin over the measures: a region flagged for high Y should get over_white's
        # worst bin, not two of under_black's.
        rankings = [ranked_bins(inside, col, d) for col, d in candidates]
        best = []
        for depth in range(max((len(r) for r in rankings), default=0)):
            for ranking in rankings:
                if depth < len(ranking) and ranking[depth] not in best:
                    best.append(ranking[depth])
        if not best:     # nothing ranks inside (all zero): take the span's middle
            best = [r["bin_start"] for r in inside][len(inside) // 2:len(inside) // 2 + 1]
        out.setdefault(vid, []).extend((b, "region") for b in best[:per_region])
        print(f"regions: {vid} {reg['tc_in']}-{reg['tc_out']} = {lo:.0f}-{hi:.0f}s -> bins "
              f"{[int(b) for b in best[:per_region]]}", file=sys.stderr)
    return out


def config_picks(ctx, names, skip):
    """[(bin_start, 'pick:<config>')]: evidence bins that each named tag_weights configuration
    targets at 6 x 30 and nobody has labeled."""
    import tag_weights   # imports this module, so not at the top
    from _selection import score, scoring, select_periods
    from AV_Spex.checks import bin_scoring
    picks = []
    for name in names:
        metrics, weights = tag_weights.CONFIGS[name]
        with scoring(metrics, weights):
            scores = score(ctx)
            periods = select_periods(ctx, 6, 30, scores=scores)
        for p in periods:
            for e in bin_scoring.evidence_within(p[0], p[1], scores):
                if e.bin_start not in skip:
                    picks.append((e.bin_start, f"pick:{name}"))
    return picks


def labeled_bins():
    """(video_id, bin_start) already labeled, in any filled sheet or the seed file."""
    done = set()
    for sheet in LABELS.glob("sheet_*.csv"):
        key_path = sheet.with_name(sheet.name.replace("sheet_", "key_", 1)).with_suffix(".json")
        if not key_path.exists():
            continue
        key = json.loads(key_path.read_text())
        for row in csv.DictReader(open(sheet)):
            if (row.get("defect") or "").strip() and row["blind_id"] in key:
                k = key[row["blind_id"]]
                done.add((k["video_id"], float(k["bin_start"])))
    seed = LABELS / "seed_labels.csv"
    if seed.exists():
        for row in csv.DictReader(open(seed)):
            done.add((row["video_id"], float(row["bin_start"])))
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--name", default=date.today().isoformat())
    ap.add_argument("--round", choices=("tags", "legality"), default="tags",
                    help="tags: round 1's broad sheet; legality: the legal-floor luma round")
    ap.add_argument("--per-tape", type=int, default=None, help="default 12 (tags) / 6 (legality)")
    ap.add_argument("--controls", type=int, default=None, help="default 2 (tags) / 1 (legality)")
    ap.add_argument("--regions", default=None, help="CSV of video_id,tc_in,tc_out flagged as missed")
    ap.add_argument("--id-prefix", default=None, help="prefix for blind IDs (default 'L' for legality)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--probe", action="store_true", help="read start timecodes from the source videos")
    args = ap.parse_args()
    legality = args.round == "legality"
    per_tape = args.per_tape or (6 if legality else 12)
    n_controls = args.controls if args.controls is not None else (1 if legality else 2)
    prefix = args.id_prefix if args.id_prefix is not None else ("L" if legality else "")
    candidates = LEGALITY_CANDIDATES if legality else CANDIDATES
    depth = legality_depth if legality else round1_depth
    logger.setLevel(logging.ERROR)
    LABELS.mkdir(parents=True, exist_ok=True)

    pkgs = find_packages(args.runs)
    tc_path = LABELS / "timecode.json"
    tc_cache = json.loads(tc_path.read_text()) if tc_path.exists() else {}
    if args.probe:
        tc_cache = probe_timecodes(pkgs, tc_cache)
        tc_path.write_text(json.dumps(tc_cache, indent=2, sort_keys=True))

    sheet_path = LABELS / f"sheet_{args.name}.csv"
    key_path = LABELS / f"key_{args.name}.json"
    if sheet_path.exists():
        sys.exit(f"{sheet_path} exists; pick another --name (it may already hold labels)")

    skip = labeled_bins()
    loaded = []
    for pkg in pkgs:
        ctx = load_package(pkg)
        rows, _ = package_rows(ctx, 6, 30)
        tc = tc_cache.get(ctx.name) or {}
        fps = tc.get("fps") or ctx.meta.get("fps")
        start_frames, drop = _tc_parse_start_timecode(tc.get("start_timecode"), max(1, round(fps or 30)))
        loaded.append((ctx, rows, fps, start_frames, drop))
    regions = (region_bins(args.regions, {c.name: r for c, r, *_ in loaded},
                           {c.name: (f, sf, d) for c, _, f, sf, d in loaded}, candidates)
               if args.regions else {})

    sheet, key = [], {}
    for ctx, rows, fps, start_frames, drop in loaded:
        tape_skip = {b for v, b in skip if v == ctx.name}
        forced = list(regions.get(ctx.name, []))
        if legality:
            forced += config_picks(ctx, LEGALITY_CONFIGS, tape_skip)
        rng = random.Random(f"{args.seed}:{ctx.name}")
        chosen = choose_bins(rows, per_tape, n_controls, rng, tape_skip, candidates=candidates,
                             depth=depth, evidence=not legality, forced=forced)
        note = "" if ctx.name in tc_cache else "start TC not probed: positions from 00:00:00:00"
        for i, b in enumerate(sorted(chosen), 1):
            blind = f"{prefix}{ctx.name[-5:]}-{i:02d}"
            sheet.append({"video_id": ctx.name, "tc_in": _tc_format_timecode(b, fps, start_frames, drop),
                          "tc_out": _tc_format_timecode(b + BIN, fps, start_frames, drop),
                          "blind_id": blind, "defect": "", "severity": "", "origin": "", "notes": note})
            key[blind] = {"video_id": ctx.name, "bin_start": b, "selectors": chosen[b]}
        print(f"{ctx.name}: {len(chosen)} bins", file=sys.stderr)

    with open(sheet_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["video_id", "tc_in", "tc_out", "blind_id", "defect", "severity",
                                           "origin", "notes"])
        w.writeheader()
        w.writerows(sheet)
    key_path.write_text(json.dumps(key, indent=1))
    print(f"\nWrote {len(sheet)} bins to {sheet_path}\nKey (don't open while labeling): {key_path}")
    print(f"defect values: {', '.join(DEFECTS)}; severity 0/1/2; origin {', '.join(ORIGINS)}")


if __name__ == "__main__":
    main()
