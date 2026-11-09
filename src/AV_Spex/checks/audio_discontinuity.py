"""
Audio-domain discontinuity scan for dropped-sample detection.

When frames (and the audio samples riding with them) are lost in a capture
chain, the two sides of the gap are butted together wherever the waveform
happens to be. Unlike a frame-accurate edit, that join is almost never at a
zero crossing, so the waveform takes a step — a broadband click that shows up
in a spectrogram as a thin vertical line. A capture that loses its input
altogether (SDI cable pulled, de-embedder losing lock) instead writes exact
digital zeros until the signal comes back.

This module looks for both directly in the decoded PCM:

* **Splices** — a sample where two independent detectors fire together, on
  every channel carrying signal:

  1. *LPC residual*: each block is whitened by a short linear predictor, so
     predictable content (tone, speech, music) drops out and a step leaves an
     impulse. Scored against the block's robust (MAD) residual level.
  2. *Near-Nyquist energy*: everything that came through the ADC was
     anti-alias filtered, so the band just below Nyquist holds little more
     than the converter's noise floor. A step made *after* the converter puts
     energy all the way up to Nyquist. Scored against the band's local level.

  Content transients trip one or the other — loud impulsive content raises the
  residual but not the near-Nyquist band, converter noise bursts the reverse
  — so the per-channel score is the *minimum* of the two. A capture splice
  lands on every channel at the same sample, so the event score is the
  minimum over the channels that carry signal (a near-silent channel can't
  show a jump and is ignored rather than allowed to veto).

* **Digital-silence gaps** — runs where every channel of a stream is exactly
  zero, away from the head and tail of the file. Analog-origin audio never
  produces exact zeros on its own; a run of them mid-program means the
  capture chain stopped receiving audio.

Calibration (Nov 2026): two LC-made test files (SmashEditTest.mkv — a few
seconds cut out of a bars/tone recording; DropOutTest.mkv — SDI patch pulled
three times) plus JPC sample tapes as negatives, and synthetic splices cut
into real program audio. See tests/test_audio_discontinuity.py and
developer_docs/processing-internals.md.

Limits, stated plainly: a splice through quiet or slowly-varying content
makes only a small step and is not detectable here (nor audible). Recall on
real program audio is roughly "the audible ones". A single dropped sample in
program audio is almost never detectable this way.
"""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import numpy as np
from scipy.linalg import solve_toeplitz
from scipy.ndimage import maximum_filter1d
from scipy.signal import butter, lfilter, sosfiltfilt

from AV_Spex.utils.log_setup import logger


# --- Tuning -----------------------------------------------------------------

# LPC whitening: order and block length (samples).
LPC_ORDER = 24
LPC_BLOCK = 4096

# Near-Nyquist band starts at this fraction of Nyquist (21.6 kHz at 48 kHz).
HF_CUTOFF_FRACTION = 0.9

# A channel "carries signal" at a sample when its RMS over the surrounding
# 100 ms is above this level. Channels below it are ignored for coincidence.
ACTIVE_LEVEL_DBFS = -50.0

# The two activity windows start this far from the sample being scored, so a
# tone that stops dead (LC 21458342/21459128 at the end of reference tone) is
# not "signal on both sides" just because the last cycle sits in the window.
ACTIVITY_GAP_MS = 1.0

# Residual impulses smaller than this are ignored outright (an inaudible step).
MIN_STEP_DBFS = -60.0

# Event threshold on min(LPC score, HF score), min over active channels.
# Real-tape background peaks reach ~34 (JPC_AV_02041, loud impulsive content);
# the LC test splices score in the hundreds to thousands.
SPLICE_SCORE_THRESHOLD = 40.0

# Every other active channel must reach at least this at the same sample.
SECONDARY_SCORE_MIN = 4.0

# A splice is compact: one discontinuity, so within ±EVENT_MERGE_MS of the peak
# only a few samples score above a quarter of it — 5 for the LC splice, 7-19
# for synthetic one-frame cuts in real program (the 5-sample max filters in
# score_samples account for 4). Analog damage — clipped or crackling program —
# spreads over 25-138 samples (JPC_AV_01663/01710, scoring up to 135).
MAX_EVENT_WIDTH_SAMPLES = 20

# Samples within this many ms of one another are one event.
EVENT_MERGE_MS = 10.0

# Findings within this many seconds of the start or end of a stream are
# ignored: the recording starting mid-waveform is not a splice (JPC_AV_02213
# scores 97 at 0.05 s), and leading/trailing silence is normal.
EDGE_GUARD_S = 1.0

# Digital-silence gaps shorter than this are ignored.
MIN_SILENCE_GAP_MS = 5.0

