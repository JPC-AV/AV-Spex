"""
Utility functions for importing MediaInfo data from JSON files.
Supports MediaInfo JSON output format (--Output=JSON).

Mirrors exiftool_import.py but adapted for MediaInfo's three-section
(General/Video/Audio) structure.
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
    MediainfoProfile, MediainfoGeneralValues,
    MediainfoVideoValues, MediainfoAudioValues
)


def parse_mediainfo_json_file(file_path: str) -> Optional[Dict[str, Dict[str, Any]]]:
    """
    Parse a MediaInfo JSON output file into section dictionaries.
    
    Handles the standard MediaInfo JSON structure:
        {"media": {"track": [{"@type": "General", ...}, {"@type": "Video", ...}, ...]}}
    
    Args:
        file_path: Path to the MediaInfo JSON file
        
    Returns:
        Dict with 'General', 'Video', 'Audio' keys containing track data,
        or None on failure
    """
    if not os.path.exists(file_path):
        logger.error(f"File not found: {file_path}")
        return None
    
    try:
        # Read in binary to handle encoding issues (same approach as mediainfo_check)
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
        
        mediainfo = json.loads(decoded_content)
        
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
        return None
    
    section_data = {"General": {}, "Video": {}, "Audio": {}}
    
    # Extract track information from the JSON structure
    if 'media' in mediainfo and 'track' in mediainfo['media']:
        tracks = mediainfo['media']['track']
        
        for track in tracks:
            track_type = track.get('@type')
            
            if track_type == 'General':
                section_data["General"] = track
            elif track_type == 'Video':
                section_data["Video"] = track
            elif track_type == 'Audio':
                section_data["Audio"] = track
    else:
        logger.error(f"Expected MediaInfo JSON structure not found in {file_path}")
        return None
    
    # Validate we got at least some data
    if not any(section_data.values()):
        logger.error(f"No valid MediaInfo data found in {file_path}")
        return None
    
    return section_data


def _get_fields_for_dataclass(dataclass_type) -> List[str]:
    """Get field names from a dataclass type."""
    return profile_import.get_dataclass_field_names(dataclass_type)

def extract_general_profile_fields(track_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract General section fields matching MediainfoGeneralValues.
    
    Args:
        track_data: Raw General track data from MediaInfo JSON
        
    Returns:
        Dict with fields matching MediainfoGeneralValues
    """
    fields_to_extract = _get_fields_for_dataclass(MediainfoGeneralValues)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name in track_data:
            profile_fields[field_name] = track_data[field_name]
    
    # Handle extra fields
    if "extra" in track_data:
        extra = track_data["extra"]
        if "ErrorDetectionType" in extra and "ErrorDetectionType" in fields_to_extract:
            profile_fields["ErrorDetectionType"] = extra["ErrorDetectionType"]
    
    return profile_fields


def extract_video_profile_fields(track_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract Video section fields matching MediainfoVideoValues.
    
    Handles special cases from the 'extra' sub-dict (MaxSlicesCount,
    ErrorDetectionType) matching mediainfo_check.extract_video_data().
    
    Args:
        track_data: Raw Video track data from MediaInfo JSON
        
    Returns:
        Dict with fields matching MediainfoVideoValues
    """
    fields_to_extract = _get_fields_for_dataclass(MediainfoVideoValues)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name in track_data:
            profile_fields[field_name] = track_data[field_name]
    
    # Handle special cases from extra field
    if "extra" in track_data:
        extra = track_data["extra"]
        if "MaxSlicesCount" in extra and "MaxSlicesCount" in fields_to_extract:
            profile_fields["MaxSlicesCount"] = extra["MaxSlicesCount"]
        if "ErrorDetectionType" in extra and "ErrorDetectionType" in fields_to_extract:
            profile_fields["ErrorDetectionType"] = extra["ErrorDetectionType"]
    
    return profile_fields


def extract_audio_profile_fields(track_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract Audio section fields matching MediainfoAudioValues.
    
    Args:
        track_data: Raw Audio track data from MediaInfo JSON
        
    Returns:
        Dict with fields matching MediainfoAudioValues
    """
    fields_to_extract = _get_fields_for_dataclass(MediainfoAudioValues)
    profile_fields = {}
    
    for field_name in fields_to_extract:
        if field_name in track_data:
            value = track_data[field_name]
            profile_fields[field_name] = value
    
    return profile_fields


def _apply_defaults(fields: Dict[str, Any], dataclass_type) -> Dict[str, Any]:
    """Fill in default values for any fields missing from extracted data."""
    return profile_import.apply_defaults(fields, dataclass_type)


# The parsing and extraction above is MediaInfo-specific; everything below is
# the shared sectioned-import machinery, parameterised by this spec.
SPEC = profile_import.ImportSpec(
    label='MediaInfo',
    profile_class=MediainfoProfile,
    parse=parse_mediainfo_json_file,
    sections=(
        profile_import.ImportSection(
            key='general', source_key='General',
            extract=extract_general_profile_fields,
            values_class=MediainfoGeneralValues),
        profile_import.ImportSection(
            key='video', source_key='Video',
            extract=extract_video_profile_fields,
            values_class=MediainfoVideoValues),
        profile_import.ImportSection(
            key='audio', source_key='Audio',
            extract=extract_audio_profile_fields,
            values_class=MediainfoAudioValues),
    ),
)


def import_mediainfo_file_to_profile(file_path: str) -> Optional[MediainfoProfile]:
    """Import a MediaInfo JSON file and create a MediainfoProfile from it."""
    return profile_import.import_file_to_profile(SPEC, file_path)


def compare_with_expected(imported_data: Dict[str, Dict],
                          expected_profile: MediainfoProfile) -> Dict[str, Dict]:
    """Compare imported MediaInfo data ('general'/'video'/'audio') with a profile."""
    return profile_import.compare_with_expected(SPEC, imported_data, expected_profile)


def validate_file_against_profile(file_path: str,
                                  profile: MediainfoProfile) -> Dict[str, Any]:
    """Validate a MediaInfo JSON file against an expected profile."""
    return profile_import.validate_file_against_profile(SPEC, file_path, profile)
