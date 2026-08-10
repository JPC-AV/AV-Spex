import json
import os

import pytest
from unittest.mock import MagicMock, patch

from AV_Spex.utils import config_edit
from AV_Spex.utils.config_setup import (
    SpexConfig, PROTECTED_PROFILES, is_protected_profile
)
from AV_Spex.utils.config_manager import ConfigManager


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------

def test_is_protected_profile_known_defaults():
    assert is_protected_profile('exiftool', 'Standard MKV Profile')
    assert is_protected_profile('mediainfo', 'Standard MKV Profile')
    assert is_protected_profile('ffprobe', 'Standard MKV Profile')
    assert is_protected_profile('filename', 'JPC Filename Profile')
    assert is_protected_profile('filename', 'Bowser Filename Profile')
    assert is_protected_profile('signalflow', 'JPC_AV_SVHS Signal Flow')
    assert is_protected_profile('signalflow', 'BVH3100 Signal Flow')


def test_is_protected_profile_custom_names():
    assert not is_protected_profile('exiftool', 'My Custom Profile')
    assert not is_protected_profile('filename', 'Standard MKV Profile')
    assert not is_protected_profile('unknown_domain', 'Standard MKV Profile')


@pytest.mark.parametrize("save_func,domain", [
    (config_edit.save_exiftool_profile, 'exiftool'),
    (config_edit.save_mediainfo_profile, 'mediainfo'),
    (config_edit.save_ffprobe_profile, 'ffprobe'),
    (config_edit.save_filename_profile, 'filename'),
    (config_edit.save_signalflow_profile, 'signalflow'),
])
def test_save_refuses_protected_profile(save_func, domain):
    protected_name = PROTECTED_PROFILES[domain][0]
    mock_mgr = MagicMock()
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        assert save_func(protected_name, {}) is False
    mock_mgr.replace_config_section.assert_not_called()


@pytest.mark.parametrize("delete_func,domain", [
    (config_edit.delete_exiftool_profile, 'exiftool'),
    (config_edit.delete_mediainfo_profile, 'mediainfo'),
    (config_edit.delete_ffprobe_profile, 'ffprobe'),
    (config_edit.delete_filename_profile, 'filename'),
    (config_edit.delete_signalflow_profile, 'signalflow'),
])
def test_delete_refuses_protected_profile(delete_func, domain):
    protected_name = PROTECTED_PROFILES[domain][0]
    mock_mgr = MagicMock()
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        assert delete_func(protected_name) is False
    mock_mgr.replace_config_section.assert_not_called()


def test_delete_custom_filename_profile():
    mock_mgr = MagicMock()
    filename_config = MagicMock()
    filename_config.filename_profiles = {
        'JPC Filename Profile': {'fn_sections': {}, 'FileExtension': 'mkv'},
        'My Custom': {'fn_sections': {}, 'FileExtension': 'mkv'},
    }
    mock_mgr.get_config.return_value = filename_config
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        assert config_edit.delete_filename_profile('My Custom') is True
    args = mock_mgr.replace_config_section.call_args.args
    assert args[0] == 'filename'
    assert args[1] == 'filename_profiles'
    assert 'My Custom' not in args[2]
    assert 'JPC Filename Profile' in args[2]


def test_save_custom_signalflow_profile_sets_name():
    mock_mgr = MagicMock()
    signalflow_config = MagicMock()
    signalflow_config.signalflow_profiles = {}
    # get_config returns the same config for save and for verification
    verified = MagicMock()
    verified.signalflow_profiles = {'My Chain': {}}
    mock_mgr.get_config.side_effect = [signalflow_config, verified]
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        result = config_edit.save_signalflow_profile(
            'My Chain', {'Source_VTR': ['Deck 1'], 'TBC_Framesync': []})
    assert result is True
    saved = mock_mgr.replace_config_section.call_args.args[2]['My Chain']
    assert saved['name'] == 'My Chain'
    assert saved['Source_VTR'] == ['Deck 1']


# ---------------------------------------------------------------------------
# Active profile storage
# ---------------------------------------------------------------------------

def _mock_spex(active_profiles=None):
    spex = MagicMock()
    spex.active_profiles = active_profiles or {}
    return spex


def test_set_active_profile_records_name():
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = _mock_spex()
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        config_edit.set_active_profile('exiftool', 'My Profile')
    mock_mgr.replace_config_section.assert_called_once_with(
        'spex', 'active_profiles', {'exiftool': 'My Profile'})


def test_set_active_profile_none_clears_entry():
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = _mock_spex({'exiftool': 'Old', 'filename': 'Keep'})
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        config_edit.set_active_profile('exiftool', None)
    mock_mgr.replace_config_section.assert_called_once_with(
        'spex', 'active_profiles', {'filename': 'Keep'})


def test_apply_exiftool_profile_records_active_profile():
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = _mock_spex()
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        config_edit.apply_exiftool_profile({'FileType': 'Matroska'}, profile_name='My Profile')
    replaced = {c.args[1]: c.args[2] for c in mock_mgr.replace_config_section.call_args_list}
    assert replaced['exiftool_values'] == {'FileType': 'Matroska'}
    assert replaced['active_profiles'] == {'exiftool': 'My Profile'}


