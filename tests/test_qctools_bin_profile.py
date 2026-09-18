"""Tests for checks.qctools_bin_profile.

Phase 0 of the period-selection rework: the parser now builds a per-bin
profile of every QCTools measure present in the report, alongside the BRNG
histogram it has always built. Nothing consumes the profiles yet, so these
tests pin the collection itself — bin boundaries, black-frame handling,
optional metrics, and the fact that the legacy histogram is derivable from the
profiles (the bridge later phases will cross).

Covered:
* BinProfile properties (picture_frames, black_fraction, bin_end)
* BinProfiler: binning, streaming close, out-of-order frames, excluded frames
* Metric summarization: means/maxes/percentiles, PSNR clipping, idet counts,
  cropdetect medians + edge IQR
* Graceful degradation when a report lacks whole metric families
* Integration through QCToolsParser.parse_for_violations_streaming
* violation_histogram_from_profiles round-trip
"""

import xml.etree.ElementTree as ET

import pytest

from AV_Spex.checks import frame_analysis as fa
from AV_Spex.checks import qctools_bin_profile as qbp

from test_frame_analysis import _write_qctools, _BLACK_TAGS, _NORMAL_TAGS


# ===========================================================================
# Helpers
# ===========================================================================

def _elem(tags):
    """Build a <frame> element from {full-key-suffix: value}."""
    parts = ['<frame>']
    for key, value in tags.items():
        full = key if key.startswith('lavfi.') else f'lavfi.{key}'
        parts.append(f'<tag key="{full}" value="{value}"/>')
    parts.append('</frame>')
    return ET.fromstring(''.join(parts))


def _signalstats(**kwargs):
    return {f'signalstats.{k.upper()}': v for k, v in kwargs.items()}


# ===========================================================================
# BinProfile
# ===========================================================================

def test_bin_profile_properties():
    p = qbp.BinProfile(bin_start=30.0, frames=10, black_frames=4)
    assert p.bin_end == 40.0
    assert p.picture_frames == 6
    assert p.black_fraction == pytest.approx(0.4)


def test_bin_profile_black_fraction_empty_bin():
    """A bin with no profiled frames reports 0.0, not a ZeroDivisionError."""
    assert qbp.BinProfile(bin_start=0.0).black_fraction == 0.0


# ===========================================================================
# BinProfiler — binning and streaming behaviour
# ===========================================================================

def test_profiler_bins_by_ten_seconds():
    prof = qbp.BinProfiler()
    for ts in (0.0, 5.0, 9.99, 10.0, 25.0):
        prof.add_frame(ts, _elem(_signalstats(brng='0.02')))
    profiles = prof.finalize()
    assert sorted(profiles) == [0.0, 10.0, 20.0]
    assert profiles[0.0].frames == 3
    assert profiles[10.0].frames == 1
    assert profiles[20.0].frames == 1


def test_profiler_closes_bins_as_time_advances():
    """Only the open bin holds samples; earlier bins are already summarized."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.02')))
    prof.add_frame(11.0, _elem(_signalstats(brng='0.04')))
    assert 0.0 in prof.profiles          # closed without finalize()
    assert 10.0 not in prof.profiles     # still open
    prof.finalize()
    assert 10.0 in prof.profiles


def test_profiler_ignores_frames_for_a_closed_bin():
    """Out-of-order frames are counted and dropped, never merged blindly."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.02')))
    prof.add_frame(11.0, _elem(_signalstats(brng='0.04')))
    prof.add_frame(2.0, _elem(_signalstats(brng='0.90')))   # back in time
    profiles = prof.finalize()
    assert prof.out_of_order_frames == 1
    assert profiles[0.0].frames == 1
    assert profiles[0.0].brng_max == pytest.approx(0.02)


def test_profiler_counts_excluded_frames_without_measuring_them():
    prof = qbp.BinProfiler()
    prof.note_excluded(1.0)
    prof.note_excluded(2.0)
    prof.add_frame(3.0, _elem(_signalstats(brng='0.02')))
    profile = prof.finalize()[0.0]
    assert profile.excluded_frames == 2
    assert profile.frames == 1


def test_profiler_bin_of_only_excluded_frames_has_no_metrics():
    """A fully-excluded bin exists with counts but no statistics."""
    prof = qbp.BinProfiler()
    prof.note_excluded(1.0)
    profile = prof.finalize()[0.0]
    assert profile.frames == 0
    assert profile.excluded_frames == 1
    assert profile.brng_mean is None
    assert profile.metrics == ()


