"""Scan core of video dropout detection, on synthetic frames."""
import numpy as np
import pytest

from AV_Spex.checks.video_dropout_detection import (
    BLACK, WHITE, StreakParams, find_streaks,
)

H, W = 486, 720
P = StreakParams()


def _frames(level=100, noise=0, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(3):
        f = np.full((H, W), level, dtype=np.int16)
        if noise:
            f = f + rng.integers(-noise, noise + 1, size=(H, W))
        out.append(np.clip(f, 0, 255).astype(np.uint8))
    return out


def test_white_streak_in_one_frame_is_found():
    prev, cur, nxt = _frames()
    cur[201, 300:312] = 235
    runs, n, _ = find_streaks(prev, cur, nxt, P)
    assert n == 1
    r = runs[0]
    assert (r.line, r.x, r.length, r.polarity) == (201, 300, 12, WHITE)
    assert r.spatial == pytest.approx(135) and r.temporal == pytest.approx(135)
    assert r.flatness == pytest.approx(0)


def test_black_streak_is_found_with_black_polarity():
    prev, cur, nxt = _frames(level=150)
    cur[120, 400:420] = 16
    runs, n, _ = find_streaks(prev, cur, nxt, P)
    assert n == 1 and runs[0].polarity == BLACK and runs[0].length == 20


def test_line_present_in_adjacent_frames_is_not_a_streak():
    """A persistent thin bright line (text, an edge) fails the temporal test."""
    prev, cur, nxt = _frames()
    for f in (prev, cur, nxt):
        f[201, 300:340] = 235
    assert find_streaks(prev, cur, nxt, P)[1] == 0


def test_line_in_only_one_neighbour_frame_still_fails_temporal_test():
    """A streak lasting two frames is judged against the max of both neighbours, so the frame
    whose neighbour shares the line does not pass (the other frame of the pair carries it)."""
    prev, cur, nxt = _frames()
    prev[201, 300:340] = 235
    cur[201, 300:340] = 235
    assert find_streaks(prev, cur, nxt, P)[1] == 0


def test_area_change_fails_the_field_line_test():
    """A flash brightens a whole region in one frame: no single line stands out."""
    prev, cur, nxt = _frames()
    cur[100:300, 100:600] = 235
    assert find_streaks(prev, cur, nxt, P)[1] == 0


def test_neighbour_in_other_field_does_not_mask_the_streak():
    """Interlaced: the adjacent frame line belongs to the other field, so a bright line next
    to the streak in the other field is irrelevant."""
    prev, cur, nxt = _frames()
    cur[200, 300:312] = 235     # other field, also new this frame
    cur[201, 300:312] = 235
    runs, n, _ = find_streaks(prev, cur, nxt, P)
    assert {r.line for r in runs} == {200, 201}


def test_margins_and_short_runs_are_ignored():
    prev, cur, nxt = _frames()
    cur[H - 4, 300:340] = 235          # head-switching area
    cur[201, 2:20] = 235               # side blanking
    cur[250, 300:303] = 235            # shorter than min_length
    assert find_streaks(prev, cur, nxt, P)[1] == 0


def test_runs_are_capped_but_counted():
    prev, cur, nxt = _frames()
    params = StreakParams(max_runs_per_frame=5)
    for line in range(20, 420, 4):
        cur[line, 300:310] = 235
    runs, n, pixels = find_streaks(prev, cur, nxt, params)
    assert len(runs) == 5 and n == 100 and pixels == 1000


def test_noise_alone_produces_no_runs_at_default_thresholds():
    prev, cur, nxt = _frames(noise=8)
    assert find_streaks(prev, cur, nxt, P)[1] == 0


# --- event filter ----------------------------------------------------------------------------

from AV_Spex.checks.video_dropout_detection import (  # noqa: E402
    EventFilter, passes_filter, recurrence, symmetry,
)


def test_symmetry_is_the_ratio_of_the_weaker_to_the_stronger_contrast():
    assert symmetry(50, 100) == pytest.approx(0.5)
    assert symmetry(100, 50) == pytest.approx(0.5)
    assert symmetry(0, 0) == 0.0


def test_recurrence_ignores_the_adjacent_frames_and_counts_frames_not_runs():
    pos = {
        99: [(200, 300, 10)],                       # adjacent: skipped (a two-frame dropout)
        95: [(204, 310, 8), (206, 312, 8)],         # two runs nearby: one frame
        90: [(260, 300, 10)],                       # too far vertically
        80: [(200, 300, 10)],                       # outside the window
    }
    assert recurrence(100, 200, 300, 10, pos) == 1


def test_filter_rejects_asymmetric_and_recurring_runs():
    f = EventFilter(max_recurrence=0)
    assert passes_filter(100, 200, 300, 10, 60, 62, 60, {}, f)
    assert not passes_filter(100, 200, 300, 10, 30, 90, 30, {}, f)      # motion-like
    assert not passes_filter(100, 200, 300, 10, 60, 62, 60, {110: [(200, 300, 10)]}, f)


def test_filter_keeps_only_runs_inside_the_field_ratio_band():
    """other_field is judged against the weaker of the two contrasts (60 here)."""
    f = EventFilter()
    assert passes_filter(100, 200, 300, 10, 60, 62, 54, {}, f)          # 0.90: lower edge
    assert passes_filter(100, 200, 300, 10, 60, 62, 72, {}, f)          # 1.20: upper edge
    assert not passes_filter(100, 200, 300, 10, 60, 62, 20, {}, f)      # in both fields: content
    assert not passes_filter(100, 200, 300, 10, 60, 62, 90, {}, f)      # above the band
    wide = EventFilter(min_field_ratio=0.0, max_field_ratio=float("inf"))
    assert passes_filter(100, 200, 300, 10, 60, 62, 20, {}, wide)


def test_filter_drops_short_faint_runs_below_the_score_cut():
    """score = length x the weaker contrast; the frozen cut is 400."""
    f = EventFilter()
    assert passes_filter(100, 200, 300, 10, 40, 41, 40, {}, f)          # 400: on the cut
    assert not passes_filter(100, 200, 300, 5, 40, 41, 40, {}, f)       # 200: short and faint
    assert passes_filter(100, 200, 300, 5, 80, 82, 80, {}, f)           # 400: short but strong
    assert passes_filter(100, 200, 300, 5, 40, 41, 40, {}, EventFilter(min_score=0))


def test_frozen_operating_point():
    """Frozen 2026-11-10 for the held-out confirmation; a change here needs a new study round."""
    f = EventFilter()
    assert (f.min_symmetry, f.max_recurrence, f.min_field_ratio, f.max_field_ratio, f.min_score) \
        == (0.9, 2, 0.9, 1.2, 400.0)


def test_single_field_streak_has_field_ratio_near_one():
    prev, cur, nxt = _frames()
    cur[201, 300:312] = 235
    r = find_streaks(prev, cur, nxt, P)[0][0]
    assert r.other_field == pytest.approx(135)
    assert r.field_ratio == pytest.approx(1.0)


def test_object_in_both_fields_has_low_field_ratio():
    """A glint two frame lines tall covers one line of each field: it still passes the same-field
    and temporal tests, but barely stands out against the opposite field."""
    prev, cur, nxt = _frames()
    cur[200:202, 300:312] = 235
    runs = find_streaks(prev, cur, nxt, P)[0]
    assert runs and all(r.field_ratio < 0.1 for r in runs)


# --- event model -----------------------------------------------------------------------------

from AV_Spex.checks import video_dropout_detection as vdd  # noqa: E402


def _logit(n_runs, pixels, flatness=30.0, length=14):
    return vdd.event_logit(vdd.model_features(length, 60, 62, 61, 150, flatness, "white", 200,
                                              n_runs, pixels))


def test_model_constants_line_up_with_the_features():
    n = len(vdd.model_features(10, 60, 62, 61, 150, 20, "white", 200, 3, 80))
    assert n == 20
    assert len(vdd.MODEL_MEAN) == len(vdd.MODEL_SCALE) == len(vdd.MODEL_WEIGHTS) == n


def test_isolated_streak_in_a_quiet_frame_is_reported_and_one_in_a_busy_frame_is_not():
    """The model's main lever: the same run scores high alone and low among many others."""
    quiet, busy = _logit(2, 40), _logit(60, 1500)
    assert vdd.is_dropout_event(quiet) and not vdd.is_dropout_event(busy)


def test_a_run_that_varies_along_its_length_scores_higher_than_a_flat_one():
    assert _logit(4, 80, flatness=30.0) > _logit(4, 80, flatness=5.0)


def test_frozen_model():
    """Frozen 2026-11-12 for the confirmation sheet; a refit needs a new study round."""
    assert vdd.MODEL_THRESHOLD == 0.8608 and vdd.MODEL_BIAS == 0.746318
    assert _logit(2, 40) == pytest.approx(1.5650, abs=1e-3)


# --- tape-level assessment over the analysis periods -----------------------------------------

FPS = 30.0


def _frame(index, start=0.0, dropout=False):
    """A scanned frame; with `dropout`, one isolated streak that passes the filter and model."""
    runs = [vdd.StreakRun(line=200, x=300, length=14, polarity="white", spatial=60, temporal=62,
                          level=150, flatness=30.0, other_field=61)] if dropout else []
    return vdd.FrameStreaks(index=index, time=start + index / FPS, runs=runs,
                            n_runs=len(runs) * 2, candidate_pixels=40 if dropout else 0)


def _fake_scan(per_period, short=(), fail=()):
    """scan_video stand-in: `per_period` maps a period start to its number of dropout frames."""
    def scan(video, width, height, fps, params, start=0.0, duration=None, check_cancelled=None):
        if start in fail:
            raise OSError("decode failed")
        n = int(duration * fps) - 2
        if start in short:
            n //= 4
        hits = per_period.get(start, 0)
        step = max(1, n // max(1, hits)) if hits else 0
        # spread the dropout frames out so no two fall inside the recurrence window
        wanted = {i * step for i in range(hits)} if hits else set()
        return iter([_frame(i, start, i in wanted) for i in range(n)])
    return scan


def _assess(per_period, periods=None, **kw):
    periods = periods if periods is not None else [(s, 30) for s in (0.0, 100.0, 200.0, 300.0, 400.0, 500.0)]
    return vdd.assess_periods("x.mkv", 720, 486, FPS, periods, scan=_fake_scan(per_period, **kw))


def test_the_test_streak_is_one_the_filter_and_model_report():
    frames = [_frame(i, dropout=(i == 50)) for i in range(100)]
    hits = vdd.reported_frames(frames)
    assert [f.index for f, _run, _logit in hits] == [50]


def test_a_streak_that_recurs_in_place_is_not_reported():
    """Recurrence is the filter's test for picture content: the same spot, frame after frame."""
    frames = [_frame(i, dropout=True) for i in range(100)]
    assert vdd.reported_frames(frames) == []


def test_tape_with_dropout_in_every_period_is_flagged():
    result = _assess({s: 10 for s in (0.0, 100.0, 200.0, 300.0, 400.0, 500.0)})   # ~20 a minute
    assert result.status == vdd.STATUS_FLAGGED
    assert result.median_per_minute >= vdd.TAPE_FLAG_MEDIAN_PER_MINUTE
    assert result.reported_frames == 60 and len(result.periods) == 6
    assert len(result.examples) == 6     # one per period, so the images are spread over the tape


def test_one_busy_period_does_not_flag_a_tape():
    """JPC_AV_02217: 76 a minute in one period, almost nothing in the rest."""
    result = _assess({0.0: 38, 100.0: 1, 300.0: 2})
    assert result.status == vdd.STATUS_NOT_FLAGGED
    assert result.total_per_minute > result.median_per_minute
    assert [e["period_start"] for e in result.examples] == [0.0, 100.0, 300.0]


def test_clean_tape_is_not_flagged_and_has_no_examples():
    result = _assess({})
    assert result.status == vdd.STATUS_NOT_FLAGGED
    assert result.median_per_minute == 0 and result.examples == []


def test_no_periods_is_unknown_not_a_pass():
    result = _assess({}, periods=[])
    assert result.status == vdd.STATUS_UNKNOWN and result.median_per_minute is None


def test_periods_that_cannot_be_decoded_are_unknown_not_a_pass():
    every = (0.0, 100.0, 200.0, 300.0, 400.0, 500.0)
    assert _assess({}, fail=every).status == vdd.STATUS_UNKNOWN
    assert _assess({}, short=every).status == vdd.STATUS_UNKNOWN
    # one unreadable period in six leaves under 90% of the frames: still not a verdict
    assert _assess({}, fail=(0.0,)).status == vdd.STATUS_UNKNOWN


def test_cancelling_returns_nothing():
    result = vdd.assess_periods("x.mkv", 720, 486, FPS, [(0.0, 30)], scan=_fake_scan({}),
                                check_cancelled=lambda: True)
    assert result is None


def test_frozen_tape_threshold():
    """Set 2026-11-15 from the ten-tape skim; changing it needs fresh tapes to confirm."""
    assert vdd.TAPE_FLAG_MEDIAN_PER_MINUTE == 15.0 and vdd.MIN_SCANNED_FRACTION == 0.9
