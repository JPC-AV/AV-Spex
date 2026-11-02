#!/usr/bin/env python3
"""
label_sheet.py: a blind labeling sheet for weighing QCTools tags in period selection.

The corpus ground truth (active-area BRNG/TOUT/VREP) is the same signal period selection
already scores, so it cannot credit a tag that is not scored yet. An operator's eye can. This
picks ~12 bins per tape, each 10 s long:

  - random analyzable bins as controls (the base rate each tag's hit rate is compared against)
  - the bin where each candidate tag peaks (study.CANDIDATES: new tags and today's scored ones)
  - the evidence bins of today's selection (replayed at 6 x 30, the app default)
  - the 2nd-ranked bin of each new candidate, while room remains

and writes them to $AVSPEX_CORPUS/labels/ as

  sheet_<name>.csv   video_id, tc_in, tc_out (the file's own NDF/DF timecode, matching the NLE),
                     blind_id, and empty defect / severity / origin / notes columns to fill in
                     (labels.SHEET_COLUMNS)
  key_<name>.json    which selector(s) put each bin on the sheet. Don't open it while labeling.

Rows are sorted by tape and timecode so each tape is one pass through the NLE; nothing on the
sheet says why a bin is there. Bins already labeled in an earlier sheet (or seed_labels.csv)
are skipped, so a second round only asks about new bins. The sheets must pass labels.py's
check first.

    python3 dev_tools/label_sheet.py period-study-2026-10 --probe          # first time; drives mounted
    python3 dev_tools/label_sheet.py period-study-2026-10 --name round1

--round legality builds a smaller sheet aimed at one question: should the legal-floor luma
measures (_selection.add_legal_floor_columns: how far a bin falls below legal black or rises
above legal white) be scored? Per tape, up to --per-tape 6:

  - 1 random control
  - the top bin of under_black, over_white and BRNG (for comparison)
  - bins from --regions, a CSV of video_id,tc_in,tc_out an operator flagged as missed
  - the unlabeled evidence bins of the legality scoring configurations (study.LEGALITY_CONFIGS)
  - the 2nd and 3rd bins of under_black and over_white, while room remains

    python3 dev_tools/label_sheet.py period-study-2026-10 --round legality --name round2 \
        --regions ~/git/JPC_AV/avspex_corpus/labels/missed_regions.csv

--bins FILE builds a sheet of exactly the bins in FILE (video_id,bin_start, e.g. the
tag_weights_<MODE>_stake_bins.csv that `tag_weights.py --stake` writes), plus --controls random
controls per tape (default 1), so a round asks only about bins that can change a verdict:

    python3 dev_tools/label_sheet.py period-study-2026-10 --bins tag_weights_tape_stake_bins.csv \
        --name round3

--probe reads each package's start timecode from its source video (package.json "source") and
caches it in labels/timecode.json. Without a cached start timecode a tape's rows are written
from 00:00:00:00 and flagged in the notes column.

Filling in: see labels.py. In short, defect is one of labels.DEFECTS (several joined with '+',
e.g. dropout+noise), severity 0 (nothing worth a period), 1 (visible) or 2 (obvious/serious), and
origin one of labels.ORIGINS (where the problem comes from; blank for none). A bin with findings
of different severity or origin takes one row each, under the same blind_id. A row left blank
counts as unlabeled. Check a sheet with `python3 dev_tools/labels.py` after editing it.
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
from corpus import find_packages  # noqa: E402
from labels import DEFECTS, LABELS, ORIGINS, SHEET_COLUMNS, labeled_bins  # noqa: E402
from bin_table import package_rows  # noqa: E402
from study import CANDIDATES, CONFIGS, LEGALITY_CANDIDATES, LEGALITY_CONFIGS, NEW_CANDIDATES  # noqa: E402
from _selection import BIN, load_package, ranked_bins, score, scoring, select_periods, selector  # noqa: E402

from AV_Spex.checks import bin_scoring
from AV_Spex.checks.qct_parse import _tc_format_timecode, _tc_parse_start_timecode
from AV_Spex.utils import ffprobe_probe
from AV_Spex.utils.log_setup import logger


def round1_depth(col, d):
    return 2 if (col, d) in NEW_CANDIDATES else 1


def legality_depth(col, d):
    return 1 if col == "bp_brng_mean" else 3


RANK_NAMES = {1: "2nd", 2: "3rd"}


def choose_bins(rows, per_tape, n_controls, rng, skip, candidates=CANDIDATES, depth=round1_depth,
                evidence=True, forced=(), forced_anywhere=False):
    """{bin_start: [selectors]} for one tape. Every selector a chosen bin qualifies for is
    recorded, so a bin picked as a control that is also a tag's peak counts for both.

    Fill order: controls, each candidate's top bin, then `forced` ((bin_start, selector) pairs:
    missed regions, configuration picks), today's evidence bins, and the deeper-ranked bins
    (`depth(col, d)` of them per candidate) while room remains. Forced bins must be analyzable
    unless `forced_anywhere` (a list someone asked for by name, e.g. a scored evidence bin whose
    midpoint falls in a short black segment)."""
    every_bin = {r["bin_start"] for r in rows}
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
    allowed = every_bin if forced_anywhere else {r["bin_start"] for r in rows}
    forced = [(b, sel) for b, sel in forced if b in allowed and b not in skip]
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
    picks = []
    for name in names:
        metrics, weights = CONFIGS[name]
        with scoring(metrics, weights):
            scores = score(ctx)
            periods = select_periods(ctx, 6, 30, scores=scores)
        for p in periods:
            for e in bin_scoring.evidence_within(p[0], p[1], scores):
                if e.bin_start not in skip:
                    picks.append((e.bin_start, f"pick:{name}"))
    return picks


def read_bins(path):
    """{video_id: [bin_start]} from a CSV with video_id and bin_start columns."""
    out = {}
    for row in csv.DictReader(open(Path(path).expanduser(), newline="")):
        out.setdefault(row["video_id"].strip(), []).append(float(row["bin_start"]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--name", default=date.today().isoformat())
    ap.add_argument("--round", choices=("tags", "legality"), default="tags",
                    help="tags: round 1's broad sheet; legality: the legal-floor luma round")
    ap.add_argument("--per-tape", type=int, default=None, help="default 12 (tags) / 6 (legality)")
    ap.add_argument("--controls", type=int, default=None, help="default 2 (tags) / 1 (legality)")
    ap.add_argument("--regions", default=None, help="CSV of video_id,tc_in,tc_out flagged as missed")
    ap.add_argument("--bins", default=None,
                    help="CSV of video_id,bin_start: a sheet of exactly these bins (+ --controls); "
                         "overrides --round")
    ap.add_argument("--id-prefix", default=None,
                    help="prefix for blind IDs (default 'L' for legality, 'S' for --bins)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--probe", action="store_true", help="read start timecodes from the source videos")
    args = ap.parse_args()
    listed = read_bins(args.bins) if args.bins else None
    legality = args.round == "legality" and listed is None
    per_tape = args.per_tape or (6 if legality else 12)
    n_controls = args.controls if args.controls is not None else (1 if legality or listed is not None else 2)
    prefix = args.id_prefix if args.id_prefix is not None else ("L" if legality else "S" if listed is not None else "")
    candidates = LEGALITY_CANDIDATES if legality else () if listed is not None else CANDIDATES
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
        if listed is not None:
            forced += [(b, "listed") for b in listed.get(ctx.name, [])]
            if not forced:
                continue
        rng = random.Random(f"{args.seed}:{ctx.name}")
        chosen = choose_bins(rows, per_tape if listed is None else len(forced) + n_controls, n_controls,
                             rng, tape_skip, candidates=candidates, depth=depth,
                             evidence=listed is None and not legality, forced=forced,
                             forced_anywhere=listed is not None)
        note = "" if ctx.name in tc_cache else "start TC not probed: positions from 00:00:00:00"
        for i, b in enumerate(sorted(chosen), 1):
            blind = f"{prefix}{ctx.name[-5:]}-{i:02d}"
            sheet.append({"video_id": ctx.name, "tc_in": _tc_format_timecode(b, fps, start_frames, drop),
                          "tc_out": _tc_format_timecode(b + BIN, fps, start_frames, drop),
                          "blind_id": blind, "defect": "", "severity": "", "origin": "", "notes": note})
            key[blind] = {"video_id": ctx.name, "bin_start": b, "selectors": chosen[b]}
        print(f"{ctx.name}: {len(chosen)} bins", file=sys.stderr)

    with open(sheet_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        w.writerows(sheet)
    key_path.write_text(json.dumps(key, indent=1))
    print(f"\nWrote {len(sheet)} bins to {sheet_path}\nKey (don't open while labeling): {key_path}")
    print(f"defect values: {', '.join(DEFECTS)}; severity 0/1/2; origin {', '.join(ORIGINS)}")


if __name__ == "__main__":
    main()