# A splice within this many ms of a silence-gap edge is that gap's edge, not a
# separate finding.
GAP_EDGE_ATTRIBUTION_MS = 20.0

# Decode/analysis chunk length (seconds) and analysis context on each side.
CHUNK_SECONDS = 20.0
CONTEXT_SECONDS = 0.1


@dataclass
class SpliceEvent:
    time_s: float          # wall-clock seconds from the start of the stream
    stream: int            # 0-based audio stream index
    score: float           # min(LPC, HF) score of the strongest active channel
    step_dbfs: float       # largest residual impulse at the event, dBFS
    channels: int          # number of active channels (all must agree)


@dataclass
class SilenceGap:
    start_s: float
    end_s: float
    stream: int

    @property
    def duration_ms(self) -> float:
        return (self.end_s - self.start_s) * 1000.0


@dataclass
class DiscontinuityScan:
    splices: List[SpliceEvent] = field(default_factory=list)
    silence_gaps: List[SilenceGap] = field(default_factory=list)
    streams_scanned: int = 0
    seconds_scanned: float = 0.0
    sample_rate: int = 0
    # False when no stream could be decoded — "measured nothing" must stay
    # distinguishable from "could not measure".
    measured: bool = False


# --- Per-chunk scoring ------------------------------------------------------

def _lpc_scores(x: np.ndarray) -> np.ndarray:
    """|LPC residual| / robust local residual level, per sample (1-D input).

    The local level is the largest MAD-derived sigma among this block and its
    neighbours. A block straddling a loud onset otherwise takes its level from
    the quiet half and scores the loud half as one long impulse (a test tone
    switched on in JPC_AV_01581 scored 102 that way).
    """
    n = len(x)
    res_all = np.zeros(n)
    window = np.hanning(LPC_BLOCK + LPC_ORDER)
    starts = list(range(0, n, LPC_BLOCK))
    sigmas = np.zeros(len(starts))
    for k, b in enumerate(starts):
        lo = max(0, b - LPC_ORDER)
        seg = x[lo:b + LPC_BLOCK]
        if len(seg) <= LPC_ORDER + 8:
            continue
        ww = seg * window[:len(seg)]
        r = np.array([np.dot(ww[:len(ww) - j], ww[j:]) for j in range(LPC_ORDER + 1)])
        if r[0] <= 1e-20:
            continue  # digital silence: nothing to predict, nothing to score
        r[0] = r[0] * (1.0 + 1e-6) + 1e-12  # light regularization
        try:
            a = solve_toeplitz(r[:LPC_ORDER], r[1:LPC_ORDER + 1])
        except Exception:
            continue
        res = lfilter(np.r_[1.0, -a], [1.0], seg)[b - lo:]
        res_all[b:b + len(res)] = res
        sigmas[k] = 1.4826 * np.median(np.abs(res))
    local = maximum_filter1d(sigmas, 3) if len(sigmas) else sigmas
    level = np.repeat(np.maximum(local, 1e-9), LPC_BLOCK)[:n]
    score = np.abs(res_all) / level
    score[np.abs(res_all) < 10 ** (MIN_STEP_DBFS / 20)] = 0.0
    return score


def _hf_scores(x: np.ndarray, sos, block: int) -> np.ndarray:
    """|near-Nyquist band| / robust local band level, per sample (1-D input).

    Levels are block medians; the local level is the largest within ±2 blocks,
    for the same straddling reason as in _lpc_scores.
    """
    n = len(x)
    if n <= 64:
        return np.zeros(n)
    y = np.abs(sosfiltfilt(sos, x))
    nb = (n + block - 1) // block
    lev = np.array([np.median(y[i * block:(i + 1) * block]) for i in range(nb)])
    lev = maximum_filter1d(lev, 5)
    lev = np.repeat(np.maximum(lev * 1.4826, 1e-6), block)[:n]
    return y / lev


def _active_mask(a: np.ndarray, win: int, gap: int = 2) -> np.ndarray:
    """(channels, n) bool: channel carries signal on BOTH sides of each sample.

    True where the RMS over the `win` samples before (ending `gap` samples
    early) and the `win` samples after (starting `gap` samples late) are both
    above ACTIVE_LEVEL_DBFS. Two-sided on purpose: a splice joins signal to
    signal, while a step out of near-silence is indistinguishable from content
    starting (a test tone switched on, JPC_AV_01581 at ~1407 s). Joins into or
    out of exact digital silence are the silence-gap detector's job.
    """
    n = len(a)
    thr = (10 ** (ACTIVE_LEVEL_DBFS / 20)) ** 2
    c = np.vstack([np.zeros((1, a.shape[1])), np.cumsum(a ** 2, axis=0)])
    idx = np.arange(n)
    b_hi = np.clip(idx - gap, 0, n)
    b_lo = np.clip(b_hi - win, 0, n)
    a_lo = np.clip(idx + gap + 1, 0, n)
    a_hi = np.clip(a_lo + win, 0, n)
    before = (c[b_hi] - c[b_lo]) / np.maximum(b_hi - b_lo, 1)[:, None]
    after = (c[a_hi] - c[a_lo]) / np.maximum(a_hi - a_lo, 1)[:, None]
    return ((before > thr) & (after > thr)).T


