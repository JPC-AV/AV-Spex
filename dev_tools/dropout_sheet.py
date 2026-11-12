#!/usr/bin/env python3
"""
dropout_sheet.py: a blind verification sheet (xlsx) of video dropout candidates.

Reads the loose runs dropout_scan.py cached per tape and picks frames for an operator to judge.
Each row shows one frame with a box around one region; the operator says whether that region
holds dropout. Rows are:

  - candidates: the frame's strongest run (contrast x length), sampled across three score bands
    (tertiles over all the sheet's tapes, so a band means the same thing on every tape) and,
    where a band has both, across white and black streaks. A pilot that only asked about the
    top scores could not say where precision falls off.
    That is --mode bands (pilot1). --mode filtered (pilot2 on) instead samples what the detector
    would report: per tape, frames drawn uniformly from those with a run passing
    video_dropout_detection.passes_filter() (--passed), to measure its precision. Three more
    pools measure what the filter throws away: frames whose best claim fails only the score cut
    (--low-score, the held-out sheet), frames whose best claim passes symmetry and recurrence
    but fails the opposite-field band (--near-miss, pilot3 on), and frames whose runs fail the
    rest (--rejected). Frames already on an earlier sheet (any events_key_*.json) are
    not asked about again. The key records, for every row, how many other frames the filter
    flags within 1, 5 and 30 seconds (flag_density), so the verdicts also show whether true
    dropout sits in denser stretches of flags than false alarms do.
  - controls: a random analyzable frame with a box at a random place. Their verdicts estimate
    how often dropout is there when the scan did not pick the spot, and they keep the sheet
    blind: every row looks the same.

Frames in the tape's avoid spans (black, bars, unanalyzable; recorded by dropout_scan.py) and
within --spacing seconds of another row are skipped.

Writes to $AVSPEX_CORPUS/labels/ (named events_*, so labels.py's bin-sheet reader ignores them):

  events_<name>.xlsx       one row per item, with an image, the file's own NDF/DF timecode, and
                           dropdown columns to fill in. An "instructions" tab defines them.
  events_key_<name>.json   why each row is there and the run behind it. Don't open it while
                           labeling.
  events_<name>_images/    the full-size image of each row (the sheet links to it)

Each image: previous / this / next frame, cropped around the box and enlarged 2x without
smoothing (so single field lines stay sharp), above the whole frame at half size with the box.
Before an image is written, the three frames are re-decoded and re-scanned; a candidate whose
run is not found again is reported (it means the seek landed on the wrong frame).

    python3 dev_tools/dropout_sheet.py period-study-2026-10 --name pilot1 \\
        --tapes JPC_AV_02213 JPC_AV_03801 JPC_AV_03802 JPC_AV_03806 JPC_AV_02319 JPC_AV_03796
"""
import argparse
import bisect
from dataclasses import asdict, replace
import csv
import gzip
import json
import random
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from corpus import CORPUS, resolve_run  # noqa: E402
from dropout_scan import SCAN_DIR, package_for  # noqa: E402
from AV_Spex.checks import video_dropout_detection as vdd  # noqa: E402
from AV_Spex.checks.qct_parse import _tc_format_timecode, _tc_parse_start_timecode  # noqa: E402

LABELS = CORPUS / "labels"

VERDICTS = ("dropout", "not_dropout", "unsure")
POLARITIES = ("white", "black", "both", "n/a")
VISIBILITY = ("faint", "clear")
LOCATIONS = ("picture", "bottom_band")
CAUSES = ("head_switching", "tracking_noise", "noise_grain", "motion_interlace", "edge_or_text",
          "graphics", "other")
YESNO = ("yes", "no")
COLUMNS = ("blind_id", "video_id", "timecode", "image", "verdict", "polarity", "visibility",
           "location", "cause", "dropout_elsewhere", "reviewer", "notes", "image_file")
DROPDOWNS = {"verdict": VERDICTS, "polarity": POLARITIES, "visibility": VISIBILITY,
             "location": LOCATIONS, "cause": CAUSES, "dropout_elsewhere": YESNO}