def test_profiler_custom_bin_size():
    prof = qbp.BinProfiler(bin_size=5.0)
    prof.add_frame(4.0, _elem(_signalstats(brng='0.02')))
    prof.add_frame(6.0, _elem(_signalstats(brng='0.02')))
    assert sorted(prof.finalize()) == [0.0, 5.0]


# ===========================================================================
# BinProfiler — black frames
# ===========================================================================

def test_black_frames_counted_but_not_measured():
    """Analog black would dominate any average it entered."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.40', yavg='65')), is_black=True)
    prof.add_frame(2.0, _elem(_signalstats(brng='0.02', yavg='500')))
    profile = prof.finalize()[0.0]
    assert profile.frames == 2
    assert profile.black_frames == 1
    assert profile.picture_frames == 1
    assert profile.brng_mean == pytest.approx(0.02)
    assert profile.yavg_mean == pytest.approx(500.0)


def test_all_black_bin_has_counts_but_no_statistics():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.40')), is_black=True)
    profile = prof.finalize()[0.0]
    assert profile.black_fraction == 1.0
    assert profile.brng_mean is None
    assert profile.metrics == ()


def test_black_frame_violation_score_still_counted():
    """Counts mirror the caller's decision; the caller gates black itself."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.40')), is_black=True,
                   violation_score=0.4)
    profile = prof.finalize()[0.0]
    assert profile.violation_frames == 1
    assert profile.violation_score_sum == pytest.approx(0.4)


# ===========================================================================
# Metric summarization
# ===========================================================================

def test_signalstats_summary_statistics():
    prof = qbp.BinProfiler()
    for brng, tout, vrep, ydif in (('0.01', '0.0', '0.0', '2.0'),
                                   ('0.03', '0.5', '0.25', '10.0'),
                                   ('0.02', '0.1', '0.0', '4.0')):
        prof.add_frame(1.0, _elem(_signalstats(
            brng=brng, tout=tout, vrep=vrep, ydif=ydif,
            satmax='377', satavg='120', yavg='400')))
    p = prof.finalize()[0.0]
    assert p.brng_mean == pytest.approx(0.02)
    assert p.brng_max == pytest.approx(0.03)
    assert p.tout_mean == pytest.approx(0.2)
    assert p.tout_p95 == pytest.approx(0.46)
    assert p.vrep_max == pytest.approx(0.25)
    assert p.ydif_mean == pytest.approx(16.0 / 3)
    assert p.ydif_p95 == pytest.approx(9.4)
    assert p.satmax_max == pytest.approx(377.0)
    assert p.satavg_mean == pytest.approx(120.0)
    assert p.yavg_mean == pytest.approx(400.0)
    assert p.metrics == ('signalstats',)


def test_deflicker_uses_absolute_magnitude():
    """relative_change is signed; a big negative swing is still a big swing."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem({'deflicker.relative_change': '0.0002'}))
    prof.add_frame(2.0, _elem({'deflicker.relative_change': '-0.5'}))
    p = prof.finalize()[0.0]
    assert p.deflicker_absmax == pytest.approx(0.5)
    assert p.metrics == ('deflicker',)


def test_ssim_mean_and_min():
    prof = qbp.BinProfiler()
    for v in ('0.95', '0.80', '0.99'):
        prof.add_frame(1.0, _elem({'ssim.All': v}))
    p = prof.finalize()[0.0]
    assert p.ssim_mean == pytest.approx(0.913333, abs=1e-5)
    assert p.ssim_min == pytest.approx(0.80)


def test_psnr_infinity_is_clipped():
    """Identical frames give an infinite PSNR; the bin mean must stay finite."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem({'psnr.psnr_avg': 'inf'}))
    prof.add_frame(2.0, _elem({'psnr.psnr_avg': '30.0'}))
    p = prof.finalize()[0.0]
    assert p.psnr_mean == pytest.approx((qbp.PSNR_CLIP + 30.0) / 2)


def test_non_finite_non_psnr_value_is_dropped():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='nan')))
    prof.add_frame(2.0, _elem(_signalstats(brng='0.04')))
    assert prof.finalize()[0.0].brng_mean == pytest.approx(0.04)


def test_unparseable_value_is_skipped():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='n/a')))
    assert prof.finalize()[0.0].brng_mean is None


def test_idet_counts_repeated_fields():
    prof = qbp.BinProfiler()
    for value in ('neither', 'top', 'bottom', 'neither'):
        prof.add_frame(1.0, _elem({'idet.repeated.current_frame': value}))
    p = prof.finalize()[0.0]
    assert p.repeated_frames == 2
    assert 'idet' in p.metrics


