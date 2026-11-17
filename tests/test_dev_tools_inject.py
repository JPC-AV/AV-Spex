"""dev_tools/_inject.py and inject.py: the fault functions and how a fault is counted as found.

The faults are what the recall figures rest on, so what is pinned is that each one is exactly
what it says (a butt joint, exact zeros, a sine at the stated level, a held frame), that it is
recorded where it lands in the output, and that matching does not credit a detection to a
fault it is nowhere near.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dev_tools"))
import _inject  # noqa: E402
import inject  # noqa: E402

SR = 48000


def ramp(seconds=10, channels=2):
    """Audio whose every sample is its own index (times 1000), so a cut is easy to see."""
    n = int(seconds * SR)
    return (np.arange(n, dtype=np.int64)[:, None] * 1000 * np.ones((1, channels), dtype=np.int64)).astype(np.int32)


def test_fault_positions_are_spaced_and_clear_of_the_ends():
    rng = np.random.default_rng(1)
    pos = _inject.fault_positions(60 * SR, SR, rng)
    assert len(pos) == 11
    assert pos[0] >= _inject.EDGE_S * SR and pos[-1] <= (60 - _inject.EDGE_S) * SR
    assert all(b - a >= (_inject.FAULT_SPACING_S - _inject.FAULT_JITTER_S) * SR for a, b in zip(pos, pos[1:]))
    assert _inject.fault_positions(60 * SR, SR, np.random.default_rng(1)) == pos       # repeatable


def test_splice_removes_exactly_the_cut_and_records_the_join_in_output_time():
    a = ramp()
    out, faults = _inject.splice_out(a, SR, [2 * SR, 5 * SR], [480, 1602])
    assert len(out) == len(a) - 480 - 1602
    first, second = faults
    j = int(round(first["time_s"] * SR))
    assert (out[j - 1, 0], out[j, 0]) == (a[2 * SR - 1, 0], a[2 * SR + 480, 0])       # butt joint
    assert first["time_s"] == pytest.approx(2.0)
    assert second["time_s"] == pytest.approx(5.0 - 480 / SR, abs=1e-4)                 # moved up by the first cut
    assert second["cut_frames"] == pytest.approx(1.0, abs=0.01)
    # the ramp moves 1000 per sample; cutting 480 samples makes a step 481 times that
    assert first["stands_out_db"] == pytest.approx(20 * np.log10(481), abs=0.1)


def test_splice_skips_a_cut_that_runs_off_the_end():
    a = ramp(2)
    out, faults = _inject.splice_out(a, SR, [SR, 2 * SR - 10], [100, 100])
    assert len(faults) == 1 and len(out) == len(a) - 100


def test_gaps_are_exact_zeros_on_every_channel_and_nothing_else_changes():
    a = ramp() + 1
    out, faults = _inject.zero_gaps(a, SR, [3 * SR], [10])
    (gap,) = faults
    lo, hi = 3 * SR, 3 * SR + 480
    assert not out[lo:hi].any() and (gap["time_s"], gap["end_s"], gap["gap_ms"]) == (3.0, 3.01, 10)
    assert np.array_equal(out[:lo], a[:lo]) and np.array_equal(out[hi:], a[hi:])
    assert a[lo:hi].all()                                                              # the input is not touched


def test_tones_land_on_one_channel_at_the_stated_level():
    a = np.zeros((SR, 2), dtype=np.int32)
    out = _inject.add_tones(a, SR, -60, harmonics=(1000,), channel=0)
    assert not out[:, 1].any()
    peak = np.abs(out[:, 0]).max() / _inject.FULL_SCALE
    assert 20 * np.log10(peak) == pytest.approx(-60, abs=0.05)
    spectrum = np.abs(np.fft.rfft(out[:, 0]))
    assert spectrum.argmax() == 1000                                                   # 1 Hz bins over one second
    comb = np.abs(np.fft.rfft(_inject.add_tones(a, SR, -60)[:, 0]))
    assert sorted(np.argsort(comb)[-5:]) == [1000, 2000, 3000, 4000, 6000]


def test_tones_do_not_wrap_at_full_scale():
    a = np.full((SR, 1), 2 ** 31 - 1, dtype=np.int32)
    assert _inject.add_tones(a, SR, -20, harmonics=(1000,)).max() == 2 ** 31 - 1


def test_a_fault_is_found_only_by_a_detection_at_it():
    faults = [{"kind": "splice", "time_s": 10.0, "end_s": 10.0}, {"kind": "gap", "time_s": 20.0, "end_s": 20.5},
              {"kind": "splice", "time_s": 30.0, "end_s": 30.0}]
    events = [("splice", 10.02, 10.02), ("splice", 20.0, 20.0), ("splice", 31.0, 31.0), ("gap", 55.0, 56.0)]
    extra = _inject.match_faults(faults, events)
    assert [f["found"] for f in faults] == [True, True, False]
    assert faults[1]["found_as"] == "splice"            # found, but reported as the other kind
    assert extra == [("splice", 31.0, 31.0), ("gap", 55.0, 56.0)]


def test_duplicate_plan_cycles_run_lengths_and_fits_the_clip():
    plan = _inject.duplicate_plan(int(45 * 29.97), 29.97)
    assert [k for _, k in plan[:6]] == [1, 2, 3, 5, 15, 1]
    assert all(b - a > max(_inject.DUPLICATE_RUNS) for (a, _), (b, _) in zip(plan, plan[1:]))   # freezes never touch
    assert plan[-1][0] + plan[-1][1] < int(45 * 29.97)


def test_held_frame_is_identical_or_carries_its_own_noise():
    frame = np.full(720 * 486 * 2, 500, dtype="<u2")
    assert _inject.held_frame(frame) is frame
    rng = np.random.default_rng(3)
    one = _inject.held_frame(frame, rng, _inject.REDIGITIZED_NOISE_SIGMA)
    two = _inject.held_frame(frame, rng, _inject.REDIGITIZED_NOISE_SIGMA)
    assert one.dtype == frame.dtype and not np.array_equal(one, two)
    ydif = np.abs(one.astype(int) - two.astype(int)).mean()
    assert 0.6 <= ydif <= 0.8        # repeat to repeat, where the verified LC freezes sit; the detector's limit is 1.0
    assert _inject.held_frame(np.zeros(1000, dtype="<u2"), rng, 5.0).min() == 0           # clipped, not wrapped


def test_recall_table_counts_per_group_in_the_order_given():
    rows = [{"k": "a", "found": True}, {"k": "a", "found": False}, {"k": "b", "found": True}]
    lines = inject.recall_table(rows, lambda r: r["k"], ["b", "a", "c"], "group")
    assert [l.split() for l in lines[1:]] == [["b", "1", "1", "100%"], ["a", "2", "1", "50%"]]


def test_bins():
    assert [inject.stand_out_bin(x) for x in (0, 6, 19.9, 20, 45)] == \
        ["under 6 dB", "6 to 12 dB", "12 to 20 dB", "20 to 30 dB", "30 dB or more"]
    assert [inject.level_bin(x) for x in (-70, -50, -29)] == list(inject.LEVELS)


def test_every_fault_has_a_runner_a_topic_a_report_and_a_clip_length():
    assert set(_inject.RUNNERS) == set(_inject.TOPICS) == set(_inject.DEFAULT_SECONDS) == set(inject.REPORTS)
