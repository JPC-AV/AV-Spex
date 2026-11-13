"""
Video dropout detection: a pixel-level scan for short horizontal streaks.

Tape dropout (oxide shed, hydrolysis) is a brief loss of RF signal on one head pass. On the
picture it shows as short horizontal lines, most often white, sometimes black, on one field
line, in one or two frames. A QCTools report cannot find it: tested against operator labels
(Nov 2026), frame-wide signalstats (TOUT, YMAX, MSE, YDIF), even over the active area, rank
labeled dropout bins no better than chance within a tape, because transitions and motion
dominate any whole-frame statistic. So this module reads the picture.

A pixel is a streak candidate when, in its own field, it is brighter (white) or darker (black)
than both field lines above and below it by `spatial_threshold`, *and* brighter/darker than the
same pixel in both the previous and the next frame by `temporal_threshold`. The temporal test
removes picture edges and text that persist across frames; the field-line test removes area
changes such as flashes and cuts. Candidates are grouped into horizontal runs, and runs of at
least `min_length` pixels are reported with their contrast and flatness, so a later stage (or a
calibration replay) can decide which runs are dropout.

This file holds the scan core only. Thresholds here are deliberately loose: the November 2026
study (dev_tools/dropout_scan.py, dev_tools/dropout_sheet.py) caches every loose run per tape and
tunes the operating point against operator verdicts before the detector is wired into the app.
"""
import logging
import subprocess
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Iterator, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

WHITE = "white"
BLACK = "black"


@dataclass(frozen=True)
class StreakParams:
    """Scan thresholds, in 8-bit luma code values (the scan decodes to 8-bit gray)."""
    spatial_threshold: int = 25     # vs both field lines above and below
    temporal_threshold: int = 25    # vs the same pixel in the previous and next frame
    min_length: int = 5             # pixels in one horizontal run
    margin_top: int = 8             # frame lines skipped at the top (blanking, VITC remnants)
    margin_bottom: int = 12         # frame lines skipped at the bottom (head switching)
    margin_side: int = 24           # pixels skipped left and right (blanking edges)
    max_runs_per_frame: int = 100   # runs kept per frame, strongest first; the count is kept whole


@dataclass
class StreakRun:
    """One horizontal run of streak pixels."""
    line: int           # frame line (0 = top); field is line % 2
    x: int              # first pixel
    length: int
    polarity: str       # WHITE or BLACK
    spatial: float      # mean contrast against the field lines above and below
    temporal: float     # mean contrast against the adjacent frames
    level: float        # mean luma of the run
    flatness: float     # std of luma along the run
    other_field: float = 0.0    # mean contrast against the opposite-field lines directly above
                                # and below (one frame line away)

    @property
    def field_ratio(self) -> float:
        """other_field / strength. Dropout is a loss on one head pass, so it sits on one field's
        line and stands out about as much against the other field as against its own: ~1.
        A real object (glint, teeth, an edge) is in both fields, so the ratio falls well
        below 1. Measured on the November 2026 pilots: 19 of 22 dropouts in [0.9, 1.2], 22 of
        156 non-dropouts."""
        return field_ratio(self.spatial, self.temporal, self.other_field)

    @property
    def strength(self) -> float:
        """The weaker of the two contrasts: a run is only as convincing as its weaker test."""
        return min(self.spatial, self.temporal)


@dataclass
class FrameStreaks:
    index: int          # frame number from the start of the scan
    time: float         # seconds, index / fps + scan start
    runs: List[StreakRun] = field(default_factory=list)
    n_runs: int = 0             # all runs >= min_length, before max_runs_per_frame
    candidate_pixels: int = 0   # pixels passing both tests, in runs of any length (busyness)


def _roi(height: int, width: int, p: StreakParams) -> np.ndarray:
    roi = np.zeros((height, width), dtype=bool)
    roi[p.margin_top:height - p.margin_bottom, p.margin_side:width - p.margin_side] = True
    return roi


def _field_neighbours_max(g: np.ndarray) -> np.ndarray:
    """Per pixel, the max of the lines directly above and below *in the same field*
    (two frame lines away). Edge lines get -inf, so they never pass; the ROI drops them anyway."""
    out = np.full(g.shape, -np.inf, dtype=np.float32)
    out[2:-2] = np.maximum(g[:-4], g[4:])
    return out


def _other_field_max(g: np.ndarray) -> np.ndarray:
    """Per pixel, the max of the lines directly above and below (the opposite field)."""
    out = np.full(g.shape, np.inf, dtype=np.float32)
    out[1:-1] = np.maximum(g[:-2], g[2:])
    return out


