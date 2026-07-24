# tests/test_eval_bars_timeline.py
import pytest

from AV_Spex.utils.generate_report import (
    select_failure_peaks,
    make_eval_bars_timeline_html,
    make_profile_piecharts,
    summarize_failures,
)


def _write_failures_csv(tmp_path, clusters):
    """Write a synthetic failures CSV from (start_seconds, n_frames, tag, value, threshold) clusters"""
    lines = ["Timestamp,Tag,Tag Value,Threshold"]
    for start_seconds, n_frames, tag, value, threshold in clusters:
        for i in range(n_frames):
            seconds = start_seconds + i * (1.0 / 29.97)
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = seconds % 60
            timestamp = f"{hours:02d}:{minutes:02d}:{secs:07.4f}"
            lines.append(f"{timestamp},{tag},{value},{threshold}")
    csv_path = tmp_path / "qct-parse_colorbars_eval_failures.csv"
    csv_path.write_text("\n".join(lines))
    return str(csv_path)


@pytest.fixture
def two_cluster_csv(tmp_path):
    """Two distinct failure clusters, 90 seconds apart"""
    return _write_failures_csv(tmp_path, [
        (10.0, 30, "YMAX", 925.0, 897.0),
        (100.0, 20, "SATMAX", 592.0, 358.0),
    ])


def test_select_failure_peaks_finds_clusters(two_cluster_csv):
    peaks = select_failure_peaks(two_cluster_csv, duration=200.0)

    assert len(peaks) == 2
    # Peaks come back in time order with a representative frame from each cluster
    assert 9.0 <= peaks[0]['seconds'] <= 12.0
    assert peaks[0]['tag'] == "YMAX"
    assert 99.0 <= peaks[1]['seconds'] <= 102.0
    assert peaks[1]['tag'] == "SATMAX"
    for peak in peaks:
        assert peak['count'] > 0
        assert peak['timestamp'].count(':') == 2


def test_select_failure_peaks_respects_max_peaks(two_cluster_csv):
    peaks = select_failure_peaks(two_cluster_csv, duration=200.0, max_peaks=1)
    assert len(peaks) == 1
    # The larger cluster wins
    assert peaks[0]['tag'] == "YMAX"


def test_select_failure_peaks_min_gap(tmp_path):
    # Two clusters 5 seconds apart on a 200s tape collapse into one peak
    # (min gap is max(10, 10% of duration) = 20s)
    csv_path = _write_failures_csv(tmp_path, [
        (10.0, 30, "YMAX", 925.0, 897.0),
        (15.0, 20, "SATMAX", 592.0, 358.0),
    ])
    peaks = select_failure_peaks(csv_path, duration=200.0)
    assert len(peaks) == 1


def test_select_failure_peaks_missing_file(tmp_path):
    assert select_failure_peaks(str(tmp_path / "nope.csv")) == []


def test_make_eval_bars_timeline_html(two_cluster_csv):
    peaks = select_failure_peaks(two_cluster_csv, duration=200.0)
    html = make_eval_bars_timeline_html(two_cluster_csv, "JPC_AV_TEST", peaks=peaks,
                                        video_duration=200.0, frame_rate=29.97)

    assert html is not None
    assert "plotly" in html
    assert "YMAX" in html
    assert "SATMAX" in html
    assert "% of frames outside threshold" in html
    assert "Show all failures" in html
    assert "toggleTable('evalbars_all')" in html


def test_make_eval_bars_timeline_html_with_thumbs(two_cluster_csv, tmp_path):
    import cv2
    import numpy as np
    thumb_path = tmp_path / "peak_thumb.jpg"
    cv2.imwrite(str(thumb_path), np.zeros((10, 10, 3), dtype=np.uint8))

    peaks = select_failure_peaks(two_cluster_csv, duration=200.0)
    peaks[0]['thumb_path'] = str(thumb_path)
    html = make_eval_bars_timeline_html(two_cluster_csv, "JPC_AV_TEST", peaks=peaks,
                                        video_duration=200.0, frame_rate=29.97)

    assert "data:image" in html
    assert "Peak 1:" in html
    assert "Click to enlarge" in html


def test_make_eval_bars_timeline_html_missing_file(tmp_path):
    assert make_eval_bars_timeline_html(str(tmp_path / "nope.csv"), "JPC_AV_TEST") is None


def test_make_profile_piecharts_without_failure_details(two_cluster_csv, tmp_path):
    summary_content = """Metadata line 1
Metadata line 2
TotalFrames,15000
Tag,Number of failed frames,Percentage of failed frames
YMAX,30,0.20
SATMAX,20,0.13
Total,50,0.33"""
    summary_path = tmp_path / "qct-parse_colorbars_eval_summary.csv"
    summary_path.write_text(summary_content)

    failure_info = summarize_failures(two_cluster_csv)
    html = make_profile_piecharts(str(summary_path), {}, failure_info, "JPC_AV_TEST",
                                  failure_csv_path=two_cluster_csv, failure_details=False)

    assert html is not None
    assert "see timeline below" in html
    # Per-tag failure lists and expandable tables are suppressed
    # (the string "Show all failures" itself still appears in the shared JS)
    assert 'id="link_tag_' not in html
    assert 'id="table_tag_' not in html
    assert "Peak Values outside of Threshold" not in html