def test_repeated_frames_is_none_when_idet_absent():
    """None means unmeasured; 0 would claim 'no repeats found'."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_signalstats(brng='0.02')))
    assert prof.finalize()[0.0].repeated_frames is None


def test_cropdetect_medians_and_edge_iqr():
    prof = qbp.BinProfiler()
    for x1 in ('5', '5', '40', '5'):          # a left edge that jumps
        prof.add_frame(1.0, _elem({'cropdetect.x1': x1, 'cropdetect.x2': '704',
                                   'cropdetect.y1': '1', 'cropdetect.y2': '728'}))
    p = prof.finalize()[0.0]
    assert p.crop_x1_median == pytest.approx(5.0)
    assert p.crop_x2_median == pytest.approx(704.0)
    assert p.crop_y1_median == pytest.approx(1.0)
    assert p.crop_y2_median == pytest.approx(728.0)
    assert p.crop_edge_iqr_max > 0        # the moving edge shows up
    assert 'cropdetect' in p.metrics


def test_stable_geometry_has_zero_edge_iqr():
    prof = qbp.BinProfiler()
    for _ in range(4):
        prof.add_frame(1.0, _elem({'cropdetect.x1': '5', 'cropdetect.x2': '704',
                                   'cropdetect.y1': '1', 'cropdetect.y2': '728'}))
    assert prof.finalize()[0.0].crop_edge_iqr_max == pytest.approx(0.0)


# ===========================================================================
# Graceful degradation — metric families vary between reports
# ===========================================================================

def test_sparse_report_reports_only_the_families_it_has():
    """Some sidecars carry signalstats + psnr and nothing else."""
    prof = qbp.BinProfiler()
    tags = dict(_signalstats(brng='0.02', tout='0.01'))
    tags['psnr.psnr_avg'] = '31.0'
    prof.add_frame(1.0, _elem(tags))
    p = prof.finalize()[0.0]
    assert p.metrics == ('psnr', 'signalstats')
    assert prof.metrics_present == {'psnr', 'signalstats'}
    assert p.entropy_mean is None
    assert p.ssim_mean is None
    assert p.crop_x1_median is None
    assert p.crop_edge_iqr_max is None


def test_rich_report_reports_every_family():
    tags = dict(_signalstats(brng='0.02'))
    tags.update({'entropy.normalized_entropy.normal.Y': '0.76',
                 'ssim.All': '0.95',
                 'psnr.psnr_avg': '30.3',
                 'deflicker.relative_change': '0.0002',
                 'cropdetect.x1': '5', 'cropdetect.x2': '704',
                 'cropdetect.y1': '1', 'cropdetect.y2': '728',
                 'idet.repeated.current_frame': 'neither'})
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(tags))
    p = prof.finalize()[0.0]
    assert p.metrics == ('cropdetect', 'deflicker', 'entropy', 'idet',
                         'psnr', 'signalstats', 'ssim')
    assert p.entropy_mean == pytest.approx(0.76)


def test_unknown_tags_are_ignored():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem({'astats.Overall.RMS_level': '-20.0', 'blur': '5.2'}))
    p = prof.finalize()[0.0]
    assert p.frames == 1
    assert p.metrics == ()


# ===========================================================================
# Histogram bridge
# ===========================================================================

def test_violation_histogram_from_profiles_skips_clean_bins():
    profiles = {
        0.0: qbp.BinProfile(bin_start=0.0, frames=10, violation_frames=3,
                            violation_score_sum=0.15),
        10.0: qbp.BinProfile(bin_start=10.0, frames=10),
    }
    histogram, severity = qbp.violation_histogram_from_profiles(profiles)
    assert histogram == {0.0: 3}
    assert severity == {0.0: pytest.approx(0.15)}


# ===========================================================================
# Integration through QCToolsParser
# ===========================================================================

def _violating(ts, **extra):
    tags = dict(_NORMAL_TAGS, BRNG="0.05")
    tags.update(extra)
    return {"pkt_pts_time": str(ts), "tags": tags}


def test_parser_collects_profiles_alongside_histogram(tmp_path):
    frames = [_violating(1.0), _violating(2.0), _violating(15.0)]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    parser.parse_for_violations_streaming()

    assert sorted(parser.bin_profiles) == [0.0, 10.0]
    assert parser.bin_profiles[0.0].frames == 2
    assert parser.bin_profiles[0.0].brng_max == pytest.approx(0.05)
    assert parser.bin_profile_metrics == ('signalstats',)


def test_parser_profiles_reproduce_the_legacy_histogram(tmp_path):
    """The dicts period selection ranks on are a projection of the profiles."""
    frames = [_violating(1.0), _violating(2.0), _violating(15.0),
              {"pkt_pts_time": "16.0", "tags": dict(_NORMAL_TAGS, BRNG="0.001")},
              {"pkt_pts_time": "17.0", "tags": dict(_BLACK_TAGS, BRNG="0.40")}]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    parser.parse_for_violations_streaming()

    histogram, severity = qbp.violation_histogram_from_profiles(parser.bin_profiles)
    assert histogram == parser.violation_histogram
    assert severity == pytest.approx(parser.violation_severity)


def test_parser_profiles_record_black_frames(tmp_path):
    frames = [_violating(1.0),
              {"pkt_pts_time": "2.0", "tags": dict(_BLACK_TAGS, BRNG="0.40")}]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    parser.parse_for_violations_streaming()

    profile = parser.bin_profiles[0.0]
    assert profile.frames == 2
    assert profile.black_frames == 1
    assert profile.brng_mean == pytest.approx(0.05)   # black frame excluded


def test_parser_profiles_count_color_bars_as_excluded(tmp_path):
    frames = [_violating(1.0), _violating(2.0), _violating(15.0)]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    parser.parse_for_violations_streaming(skip_color_bars=True,
                                          color_bars_end_time=10.0)

    assert parser.bin_profiles[0.0].excluded_frames == 2
    assert parser.bin_profiles[0.0].frames == 0
    assert parser.bin_profiles[10.0].frames == 1


def test_parser_profiles_count_mid_file_bars_as_excluded(tmp_path):
    frames = [_violating(1.0), _violating(15.0)]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    parser.parse_for_violations_streaming(exclude_regions=[(14.0, 16.0)])

    assert parser.bin_profiles[10.0].excluded_frames == 1
    assert parser.bin_profiles[10.0].frames == 0


def test_parser_can_skip_profile_collection(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [_violating(1.0)]))
    parser.parse_for_violations_streaming(collect_bin_profiles=False)
    assert parser.bin_profiles == {}
    assert parser.bin_profile_metrics == ()
    assert parser.violation_histogram == {0.0: 1}


def test_parser_profiles_empty_before_parsing(tmp_path):
    parser = fa.QCToolsParser(_write_qctools(tmp_path, [_violating(1.0)]))
    assert parser.bin_profiles == {}
    assert parser.bin_profile_metrics == ()


def test_parser_profiles_survive_a_truncated_report(tmp_path):
    """A report that fails part-way still yields profiles for what parsed."""
    good = _write_qctools(tmp_path, [_violating(1.0), _violating(2.0)])
    text = open(good).read()
    truncated = tmp_path / "truncated.xml"
    truncated.write_text(text[:text.index("</frames>")])

    parser = fa.QCToolsParser(str(truncated))
    parser.parse_for_violations_streaming()
    assert parser.bin_profiles[0.0].frames == 2


def test_parser_ignores_interleaved_audio_frames(tmp_path):
    """QCTools interleaves audio frames on the same timeline as video ones.

    They carry no signalstats, so they were always no-ops for the violation
    list — but left in, they inflate per-bin frame counts and, arriving
    between video frames, look like out-of-order video.
    """
    path = _write_qctools(tmp_path, [_violating(1.0), _violating(2.0)])
    text = open(path).read().replace(
        '<frame media_type="video" pkt_pts_time="2.0"',
        '<frame media_type="audio" pkt_pts_time="1.5"></frame>'
        '<frame media_type="video" pkt_pts_time="2.0"')
    mixed = tmp_path / "mixed.xml"
    mixed.write_text(text)

    parser = fa.QCToolsParser(str(mixed))
    parser.parse_for_violations_streaming()
    assert parser.bin_profiles[0.0].frames == 2


def test_parser_treats_frames_without_media_type_as_video(tmp_path):
    path = _write_qctools(tmp_path, [_violating(1.0)])
    text = open(path).read().replace('media_type="video" ', '')
    untyped = tmp_path / "untyped.xml"
    untyped.write_text(text)

    parser = fa.QCToolsParser(str(untyped))
    parser.parse_for_violations_streaming()
    assert parser.bin_profiles[0.0].frames == 1


# ===========================================================================
# detect_black_segments — same audio-frame handling
# ===========================================================================

def _black(ts):
    # UAVG pins the report to 10-bit scale, which is what _BLACK_TAGS is
    # written for: without a chroma tag _detect_bit_depth falls back to YMAX,
    # reads these frames as 8-bit, and the black thresholds scale down by 4.
    return {"pkt_pts_time": str(ts), "tags": dict(_BLACK_TAGS, UAVG="512")}


def _with_audio_frame(path, at, before_ts, tmp_path, name):
    """Insert an audio frame at `at`, just before the video frame at `before_ts`."""
    text = open(path).read().replace(
        f'<frame media_type="video" pkt_pts_time="{before_ts}"',
        f'<frame media_type="audio" pkt_pts_time="{at}"></frame>'
        f'<frame media_type="video" pkt_pts_time="{before_ts}"')
    out = tmp_path / name
    out.write_text(text)
    return str(out)


def test_black_segments_ignore_interleaved_audio_frames(tmp_path):
    """An audio frame is not a non-black video frame and must not end a segment.

    The frames here are 1s apart, so a single audio frame landing between them
    exceeds the 0.5s gap tolerance: counted as picture, it splits one 5s
    segment into two that min_duration then discards.
    """
    frames = [_black(float(t)) for t in range(6)]
    path = _write_qctools(tmp_path, frames)
    mixed = _with_audio_frame(path, at=2.5, before_ts="3.0",
                              tmp_path=tmp_path, name="mixed_black.xml")

    parser = fa.QCToolsParser(mixed)
    segments = parser.detect_black_segments(min_duration=2.0)
    assert segments == [(0.0, 5.0)]


def test_black_segments_still_end_at_real_picture(tmp_path):
    frames = [_black(float(t)) for t in range(4)]
    frames += [{"pkt_pts_time": str(float(t)), "tags": dict(_NORMAL_TAGS, UAVG="512")}
               for t in range(4, 8)]
    parser = fa.QCToolsParser(_write_qctools(tmp_path, frames))
    assert parser.detect_black_segments(min_duration=2.0) == [(0.0, 3.0)]


def test_black_segments_treat_untyped_frames_as_video(tmp_path):
    frames = [_black(float(t)) for t in range(6)]
    text = open(_write_qctools(tmp_path, frames)).read().replace('media_type="video" ', '')
    untyped = tmp_path / "untyped_black.xml"
    untyped.write_text(text)

    parser = fa.QCToolsParser(str(untyped))
    assert parser.detect_black_segments(min_duration=2.0) == [(0.0, 5.0)]


# ===========================================================================
# cropdetect degenerate boxes
# ===========================================================================

def _crop(x1, x2, y1, y2):
    return {'cropdetect.x1': x1, 'cropdetect.x2': x2,
            'cropdetect.y1': y1, 'cropdetect.y2': y2}


def test_degenerate_crop_box_is_dropped():
    """cropdetect emits x1 beyond x2 when it finds no non-black content.

    A box with negative width is not a measurement; averaging it into an edge
    position invents geometry that was never detected.
    """
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_crop('5', '704', '1', '486')))
    prof.add_frame(2.0, _elem(_crop('566', '278', '407', '728')))   # degenerate
    p = prof.finalize()[0.0]
    assert p.crop_x1_median == pytest.approx(5.0)
    assert p.crop_x2_median == pytest.approx(704.0)
    assert p.crop_edge_iqr_max == pytest.approx(0.0)


def test_degenerate_crop_box_alone_reports_no_cropdetect():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_crop('719', '0', '485', '243')))
    p = prof.finalize()[0.0]
    assert p.crop_x1_median is None
    assert 'cropdetect' not in p.metrics


def test_inverted_vertical_crop_box_is_dropped():
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem(_crop('5', '704', '400', '100')))
    assert prof.finalize()[0.0].crop_y1_median is None


def test_partial_crop_tags_are_dropped():
    """All four edges are needed to validate the box."""
    prof = qbp.BinProfiler()
    prof.add_frame(1.0, _elem({'cropdetect.x1': '5', 'cropdetect.x2': '704'}))
    p = prof.finalize()[0.0]
    assert p.crop_x1_median is None
    assert 'cropdetect' not in p.metrics


def test_valid_crop_boxes_still_measure_edge_movement():
    prof = qbp.BinProfiler()
    for x1 in ('5', '5', '40', '5'):
        prof.add_frame(1.0, _elem(_crop(x1, '704', '1', '486')))
    p = prof.finalize()[0.0]
    assert p.crop_edge_iqr_max > 0
    assert 'cropdetect' in p.metrics