def score_samples(a: np.ndarray, sample_rate: int):
    """Event scores per sample for a (n, channels) float array.

    Returns (primary, secondary, n_active, step), each of length n:
    the highest per-channel min(LPC, HF) score among active channels, the
    lowest among the *other* active channels (inf when only one is active),
    the active-channel count, and the largest sample-to-sample step.

    Why not a plain minimum over channels: a splice lands on every channel at
    the same sample, but the size of each channel's step depends on where its
    own waveform was at the cut. On stereo or uncorrelated channels one channel
    routinely takes a small step, so requiring every channel to clear the full
    threshold lost about half the audible synthetic splices. The other active
    channels must instead show a weak coincident response (SECONDARY_SCORE_MIN)
    — far above a typical sample, far below the event threshold.
    """
    n, ch = a.shape
    sos = butter(10, HF_CUTOFF_FRACTION * sample_rate / 2, 'highpass',
                 fs=sample_rate, output='sos')
    block = max(1, int(sample_rate * 0.025))
    active = _active_mask(a, max(1, int(sample_rate * 0.02)),
                          gap=max(2, int(sample_rate * ACTIVITY_GAP_MS / 1000)))
    per_ch = np.zeros((ch, n))
    for c in range(ch):
        lpc = maximum_filter1d(_lpc_scores(a[:, c]), 5)
        hf = maximum_filter1d(_hf_scores(a[:, c], sos, block), 5)
        per_ch[c] = np.minimum(lpc, hf)
    n_active = active.sum(axis=0)
    masked = np.where(active, per_ch, -np.inf)
    primary = masked.max(axis=0)
    primary[n_active == 0] = 0.0
    if ch > 1:
        # Lowest score among the active channels other than the strongest one.
        ranked = np.sort(masked, axis=0)  # ascending; inactive (-inf) first
        first_active = np.clip(ch - n_active, 0, ch - 1)
        lowest_active = np.take_along_axis(ranked, first_active[None, :], axis=0)[0]
        secondary = np.where(n_active >= 2, lowest_active, np.inf)
    else:
        secondary = np.full(n, np.inf)
    step = np.abs(np.diff(a, axis=0, prepend=a[:1])).max(axis=1)
    return primary, secondary, n_active, step


def find_splices(a: np.ndarray, sample_rate: int, offset: int = 0,
                 valid: Optional[tuple] = None, stream: int = 0) -> List[SpliceEvent]:
    """Splice events in (n, channels) array `a`; sample indices are offset by
    `offset`. Only events inside `valid` (lo, hi) local indices are returned."""
    score, secondary, n_active, step = score_samples(a, sample_rate)
    lo, hi = valid if valid else (0, len(a))
    hit = (score > SPLICE_SCORE_THRESHOLD) & (secondary > SECONDARY_SCORE_MIN)
    idx = np.where(hit[lo:hi])[0] + lo
    if len(idx) == 0:
        return []
    merge = int(sample_rate * EVENT_MERGE_MS / 1000)
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= merge:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    events = []
    for g in groups:
        g = np.asarray(g)
        i = int(g[np.argmax(score[g])])
        around = score[max(0, i - merge):i + merge + 1]
        if int((around > score[i] / 4).sum()) > MAX_EVENT_WIDTH_SAMPLES:
            continue  # a burst, not a single discontinuity
        w = step[max(0, i - 4):i + 5].max()
        events.append(SpliceEvent(
            time_s=round((offset + i) / sample_rate, 4),
            stream=stream,
            score=round(float(score[i]), 1),
            step_dbfs=round(float(20 * np.log10(max(w, 1e-12))), 1),
            channels=int(n_active[i]),
        ))
    return events


def find_silence_runs(a: np.ndarray, min_len: int, offset: int = 0):
    """(start, end) absolute sample indices of all-channel exact-zero runs."""
    z = np.all(a == 0, axis=1).astype(np.int8)
    d = np.diff(np.r_[0, z, 0])
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    return [(int(s) + offset, int(e) + offset) for s, e in zip(starts, ends) if e - s >= min_len]


