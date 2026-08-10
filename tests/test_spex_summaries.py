import json
import os

import pytest

from AV_Spex.utils import spex_summaries
from AV_Spex.utils.config_setup import (
    FilenameValues, FilenameSection, ExiftoolValues
)


@pytest.fixture
def bundled_spex():
    path = os.path.join(
        os.path.dirname(spex_summaries.__file__), '..', 'config', 'spex_config.json')
    with open(path) as f:
        return json.load(f)


# --- Full bundled config ----------------------------------------------------

def test_filename_summary_from_bundled_config(bundled_spex):
    assert spex_summaries.filename_summary(bundled_spex['filename_values']) == 'JPC_AV_#####.mkv'


def test_mediainfo_summary_from_bundled_config(bundled_spex):
    summary = spex_summaries.mediainfo_summary(bundled_spex['mediainfo_values'])
    assert 'FFV1' in summary
    assert '720×486' in summary
    assert 'Interlaced BFF' in summary


def test_exiftool_summary_from_bundled_config(bundled_spex):
    summary = spex_summaries.exiftool_summary(bundled_spex['exiftool_values'])
    assert 'MKV' in summary
    assert '720×486' in summary
    assert '24-bit audio' in summary


def test_ffprobe_summary_from_bundled_config(bundled_spex):
    summary = spex_summaries.ffprobe_summary(bundled_spex['ffmpeg_values'])
    assert 'ffv1' in summary
    assert 'yuv422p10le' in summary
    assert 'flac/pcm_s24le' in summary


def test_signalflow_summary_from_bundled_config(bundled_spex):
    summary = spex_summaries.signalflow_summary(bundled_spex['mediatrace_values'])
    assert '→' in summary
    assert summary.startswith('Sony BVH3100')


def test_qct_parse_summary_from_bundled_config(bundled_spex):
    summary = spex_summaries.qct_parse_summary(bundled_spex['qct_parse_values'])
    assert 'of 38 content thresholds set' in summary
    assert 'SMPTE bars limits configured' in summary


# --- Dataclass inputs -------------------------------------------------------

def test_filename_summary_accepts_dataclass():
    values = FilenameValues(
        fn_sections={
            'section1': FilenameSection(value='JPC', section_type='literal'),
            'section2': FilenameSection(value='#####', section_type='wildcard'),
        },
        FileExtension='mkv',
    )
    assert spex_summaries.filename_summary(values) == 'JPC_#####.mkv'


def test_exiftool_summary_accepts_dataclass():
    values = ExiftoolValues(
        FileType='MKV', FileTypeExtension='mkv', MIMEType='video/x-matroska',
        VideoFrameRate='29.97', ImageWidth='720', ImageHeight='486',
        VideoScanType='Interlaced', DisplayWidth='400', DisplayHeight='297',
        DisplayUnit='DAR', CodecID=['A_FLAC'], AudioChannels='2',
        AudioSampleRate='48000', AudioBitsPerSample='24',
    )
    summary = spex_summaries.exiftool_summary(values)
    assert 'MKV' in summary and '720×486' in summary


# --- Partial / empty / hostile inputs never raise ---------------------------

@pytest.mark.parametrize("func", [
    spex_summaries.filename_summary,
    spex_summaries.mediainfo_summary,
    spex_summaries.exiftool_summary,
    spex_summaries.ffprobe_summary,
    spex_summaries.signalflow_summary,
    spex_summaries.qct_parse_summary,
])
@pytest.mark.parametrize("value", [None, {}, [], "garbage", 42, {'unexpected': {'nested': None}}])
def test_summaries_are_total(func, value):
    result = func(value)
    assert isinstance(result, str)
    assert result  # never empty


def test_mediainfo_summary_partial_values():
    summary = spex_summaries.mediainfo_summary({'expected_video': {'Format': 'FFV1'}})
    assert summary == 'FFV1'


def test_signalflow_summary_empty_settings():
    assert spex_summaries.signalflow_summary(
        {'ENCODER_SETTINGS': {}}) == 'No signal flow set'


def test_signalflow_summary_comma_joined_entries():
    # Entries stored as one comma-joined string still summarize to the device
    summary = spex_summaries.signalflow_summary(
        {'ENCODER_SETTINGS': {'Source_VTR': ['SVO5800, SN 12345, composite']}})
    assert summary == 'SVO5800'
