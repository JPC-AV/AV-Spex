"""
_inject.py: put known faults into a clip of a real tape, run a detector on it, and say which
faults it found. Used by inject.py; kept importable so worker processes can load it.

The fault functions are pure (arrays in, arrays out, plus the list of what was injected and
where), so they are tested without ffmpeg. Audio is handled as int32 PCM so a silence gap is
exact zeros and a splice is an exact butt joint, as on a real capture.
"""
import json
import logging
import subprocess
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np

FULL_SCALE = float(2 ** 31)
FRAME_SAMPLES = 48000 * 1001 / 30000          # audio samples per NTSC video frame at 48 kHz

# Dropped samples: what is injected
SPLICE_FRAMES = (0.3, 1, 2, 10)               # audio removed, in video frames (a dropped frame is 1)
GAP_MS = (2, 5, 10, 20, 50, 200, 1000)        # exact-zero stretches; the detector's floor is 5 ms
FAULT_SPACING_S = 5.0                         # one fault per this much clip
FAULT_JITTER_S = 2.0
EDGE_S = 3.0                                  # keep clear of the clip's ends (the detector guards 1 s)
MATCH_S = 0.05                                # a detection this close to an injected fault found it

# Tone leak: what is injected
TONE_HARMONICS = (1000, 2000, 3000, 4000, 6000)
TONE_LEVELS_DB = (-50, -60, -70, -80, -90, -100)   # peak level of each harmonic, dBFS

# Duplicate frames: what is injected
DUPLICATE_RUNS = (1, 2, 3, 5, 15)             # repeats of the held frame (1 = one frame shown twice)
DUPLICATE_SPACING_S = 3.0
# A freeze the deck re-digitizes repeats the picture but not the capture noise: each repeat is
# the held picture plus its own noise, so two repeats differ by two noises. This much Gaussian
# noise per repeat (10-bit codes; scaled for 8-bit) puts the repeat-to-repeat YDIF near 0.7,
# the middle of the 0.6-0.8 the verified freezes on the LC tapes measure (the detector's
# limit is 1.0), and the 8-bit luma MSE inside their 0.02-0.09.
REDIGITIZED_NOISE_SIGMA = 0.62
PIX_FMTS = {"yuv422p10le": ("<u2", 2), "yuv422p": ("u1", 1)}   # numpy type, bytes per sample


# ------------------------------------------------------------------ faults ---

def duplicate_plan(n_frames, fps):
    """[(held frame index, repeats), ...]: one freeze every DUPLICATE_SPACING_S, run lengths cycling."""
    plan, i, f = [], 0, int(EDGE_S * fps)
    while f + max(DUPLICATE_RUNS) + int(EDGE_S * fps) < n_frames:
        plan.append((f, DUPLICATE_RUNS[i % len(DUPLICATE_RUNS)]))
        f += int(DUPLICATE_SPACING_S * fps)
        i += 1
    return plan


def held_frame(frame, rng=None, sigma=0.0, peak=1023):
    """The frame a freeze shows again: the same picture, bit for bit, or with fresh capture
    noise when the deck re-digitizes it (`frame` is a flat array of samples)."""
    if not sigma:
        return frame
    noisy = frame.astype(np.float32) + rng.normal(0.0, sigma, frame.shape).astype(np.float32)
    return np.clip(np.rint(noisy), 0, peak).astype(frame.dtype)


def fault_positions(n_samples, sr, rng):
    """Sample positions for one fault every FAULT_SPACING_S, jittered, clear of the ends."""
    out, t = [], EDGE_S
    while t + FAULT_JITTER_S + EDGE_S < n_samples / sr:
        out.append(int((t + rng.uniform(0, FAULT_JITTER_S)) * sr))
        t += FAULT_SPACING_S
    return out


def _dbfs(x):
    return round(float(20 * np.log10(max(float(x), 1.0) / FULL_SCALE)), 1)


def local_level_dbfs(a, p, sr, ms=50):
    """RMS of the loudest channel in the `ms` before sample p."""
    seg = a[max(0, p - int(sr * ms / 1000)):p].astype(np.float64)
    return _dbfs(np.sqrt((seg ** 2).mean(axis=0)).max()) if len(seg) else -200.0