def _runs(mask: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(rows, starts, ends) of every horizontal run of True in a 2-D mask; ends exclusive."""
    padded = np.zeros((mask.shape[0], mask.shape[1] + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    d = np.diff(padded, axis=1)
    r_s, c_s = np.nonzero(d == 1)
    r_e, c_e = np.nonzero(d == -1)
    # np.nonzero is row-major, and each row has as many starts as ends, so they pair up.
    return r_s, c_s, c_e


def _row_means(values: np.ndarray, rows, starts, ends) -> np.ndarray:
    csum = np.zeros((values.shape[0], values.shape[1] + 1), dtype=np.float64)
    np.cumsum(values, axis=1, out=csum[:, 1:])
    return (csum[rows, ends] - csum[rows, starts]) / (ends - starts)


def find_streaks(prev: np.ndarray, cur: np.ndarray, nxt: np.ndarray,
                 params: StreakParams = StreakParams(),
                 roi: Optional[np.ndarray] = None) -> Tuple[List[StreakRun], int, int]:
    """Streak runs in `cur`, judged against its field neighbours and the adjacent frames.

    Frames are 2-D luma arrays of one size. Returns (runs, n_runs, candidate_pixels): the
    strongest `max_runs_per_frame` runs, the count of all runs >= min_length, and the number of
    pixels that passed both tests (a busyness figure: noise and motion pass many, a clean frame
    with one dropout passes few).
    """
    p = params
    if roi is None:
        roi = _roi(*cur.shape, p)
    cur_f = cur.astype(np.float32)
    prev_f = prev.astype(np.float32)
    nxt_f = nxt.astype(np.float32)

    runs: List[StreakRun] = []
    total_runs = 0
    candidate_pixels = 0
    for polarity, sign in ((WHITE, 1.0), (BLACK, -1.0)):
        g = sign * cur_f
        spatial = g - _field_neighbours_max(g)
        temporal = g - np.maximum(sign * prev_f, sign * nxt_f)
        mask = (spatial > p.spatial_threshold) & (temporal > p.temporal_threshold) & roi
        candidate_pixels += int(mask.sum())
        rows, starts, ends = _runs(mask)
        keep = (ends - starts) >= p.min_length
        rows, starts, ends = rows[keep], starts[keep], ends[keep]
        total_runs += len(rows)
        if not len(rows):
            continue
        sp = _row_means(np.where(mask, spatial, 0), rows, starts, ends)
        tp = _row_means(np.where(mask, temporal, 0), rows, starts, ends)
        lv = _row_means(cur_f, rows, starts, ends)
        sq = _row_means(cur_f * cur_f, rows, starts, ends)
        flat = np.sqrt(np.maximum(sq - lv * lv, 0))
        of = _row_means(g - _other_field_max(g), rows, starts, ends)
        for i in range(len(rows)):
            runs.append(StreakRun(
                line=int(rows[i]), x=int(starts[i]), length=int(ends[i] - starts[i]),
                polarity=polarity, spatial=float(sp[i]), temporal=float(tp[i]),
                level=float(lv[i]), flatness=float(flat[i]), other_field=float(of[i])))
    if len(runs) > p.max_runs_per_frame:
        runs.sort(key=lambda r: r.strength * r.length, reverse=True)
        runs = runs[:p.max_runs_per_frame]
    return runs, total_runs, candidate_pixels


def _frame_reader(video_path: str, width: int, height: int, start: float = 0.0,
                  duration: Optional[float] = None) -> Iterator[np.ndarray]:
    """Decode a video to 8-bit gray frames through an ffmpeg pipe."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(video_path), "-map", "0:v:0"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    size = width * height
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def scan_video(video_path: str, width: int, height: int, fps: float,
               params: StreakParams = StreakParams(), start: float = 0.0,
               duration: Optional[float] = None,
               check_cancelled=None) -> Iterator[FrameStreaks]:
    """Scan every frame (except the first and last, which lack a neighbour) and yield a
    FrameStreaks for each, in order. `time` is index / fps + start, matching QCTools
    pkt_pts_time for a file whose video starts at 0."""
    roi = _roi(height, width, params)
    window = deque(maxlen=3)
    index = -1
    for frame in _frame_reader(video_path, width, height, start, duration):
        index += 1
        window.append(frame)
        if len(window) < 3:
            continue
        if check_cancelled and index % 300 == 0 and check_cancelled():
            return
        prev, cur, nxt = window
        runs, n_runs, pixels = find_streaks(prev, cur, nxt, params, roi)
        mid = index - 1
        yield FrameStreaks(index=mid, time=start + mid / fps, runs=runs,
                           n_runs=n_runs, candidate_pixels=pixels)


def params_dict(params: StreakParams) -> dict:
    return asdict(params)


# --- event filter: which runs look like dropout rather than picture content ------------------
#
# Measured on the November 2026 pilot sheet (70 operator-judged candidates, 11 dropout):
#   - symmetry: dropout replaces the signal, so it stands out about equally against the field
#     lines around it and against the neighbouring frames. Content fails one test: motion
#     inflates the temporal contrast, fine detail (teeth, stripes, glints) the spatial one.
#     All 11 dropouts had min/max >= 0.9; 57% of the non-dropout candidates fell below it.
#   - recurrence: dropout lands at random positions, while content that trips the scan keeps
#     tripping it at about the same place for a while. 9 of the 11 dropouts had no run near them
#     in frames 2..15 either side; most content candidates had several.
#   - field ratio: dropout is a loss on one head pass, so it sits on one field's line and
#     stands out about as much against the opposite-field lines as against its own field
#     (ratio ~1). A real object (glint, teeth, an edge) is in both fields, so the ratio falls.
#     Over both pilots (178 judged rows, 22 dropout): 19 of 22 dropouts in [0.9, 1.2], 22 of 156
#     non-dropouts. The band was chosen after seeing those labels, so it is unconfirmed until a
#     sheet sampled with it applied (pilot3) is judged. Known loss: dropouts several lines tall
#     cover both fields and fail it.
#   - score: pilot3 sampled what the three tests above report (60 frames over 10 tapes) and
#     measured their precision at 33 of 55 (0.60); the field band discarded no dropout (0 of 20).
#     17 of the 22 false reports were interlaced motion edges, mostly 5-6 px long and faint, so
#     length x strength separates them: at >= 400, 25 of 30 are dropout, keeping 18 of the 20
#     dropouts the operator called clear. What it loses is mostly faint dropout.
# Operating point frozen 2026-11-10 for the held-out confirmation (the 42162 tapes). The score
# cut was chosen after seeing pilot3's labels, so the held-out sheet is its first real test.
# Do not retune these against held-out verdicts.

@dataclass(frozen=True)
class EventFilter:
    min_symmetry: float = 0.9       # min(spatial, temporal) / max(spatial, temporal)
    recurrence_window: int = 15     # frames either side searched for a run near this one
    recurrence_skip: int = 1        # frames either side not searched (a dropout can last two)
    recurrence_dx: int = 24         # pixels of horizontal slack around the run
    recurrence_dy: int = 12         # frame lines of vertical slack
    max_recurrence: int = 2         # frames with a nearby run allowed
    min_field_ratio: float = 0.9    # other_field / min(spatial, temporal), see StreakRun
    max_field_ratio: float = 1.2
    min_score: float = 400.0        # length x min(spatial, temporal)


def symmetry(spatial: float, temporal: float) -> float:
    hi = max(spatial, temporal)
    return min(spatial, temporal) / hi if hi > 0 else 0.0


def field_ratio(spatial: float, temporal: float, other_field: float) -> float:
    """Contrast against the opposite-field lines over the weaker same-field/temporal contrast."""
    s = min(spatial, temporal)
    return other_field / s if s > 0 else 0.0


def run_score(length: int, spatial: float, temporal: float) -> float:
    """Length times the weaker contrast: how much of a streak there is to see."""
    return length * min(spatial, temporal)


def recurrence(frame: int, line: int, x: int, length: int, positions_by_frame,
               f: EventFilter = EventFilter()) -> int:
    """Frames near `frame` (excluding the skipped ones) holding a run within the slack box.

    positions_by_frame maps frame index -> iterable of (line, x, length) for every run found."""
    count = 0
    lo, hi = x - f.recurrence_dx, x + length + f.recurrence_dx
    for g in range(frame - f.recurrence_window, frame + f.recurrence_window + 1):
        if abs(g - frame) <= f.recurrence_skip:
            continue
        for l2, x2, len2 in positions_by_frame.get(g, ()):
            if abs(l2 - line) <= f.recurrence_dy and x2 <= hi and x2 + len2 >= lo:
                count += 1
                break
    return count


def passes_filter(frame: int, line: int, x: int, length: int, spatial: float, temporal: float,
                  other_field: float, positions_by_frame, f: EventFilter = EventFilter()) -> bool:
    if symmetry(spatial, temporal) < f.min_symmetry:
        return False
    if run_score(length, spatial, temporal) < f.min_score:
        return False
    if not f.min_field_ratio <= field_ratio(spatial, temporal, other_field) <= f.max_field_ratio:
        return False
    return recurrence(frame, line, x, length, positions_by_frame, f) <= f.max_recurrence


# --- event model: weighing a reported run's measurements together ------------------------------
#
# The four tests above, each a single threshold, reached 0.55 precision on tapes the study had
# not seen (69 of 126 reported frames; November 2026 held-out sheet). Two measurements they
# ignore separate dropout from the false reports better than any of them:
#   - how busy the frame is: a dropout is an isolated streak in a quiet frame (median 4 runs),
#     while motion, graphics and edges trip the scan all over it (median 32);
#   - variation along the run: a dropout's luma varies more along its length than an edge's.
# So a run that passes the filter is then scored by a logistic model over ten measurements and
# their squares, fitted to the 156 operator-judged runs the filter reports (94 dropout, 42
# tapes; studies/2026-11_video_dropout/scripts/fit_event_model.py in the corpus). Scored with
# whole tapes left out, 66 of the 72 runs above the threshold were dropout (0.92), keeping 70%
# of the dropouts. Those labels include the held-out sheet, so that figure is in-sample in the
# sense that matters; it stands unconfirmed until a sheet sampled with the model applied is
# judged. Frozen 2026-11-12. Whole frames of static are busy, so the model scores them low: it
# is a detector of isolated streaks.

MODEL_FRAME_HEIGHT = 486.0      # the fit's tapes are all 486 lines; other heights are scaled to it


def model_features(length: int, spatial: float, temporal: float, other_field: float,
                   level: float, flatness: float, polarity: str, line: int,
                   frame_n_runs: int, frame_candidate_pixels: int,
                   height: float = MODEL_FRAME_HEIGHT) -> List[float]:
    """The model's inputs for one run: ten measurements, then their squares (which let a linear
    model prefer a middle range, as the field ratio needs)."""
    strength = min(spatial, temporal)
    base = [
        float(np.log(length)),
        float(np.log(strength)),
        symmetry(spatial, temporal),
        abs(field_ratio(spatial, temporal, other_field) - 1.03),   # 1.03: the dropout median
        level / 255.0,
        flatness / 50.0,
        1.0 if polarity == WHITE else 0.0,
        line / height,
        float(np.log1p(frame_n_runs)),          # every run in the frame, before the per-frame cap
        float(np.log1p(frame_candidate_pixels)),
    ]
    return base + [v * v for v in base]


# Fitted constants: one entry per model_features() value, in its order.
MODEL_MEAN = (
    2.73859, 4.244104, 0.959807, 0.046696, 0.474108,
    0.496256, 0.602564, 0.540691, 2.780376, 5.530342,
    7.908361, 18.226266, 0.922085, 0.003608, 0.294041,
    0.388516, 0.602564, 0.374913, 11.258871, 34.543866,
)
MODEL_SCALE = (
    0.639127, 0.46244, 0.029241, 0.037785, 0.263179,
    0.377154, 0.489368, 0.287344, 1.878398, 1.98977,
    4.115275, 4.013541, 0.055762, 0.005111, 0.253533,
    0.525288, 0.489368, 0.303746, 14.356688, 25.17486,
)
MODEL_WEIGHTS = (
    0.34954, 0.376255, -0.037199, -0.446236, -0.736179,
    1.0083, -0.011756, -0.860588, -0.595137, -1.133747,
    0.315465, 0.352252, -0.030275, 0.265988, 0.142219,
    -0.133198, -0.011756, 0.477322, 0.954051, -0.071393,
)
MODEL_BIAS = 0.746318
MODEL_THRESHOLD = 0.8608    # logit at which 70% of the judged dropouts were kept, tapes left out


def event_logit(features) -> float:
    """The model's score for model_features() output; higher is more like dropout."""
    x = (np.asarray(features, dtype=np.float64) - MODEL_MEAN) / MODEL_SCALE
    return float(x @ np.asarray(MODEL_WEIGHTS) + MODEL_BIAS)


def run_event_logit(run: StreakRun, frame_n_runs: int, frame_candidate_pixels: int,
                    height: float = MODEL_FRAME_HEIGHT) -> float:
    return event_logit(model_features(run.length, run.spatial, run.temporal, run.other_field,
                                      run.level, run.flatness, run.polarity, run.line,
                                      frame_n_runs, frame_candidate_pixels, height))


def is_dropout_event(logit: float, threshold: float = MODEL_THRESHOLD) -> bool:
    """Whether a run that already passed passes_filter() is reported as dropout."""
    return logit >= threshold
