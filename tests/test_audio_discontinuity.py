"""Tests for AV_Spex.checks.audio_discontinuity on synthetic audio.

Mirrors the two LC test files the detector was calibrated on:
SmashEditTest.mkv (a few seconds cut out of a bars/tone recording — one splice
at 4.004 s) and DropOutTest.mkv (SDI patch pulled three times — three
exact-zero gaps). Plus the false-positive regimes found on real tapes: a tone
switched on out of near-silence (JPC_AV_01581), a near-silent second channel
(JPC_AV_02041 and others), and leading/trailing silence.
"""

import numpy as np
import pytest

from AV_Spex.checks import audio_discontinuity as ad

SR = 48000


def _tone(seconds, freq=1000.0, amp=0.1, phase=0.0, channels=2, noise_db=-90.0, seed=0):
    t = np.arange(int(seconds * SR)) / SR
    x = amp * np.sin(2 * np.pi * freq * t + phase)
    rng = np.random.default_rng(seed)
    out = np.stack([x] * channels, axis=1)
    return out + rng.normal(0, 10 ** (noise_db / 20), out.shape)


def _cut(a, at_s, remove_samples):
    i = int(at_s * SR)
    return np.concatenate([a[:i], a[i + remove_samples:]])


def _scan(monkeypatch, a, chunk_seconds=None):
    """Run the real driver over an in-memory array instead of ffmpeg."""
    monkeypatch.setattr(ad, '_audio_streams',
                        lambda p: [{'index': 1, 'channels': a.shape[1], 'sample_rate': SR}])

    def _chunks(path, stream, channels, sample_rate, check_cancelled):
        n = int((chunk_seconds or ad.CHUNK_SECONDS) * SR)
        for i in range(0, len(a), n):
            yield a[i:i + n]
    monkeypatch.setattr(ad, '_decode_chunks', _chunks)
    return ad.scan_audio_discontinuities('/fake.mkv')


# ---------------------------------------------------------------------------
# Splices
# ---------------------------------------------------------------------------

def test_clean_tone_has_no_findings(monkeypatch):
    scan = _scan(monkeypatch, _tone(8))
    assert scan.measured
    assert scan.splices == [] and scan.silence_gaps == []


def test_smash_cut_in_tone_is_one_splice_at_the_cut(monkeypatch):
    a = _cut(_tone(10), 4.004, 1601 * 30)  # a second of NTSC frames removed
    scan = _scan(monkeypatch, a)
    assert len(scan.splices) == 1
    assert scan.splices[0].time_s == pytest.approx(4.004, abs=0.001)
    assert scan.splices[0].score > 2 * ad.SPLICE_SCORE_THRESHOLD


def test_splice_across_a_chunk_join_is_found_once(monkeypatch):
    a = _cut(_tone(10), 2.0, 777)
    scan = _scan(monkeypatch, a, chunk_seconds=2.0)  # cut lands on the join
    assert [round(e.time_s, 3) for e in scan.splices] == [2.0]


def test_splice_on_one_live_channel_with_the_other_near_silent(monkeypatch):
    """Many tapes carry program on one channel only; the silent one must not veto."""
    a = _tone(10)
    a[:, 1] = np.random.default_rng(1).normal(0, 10 ** (-72 / 20), len(a))
    a = _cut(a, 5.0, 1601)
    scan = _scan(monkeypatch, a)
    assert len(scan.splices) == 1
    assert scan.splices[0].channels == 1


def test_frame_accurate_cut_at_a_zero_crossing_is_not_flagged(monkeypatch):
    """1 kHz at 48 kHz repeats every 48 samples: removing a whole number of
    periods leaves the waveform continuous — the edit an NLE would make."""
    a = _cut(_tone(10), 4.0, 48 * 1000)
    assert _scan(monkeypatch, a).splices == []


def test_tone_switched_on_out_of_silence_is_not_a_splice(monkeypatch):
    """Content starting is not a splice (JPC_AV_01581's test tone at ~1407 s)."""
    quiet = np.random.default_rng(2).normal(0, 10 ** (-72 / 20), (int(3 * SR), 2))
    tone = _tone(3, freq=9000.0, amp=0.18, phase=1.0)
    scan = _scan(monkeypatch, np.concatenate([quiet, tone]))
    assert scan.splices == []


def test_steady_noise_has_no_splices(monkeypatch):
    noise = np.random.default_rng(3).normal(0, 0.05, (int(8 * SR), 2))
    assert _scan(monkeypatch, noise).splices == []


def test_a_crackle_burst_is_not_a_splice(monkeypatch):
    """Clipped or crackling program (JPC_AV_01663/01710) scores high but spreads
    over tens of samples; a splice is one discontinuity."""
    a = _tone(10, amp=0.05)
    i = int(5 * SR)
    a[i:i + 144] += np.clip(np.random.default_rng(4).normal(0, 0.15, (144, 1)), -0.3, 0.3)
    monkeypatch.setattr(ad, 'MAX_EVENT_WIDTH_SAMPLES', 10 ** 9)
    assert ad.find_splices(a, SR), "precondition: the burst clears the score threshold"
    monkeypatch.undo()
    assert _scan(monkeypatch, a).splices == []


def test_the_file_start_is_not_a_splice(monkeypatch):
    """The recording starting mid-waveform (filter warm-up) is not a finding."""
    a = _tone(4, phase=1.3)
    assert _scan(monkeypatch, a).splices == []


# ---------------------------------------------------------------------------
# Digital-silence gaps
# ---------------------------------------------------------------------------

def _with_gaps(seconds, gaps):
    a = _tone(seconds)
    for s, e in gaps:
        a[int(s * SR):int(e * SR)] = 0.0
    return a


def test_patch_pull_gaps_are_found_and_their_edges_not_double_counted(monkeypatch):
    gaps = [(13.6512, 15.2481), (17.4586, 20.0926), (23.0195, 25.6385)]
    scan = _scan(monkeypatch, _with_gaps(30, gaps), chunk_seconds=4.0)
    found = [(round(g.start_s, 3), round(g.end_s, 3)) for g in scan.silence_gaps]
    assert found == [(round(s, 3), round(e, 3)) for s, e in gaps]
    assert scan.splices == [], "gap edges belong to the gap, not separate splices"


def test_leading_and_trailing_silence_are_not_gaps(monkeypatch):
    a = _with_gaps(10, [(0.0, 0.5), (9.6, 10.0)])
    assert _scan(monkeypatch, a).silence_gaps == []


def test_gap_on_only_one_channel_is_not_a_stream_gap(monkeypatch):
    a = _tone(10)
    a[int(4 * SR):int(5 * SR), 1] = 0.0
    assert _scan(monkeypatch, a).silence_gaps == []


def test_gap_shorter_than_the_minimum_is_ignored(monkeypatch):
    a = _with_gaps(10, [(5.0, 5.002)])  # 2 ms
    assert _scan(monkeypatch, a).silence_gaps == []


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

def test_no_audio_streams_is_unmeasured(monkeypatch):
    monkeypatch.setattr(ad, '_audio_streams', lambda p: [])
    scan = ad.scan_audio_discontinuities('/fake.mkv')
    assert scan.measured is False