INSTRUCTIONS = [
    ("What this is", "Each row is one video frame with a yellow box around one region. Judge the boxed "
                     "region. The top strip shows the previous frame, this frame and the next frame, "
                     "cropped around the box and enlarged; below it is the whole frame. Click "
                     "image_file to open the full-size image, or go to the timecode in the NLE."),
    ("verdict", "dropout: a short horizontal line (white, or sometimes black) of signal loss in the box, "
                "typically one line, one or two frames, absent in the frames before and after. "
                "not_dropout: nothing, or something else (fill in cause). unsure: can't tell."),
    ("polarity", "For dropout: white, black, or both (several lines). Otherwise leave blank (n/a means the same)."),
    ("visibility", "For dropout: faint (visible only when looking for it) or clear (noticeable at "
                   "normal playback). Otherwise leave blank."),
    ("location", "For dropout: picture (in the active picture) or bottom_band (in or touching the "
                 "head-switching band at the bottom of the frame). Otherwise leave blank."),
    ("cause", "Only for not_dropout (leave blank for dropout; use location instead), what the box holds: head_switching (the noisy band at the bottom of "
              "the picture), tracking_noise (tracking/skew noise bars), noise_grain (snow, chroma "
              "noise, grain), motion_interlace (combing on a moving edge), edge_or_text (a picture "
              "edge, text or graphic detail), graphics, other. Leave blank if the box holds nothing."),
    ("dropout_elsewhere", "Fill in on every row, whatever the verdict. yes if you see dropout anywhere in "
                          "the middle frame (the full-frame view) outside the yellow box, no if not. The "
                          "previous/next crops are context for the verdict only; do not count them here."),
    ("reviewer", "Your initials, on every row you fill in."),
    ("Leave a row blank", "to skip it; blank rows count as unlabeled."),
]

DENSITY_WINDOWS = (1, 5, 30)    # seconds either side of a row, for the key's flag_density
BAND_NAMES = ("low", "mid", "high")
CROP_W, CROP_H, CROP_SCALE = 160, 48, 2
THUMB_SCALE = 0.5


# --- loading -------------------------------------------------------------------------------