# --- Decoding ---------------------------------------------------------------

def _audio_streams(video_path) -> list:
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'a',
           '-show_entries', 'stream=index,channels,sample_rate', '-of', 'json',
           str(video_path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout or '{}').get('streams', [])
    except Exception as e:
        logger.warning(f"Could not list audio streams for discontinuity scan: {e}")
        return []


def _decode_chunks(video_path, stream: int, channels: int, sample_rate: int,
                   check_cancelled: Callable[[], bool]):
    """Yield float64 (n, channels) chunks of one audio stream."""
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-i', str(video_path),
           '-map', f'0:a:{stream}', '-f', 'f32le', '-acodec', 'pcm_f32le', '-']
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_bytes = 4 * channels
    nbytes = int(CHUNK_SECONDS * sample_rate) * frame_bytes
    try:
        while True:
            if check_cancelled():
                return
            buf = proc.stdout.read(nbytes)
            if not buf:
                break
            usable = len(buf) - len(buf) % frame_bytes
            yield np.frombuffer(buf[:usable], dtype=np.float32).reshape(-1, channels).astype(np.float64)
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.terminate()
        proc.wait()


# --- Driver -----------------------------------------------------------------

def scan_audio_discontinuities(video_path, check_cancelled=None,
                               progress: Optional[Callable[[float], None]] = None,
                               duration_s: Optional[float] = None) -> DiscontinuityScan:
    """Scan every audio stream of `video_path` for splices and silence gaps.

    `progress`, if given, is called with a 0–1 fraction as decoding advances
    (needs `duration_s` to be meaningful).
    """
    check_cancelled = check_cancelled or (lambda: False)
    scan = DiscontinuityScan()
    streams = _audio_streams(video_path)
    if not streams:
        return scan

    total = (duration_s or 0) * len(streams)
    done_s = 0.0
    for s_idx, st in enumerate(streams):
        ch = int(st.get('channels') or 0)
        sr = int(st.get('sample_rate') or 0)
        if ch <= 0 or sr <= 0:
            continue
        scan.sample_rate = scan.sample_rate or sr
        ctx = int(sr * CONTEXT_SECONDS)
        min_gap = int(sr * MIN_SILENCE_GAP_MS / 1000)

        stream_splices: List[SpliceEvent] = []
        zero_runs = []
        carry = np.zeros((0, ch))
        buf_offset = 0     # absolute sample index of carry[0] / buf[0]
        first = True
        pending = None
        chunks = _decode_chunks(video_path, s_idx, ch, sr, check_cancelled)
        nxt = next(chunks, None)
        while nxt is not None:
            pending, nxt = nxt, next(chunks, None)
            eof = nxt is None
            buf = np.concatenate([carry, pending]) if len(carry) else pending
            lo = 0 if first else ctx
            hi = len(buf) if eof else max(lo, len(buf) - ctx)

            # Silence runs: exact zeros don't need context, so scan only the
            # new territory [lo, hi) and stitch runs across chunk joins.
            for rs, re_ in find_silence_runs(buf[lo:hi], 1, buf_offset + lo):
                if zero_runs and zero_runs[-1][1] == rs:
                    zero_runs[-1] = (zero_runs[-1][0], re_)
                else:
                    zero_runs.append((rs, re_))

            stream_splices.extend(find_splices(buf, sr, buf_offset, (lo, hi), s_idx))

            carry = buf[hi - ctx:] if hi - ctx > 0 else buf
            buf_offset += len(buf) - len(carry)
            first = False
            done_s += len(pending) / sr
            if progress and total > 0:
                progress(min(1.0, done_s / total))
        if check_cancelled():
            return scan

        n_samples = buf_offset + len(carry)
        stream_seconds = n_samples / sr
        scan.seconds_scanned = max(scan.seconds_scanned, stream_seconds)
        scan.streams_scanned += 1

        guard = int(sr * EDGE_GUARD_S)
        gaps = [SilenceGap(round(s / sr, 4), round(e / sr, 4), s_idx)
                for s, e in zero_runs
                if e - s >= min_gap and s > guard and e < n_samples - guard]
        scan.silence_gaps.extend(gaps)

        attr = GAP_EDGE_ATTRIBUTION_MS / 1000
        for ev in stream_splices:
            if ev.time_s * sr < guard or ev.time_s * sr > n_samples - guard:
                continue
            if any(g.start_s - attr <= ev.time_s <= g.end_s + attr for g in gaps):
                continue
            scan.splices.append(ev)

    scan.measured = scan.streams_scanned > 0
    return scan
