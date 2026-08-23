"""
Utility functions for importing FFprobe data from JSON files.
Supports FFprobe JSON output format (ffprobe -print_format json).

Mirrors mediainfo_import.py but adapted for FFprobe's three-section
(video_stream/audio_stream/format) structure.
"""

import json
import os
import dataclasses
from typing import Dict, Optional, List, Any
from pathlib import Path
from dataclasses import asdict

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils import profile_import
from AV_Spex.utils.config_setup import (
    FfprobeProfile, FFmpegVideoStream,
    FFmpegAudioStream, FFmpegFormat, EncoderSettings
)


def parse_ffprobe_json_file(file_path: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    Parse an FFprobe JSON output file into section dictionaries.
    
    Handles the standard FFprobe JSON structure:
        {"streams": [{...}, {...}], "format": {...}}
    
    Args:
        file_path: Path to the FFprobe JSON file
        
    Returns:
        Dict with 'video_stream', 'audio_stream', 'format' keys containing
        track data, or None on failure
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return None
    
    try:
        with open(file_path, 'rb') as f:
            content = f.read()
        
        try:
            decoded_content = content.decode('utf-8')
        except UnicodeDecodeError:
            try:
                decoded_content = content.decode('latin-1')
                logger.warning(f"Used latin-1 encoding as fallback for {file_path}")
            except Exception as e:
                logger.error(f"Failed to decode {file_path}: {e}")
                return None
        
        ffprobe_data = json.loads(decoded_content)
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
        return None
    
    section_data = {"video_stream": {}, "audio_stream": {}, "format": {}}
    
    # Extract stream information
    if 'streams' in ffprobe_data:
        streams = ffprobe_data['streams']
        
        for stream in streams:
            codec_type = stream.get('codec_type')
            
            if codec_type == 'video' and not section_data["video_stream"]:
                section_data["video_stream"] = stream
            elif codec_type == 'audio':
                # For audio, handle the case of multiple audio streams
                if not section_data["audio_stream"]:
                    section_data["audio_stream"] = stream
                else:
                    # Merge multi-stream audio list fields (codec_name, codec_long_name)
                    for list_field in ['codec_name', 'codec_long_name']:
                        if list_field in stream:
                            existing = section_data["audio_stream"].get(list_field, "")
                            if isinstance(existing, list):
                                existing.append(stream[list_field])
                            else:
                                section_data["audio_stream"][list_field] = [existing, stream[list_field]]
    else:
        logger.error(f"No 'streams' key found in {file_path}")
        return None
    
    # Extract format information
    if 'format' in ffprobe_data:
        section_data["format"] = ffprobe_data['format']
    
    # Validate we got at least some data
    if not any(section_data.values()):
        logger.error(f"No valid FFprobe data found in {file_path}")
        return None
    
    return section_data


def _get_fields_for_dataclass(dataclass_type) -> List[str]:
    """Get field names from a dataclass type."""
    return profile_import.get_dataclass_field_names(dataclass_type)

def extract_video_stream_fields(stream_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract video stream fields matching FFmpegVideoStream.
    
    Args:
        stream_data: Raw video stream data from FFprobe JSON
        
    Returns:
        Dict with fields matching FFmpegVideoStream
    """
    fields_to_extract = _get_fields_for_dataclass(FFmpegVideoStream)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name in stream_data:
            profile_fields[field_name] = stream_data[field_name]
    
    return profile_fields


def extract_audio_stream_fields(stream_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract audio stream fields matching FFmpegAudioStream.
    
    Handles list fields (codec_name, codec_long_name) which may represent
    multiple audio streams.
    
    Args:
        stream_data: Raw audio stream data from FFprobe JSON
        
    Returns:
        Dict with fields matching FFmpegAudioStream
    """
    fields_to_extract = _get_fields_for_dataclass(FFmpegAudioStream)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name in stream_data:
            value = stream_data[field_name]
            # Ensure list fields remain lists
            if field_name in ('codec_name', 'codec_long_name'):
                if not isinstance(value, list):
                    value = [value]
            profile_fields[field_name] = value
    
    return profile_fields


def extract_format_fields(format_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract format fields matching FFmpegFormat.
    
    Handles the tags sub-dictionary and ENCODER_SETTINGS.
    
    Args:
        format_data: Raw format data from FFprobe JSON
        
    Returns:
        Dict with fields matching FFmpegFormat
    """
    fields_to_extract = _get_fields_for_dataclass(FFmpegFormat)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name == 'tags':
            # Handle tags specially — extract known tag fields
            if 'tags' in format_data:
                tags = format_data['tags']
                profile_tags = {}
                
                # Known tag keys from FFmpegFormat.tags default
                known_tag_keys = [
                    'creation_time', 'ENCODER', 'TITLE',
                    'ENCODER_SETTINGS', 'DESCRIPTION',
                    'ORIGINAL MEDIA TYPE', 'ENCODED_BY'
                ]
                
                for tag_key in known_tag_keys:
                    if tag_key in tags:
                        profile_tags[tag_key] = tags[tag_key]
                    else:
                        profile_tags[tag_key] = None
                
                profile_fields['tags'] = profile_tags
        elif field_name in format_data:
            profile_fields[field_name] = format_data[field_name]
    
    return profile_fields


def _apply_defaults(fields: Dict[str, Any], dataclass_type) -> Dict[str, Any]:
    """Fill in default values for any fields missing from extracted data."""
    return profile_import.apply_defaults(fields, dataclass_type)


# FFmpegFormat.tags is a free-form dict, so introspection can supply only an
# empty one. These are the keys AV Spex expects to be present.
_FORMAT_TAGS_DEFAULT = {
    'creation_time': None,
    'ENCODER': None,
    'TITLE': None,
    'ENCODER_SETTINGS': None,
    'DESCRIPTION': None,
    'ORIGINAL MEDIA TYPE': None,
    'ENCODED_BY': None,
}


def _finalize_section(section_key: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Supply the format tags skeleton that apply_defaults cannot infer."""
    if section_key == 'format' and not fields.get('tags'):
        fields['tags'] = dict(_FORMAT_TAGS_DEFAULT)
    return fields


# The parsing and extraction above is FFprobe-specific; everything below is
# the shared sectioned-import machinery, parameterised by this spec.
SPEC = profile_import.ImportSpec(
    label='FFprobe',
    profile_class=FfprobeProfile,
    parse=parse_ffprobe_json_file,
    sections=(
        profile_import.ImportSection(
            key='video_stream', source_key='video_stream',
            extract=extract_video_stream_fields,
            values_class=FFmpegVideoStream),
        profile_import.ImportSection(
            key='audio_stream', source_key='audio_stream',
            extract=extract_audio_stream_fields,
            values_class=FFmpegAudioStream),
        profile_import.ImportSection(
            key='format', source_key='format',
            extract=extract_format_fields,
            values_class=FFmpegFormat),
    ),
    # 'tags' belongs to the signal-flow system, not to FFprobe profiles — the
    # same exclusion config_edit._ffprobe_matches applies.
    skip_fields=('tags',),
    finalize=_finalize_section,
)


def import_ffprobe_file_to_profile(file_path: str) -> Optional[FfprobeProfile]:
    """Import an FFprobe JSON file and create a FfprobeProfile from it."""
    return profile_import.import_file_to_profile(SPEC, file_path)


def compare_with_expected(imported_data: Dict[str, Dict],
                          expected_profile: FfprobeProfile) -> Dict[str, Dict]:
    """Compare imported FFprobe data against a profile ('tags' excluded)."""
    return profile_import.compare_with_expected(SPEC, imported_data, expected_profile)


def validate_file_against_profile(file_path: str,
                                  profile: FfprobeProfile) -> Dict[str, Any]:
    """Validate an FFprobe JSON file against an expected profile."""
    return profile_import.validate_file_against_profile(SPEC, file_path, profile)