def load_scan(pkg):
    d = pkg / SCAN_DIR
    meta = json.loads((d / "scan.json").read_text())
    best = {}           # frame -> (score, run dict)
    every = defaultdict(list)   # frame -> every run dict
    with gzip.open(d / "streaks.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            run = {k: (r[k] if k == "polarity" else float(r[k])) for k in r}
            score = run_score(run)
            f = int(run["frame"])
            every[f].append(run)
            if f not in best or score > best[f][0]:
                best[f] = (score, run)
    busy = {}
    with gzip.open(d / "frames.csv.gz", "rt") as fh:
        for r in csv.DictReader(fh):
            busy[int(r["frame"])] = (int(r["n_runs"]), int(r["candidate_pixels"]))
    return meta, best, busy, every


def run_score(run):
    return min(run["spatial"], run["temporal"]) * run["length"]


def positions(every):
    return {f: [(int(r["line"]), int(r["x"]), int(r["length"])) for r in rs] for f, rs in every.items()}


def filter_features(run, pos):
    return {"symmetry": round(vdd.symmetry(run["spatial"], run["temporal"]), 3),
            "field_ratio": round(vdd.field_ratio(run["spatial"], run["temporal"],
                                                 run["other_field"]), 3),
            "score": round(run_score(run), 1),
            "recurrence": vdd.recurrence(int(run["frame"]), int(run["line"]), int(run["x"]),
                                         int(run["length"]), pos)}


def in_spans(t, spans):
    return any(a <= t < b for a, b in spans)


# --- choosing rows -------------------------------------------------------------------------

def spaced(picks, t, spacing):
    return all(abs(t - p) >= spacing for p in picks)


def choose(tapes, per_band, n_controls, spacing, rng):
    """[(video_id, frame, selector, run-or-None)] per the module docstring."""
    pools = {}
    all_scores = []
    for vid, (meta, best, busy, _) in tapes.items():
        cands = [(s, run) for f, (s, run) in best.items() if not in_spans(run["time"], meta["avoid"])]
        pools[vid] = cands
        all_scores += [s for s, _ in cands]
    if not all_scores:
        sys.exit("no candidate runs outside the avoid spans")
    edges = np.percentile(all_scores, [100 / 3, 200 / 3])
    print(f"score bands: low < {edges[0]:.0f} <= mid < {edges[1]:.0f} <= high", file=sys.stderr)

    rows = []
    for vid, cands in pools.items():
        meta = tapes[vid][0]
        times = []
        bands = defaultdict(lambda: defaultdict(list))
        for s, run in cands:
            band = BAND_NAMES[int(np.searchsorted(edges, s, side="right"))]
            bands[band][run["polarity"]].append(run)
        for band in BAND_NAMES:
            by_pol = bands[band]
            for pol in by_pol.values():
                rng.shuffle(pol)
            # alternate polarities so both are represented when both exist
            order = [p for p in ("white", "black") if by_pol.get(p)]
            taken, i = 0, 0
            while taken < per_band and any(by_pol[p] for p in order):
                pol = order[i % len(order)]
                i += 1
                while by_pol[pol]:
                    run = by_pol[pol].pop()
                    if spaced(times, run["time"], spacing):
                        times.append(run["time"])
                        rows.append((vid, int(run["frame"]), f"band:{band}", run))
                        taken += 1
                        break
            if taken < per_band:
                print(f"{vid}: only {taken} of {per_band} {band}-band candidates", file=sys.stderr)
        rows += controls(vid, meta, n_controls, times, spacing, rng)
    return rows


def controls(vid, meta, n, times, spacing, rng):
    """Random analyzable frames with a box at a random place; appends to times."""
    p = vdd.StreakParams()
    rows, tries = [], 0
    while len(rows) < n and tries < 1000:
        tries += 1
        f = rng.randrange(1, meta["frames_scanned"] - 1)
        t = f / meta["fps"]
        if in_spans(t, meta["avoid"]) or not spaced(times, t, spacing):
            continue
        line = rng.randrange(p.margin_top + 2, meta["height"] - p.margin_bottom - 2)
        length = rng.randrange(8, 30)
        x = rng.randrange(p.margin_side, meta["width"] - p.margin_side - length)
        times.append(t)
        rows.append((vid, f, "control", {"frame": f, "time": t, "line": line, "x": x, "length": length}))
    return rows


def earlier_frames(exclude_name):
    """{video_id: [time, ...]} of rows on every earlier events sheet."""
    seen = defaultdict(list)
    for kp in LABELS.glob("events_key_*.json"):
        if kp.name == f"events_key_{exclude_name}.json":
            continue
        for row in json.loads(kp.read_text())["rows"].values():
            seen[row["video_id"]].append(row["time"])
    return seen


def flag_density(t, flagged_times):
    """{window: other flagged frames within that many seconds of t}; flagged_times is sorted."""
    out = {}
    for w in DENSITY_WINDOWS:
        lo = bisect.bisect_left(flagged_times, t - w)
        hi = bisect.bisect_right(flagged_times, t + w)
        i = bisect.bisect_left(flagged_times, t)
        own = i < len(flagged_times) and abs(flagged_times[i] - t) < 1e-6
        out[f"{w}s"] = hi - lo - (1 if own else 0)
    return out


def choose_filtered(tapes, n_passed, n_low, n_near, n_rejected, n_controls, spacing, rng, seen):
    """Per tape: uniform samples of frames that pass the event filter (their best passing run),
    of low-score frames (no passing run, but one that fails only the score cut), of near misses
    (neither, but a run that passes symmetry and recurrence and fails the field band), and of
    the rest (their best run), plus controls. See the module docstring.

    Returns (rows, positions per tape, sorted times of every flagged frame per tape)."""
    rows = []
    flagged = {}
    f_full = vdd.EventFilter()
    f_base = replace(f_full, min_field_ratio=0.0, max_field_ratio=float("inf"), min_score=0.0)

    def in_band(r):
        return (f_full.min_field_ratio
                <= vdd.field_ratio(r["spatial"], r["temporal"], r["other_field"])
                <= f_full.max_field_ratio)
    for vid, (meta, _best, _busy, every) in tapes.items():
        pos = positions(every)
        passed, low, near, rejected = [], [], [], []
        for f, rs in every.items():
            rs = [r for r in rs if not in_spans(r["time"], meta["avoid"])]
            if not rs:
                continue
            # symmetry and recurrence first (recurrence is the slow test), then the two cheap ones
            base = [r for r in rs if vdd.passes_filter(f, int(r["line"]), int(r["x"]), int(r["length"]),
                                                       r["spatial"], r["temporal"], r["other_field"],
                                                       pos, f_base)]
            banded = [r for r in base if in_band(r)]
            ok = [r for r in banded if run_score(r) >= f_full.min_score]
            if ok:
                passed.append(max(ok, key=run_score))
            elif banded:
                low.append(max(banded, key=run_score))
            elif base:
                near.append(max(base, key=run_score))
            else:
                rejected.append(max(rs, key=run_score))
        flagged[vid] = sorted(r["time"] for r in passed)
        mins = meta["frames_scanned"] / meta["fps"] / 60
        print(f"{vid}: {len(passed)} frames pass the filter ({len(passed) / mins:.1f}/min), "
              f"{len(low)} low-score, {len(near)} near misses, {len(rejected)} have only rejected runs",
              file=sys.stderr)
        times = list(seen.get(vid, ()))
        for label, pool, n in (("passed", passed, n_passed), ("low_score", low, n_low),
                               ("near_miss", near, n_near),
                               ("rejected", rejected, n_rejected)):
            rng.shuffle(pool)
            taken = 0
            for run in pool:
                if taken == n:
                    break
                if spaced(times, run["time"], spacing):
                    times.append(run["time"])
                    rows.append((vid, int(run["frame"]), label, run))
                    taken += 1
            if taken < n:
                print(f"{vid}: only {taken} of {n} {label} frames", file=sys.stderr)
        rows += controls(vid, meta, n_controls, times, spacing, rng)
    return rows, {vid: positions(t[3]) for vid, t in tapes.items()}, flagged


# --- images --------------------------------------------------------------------------------

def decode3(video, frame, fps, w, h, pix_fmt):
    """Frames frame-1, frame, frame+1. The seek target sits 0.3 frame before frame-1, so the
    first frame at or after it is frame-1 even with millisecond-rounded timestamps."""
    target = max(0.0, (frame - 1 - 0.3) / fps)
    ch = 3 if pix_fmt == "rgb24" else 1
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{target:.5f}", "-i", str(video),
           "-map", "0:v:0", "-frames:v", "3", "-f", "rawvideo", "-pix_fmt", pix_fmt, "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (w * h * ch)
    shape = (n, h, w, ch) if ch == 3 else (n, h, w)
    return np.frombuffer(raw[:n * w * h * ch], np.uint8).reshape(shape)


def recheck(video, run, meta):
    """Whether re-scanning the decoded frames finds the run again (proves the seek)."""
    g = decode3(video, int(run["frame"]), meta["fps"], meta["width"], meta["height"], "gray")
    if len(g) < 3:
        return False
    runs, _, _ = vdd.find_streaks(g[0], g[1], g[2], vdd.StreakParams())
    return any(r.line == int(run["line"]) and r.x == int(run["x"]) for r in runs)


def box_of(run, w, h):
    x0 = max(0, int(run["x"]) - 4)
    x1 = min(w - 1, int(run["x"]) + int(run["length"]) + 3)
    y0 = max(0, int(run["line"]) - 3)
    y1 = min(h - 1, int(run["line"]) + 3)
    return x0, y0, x1, y1


def make_image(video, run, meta, path):
    from PIL import Image, ImageDraw
    w, h = meta["width"], meta["height"]
    rgb = decode3(video, int(run["frame"]), meta["fps"], w, h, "rgb24")
    if len(rgb) < 3:
        raise RuntimeError("could not decode three frames")
    x0, y0, x1, y1 = box_of(run, w, h)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    left = min(max(0, cx - CROP_W // 2), w - CROP_W)
    top = min(max(0, cy - CROP_H // 2), h - CROP_H)
    # keep the crop on the same field parity, so field lines line up across the three
    top -= top % 2
    pad = 18
    cw, chh = CROP_W * CROP_SCALE, CROP_H * CROP_SCALE
    tw, th = int(w * THUMB_SCALE), int(h * THUMB_SCALE)
    canvas = Image.new("RGB", (3 * cw + 4 * 8, pad + chh + 8 + th + 8), (40, 40, 40))
    draw = ImageDraw.Draw(canvas)
    for i, (label, frame) in enumerate(zip(("previous", "THIS FRAME", "next"), rgb)):
        crop = Image.fromarray(frame[top:top + CROP_H, left:left + CROP_W])
        crop = crop.resize((cw, chh), Image.NEAREST)
        cd = ImageDraw.Draw(crop)
        cd.rectangle([(x0 - left) * CROP_SCALE - 2, (y0 - top) * CROP_SCALE - 2,
                      (x1 - left) * CROP_SCALE + 2, (y1 - top) * CROP_SCALE + 2],
                     outline=(255, 220, 0))
        ox = 8 + i * (cw + 8)
        canvas.paste(crop, (ox, pad))
        draw.text((ox, 3), label, fill=(255, 255, 255))
    thumb = Image.fromarray(rgb[1]).resize((tw, th), Image.BILINEAR)
    td = ImageDraw.Draw(thumb)
    td.rectangle([x0 * THUMB_SCALE - 3, y0 * THUMB_SCALE - 3, x1 * THUMB_SCALE + 3, y1 * THUMB_SCALE + 3],
                 outline=(255, 220, 0), width=2)
    canvas.paste(thumb, (8, pad + chh + 8))
    canvas.save(path)
    return canvas.size


# --- the workbook --------------------------------------------------------------------------

def write_workbook(path, rows, image_dir):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Alignment, Font
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = "labels"
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = "E2"
    col = {name: i + 1 for i, name in enumerate(COLUMNS)}
    letter = {name: ws.cell(row=1, column=i).column_letter for name, i in col.items()}
    ws.column_dimensions[letter["image"]].width = 75
    for name in ("blind_id", "video_id", "timecode"):
        ws.column_dimensions[letter[name]].width = 15
    ws.column_dimensions[letter["notes"]].width = 40
    last = len(rows) + 1
    for name, options in DROPDOWNS.items():
        dv = DataValidation(type="list", formula1='"' + ",".join(options) + '"', allow_blank=True,
                            showErrorMessage=True, error=f"Pick one of: {', '.join(options)}")
        ws.add_data_validation(dv)
        dv.add(f"{letter[name]}2:{letter[name]}{last}")
        ws.column_dimensions[letter[name]].width = max(12, max(len(o) for o in options) + 3)

    for r, row in enumerate(rows, start=2):
        for name in ("blind_id", "video_id", "timecode"):
            ws.cell(row=r, column=col[name], value=row[name])
        link = ws.cell(row=r, column=col["image_file"], value=row["image_file"])
        link.hyperlink = str(image_dir / row["image_file"])
        link.font = Font(color="0563C1", underline="single")
        img = XLImage(str(image_dir / row["image_file"]))
        scale = 520 / img.width
        img.width, img.height = int(img.width * scale), int(img.height * scale)
        ws.add_image(img, f"{letter['image']}{r}")
        ws.row_dimensions[r].height = img.height * 0.75 + 4
        for c in ws[r]:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    info = wb.create_sheet("instructions")
    info.column_dimensions["A"].width = 20
    info.column_dimensions["B"].width = 110
    for term, text in INSTRUCTIONS:
        info.append((term, text))
    for row in info.iter_rows():
        row[0].font = Font(bold=True)
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    wb.save(path)


# --- main ----------------------------------------------------------------------------------

def timing_for(vid, fps, video=None):
    """(start frames, drop-frame, known) from labels/timecode.json; a tape not cached there yet
    is probed from its video and added, so timecodes match the NLE."""
    path = LABELS / "timecode.json"
    try:
        cache = json.loads(path.read_text())
    except FileNotFoundError:
        cache = {}
    if vid not in cache and video is not None:
        from AV_Spex.utils import ffprobe_probe
        cache[vid] = {"fps": fps, "start_timecode": ffprobe_probe.start_timecode(str(video))}
        path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tc = (cache.get(vid) or {}).get("start_timecode")
    start_frames, drop = _tc_parse_start_timecode(tc, max(1, round(fps)))
    return start_frames, drop, vid in cache


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run")
    ap.add_argument("--name", required=True)
    ap.add_argument("--tapes", nargs="+", required=True, help="video IDs, each scanned by dropout_scan.py")
    ap.add_argument("--mode", choices=("bands", "filtered"), default="bands")
    ap.add_argument("--per-band", type=int, default=4, help="bands mode: candidates per score band per tape")
    ap.add_argument("--passed", type=int, default=8, help="filtered mode: frames passing the filter per tape")
    ap.add_argument("--low-score", type=int, default=0,
                    help="filtered mode: frames failing only the score cut, per tape")
    ap.add_argument("--near-miss", type=int, default=0,
                    help="filtered mode: frames failing only the opposite-field band, per tape")
    ap.add_argument("--rejected", type=int, default=3, help="filtered mode: frames failing it per tape")
    ap.add_argument("--controls", type=int, default=2, help="random controls per tape")
    ap.add_argument("--spacing", type=float, default=2.0, help="minimum seconds between rows of a tape")
    ap.add_argument("--seed", type=int, default=20261102)
    args = ap.parse_args()

    run = resolve_run(args.run)
    sheet = LABELS / f"events_{args.name}.xlsx"
    key_path = LABELS / f"events_key_{args.name}.json"
    image_dir = LABELS / f"events_{args.name}_images"
    if sheet.exists() or key_path.exists():
        sys.exit(f"{sheet.name} or {key_path.name} already exists; pick another --name")

    tapes, videos = {}, {}
    for vid in args.tapes:
        pkg = package_for(run, Path(vid))
        if pkg is None or not (pkg / SCAN_DIR / "scan.json").exists():
            sys.exit(f"{vid}: no dropout scan in {run.name} (run dropout_scan.py first)")
        tapes[vid] = load_scan(pkg)
        videos[vid] = Path(tapes[vid][0]["video"])
        if not videos[vid].is_file():
            sys.exit(f"{vid}: video {videos[vid]} not reachable")

    rng = random.Random(args.seed)
    pos_by_vid, flagged = {}, {}
    if args.mode == "bands":
        picks = choose(tapes, args.per_band, args.controls, args.spacing, rng)
    else:
        picks, pos_by_vid, flagged = choose_filtered(
            tapes, args.passed, args.low_score, args.near_miss, args.rejected, args.controls, args.spacing, rng,
            earlier_frames(args.name))
    picks.sort(key=lambda p: (p[0], p[1]))

    image_dir.mkdir(parents=True, exist_ok=True)
    rows, key = [], {}
    seq = defaultdict(int)
    for vid, frame, selector, run_ in picks:
        meta = tapes[vid][0]
        seq[vid] += 1
        blind = f"D{vid[-5:]}-{seq[vid]:02d}"
        if selector != "control" and not recheck(videos[vid], run_, meta):
            print(f"{blind}: run not found again after seeking to frame {frame}; check the seek",
                  file=sys.stderr)
        fname = f"{blind}.png"
        make_image(videos[vid], run_, meta, image_dir / fname)
        start_frames, drop, known = timing_for(vid, meta["fps"], videos[vid])
        tc = _tc_format_timecode(frame / meta["fps"], meta["fps"], start_frames, drop)
        rows.append({"blind_id": blind, "video_id": vid, "timecode": tc, "image_file": fname})
        busy = tapes[vid][2].get(frame, (0, 0))
        key[blind] = {"video_id": vid, "frame": frame, "time": round(frame / meta["fps"], 4),
                      "timecode": tc, "start_timecode_known": known, "selector": selector,
                      "box": box_of(run_, meta["width"], meta["height"]),
                      "run": run_, "frame_n_runs": busy[0], "frame_candidate_pixels": busy[1]}
        if selector != "control" and vid in pos_by_vid:
            key[blind]["filter"] = filter_features(run_, pos_by_vid[vid])
        if vid in flagged:
            key[blind]["flag_density"] = flag_density(frame / meta["fps"], flagged[vid])
        print(f"{blind} {vid} {tc} {selector}", file=sys.stderr)

    write_workbook(sheet, rows, image_dir)
    key_path.write_text(json.dumps({"run": run.name, "args": vars(args),
                                    "filter_params": asdict(vdd.EventFilter()),
                                    "streak_params": vdd.params_dict(vdd.StreakParams()),
                                    "rows": key}, indent=1))
    n_ctrl = sum(k["selector"] == "control" for k in key.values())
    print(f"wrote {sheet} ({len(rows)} rows, {n_ctrl} controls), {key_path.name}, {image_dir.name}/")


if __name__ == "__main__":
    main()
