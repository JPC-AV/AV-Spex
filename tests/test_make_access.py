"""Tests for checks.make_access.

Covers:
* get_duration / get_video_dimensions — ffprobe wrapper helpers
* make_access_file — the ffmpeg command construction logic, especially:
  - NTSC (486) crop_to_480 toggle (720x480 vs native 720x486)
  - PAL (576) leave-as-is
  - Optional crop_area for sophisticated border detection
  - Optional start_time to skip color bars detected by qct-parse
  - Even-dimension safety for yuv420p
* process_access_file — config gating, existing-file short-circuit, signal emission
* determine_excluded_audio_channels — silent/LTC channel exclusion decisions
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from AV_Spex.checks import make_access as ma
from AV_Spex.utils import ffprobe_probe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_popen(stdout_lines=()):
    """Build a Popen-like object whose stdout.readline() returns the given lines
    then "" forever. stderr.read() returns ""."""
    proc = MagicMock()
    iterator = iter(list(stdout_lines) + [""])
    proc.stdout.readline.side_effect = lambda: next(iterator, "")
    proc.stderr.read.return_value = ""
    return proc


def _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486), duration="60.0", audio_streams=(2,)):
    """Patch helpers + Popen so we can inspect the ffmpeg command list build.

    audio_streams defaults to a single stereo stream ``(2,)`` (typical MKV);
    pass e.g. ``(1, 1)`` to simulate two mono streams (typical MXF).
    """
    monkeypatch.setattr(ma, "get_duration", lambda _p: duration)
    monkeypatch.setattr(ma, "get_video_dimensions", lambda _p: dim)
    monkeypatch.setattr(ma, "get_audio_stream_channels", lambda _p: list(audio_streams))
    fake_proc = _fake_popen()
    popen_mock = MagicMock(return_value=fake_proc)
    monkeypatch.setattr(ma.subprocess, "Popen", popen_mock)
    return popen_mock


def _vf_filters_from(cmd):
    """Pull the comma-separated -vf argument out of a captured ffmpeg command list."""
    vf_index = cmd.index("-vf")
    return cmd[vf_index + 1].split(",")


# ---------------------------------------------------------------------------
# get_duration / get_video_dimensions
# ---------------------------------------------------------------------------

def _fake_ffprobe(monkeypatch, stdout):
    """Probing moved to utils.ffprobe_probe, which captures text not bytes."""
    monkeypatch.setattr(ffprobe_probe.subprocess, "run",
                        lambda *a, **kw: MagicMock(returncode=0, stdout=stdout, stderr=""))


def test_get_duration_strips_whitespace(monkeypatch):
    _fake_ffprobe(monkeypatch, "  60.123\n")
    assert ma.get_duration("/v.mkv") == "60.123"


def test_get_video_dimensions_parses_wxh(monkeypatch):
    _fake_ffprobe(monkeypatch, "720,486\n")
    assert ma.get_video_dimensions("/v.mkv") == (720, 486)


def test_get_video_dimensions_empty_returns_none_pair(monkeypatch):
    fake_result = MagicMock(stdout=b"\n")
    monkeypatch.setattr(ma.subprocess, "run", lambda *a, **kw: fake_result)
    assert ma.get_video_dimensions("/v.mkv") == (None, None)


def test_get_video_dimensions_garbage_returns_none_pair(monkeypatch):
    fake_result = MagicMock(stdout=b"not-a-resolution")
    monkeypatch.setattr(ma.subprocess, "run", lambda *a, **kw: fake_result)
    assert ma.get_video_dimensions("/v.mkv") == (None, None)


def test_get_video_dimensions_subprocess_error_returns_none_pair(monkeypatch):
    monkeypatch.setattr(ma.subprocess, "run", MagicMock(side_effect=ma.subprocess.SubprocessError("boom")))
    assert ma.get_video_dimensions("/v.mkv") == (None, None)


# ---------------------------------------------------------------------------
# make_access_file — crop_to_480 vs native
# ---------------------------------------------------------------------------

def test_make_access_file_ntsc_crops_to_480_by_default(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file("/v/in.mkv", "/v/out.mp4", check_cancelled=lambda: False)

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    assert "crop=720:480:0:3" in vf
    assert "yadif=1" in vf
    assert "format=yuv420p" in vf


def test_make_access_file_ntsc_crop_to_480_false_keeps_486(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file("/v/in.mkv", "/v/out.mp4", check_cancelled=lambda: False, crop_to_480=False)

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # Should NOT add the default 720:480 crop when crop_to_480 is disabled
    assert not any(f.startswith("crop=720:480") for f in vf)
    # yadif + yuv420p still applied
    assert "yadif=1" in vf
    assert "format=yuv420p" in vf


def test_make_access_file_pal_keeps_native_576(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 576))
    ma.make_access_file("/v/in.mkv", "/v/out.mp4", check_cancelled=lambda: False)

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # PAL: no default crop applied
    assert not any(f.startswith("crop=") for f in vf)


def test_make_access_file_unknown_dim_falls_back_to_input(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(640, 480))
    ma.make_access_file("/v/in.mkv", "/v/out.mp4", check_cancelled=lambda: False)

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # Unknown standard: no implicit crop
    assert not any(f.startswith("crop=") for f in vf)


# ---------------------------------------------------------------------------
# make_access_file — explicit crop_area
# ---------------------------------------------------------------------------

def test_make_access_file_with_crop_area_ntsc_scales_to_480(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        crop_area=(8, 6, 700, 472),
    )

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # Crop applied first
    assert vf[0] == "crop=700:472:8:6"
    # Then scaled to 720x480 because crop_to_480 defaults to True for NTSC
    assert "scale=720:480" in vf


def test_make_access_file_with_crop_area_ntsc_native_height_when_crop_to_480_false(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        crop_area=(0, 0, 720, 486),
        crop_to_480=False,
    )

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # Scaled to native 720x486 because crop_to_480 is disabled
    assert "scale=720:486" in vf


def test_make_access_file_crop_area_odd_dimensions_made_even(monkeypatch):
    """yuv420p requires even crop dimensions, so odd width/height get rounded down by 1."""
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        crop_area=(0, 0, 701, 473),
    )

    cmd = popen_mock.call_args[0][0]
    vf = _vf_filters_from(cmd)
    # 701 → 700, 473 → 472
    assert vf[0] == "crop=700:472:0:0"


# ---------------------------------------------------------------------------
# make_access_file — start_time (color-bars trim)
# ---------------------------------------------------------------------------

def test_make_access_file_with_start_time_adds_ss_flag(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        start_time=12.345,
    )

    cmd = popen_mock.call_args[0][0]
    # -ss appears before -i
    ss_idx = cmd.index("-ss")
    i_idx = cmd.index("-i")
    assert ss_idx < i_idx
    assert cmd[ss_idx + 1] == "12.345"


def test_make_access_file_zero_start_time_no_ss(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        start_time=0,
    )

    cmd = popen_mock.call_args[0][0]
    assert "-ss" not in cmd


# ---------------------------------------------------------------------------
# process_access_file — config / existing-file guards
# ---------------------------------------------------------------------------

def _patch_process_config(monkeypatch, *, access_file_enabled):
    fake_cfg = MagicMock()
    fake_cfg.outputs.access_file = access_file_enabled
    fake_mgr = MagicMock()
    fake_mgr.get_config.return_value = fake_cfg
    monkeypatch.setattr(ma, "ConfigManager", lambda: fake_mgr)


def test_process_access_file_returns_none_when_disabled(tmp_path, monkeypatch):
    _patch_process_config(monkeypatch, access_file_enabled=False)
    out = ma.process_access_file("/v/in.mkv", str(tmp_path), "video_x")
    assert out is None


def test_process_access_file_short_circuits_on_existing_mp4(tmp_path, monkeypatch):
    _patch_process_config(monkeypatch, access_file_enabled=True)
    # Pre-existing .mp4 in source dir
    (tmp_path / "video_x_existing.mp4").write_text("")

    signals = MagicMock()
    with patch.object(ma, "make_access_file") as make_mock:
        out = ma.process_access_file("/v/in.mkv", str(tmp_path), "video_x", signals=signals)

    assert out is None
    make_mock.assert_not_called()
    signals.step_completed.emit.assert_called_with("Generate Access File")


def test_process_access_file_calls_make_access_file_when_enabled(tmp_path, monkeypatch):
    _patch_process_config(monkeypatch, access_file_enabled=True)

    signals = MagicMock()
    with patch.object(ma, "make_access_file") as make_mock:
        out = ma.process_access_file(
            "/v/in.mkv", str(tmp_path), "video_x",
            signals=signals,
            color_bars_end_time=4.5,
            crop_area=(0, 0, 720, 480),
            crop_to_480=True,
        )

    expected_out = os.path.join(str(tmp_path), "video_x_access.mp4")
    assert out == expected_out

    # make_access_file called with the right kwargs propagated through
    _, call_kwargs = make_mock.call_args
    assert call_kwargs["start_time"] == 4.5
    assert call_kwargs["crop_area"] == (0, 0, 720, 480)
    assert call_kwargs["crop_to_480"] is True
    assert call_kwargs["signals"] is signals

    signals.step_completed.emit.assert_called_with("Generate Access File")


def test_process_access_file_forwards_excluded_audio_channels(tmp_path, monkeypatch):
    _patch_process_config(monkeypatch, access_file_enabled=True)

    with patch.object(ma, "make_access_file") as make_mock:
        ma.process_access_file(
            "/v/in.mkv", str(tmp_path), "video_x",
            excluded_audio_channels=[2],
        )

    _, call_kwargs = make_mock.call_args
    assert call_kwargs["excluded_audio_channels"] == [2]


# ---------------------------------------------------------------------------
# make_access_file — excluded audio channels
# ---------------------------------------------------------------------------

def test_make_access_file_excluded_ch2_maps_first_stream_dual_mono_ch1(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[2],
    )

    cmd = popen_mock.call_args[0][0]
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:a:0?" in mapped
    assert "0:a?" not in mapped
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c0|c1=c0"


def test_make_access_file_excluded_ch1_pans_from_ch2(monkeypatch):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[1],
    )

    cmd = popen_mock.call_args[0][0]
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c1|c1=c1"


def test_make_access_file_excluded_ch1_on_four_channel_pans_ch2_dual_mono(monkeypatch):
    # 4-channel source with ch1 (timecode) excluded: pan extracts the program
    # channel (c1) as dual-mono and drops the extra channels (c2, c3).
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[1],
    )

    cmd = popen_mock.call_args[0][0]
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:a:0?" in mapped
    assert "0:a?" not in mapped
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c1|c1=c1"


def test_make_access_file_excluded_extra_channels_keeps_pair_as_stereo(monkeypatch):
    # When only extra (non-pair) channels are excluded, both analyzed channels
    # survive and are output as straight stereo.
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[3, 4],
    )

    cmd = popen_mock.call_args[0][0]
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c0|c1=c1"


@pytest.mark.parametrize("excluded", [None, []])
def test_make_access_file_no_exclusion_keeps_default_audio_mapping(monkeypatch, excluded):
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486))
    ma.make_access_file(
        "/v/in.mkv", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=excluded,
    )

    cmd = popen_mock.call_args[0][0]
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:a?" in mapped
    assert "-af" not in cmd


def test_make_access_file_multi_mono_streams_merged_to_one_track(monkeypatch):
    """Two mono audio streams (MXF) are amerged into a single track."""
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486), audio_streams=(1, 1))
    ma.make_access_file(
        "/v/in.mxf", "/v/out.mp4",
        check_cancelled=lambda: False,
    )

    cmd = popen_mock.call_args[0][0]
    fc_idx = cmd.index("-filter_complex")
    assert cmd[fc_idx + 1] == "[0:a:0][0:a:1]amerge=inputs=2[aout]"
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "[aout]" in mapped
    assert "0:a?" not in mapped


def test_make_access_file_multi_mono_excluded_ch2_keeps_first_stream(monkeypatch):
    """With two mono streams, excluding channel 2 keeps stream a:0 as dual-mono."""
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486), audio_streams=(1, 1))
    ma.make_access_file(
        "/v/in.mxf", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[2],
    )

    cmd = popen_mock.call_args[0][0]
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:a:0?" in mapped
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c0|c1=c0"
    assert "-filter_complex" not in cmd


def test_make_access_file_multi_mono_excluded_ch1_keeps_second_stream(monkeypatch):
    """With two mono streams, excluding channel 1 keeps stream a:1 as dual-mono."""
    popen_mock = _capture_ffmpeg_cmd(monkeypatch, dim=(720, 486), audio_streams=(1, 1))
    ma.make_access_file(
        "/v/in.mxf", "/v/out.mp4",
        check_cancelled=lambda: False,
        excluded_audio_channels=[1],
    )

    cmd = popen_mock.call_args[0][0]
    map_indices = [i for i, v in enumerate(cmd) if v == "-map"]
    mapped = [cmd[i + 1] for i in map_indices]
    assert "0:a:1?" in mapped
    af_idx = cmd.index("-af")
    assert cmd[af_idx + 1] == "pan=stereo|c0=c0|c1=c0"


# ---------------------------------------------------------------------------
# determine_excluded_audio_channels
# ---------------------------------------------------------------------------

def _findings(silent=(), regions=(), duration=100.0, num_channels=2):
    return {
        "silent_channels": list(silent),
        "num_channels": num_channels,
        "timecode_consensus_regions": list(regions),
        "duration": duration,
    }


def _region(start, end, channel):
    return {"start_time": start, "end_time": end, "channel": channel}


def test_determine_none_findings_returns_empty():
    assert ma.determine_excluded_audio_channels(None) == ([], [])


def test_determine_silent_channel_2_excluded():
    excluded, reasons = ma.determine_excluded_audio_channels(_findings(silent=[2]))
    assert excluded == [2]
    assert len(reasons) == 1
    assert "channel 2" in reasons[0]


def test_determine_silent_channel_1_excluded():
    excluded, _ = ma.determine_excluded_audio_channels(_findings(silent=[1]))
    assert excluded == [1]


def test_determine_ltc_above_coverage_threshold_excluded():
    regions = [_region(0.0, 80.0, "Channel 2")]
    excluded, reasons = ma.determine_excluded_audio_channels(_findings(regions=regions))
    assert excluded == [2]
    assert "audible timecode" in reasons[0]


def test_determine_ltc_below_coverage_threshold_not_excluded():
    regions = [_region(0.0, 50.0, "Channel 2")]
    assert ma.determine_excluded_audio_channels(_findings(regions=regions)) == ([], [])


def test_determine_ltc_overlapping_regions_not_double_counted():
    # Two 50% regions that fully overlap = 50% coverage, below threshold
    regions = [_region(0.0, 50.0, "Channel 2"), _region(0.0, 50.0, "Channel 2")]
    assert ma.determine_excluded_audio_channels(_findings(regions=regions)) == ([], [])


def test_determine_mix_based_regions_ignored():
    regions = [_region(0.0, 100.0, "Not channel-specific (mix-based)")]
    assert ma.determine_excluded_audio_channels(_findings(regions=regions)) == ([], [])


def test_determine_both_channels_ltc_safety_keeps_all_audio():
    regions = [_region(0.0, 100.0, "Both channels")]
    excluded, reasons = ma.determine_excluded_audio_channels(_findings(regions=regions))
    assert excluded == []
    # Both flagged-channel reasons preserved, plus the trailing explanation of
    # why nothing was excluded.
    assert len(reasons) == 3
    assert "no channel to rebuild" in reasons[-1]


def test_determine_silent_ch1_plus_ltc_ch2_safety_keeps_all_audio():
    regions = [_region(0.0, 100.0, "Channel 2")]
    excluded, reasons = ma.determine_excluded_audio_channels(
        _findings(silent=[1], regions=regions)
    )
    assert excluded == []
    assert len(reasons) == 3
    assert "no channel to rebuild" in reasons[-1]


def test_determine_mono_source_not_excluded():
    findings = _findings(silent=[2], num_channels=1)
    assert ma.determine_excluded_audio_channels(findings) == ([], [])


def test_determine_four_channel_source_silent_pair_channel_excluded():
    # 4-channel source with a silent pair channel: exclusion is allowed only
    # because the extra channels (3, 4) were also verified silent.
    findings = _findings(silent=[2, 3, 4], num_channels=4)
    excluded, reasons = ma.determine_excluded_audio_channels(findings)
    assert excluded == [2, 3, 4]
    assert "channel 2" in reasons[0]


def test_determine_four_channel_source_unverified_extras_keeps_all_audio():
    # A flagged pair channel on a 4-channel source, but channels 3/4 were not
    # verified silent — the exclusion would drop them, so keep all audio.
    findings = _findings(silent=[2], num_channels=4)
    excluded, reasons = ma.determine_excluded_audio_channels(findings)
    assert excluded == []
    assert "channel 2" in reasons[0]
    assert "3, 4" in reasons[-1]
    assert "not verified silent" in reasons[-1]


def test_determine_four_channel_source_ltc_ch1_excluded():
    # Mirrors the real 21463704 case: 4-channel source, ch1 carries audible
    # timecode over the whole file, ch2 is the (quiet) program channel, and
    # the extra channels (3, 4) are verified silent.
    regions = [_region(0.0, 100.0, "Channel 1")]
    excluded, reasons = ma.determine_excluded_audio_channels(
        _findings(silent=[3, 4], regions=regions, num_channels=4)
    )
    assert excluded == [1, 3, 4]
    assert any("audible timecode" in r for r in reasons)


def test_determine_four_channel_source_ltc_ch1_unverified_extras_keeps_all_audio():
    # Same LTC-on-ch1 case, but channels 3/4 were not flagged silent — the
    # exclusion may not drop them, so nothing is excluded.
    regions = [_region(0.0, 100.0, "Channel 1")]
    excluded, reasons = ma.determine_excluded_audio_channels(
        _findings(regions=regions, num_channels=4)
    )
    assert excluded == []
    assert "not verified silent" in reasons[-1]


def test_determine_silent_non_pair_channels_excluded():
    # Silent channels beyond the analyzed pair (3, 4) are reported/excluded;
    # the analyzed pair (1, 2) survives.
    findings = _findings(silent=[3, 4], num_channels=4)
    excluded, _ = ma.determine_excluded_audio_channels(findings)
    assert excluded == [3, 4]


def test_determine_zero_duration_skips_ltc_but_silent_still_works():
    regions = [_region(0.0, 100.0, "Channel 2")]
    excluded, _ = ma.determine_excluded_audio_channels(
        _findings(silent=[1], regions=regions, duration=0)
    )
    assert excluded == [1]