def test_apply_exiftool_profile_without_name_leaves_active_profiles():
    mock_mgr = MagicMock()
    mock_mgr.get_config.return_value = _mock_spex()
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        config_edit.apply_exiftool_profile({'FileType': 'Matroska'})
    replaced_sections = [c.args[1] for c in mock_mgr.replace_config_section.call_args_list]
    assert 'active_profiles' not in replaced_sections


# ---------------------------------------------------------------------------
# Drift state
# ---------------------------------------------------------------------------

def _state_fixture(active, profiles, exiftool_values):
    """Build a mock config_mgr serving spex + exiftool configs."""
    spex = _mock_spex(active)
    spex.exiftool_values = exiftool_values
    exiftool_config = MagicMock()
    exiftool_config.exiftool_profiles = profiles

    def get_config(name, cls=None):
        return {'spex': spex, 'exiftool': exiftool_config}[name]

    mock_mgr = MagicMock()
    mock_mgr.get_config.side_effect = get_config
    return mock_mgr


def test_active_profile_state_no_profile_recorded():
    mock_mgr = _state_fixture({}, {}, {})
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        state = config_edit.get_active_profile_state('exiftool')
    assert state.name is None
    assert state.exists is False
    assert state.modified is False


def test_active_profile_state_clean_match():
    values = {'FileType': 'Matroska', 'CodecID': ['A_FLAC']}
    mock_mgr = _state_fixture({'exiftool': 'P1'}, {'P1': dict(values)}, values)
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        state = config_edit.get_active_profile_state('exiftool')
    assert state.name == 'P1'
    assert state.exists is True
    assert state.modified is False


def test_active_profile_state_modified():
    mock_mgr = _state_fixture(
        {'exiftool': 'P1'},
        {'P1': {'FileType': 'Matroska'}},
        {'FileType': 'MOV'},
    )
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        state = config_edit.get_active_profile_state('exiftool')
    assert state.modified is True


def test_active_profile_state_profile_deleted():
    mock_mgr = _state_fixture({'exiftool': 'Gone'}, {}, {'FileType': 'Matroska'})
    with patch('AV_Spex.utils.config_edit.config_mgr', mock_mgr):
        state = config_edit.get_active_profile_state('exiftool')
    assert state.name == 'Gone'
    assert state.exists is False


def test_mediainfo_matcher_maps_expected_sections():
    spex = _mock_spex({'mediainfo': 'P1'})
    spex.mediainfo_values = {
        'expected_general': {'FileExtension': 'mkv'},
        'expected_video': {'Format': 'FFV1'},
        'expected_audio': {'Format': 'FLAC'},
    }
    profile = {'general': {'FileExtension': 'mkv'},
               'video': {'Format': 'FFV1'},
               'audio': {'Format': 'FLAC'}}
    assert config_edit._mediainfo_matches(spex, profile) is True
    profile['video']['Format'] = 'ProRes'
    assert config_edit._mediainfo_matches(spex, profile) is False


def test_ffprobe_matcher_skips_tags():
    spex = _mock_spex({'ffprobe': 'P1'})
    spex.ffmpeg_values = {
        'video_stream': {'codec_name': 'ffv1'},
        'audio_stream': {'codec_name': ['flac']},
        'format': {'format_name': 'matroska webm', 'tags': {'ENCODER_SETTINGS': {'A': 1}}},
    }
    profile = {'video_stream': {'codec_name': 'ffv1'},
               'audio_stream': {'codec_name': ['flac']},
               'format': {'format_name': 'matroska webm', 'tags': {'different': 'tags'}}}
    assert config_edit._ffprobe_matches(spex, profile) is True


def test_signalflow_matcher_compares_stages():
    spex = _mock_spex({'signalflow': 'P1'})
    encoder = MagicMock()
    encoder.__dict__.update({
        'Source_VTR': ['SVO5800, SN 123'],
        'TBC_Framesync': ['DPS575, SN 456'],
        'ADC': [],
        'Capture_Device': ['UltraStudio'],
        'Computer': ['Mac Pro'],
    })
    spex.mediatrace_values = MagicMock()
    spex.mediatrace_values.ENCODER_SETTINGS = {
        'Source_VTR': ['SVO5800, SN 123'],
        'TBC_Framesync': ['DPS575, SN 456'],
        'ADC': [],
        'Capture_Device': ['UltraStudio'],
        'Computer': ['Mac Pro'],
    }
    profile = {'name': 'P1',
               'Source_VTR': ['SVO5800, SN 123'],
               'TBC_Framesync': ['DPS575, SN 456'],
               'ADC': [],
               'Capture_Device': ['UltraStudio'],
               'Computer': ['Mac Pro']}
    assert config_edit._signalflow_matches(spex, profile) is True
    profile['Source_VTR'] = ['BVH3100, SN 999']
    assert config_edit._signalflow_matches(spex, profile) is False


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_old_spex_json_without_active_profiles_loads_with_empty_dict():
    """A last_used_spex_config.json written before the active_profiles field
    existed must deserialize cleanly with the default empty dict."""
    bundled = os.path.join(
        os.path.dirname(config_edit.__file__), '..', 'config', 'spex_config.json')
    with open(bundled) as f:
        data = json.load(f)
    assert 'active_profiles' not in data  # bundled default predates the field

    config_mgr = ConfigManager()
    spex = config_mgr._deserialize_dataclass(SpexConfig, data)
    assert spex.active_profiles == {}