def splice_out(a, sr, positions, lengths):
    """Remove lengths[i] samples starting at positions[i] from (n, channels) int32 audio `a`,
    as lost frames butt the waveform together. Returns (audio, faults); each fault records
    where the join lands in the output, how big the step across it is, and how far that step
    stands out from the sample-to-sample movement of the programme just before it."""
    keep, faults, removed, last = [], [], 0, 0
    for p, k in sorted(zip(positions, lengths)):
        if p <= last or p + k >= len(a):
            continue
        keep.append(a[last:p])
        before, after = a[p - 1].astype(np.int64), a[p + k].astype(np.int64)
        step = np.abs(after - before).max()
        typical = np.abs(np.diff(a[max(0, p - sr // 20):p].astype(np.int64), axis=0))
        typical = np.median(typical.max(axis=1)) if len(typical) else 0
        faults.append({
            "kind": "splice", "time_s": round((p - removed) / sr, 4), "end_s": round((p - removed) / sr, 4),
            "cut_samples": int(k), "cut_frames": round(k / (sr * 1001 / 30000), 2),
            "step_dbfs": _dbfs(step), "stands_out_db": round(float(20 * np.log10(max(step, 1) / max(typical, 1))), 1),
            "level_dbfs": local_level_dbfs(a, p, sr),
        })
        removed += k
        last = p + k
    keep.append(a[last:])
    return np.concatenate(keep), faults


def zero_gaps(a, sr, positions, durations_ms):
    """Set durations_ms[i] of every channel to exact zero from positions[i]: the input-lost
    signature. Returns (audio, faults)."""
    out, faults = a.copy(), []
    for p, ms in zip(positions, durations_ms):
        n = max(1, int(round(sr * ms / 1000)))
        if p + n >= len(a):
            continue
        out[p:p + n] = 0
        faults.append({"kind": "gap", "time_s": round(p / sr, 4), "end_s": round((p + n) / sr, 4),
                       "gap_ms": ms, "level_dbfs": local_level_dbfs(a, p, sr)})
    return out, faults


def add_tones(a, sr, level_db, harmonics=TONE_HARMONICS, channel=0):
    """Add a steady sine at each of `harmonics` (Hz), each peaking at level_db dBFS, to one
    channel. A leaked reference tone is the fundamental plus a comb of harmonics."""
    out = a.astype(np.int64)
    t = np.arange(len(a)) / sr
    amp = FULL_SCALE * 10 ** (level_db / 20)
    for hz in harmonics:
        out[:, channel] += np.round(amp * np.sin(2 * np.pi * hz * t)).astype(np.int64)
    return np.clip(out, -2 ** 31, 2 ** 31 - 1).astype(np.int32)


def match_faults(faults, events, tolerance=MATCH_S):
    """Mark each fault found or not. `events` are (kind, start_s, end_s). A fault is found by
    any event within `tolerance` of its span; `found_as` records the kind that found it.
    Returns the events that found nothing."""
    used = set()
    for f in faults:
        hits = [i for i, (_, s, e) in enumerate(events)
                if s <= f["end_s"] + tolerance and e >= f["time_s"] - tolerance]
        used.update(hits)
        kinds = {events[i][0] for i in hits}
        f["found"] = bool(hits)
        f["found_as"] = f["kind"] if f["kind"] in kinds else (sorted(kinds)[0] if kinds else "")
    return [ev for i, ev in enumerate(events) if i not in used]


# --------------------------------------------------------------------- I/O ---

def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def audio_format(video):
    """(sample_rate, channels) of the first audio stream, or None."""
    res = _run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                "stream=sample_rate,channels", "-of", "json", str(video)])
    try:
        st = json.loads(res.stdout)["streams"][0]
        return int(st["sample_rate"]), int(st["channels"])
    except (ValueError, KeyError, IndexError):
        return None


def read_audio(video, start, seconds, sr, channels):
    """The first audio stream of `video` from `start` for `seconds`, as (n, channels) int32."""
    res = _run(["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(video),
                "-map", "0:a:0", "-f", "s32le", "-acodec", "pcm_s32le", "-"])
    if res.returncode:
        raise RuntimeError(f"ffmpeg could not read the audio: {res.stderr.decode(errors='replace')[-300:]}")
    a = np.frombuffer(res.stdout, dtype="<i4")
    return a[:len(a) - len(a) % channels].reshape(-1, channels).copy()


def write_audio(a, sr, path):
    """Write (n, channels) int32 audio as a 24-bit PCM Matroska file, the form the tapes carry."""
    res = _run(["ffmpeg", "-v", "error", "-y", "-f", "s32le", "-ar", str(sr), "-ac", str(a.shape[1]),
                "-i", "-", "-c:a", "pcm_s24le", str(path)], input=a.astype("<i4").tobytes())
    if res.returncode:
        raise RuntimeError(f"ffmpeg could not write {path}: {res.stderr.decode(errors='replace')[-300:]}")
    return path


def _quiet():
    from AV_Spex.utils.log_setup import logger
    logger.setLevel(logging.CRITICAL)


def _clip(job):
    fmt = audio_format(job["video"])
    if not fmt:
        raise RuntimeError("no audio stream")
    sr, ch = fmt
    a = read_audio(job["video"], job["start"], job["seconds"], sr, ch)
    if len(a) < sr * (2 * EDGE_S + FAULT_SPACING_S):
        raise RuntimeError(f"only {len(a) / sr:.0f}s of audio in the clip")
    return a, sr


# ---------------------------------------------------------- dropped samples ---

def _scan_events(path):
    from AV_Spex.checks import audio_discontinuity
    scan = audio_discontinuity.scan_audio_discontinuities(str(path))
    if not scan.measured:
        raise RuntimeError("the scan decoded nothing")
    return ([("splice", ev.time_s, ev.time_s) for ev in scan.splices]
            + [("gap", g.start_s, g.end_s) for g in scan.silence_gaps])


def run_dropped_samples(job):
    """One tape: scan the clean clip (control), then a copy with splices and a copy with
    silence gaps. job: video_id, video, start, seconds, seed, keep (a directory or None).
    Returns {video_id, ok, error, seconds, control, faults, false_alarms}."""
    started = time.time()
    out = {"video_id": job["video_id"], "ok": False, "error": "", "control": 0, "faults": [], "false_alarms": 0}
    try:
        _quiet()
        a, sr = _clip(job)
        rng = np.random.default_rng([job["seed"], int(job["video_id"][-5:])])
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(job["keep"] or tmp)
            work.mkdir(parents=True, exist_ok=True)
            name = job["video_id"]
            control = _scan_events(write_audio(a, sr, work / f"{name}_clean.mka"))
            out["control"] = len(control)

            pos = fault_positions(len(a), sr, rng)
            lengths = [int(round(SPLICE_FRAMES[i % len(SPLICE_FRAMES)] * sr * 1001 / 30000)) for i in range(len(pos))]
            spliced, faults = splice_out(a, sr, pos, lengths)
            events = _scan_events(write_audio(spliced, sr, work / f"{name}_splices.mka"))
            extra = match_faults(faults, events)
            # A control event sits earlier in the spliced clip by the audio cut before it
            cuts = sorted((f["time_s"], f["cut_samples"] / sr) for f in faults)
            shifted = [s - sum(c for t, c in cuts if t < s) for _, s, _ in control]
            out["false_alarms"] += sum(not any(abs(s - c) <= 0.5 for c in shifted) for _, s, _ in extra)
            out["faults"] += faults

            pos = fault_positions(len(a), sr, rng)
            gapped, faults = zero_gaps(a, sr, pos, [GAP_MS[i % len(GAP_MS)] for i in range(len(pos))])
            events = _scan_events(write_audio(gapped, sr, work / f"{name}_gaps.mka"))
            extra = match_faults(faults, events)
            out["false_alarms"] += sum(not any(abs(s - c) <= 0.5 for _, c, _ in control) for _, s, _ in extra)
            out["faults"] += faults
        for f in out["faults"]:
            f["video_id"] = job["video_id"]
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()
    out["seconds"] = round(time.time() - started, 1)
    return out


# ---------------------------------------------------------------- tone leak ---

def _tone_verdicts(path, tmp):
    from AV_Spex.checks import tone_leak_check
    res = tone_leak_check.analyzeToneLeak(str(path), str(tmp))
    if res is None:
        raise RuntimeError("tone-leak analysis could not run")
    return {ch: verdict for (si, ch), verdict in res["channels"].items() if si == 0}


def run_tone_leak(job):
    """One tape: analyse the clean clip (control), then one copy per level with a 1 kHz comb
    on channel 0, and one per level with the 1 kHz fundamental alone. Each copy is one fault:
    found when the detector flags channel 0. Returns as run_dropped_samples does."""
    started = time.time()
    out = {"video_id": job["video_id"], "ok": False, "error": "", "control": 0, "faults": [], "false_alarms": 0}
    try:
        _quiet()
        a, sr = _clip(job)
        level = _dbfs(np.sqrt((a[:, 0].astype(np.float64) ** 2).mean()))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(job["keep"] or tmp)
            work.mkdir(parents=True, exist_ok=True)
            name = job["video_id"]
            clean = _tone_verdicts(write_audio(a, sr, work / f"{name}_clean.mka"), tmp)
            out["control"] = sum(v["tone_leak_detected"] for v in clean.values())
            for what, harmonics in (("comb", TONE_HARMONICS), ("1 kHz only", TONE_HARMONICS[:1])):
                for db in TONE_LEVELS_DB:
                    tag = f"{name}_{'comb' if what == 'comb' else 'fund'}_{-db}"
                    verdicts = _tone_verdicts(write_audio(add_tones(a, sr, db, harmonics), sr, work / f"{tag}.mka"), tmp)
                    v = verdicts[0]
                    out["faults"].append({
                        "video_id": name, "kind": what, "level_db": db, "clip_level_dbfs": level,
                        "found": bool(v["tone_leak_detected"]),
                        "flagged_fraction": round(v["flagged_fraction"], 3),
                        "median_comb_db": None if v["median_comb_db"] is None else round(v["median_comb_db"], 1),
                    })
                    # Any other channel newly flagged by a tone that was not put on it
                    out["false_alarms"] += sum(v2["tone_leak_detected"] and not clean[ch]["tone_leak_detected"]
                                               for ch, v2 in verdicts.items() if ch != 0)
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()
    out["seconds"] = round(time.time() - started, 1)
    return out


# --------------------------------------------------------- duplicate frames ---

def video_format(video):
    res = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height,pix_fmt,r_frame_rate", "-of", "json", str(video)])
    st = json.loads(res.stdout)["streams"][0]
    num, den = (int(x) for x in st["r_frame_rate"].split("/"))
    if st["pix_fmt"] not in PIX_FMTS:
        raise RuntimeError(f"pixel format {st['pix_fmt']} is not handled (have: {', '.join(PIX_FMTS)})")
    return int(st["width"]), int(st["height"]), st["pix_fmt"], st["r_frame_rate"], num / den


def write_frozen(clean, dest, fmt, plan, rng, sigma):
    """Copy `clean` to `dest`, replacing the frames after each held frame in `plan` with it.
    Frames are replaced, not inserted, so the clip keeps its length. Returns the faults, each
    with `motion`: the mean luma change (in the file's own codes) the freeze hides at its
    first repeat. A freeze over a still picture hides nothing and cannot be told from it."""
    width, height, pix_fmt, rate, fps = fmt
    dtype, size = PIX_FMTS[pix_fmt]
    samples = width * height * 2                       # 4:2:2: luma plus two half-width chroma planes
    raw = ["-f", "rawvideo", "-pix_fmt", pix_fmt]
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(clean), *raw, "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", *raw, "-s", f"{width}x{height}", "-r", rate, "-i", "-",
                            "-c:v", "ffv1", "-level", "3", "-g", "1", "-slices", "16", str(dest)],
                           stdin=subprocess.PIPE)
    starts = dict(plan)
    faults, held, left, n = [], None, 0, 0
    peak = 1023 if size == 2 else 255
    while True:
        buf = dec.stdout.read(samples * size)
        if len(buf) < samples * size:
            break
        frame = np.frombuffer(buf, dtype=dtype)
        if left:
            if faults[-1]["motion"] is None:
                luma = width * height
                faults[-1]["motion"] = round(float(np.abs(frame[:luma].astype(np.int32)
                                                          - held[:luma].astype(np.int32)).mean()), 2)
            frame = held_frame(held, rng, sigma, peak)
            left -= 1
        elif n in starts:
            held, left = frame, starts[n]
            faults.append({"kind": "re-digitized" if sigma else "identical", "repeats": starts[n],
                           "time_s": round((n + 1) / fps, 4), "end_s": round((n + starts[n]) / fps, 4),
                           "motion": None})
        enc.stdin.write(frame.tobytes())
        n += 1
    enc.stdin.close()
    if enc.wait() or dec.wait():
        raise RuntimeError(f"ffmpeg failed writing {dest}")
    return [f for f in faults if f["motion"] is not None]


def _duplicate_events(video, work, min_run_length):
    from AV_Spex.checks import duplicate_frame_detection
    from AV_Spex.checks.frame_analysis import QCToolsParser
    report = Path(str(video)[:-4] + ".qctools.xml.gz")
    res = _run(["qcli", "-i", str(video), "-o", str(report)])
    if not report.exists():
        raise RuntimeError(f"qcli wrote no report: {res.stderr.decode(errors='replace')[-300:]}")
    out = work / "dupes"
    out.mkdir(exist_ok=True)
    result = duplicate_frame_detection.detect_duplicate_frames(
        video, out, qctools_parser=QCToolsParser(str(report)), check_cancelled=lambda: False,
        min_run_length=min_run_length)
    if result is None or result.verification_available is False:
        raise RuntimeError("duplicate-frame detection could not run or could not verify with OpenCV")
    return [("freeze", r.start_time, r.end_time) for r in result.runs or []]


def run_duplicate_frames(job):
    """One tape: a video clip, untouched (control), with bit-identical freezes, and with
    re-digitized ones. Each needs its own QCTools report, so this runs at about three times
    the clip's length. Returns as run_dropped_samples does."""
    started = time.time()
    out = {"video_id": job["video_id"], "ok": False, "error": "", "control": 0, "faults": [], "false_alarms": 0}
    try:
        _quiet()
        from AV_Spex.utils.config_manager import ConfigManager
        bundled = ConfigManager()._load_json_config("checks", use_last_used=False)
        min_run = int(job.get("min_run_length") or bundled["outputs"]["frame_analysis"]["duplicate_min_run_length"])
        rng = np.random.default_rng([job["seed"], int(job["video_id"][-5:])])
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(job["keep"] or tmp)
            work.mkdir(parents=True, exist_ok=True)
            name = job["video_id"]
            clean = work / f"{name}_clean.mkv"
            res = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{job['start']:.3f}", "-t", f"{job['seconds']:.3f}",
                        "-i", job["video"], "-map", "0:v:0", "-c", "copy", str(clean)])
            if res.returncode:
                raise RuntimeError(f"ffmpeg could not cut the clip: {res.stderr.decode(errors='replace')[-300:]}")
            fmt = video_format(clean)
            control = _duplicate_events(clean, work, min_run)
            out["control"] = len(control)
            plan = duplicate_plan(int(job["seconds"] * fmt[4]), fmt[4])
            for tag, sigma in (("identical", 0.0), ("redigitized", REDIGITIZED_NOISE_SIGMA)):
                dest = work / f"{name}_{tag}.mkv"
                faults = write_frozen(clean, dest, fmt, plan, rng, sigma * (1.0 if fmt[2].endswith("10le") else 0.25))
                extra = match_faults(faults, _duplicate_events(dest, work, min_run))
                out["false_alarms"] += sum(not any(abs(s - c) <= 0.5 for _, c, _ in control) for _, s, _ in extra)
                out["faults"] += faults
        for f in out["faults"]:
            f["video_id"] = job["video_id"]
            f["min_run_length"] = min_run
        out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        out["trace"] = traceback.format_exc()
    out["seconds"] = round(time.time() - started, 1)
    return out


RUNNERS = {"dropped-samples": run_dropped_samples, "tone-leak": run_tone_leak,
           "duplicate-frames": run_duplicate_frames}
TOPICS = {"dropped-samples": "dropped_samples", "tone-leak": "tone_leak", "duplicate-frames": "duplicate_frames"}
DEFAULT_SECONDS = {"dropped-samples": 120.0, "tone-leak": 128.0, "duplicate-frames": 45.0}
