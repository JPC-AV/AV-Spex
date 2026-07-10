from dataclasses import asdict
from typing import List, Dict, Union, Optional

import json
import os

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_setup import (
    ChecksConfig, SpexConfig, FilenameProfile, FilenameValues,
    FilenameSection, SignalflowConfig, ChecksProfile, ChecksProfilesConfig,
    ExiftoolConfig, ExiftoolProfile, MediainfoConfig, MediainfoProfile,
    FfprobeConfig, FfprobeProfile, is_mkv_extension
)
from AV_Spex.utils.config_manager import ConfigManager


config_mgr = ConfigManager() # Gets the singleton instance


def format_config_value(value, indent=0, is_nested=False):
    """Format config values for display."""
    spacer = " " * indent
    
    if isinstance(value, dict):
        formatted_str = "\n" if is_nested else ""
        for k, v in value.items():
            formatted_str += f"{spacer}{k}: {format_config_value(v, indent + 2, True)}\n"
        return formatted_str
    
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    
    # Handle boolean values
    if isinstance(value, bool):
        return "✅" if value else "❌"
    
    # Legacy support for any remaining "yes"/"no" strings (shouldn't happen with new system)
    if value == 'yes': return "✅"
    if value == 'no': return "❌"
    
    return str(value)


def print_config(config_spec='all'):
    """
    Print config state for specified config type(s) and optional subsections.

    Args:
        config_spec (str): Specification of what to print. Can be:
            - 'all': Print all configs
            - 'checks' or 'spex': Print entire specified config
            - 'checks,tools' or 'spex,filename_values': Print specific subsection
            - 'exiftool', 'mediainfo', or 'ffprobe': Print available profiles
    """
    if not validate_config_spec(config_spec):
        logger.error(f"Invalid config specification: {config_spec}.")
        logger.error(f"Format should be 'config[,subsection]' where config is one of: all, spex, checks, exiftool, mediainfo, ffprobe, signalflow - subsection (optional) is a valid section of the specified config\n")

    configs = {}
    profile_configs = {}

    # Parse the config specification
    parts = [p.strip() for p in config_spec.split(',')]
    config_type = parts[0]
    subsection = parts[1] if len(parts) > 1 else None

    # Load the requested config(s)
    if config_type in ['all', 'checks']:
        configs['Checks Config'] = config_mgr.get_config('checks', ChecksConfig)
    if config_type in ['all', 'spex']:
        configs['Spex Config'] = config_mgr.get_config('spex', SpexConfig)
    if config_type in ['all', 'exiftool']:
        profile_configs['ExifTool Profiles'] = config_mgr.get_config('exiftool', ExiftoolConfig)
    if config_type in ['all', 'mediainfo']:
        profile_configs['MediaInfo Profiles'] = config_mgr.get_config('mediainfo', MediainfoConfig)
    if config_type in ['all', 'ffprobe']:
        profile_configs['FFprobe Profiles'] = config_mgr.get_config('ffprobe', FfprobeConfig)
    if config_type in ['all', 'signalflow']:
        profile_configs['Signalflow Profiles'] = config_mgr.get_config('signalflow', SignalflowConfig)

    # Print the standard configs
    for config_name, config in configs.items():
        print(f"\n{config_name}:")
        config_dict = asdict(config)

        if subsection:
            # Print only the specified subsection if it exists
            if subsection in config_dict:
                print(f"{subsection}:")
                print(format_config_value(config_dict[subsection], indent=2))
            else:
                print(f"Subsection '{subsection}' not found in {config_name}")
        else:
            # Print entire config
            for key, value in config_dict.items():
                print(f"{key}:")
                print(format_config_value(value, indent=2))

    # Print profile configs (exiftool, mediainfo, ffprobe)
    for config_name, config in profile_configs.items():
        print(f"\n{config_name}:")
        config_dict = asdict(config)
        # Each of these configs has a single top-level key (e.g. exiftool_profiles)
        for key, profiles in config_dict.items():
            if not profiles:
                print("  (no profiles defined)")
            else:
                for profile_name, profile_values in profiles.items():
                    print(f"  {profile_name}:")
                    print(format_config_value(profile_values, indent=4))


def validate_config_spec(config_spec: str) -> bool:
    """
    Validate the config specification format.
    
    Args:
        config_spec: String specification of config to print
        
    Returns:
        bool: True if valid, False if invalid
    """
    if not config_spec:
        return False
        
    parts = [p.strip() for p in config_spec.split(',')]
    
    # Check base config type
    if parts[0] not in ['all', 'spex', 'checks', 'exiftool', 'mediainfo', 'ffprobe', 'signalflow']:
        return False

    # If subsection specified, validate against known subsections
    if len(parts) > 1:
        config_type = parts[0]
        subsection = parts[1]

        valid_subsections = {
            'spex': ['filename_values', 'mediainfo_values', 'exiftool_values',
                    'ffmpeg_values', 'mediatrace_values', 'qct_parse_values'],
            'checks': ['outputs', 'fixity', 'tools']
        }

        # exiftool/mediainfo/ffprobe don't have named subsections
        if config_type in ['exiftool', 'mediainfo', 'ffprobe']:
            return False

        # Only check subsection validity for specific configs (not 'all')
        if config_type != 'all':
            return subsection in valid_subsections[config_type]

    return True


def resolve_config(args, config_mapping):
    return config_mapping.get(args, None)


def apply_filename_profile(selected_profile: FilenameProfile):
    """
    Apply a FilenameProfile dataclass to the current configuration.
    
    Completely replaces the existing filename configuration with the selected profile,
    ensuring all sections are properly saved and persisted.
    """
    # Debug information about the provided profile
    logger.debug(f"==== APPLYING FILENAME PROFILE ====")
    logger.debug(f"Profile has {len(selected_profile.fn_sections)} sections")
    for idx, (key, section) in enumerate(sorted(selected_profile.fn_sections.items()), 1):
        logger.debug(f"  Section {idx}: {key} = {section.value} ({section.section_type})")
    
    # Create new sections dictionary by copying from the selected profile
    new_sections = {}
    for section_key, section_value in selected_profile.fn_sections.items():
        new_sections[section_key] = {
            'value': section_value.value,
            'section_type': section_value.section_type
        }
    
    # Replace the entire fn_sections dictionary
    config_mgr.replace_config_section('spex', 'filename_values.fn_sections', new_sections)
    
    # Replace the FileExtension
    config_mgr.replace_config_section('spex', 'filename_values.FileExtension', selected_profile.FileExtension)
    
    # Force a refresh to ensure changes are persisted
    config_mgr.refresh_configs()
    
    # Verify changes persisted
    final_config = config_mgr.get_config('spex', SpexConfig)
    logger.debug(f"Final verification after refresh: Config has {len(final_config.filename_values.fn_sections)} sections")


def get_signalflow_profile(profile_name: str):
    """
    Get a signalflow profile by name from the configuration.
    
    Args:
        profile_name (str): The name of the profile to retrieve
        
    Returns:
        SignalflowProfile or None: The requested profile or None if not found
    """
    config_mgr = ConfigManager()
    signalflow_config = config_mgr.get_config('signalflow', SignalflowConfig)
    
    if profile_name in signalflow_config.signalflow_profiles:
        return signalflow_config.signalflow_profiles[profile_name]
    
    return None

def apply_signalflow_profile(selected_profile):
    """
    Apply a SignalflowProfile dataclass or dict to the current configuration.
    
    Completely replaces the existing signalflow configuration with the selected profile,
    ensuring all sections are properly saved and persisted.
    
    Args:
        selected_profile (SignalflowProfile or dict): The signalflow profile to apply
    """
    # Debug information about the provided profile
    logger.debug(f"==== APPLYING SIGNALFLOW PROFILE ====")
    
    # Convert dict to proper structure if needed
    encoder_settings = {}
    
    if isinstance(selected_profile, dict):
        # If given a direct dict (from the hardcoded profiles or custom UI)
        if "name" in selected_profile:
            # This is already in the right format from the JSON config
            for key in ["Source_VTR", "TBC_Framesync", "ADC", "Capture_Device", "Computer"]:
                if key in selected_profile:
                    encoder_settings[key] = selected_profile[key]
        else:
            # This is from the old hardcoded dict format, just use as is
            encoder_settings = selected_profile
    else:
        # If given a SignalflowProfile dataclass, convert to dict
        encoder_settings = {
            "Source_VTR": selected_profile.Source_VTR,
            "TBC_Framesync": selected_profile.TBC_Framesync,
            "ADC": selected_profile.ADC,
            "Capture_Device": selected_profile.Capture_Device,
            "Computer": selected_profile.Computer
        }
    
    # Debug the settings we're going to apply
    for idx, (key, value) in enumerate(sorted(encoder_settings.items()), 1):
        logger.debug(f"  Setting {idx}: {key} = {value}")
    
    # Get the current spex config to check structure
    config_mgr = ConfigManager()
    spex_config = config_mgr.get_config('spex', SpexConfig)
    
    # Update mediatrace_values.ENCODER_SETTINGS
    # Create a new ENCODER_SETTINGS object with the profile values
    current_encoder_settings = {}
    if hasattr(spex_config.mediatrace_values.ENCODER_SETTINGS, '__dict__'):
        # Get existing attributes that aren't in the selected profile
        for key, value in spex_config.mediatrace_values.ENCODER_SETTINGS.__dict__.items():
            if not key.startswith('_') and key not in encoder_settings:
                current_encoder_settings[key] = value
    
    # Add all settings from the profile
    for key, value in encoder_settings.items():
        current_encoder_settings[key] = value
    
    # Replace the entire ENCODER_SETTINGS object
    config_mgr.replace_config_section('spex', 'mediatrace_values.ENCODER_SETTINGS', current_encoder_settings)
    
    # Check if ffmpeg_values.format.tags exists and update it
    if (hasattr(spex_config, 'ffmpeg_values') and 
        'format' in spex_config.ffmpeg_values and 
        'tags' in spex_config.ffmpeg_values['format']):
        
        # Get current ENCODER_SETTINGS dict or create a new one
        current_ffmpeg_settings = {}
        if ('ENCODER_SETTINGS' in spex_config.ffmpeg_values['format']['tags'] and 
            spex_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'] is not None):
            # Copy existing settings that aren't in the selected profile
            for key, value in spex_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'].items():
                if key not in encoder_settings:
                    current_ffmpeg_settings[key] = value
        
        # Add all settings from the profile
        for key, value in encoder_settings.items():
            current_ffmpeg_settings[key] = value
        
        # Replace the entire ENCODER_SETTINGS dictionary
        config_mgr.replace_config_section('spex', 'ffmpeg_values.format.tags.ENCODER_SETTINGS', current_ffmpeg_settings)
    
    # Force a refresh to ensure changes are persisted
    config_mgr.refresh_configs()
    
    # Verify changes persisted
    final_config = config_mgr.get_config('spex', SpexConfig)
    logger.debug(f"Final verification after refresh: Confirming encoder settings persisted")
    
    # Detailed verification
    final_mediatrace_keys = []
    if hasattr(final_config.mediatrace_values.ENCODER_SETTINGS, '__dict__'):
        final_mediatrace_keys = list(final_config.mediatrace_values.ENCODER_SETTINGS.__dict__.keys())
    
    final_ffmpeg_keys = []
    if (hasattr(final_config, 'ffmpeg_values') and 
        'format' in final_config.ffmpeg_values and 
        'tags' in final_config.ffmpeg_values['format'] and
        'ENCODER_SETTINGS' in final_config.ffmpeg_values['format']['tags']):
        final_ffmpeg_keys = list(final_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'].keys())
    
    logger.debug(f"Mediatrace encoder settings keys: {final_mediatrace_keys}")
    logger.debug(f"FFmpeg encoder settings keys: {final_ffmpeg_keys}")


def enforce_extension_compatibility():
    """Force off the checks that only work on Matroska when the configured
    input extension is non-MKV.

    Embedded stream fixity uses mkvextract/mkvpropedit and the mediatrace
    custom-tag check reads Matroska SimpleTags; neither works on other
    containers. This mirrors the CLI guardrail (av_spex_the_file) and the GUI
    graying (gui_checks_window), and is re-applied here because applying a
    profile replaces the fixity/tools sections wholesale and can re-enable
    these MKV-only options on a non-MKV configuration.

    Returns True if it changed anything, False otherwise.
    """
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    ext = getattr(checks_config, 'video_file_extension', 'mkv')
    if is_mkv_extension(ext):
        return False

    fixity_off = {}
    for field in ('embed_stream_fixity', 'validate_stream_fixity', 'overwrite_stream_fixity'):
        if getattr(checks_config.fixity, field):
            fixity_off[field] = False

    tools_off = {}
    if checks_config.tools.mediatrace.run_tool or checks_config.tools.mediatrace.check_tool:
        tools_off['mediatrace'] = {'run_tool': False, 'check_tool': False}

    if not (fixity_off or tools_off):
        return False

    updates = {}
    if fixity_off:
        updates['fixity'] = fixity_off
    if tools_off:
        updates['tools'] = tools_off
    config_mgr.update_config('checks', updates)
    logger.warning(
        f"Input extension '{ext}' is not MKV; embedded stream fixity and the "
        "mediatrace custom-tag check only work on Matroska. Forcing them off."
    )
    return True


def apply_profile(selected_profile):
    """Apply profile changes to checks_config.

    Args:
        selected_profile (dict): The profile configuration to apply
    """
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    
    # Prepare the updates dictionary with the structure matching the dataclass
    updates = {}
    
    # Handle validate_filename (top-level field)
    if 'validate_filename' in selected_profile:
        updates['validate_filename'] = selected_profile['validate_filename']

    # Handle video_file_extension (top-level field)
    if 'video_file_extension' in selected_profile:
        updates['video_file_extension'] = selected_profile['video_file_extension']

    # Handle outputs section
    if 'outputs' in selected_profile:
        updates['outputs'] = selected_profile['outputs']
    
    # Handle fixity section
    if 'fixity' in selected_profile:
        updates['fixity'] = selected_profile['fixity']
    
    # Handle tools section with special cases
    if 'tools' in selected_profile:
        tools_updates = {}
        
        for tool_name, tool_updates in selected_profile['tools'].items():
            # No need for special cases - the update_config method will handle it
            tools_updates[tool_name] = tool_updates
        
        updates['tools'] = tools_updates
    
    # Apply all updates at once using the new update_config method
    if updates:
        config_mgr.update_config('checks', updates)

    # A profile replaces the fixity/tools sections wholesale, which can re-enable
    # MKV-only checks on a non-MKV configuration. Re-apply the extension guardrail.
    enforce_extension_compatibility()


def apply_exiftool_profile(profile_data):
    """
    Apply an exiftool profile to the current spex configuration.
    
    Completely replaces the existing exiftool configuration with the selected profile,
    ensuring all fields are properly saved and persisted.
    
    Args:
        profile_data: Either an ExiftoolProfile dataclass instance or a dictionary
                     containing exiftool field values
    """
    from dataclasses import asdict
    
    # Debug information about the provided profile
    logger.debug(f"==== APPLYING EXIFTOOL PROFILE ====")

    # Ensure spex config is loaded into cache
    config_mgr.get_config('spex', SpexConfig)
    
    # Convert profile data to dictionary if it's a dataclass
    if hasattr(profile_data, '__dataclass_fields__'):
        # It's a dataclass, convert to dict
        profile_dict = asdict(profile_data)
        logger.debug(f"Converting ExiftoolProfile dataclass to dict")
    else:
        # Already a dict
        profile_dict = profile_data
        logger.debug(f"Using profile data as dict directly")
    
    # Log the fields being applied
    logger.debug(f"Profile has {len(profile_dict)} fields")
    for field, value in profile_dict.items():
        if isinstance(value, list):
            logger.debug(f"  {field}: {value} (list with {len(value)} items)")
        else:
            logger.debug(f"  {field}: {value}")
    
    # Replace the entire exiftool_values section
    config_mgr.replace_config_section('spex', 'exiftool_values', profile_dict)
    
    # Force a refresh to ensure changes are persisted
    config_mgr.refresh_configs()
    
    # Verify changes persisted
    final_config = config_mgr.get_config('spex', SpexConfig)
    logger.debug(f"Final verification after refresh: Exiftool values updated")
    
    # Verify specific fields
    if hasattr(final_config.exiftool_values, 'FileType'):
        logger.debug(f"Verified FileType: {final_config.exiftool_values.FileType}")
    if hasattr(final_config.exiftool_values, 'ImageWidth'):
        logger.debug(f"Verified ImageWidth: {final_config.exiftool_values.ImageWidth}")
    
    return True


def update_tool_setting(tool_names: List[str], value: bool):
    """
    Update specific tool settings using config_mgr.update_config
    Args:
        tool_names: List of strings in format 'tool.field'
        value: Boolean value (True or False)
    """
    updates = {'tools': {}, 'fixity': {}}
    
    for tool_spec in tool_names:
        try:
            tool_name, field = tool_spec.split('.')
            
            # Handle fixity settings separately (not in tools)
            if tool_name == 'fixity':
                if field not in ('check_fixity', 'validate_stream_fixity', 'embed_stream_fixity', 
                               'output_fixity', 'overwrite_stream_fixity'):
                    logger.warning(f"Invalid field '{field}' for fixity settings")
                    continue
                updates['fixity'][field] = value
                
            # Special handling for mediaconch which has different field names
            elif tool_name == 'mediaconch':
                if field not in ('run_mediaconch',):
                    logger.warning(f"Invalid field '{field}' for mediaconch. To turn mediaconch on/off use 'mediaconch.run_mediaconch'.")
                    continue
                updates['tools'][tool_name] = {field: value}
                
            # QCTools only has run_tool
            elif tool_name == 'qctools':
                if field not in ('run_tool',):
                    logger.warning(f"Invalid field '{field}' for qctools. Must be 'run_tool'")
                    continue
                updates['tools'][tool_name] = {field: value}
                
            # QCT Parse uses booleans for all fields
            elif tool_name == 'qct_parse':
                if field not in ('run_tool', 'barsDetection', 'evaluateBars', 'thumbExport', 'audio_analysis', 'detect_clamped_levels'):
                    logger.warning(f"Invalid field '{field}' for qct_parse")
                    continue
                updates['tools'][tool_name] = {field: value}

            # CLAMS detection: top-level run_tool runs both bars and tone
            # detectors. Numeric tuning is JSON-only (nested under bars/tone).
            elif tool_name == 'clams_detection':
                if field != 'run_tool':
                    logger.warning(
                        f"Invalid field '{field}' for clams_detection. Only 'run_tool' "
                        f"is settable from the CLI; tune bars/tone parameters in the JSON config."
                    )
                    continue
                updates['tools'][tool_name] = {field: value}

            # Standard tools with check_tool/run_tool fields
            else:
                if field not in ('check_tool', 'run_tool'):
                    logger.warning(f"Invalid field '{field}' for {tool_name}. Must be 'check_tool' or 'run_tool'")
                    continue
                updates['tools'][tool_name] = {field: value}
                
            logger.debug(f"{tool_name}.{field} will be set to {value}")
            
        except ValueError:
            logger.warning(f"Invalid format '{tool_spec}'. Expected format: tool.field")
    
    # Remove empty dictionaries before updating
    if not updates['tools']:
        del updates['tools']
    if not updates['fixity']:
        del updates['fixity']
    
    if updates:  # Only update if we have changes
        config_mgr.update_config('checks', updates)


def toggle_on(tool_names: List[str]):
    """Turn on specified tool settings."""
    update_tool_setting(tool_names, True)


def toggle_off(tool_names: List[str]):
    """Turn off specified tool settings."""
    update_tool_setting(tool_names, False)


def get_custom_profiles_config():
    """Get the custom profiles configuration."""
    # Force reload from disk by clearing cache first
    if 'profiles_checks' in config_mgr._configs:
        del config_mgr._configs['profiles_checks']
        
    # Use last_used=True to load saved profiles, falling back to bundled config
    config = config_mgr.get_config('profiles_checks', ChecksProfilesConfig, use_last_used=True)
    logger.debug(f"Loaded custom profiles config with {len(config.custom_profiles)} profiles: {list(config.custom_profiles.keys())}")
    return config
    

def get_available_custom_profiles() -> List[str]:
    """Get list of available custom profile names."""
    profiles_config = get_custom_profiles_config()
    return list(profiles_config.custom_profiles.keys())


def get_custom_profile(profile_name: str) -> Optional[ChecksProfile]:
    """Get a specific custom profile by name."""
    profiles_config = get_custom_profiles_config()
    return profiles_config.custom_profiles.get(profile_name)


def save_custom_profile(profile: ChecksProfile):
    """Save a custom profile using ConfigManager's replace_config_section method."""
    logger.debug(f"=== SAVING CUSTOM PROFILE ===")
    logger.debug(f"Profile name: {profile.name}")
    
    try:
        # Get current profiles
        profiles_config = get_custom_profiles_config()
        logger.debug(f"Current profiles before save: {list(profiles_config.custom_profiles.keys())}")
        
        # Create updated profiles dict with the new profile
        updated_profiles = {}
        
        # Add existing profiles
        for name, existing_profile in profiles_config.custom_profiles.items():
            updated_profiles[name] = asdict(existing_profile)
        
        # Add the new profile
        updated_profiles[profile.name] = asdict(profile)
        
        logger.debug(f"Updated profiles dict will have: {list(updated_profiles.keys())}")
        
        # Use replace_config_section to replace the entire custom_profiles dict
        config_mgr.replace_config_section('profiles_checks', 'custom_profiles', updated_profiles)
        
        logger.info(f"Successfully saved custom profile: {profile.name}")
        
        # Verify the save worked
        verification_config = get_custom_profiles_config()
        if profile.name in verification_config.custom_profiles:
            logger.debug(f"Verification: Profile '{profile.name}' confirmed saved")
        else:
            logger.error(f"Verification failed: Profile '{profile.name}' not found after save")
            logger.debug(f"Available profiles after save: {list(verification_config.custom_profiles.keys())}")
        
    except Exception as e:
        logger.error(f"Error saving custom profile '{profile.name}': {str(e)}")
        import traceback
        traceback.print_exc()
        raise


def delete_custom_profile(profile_name: str) -> bool:
    """Delete a custom profile using ConfigManager's replace_config_section method."""
    profiles_config = get_custom_profiles_config()
    if profile_name not in profiles_config.custom_profiles:
        logger.warning(f"Profile '{profile_name}' not found, cannot delete")
        return False
    
    try:
        # Create updated profiles dict without the deleted profile
        updated_profiles = {k: asdict(v) for k, v in profiles_config.custom_profiles.items() if k != profile_name}
        
        # Use replace_config_section to replace the entire custom_profiles dict
        config_mgr.replace_config_section('profiles_checks', 'custom_profiles', updated_profiles)
        logger.info(f"Deleted custom profile: {profile_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting custom profile '{profile_name}': {str(e)}")
        return False


def apply_custom_profile(profile_name: str):
    """Apply a custom profile to the current checks configuration."""
    profile = get_custom_profile(profile_name)
    if not profile:
        logger.error(f"Custom profile '{profile_name}' not found")
        return False
    
    try:
        # Convert the profile to the format expected by apply_profile
        profile_dict = {
            "validate_filename": profile.validate_filename,
            "video_file_extension": profile.video_file_extension,
            "outputs": asdict(profile.outputs),
            "fixity": asdict(profile.fixity),
            "tools": asdict(profile.tools)
        }
        
        apply_profile(profile_dict)
        logger.info(f"Applied custom profile: {profile_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error applying custom profile '{profile_name}': {str(e)}")
        return False


def create_profile_from_current_config(profile_name: str, description: str = "") -> ChecksProfile:
    """Create a new custom profile from the current checks configuration."""
    current_config = config_mgr.get_config('checks', ChecksConfig)
    
    # Create a new profile with the current configuration
    new_profile = ChecksProfile(
        name=profile_name,
        description=description,
        validate_filename=current_config.validate_filename,
        video_file_extension=current_config.video_file_extension,
        outputs=current_config.outputs,
        fixity=current_config.fixity,
        tools=current_config.tools
    )
    
    return new_profile


def get_all_profiles() -> Dict[str, Union[dict, ChecksProfile]]:
    """Get all available profiles (both built-in and custom)."""
    all_profiles = {}
    
    # Add built-in profiles
    all_profiles.update({
        "Step 1 Profile": profile_step1,
        "Step 2 Profile": profile_step2,
        "All Off Profile": profile_allOff,
        "Vendor Profile": profile_vendor
    })
    
    # Add custom profiles
    custom_profiles = get_custom_profiles_config().custom_profiles
    all_profiles.update(custom_profiles)
    
    return all_profiles


def get_exiftool_profile(profile_name: str):
    """
    Get an exiftool profile by name from the configuration.
    
    Args:
        profile_name (str): The name of the profile to retrieve
        
    Returns:
        ExiftoolProfile or None: The requested profile or None if not found
    """
    from AV_Spex.utils.config_setup import ExiftoolConfig
    
    try:
        config_mgr = ConfigManager()
        exiftool_config = config_mgr.get_config('exiftool', ExiftoolConfig)
        
        if profile_name in exiftool_config.exiftool_profiles:
            return exiftool_config.exiftool_profiles[profile_name]
    except Exception as e:
        logger.warning(f"Could not retrieve exiftool profile '{profile_name}': {str(e)}")
    
    return None


def get_available_exiftool_profiles() -> List[str]:
    """
    Get a list of all available exiftool profile names.
    
    Returns:
        List[str]: List of profile names
    """
    from AV_Spex.utils.config_setup import ExiftoolConfig
    
    try:
        config_mgr = ConfigManager()
        exiftool_config = config_mgr.get_config('exiftool', ExiftoolConfig)
        
        if hasattr(exiftool_config, 'exiftool_profiles'):
            return list(exiftool_config.exiftool_profiles.keys())
    except Exception as e:
        logger.warning(f"Could not retrieve exiftool profiles: {str(e)}")
    
    return []


def save_exiftool_profile(profile_name: str, profile_data):
    """
    Save an exiftool profile to the configuration.
    
    Args:
        profile_name (str): Name for the profile
        profile_data: ExiftoolProfile dataclass or dict with profile data
        
    Returns:
        bool: True if successful, False otherwise
    """
    from AV_Spex.utils.config_setup import ExiftoolConfig, ExiftoolProfile
    from dataclasses import asdict
    
    logger.debug(f"=== SAVING EXIFTOOL PROFILE ===")
    logger.debug(f"Profile name: {profile_name}")
    
    try:
        # Get current exiftool config or create new one
        try:
            exiftool_config = config_mgr.get_config('exiftool', ExiftoolConfig)
            logger.debug(f"Current profiles before save: {list(exiftool_config.exiftool_profiles.keys())}")
        except:
            # Config doesn't exist yet, create it
            exiftool_config = ExiftoolConfig()
            logger.debug("Creating new exiftool config")
        
        # Ensure exiftool_profiles dict exists
        if not hasattr(exiftool_config, 'exiftool_profiles'):
            exiftool_config.exiftool_profiles = {}
        
        # Create updated profiles dict
        updated_profiles = {}
        
        # Add existing profiles
        for name, existing_profile in exiftool_config.exiftool_profiles.items():
            if hasattr(existing_profile, '__dataclass_fields__'):
                updated_profiles[name] = asdict(existing_profile)
            else:
                updated_profiles[name] = existing_profile
        
        # Add the new profile
        if hasattr(profile_data, '__dataclass_fields__'):
            updated_profiles[profile_name] = asdict(profile_data)
        else:
            updated_profiles[profile_name] = profile_data
        
        logger.debug(f"Updated profiles dict will have: {list(updated_profiles.keys())}")
        
        # Use replace_config_section to replace the entire exiftool_profiles dict
        config_mgr.replace_config_section('exiftool', 'exiftool_profiles', updated_profiles)
        
        logger.info(f"Successfully saved exiftool profile: {profile_name}")
        
        # Verify the save worked
        verification_config = config_mgr.get_config('exiftool', ExiftoolConfig)
        if profile_name in verification_config.exiftool_profiles:
            logger.debug(f"Verification: Profile '{profile_name}' confirmed saved")
            return True
        else:
            logger.error(f"Verification failed: Profile '{profile_name}' not found after save")
            return False
        
    except Exception as e:
        logger.error(f"Error saving exiftool profile '{profile_name}': {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def delete_exiftool_profile(profile_name: str) -> bool:
    """
    Delete an exiftool profile from the configuration.
    
    Args:
        profile_name (str): Name of the profile to delete
        
    Returns:
        bool: True if successful, False otherwise
    """
    from AV_Spex.utils.config_setup import ExiftoolConfig
    from dataclasses import asdict
    
    try:
        exiftool_config = config_mgr.get_config('exiftool', ExiftoolConfig)
        
        if profile_name not in exiftool_config.exiftool_profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False
        
        # Create updated profiles dict without the deleted profile
        updated_profiles = {}
        for k, v in exiftool_config.exiftool_profiles.items():
            if k != profile_name:
                if hasattr(v, '__dataclass_fields__'):
                    updated_profiles[k] = asdict(v)
                else:
                    updated_profiles[k] = v
        
        # Use replace_config_section to replace the entire exiftool_profiles dict
        config_mgr.replace_config_section('exiftool', 'exiftool_profiles', updated_profiles)
        
        logger.info(f"Deleted exiftool profile: {profile_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting exiftool profile '{profile_name}': {str(e)}")
        return False
    
def apply_mediainfo_profile(profile_data):
    """
    Apply a MediaInfo profile to the current spex configuration.
    
    Replaces the existing mediainfo_values section in spex config.
    
    IMPORTANT - ConfigManager compatibility note:
        SpexConfig.mediainfo_values is typed as Dict[str, Union[...]].
        ConfigManager._handle_dict does NOT auto-deserialize Union values,
        so mediainfo_values entries are plain dicts (not dataclass instances).
        Therefore, this function must write plain dicts via asdict().
    
    Args:
        profile_data: MediainfoProfile dataclass instance or dict with
                     nested 'general', 'video', 'audio' sections
    """
    from dataclasses import asdict
    
    logger.debug(f"==== APPLYING MEDIAINFO PROFILE ====")

    # Ensure spex config is loaded into cache
    config_mgr.get_config('spex', SpexConfig)
    
    # Convert profile data to dictionary if it's a dataclass
    if hasattr(profile_data, '__dataclass_fields__'):
        profile_dict = asdict(profile_data)
        logger.debug("Converting MediainfoProfile dataclass to dict")
    else:
        profile_dict = profile_data
        logger.debug("Using profile data as dict directly")
    
    # Build the mediainfo_values structure matching SpexConfig format.
    # The spex config uses keys 'expected_general', 'expected_video',
    # 'expected_audio' — the profile uses 'general', 'video', 'audio'.
    mediainfo_values = {
        'expected_general': profile_dict.get('general', {}),
        'expected_video': profile_dict.get('video', {}),
        'expected_audio': profile_dict.get('audio', {})
    }
    
    logger.debug(f"General fields: {len(mediainfo_values['expected_general'])}")
    logger.debug(f"Video fields: {len(mediainfo_values['expected_video'])}")
    logger.debug(f"Audio fields: {len(mediainfo_values['expected_audio'])}")
    
    # Replace the entire mediainfo_values section
    config_mgr.replace_config_section('spex', 'mediainfo_values', mediainfo_values)
    
    # Force a refresh to ensure changes are persisted
    config_mgr.refresh_configs()
    
    # Verify changes persisted
    final_config = config_mgr.get_config('spex', SpexConfig)
    logger.debug("Final verification after refresh: MediaInfo values updated")
    
    # Verify we can read the sections back
    mi_values = final_config.mediainfo_values
    if isinstance(mi_values, dict) and 'expected_general' in mi_values:
        logger.debug(f"Verified expected_general has {len(mi_values['expected_general'])} fields")
    
    return True


def get_mediainfo_profile(profile_name: str):
    """
    Get a MediaInfo profile by name from the configuration.
    
    Args:
        profile_name (str): The name of the profile to retrieve
        
    Returns:
        MediainfoProfile or None: The requested profile or None if not found
    """
    from AV_Spex.utils.config_setup import MediainfoConfig
    
    try:
        config_mgr_instance = ConfigManager()
        mediainfo_config = config_mgr_instance.get_config('mediainfo', MediainfoConfig)
        
        if profile_name in mediainfo_config.mediainfo_profiles:
            return mediainfo_config.mediainfo_profiles[profile_name]
    except Exception as e:
        logger.warning(f"Could not retrieve mediainfo profile '{profile_name}': {str(e)}")
    
    return None


def get_available_mediainfo_profiles() -> List[str]:
    """
    Get a list of all available MediaInfo profile names.
    
    Returns:
        List[str]: List of profile names
    """
    from AV_Spex.utils.config_setup import MediainfoConfig
    
    try:
        config_mgr_instance = ConfigManager()
        mediainfo_config = config_mgr_instance.get_config('mediainfo', MediainfoConfig)
        
        if hasattr(mediainfo_config, 'mediainfo_profiles'):
            return list(mediainfo_config.mediainfo_profiles.keys())
    except Exception as e:
        logger.warning(f"Could not retrieve mediainfo profiles: {str(e)}")
    
    return []


def save_mediainfo_profile(profile_name: str, profile_data) -> bool:
    """
    Save a MediaInfo profile to the configuration.
    
    Follows the same pattern as save_exiftool_profile():
    - Get or create MediainfoConfig
    - Build updated profiles dict (existing + new)
    - Replace entire mediainfo_profiles section via ConfigManager
    - Verify save
    
    Args:
        profile_name (str): Name for the profile
        profile_data: MediainfoProfile dataclass or dict with profile data
        
    Returns:
        bool: True if successful, False otherwise
    """
    from AV_Spex.utils.config_setup import MediainfoConfig, MediainfoProfile
    from dataclasses import asdict
    
    logger.debug(f"=== SAVING MEDIAINFO PROFILE ===")
    logger.debug(f"Profile name: {profile_name}")
    
    try:
        # Get current mediainfo config or create new one
        try:
            mediainfo_config = config_mgr.get_config('mediainfo', MediainfoConfig)
            logger.debug(f"Current profiles before save: {list(mediainfo_config.mediainfo_profiles.keys())}")
        except:
            # Config doesn't exist yet, create it
            mediainfo_config = MediainfoConfig()
            # Place in cache so replace_config_section can operate on it
            config_mgr._configs['mediainfo'] = mediainfo_config
            logger.debug("Creating new mediainfo config")
        
        # Ensure mediainfo_profiles dict exists
        if not hasattr(mediainfo_config, 'mediainfo_profiles'):
            mediainfo_config.mediainfo_profiles = {}
        
        # Create updated profiles dict
        updated_profiles = {}
        
        # Add existing profiles
        for name, existing_profile in mediainfo_config.mediainfo_profiles.items():
            if hasattr(existing_profile, '__dataclass_fields__'):
                updated_profiles[name] = asdict(existing_profile)
            else:
                updated_profiles[name] = existing_profile
        
        # Add the new profile
        if hasattr(profile_data, '__dataclass_fields__'):
            updated_profiles[profile_name] = asdict(profile_data)
        else:
            updated_profiles[profile_name] = profile_data
        
        logger.debug(f"Updated profiles dict will have: {list(updated_profiles.keys())}")
        
        # Use replace_config_section to replace the entire mediainfo_profiles dict
        config_mgr.replace_config_section('mediainfo', 'mediainfo_profiles', updated_profiles)
        
        logger.info(f"Successfully saved mediainfo profile: {profile_name}")
        
        # Verify the save worked
        verification_config = config_mgr.get_config('mediainfo', MediainfoConfig)
        if profile_name in verification_config.mediainfo_profiles:
            logger.debug(f"Verification: Profile '{profile_name}' confirmed saved")
            return True
        else:
            logger.error(f"Verification failed: Profile '{profile_name}' not found after save")
            return False
        
    except Exception as e:
        logger.error(f"Error saving mediainfo profile '{profile_name}': {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def delete_mediainfo_profile(profile_name: str) -> bool:
    """
    Delete a MediaInfo profile from the configuration.
    
    Args:
        profile_name (str): Name of the profile to delete
        
    Returns:
        bool: True if successful, False otherwise
    """
    from AV_Spex.utils.config_setup import MediainfoConfig
    from dataclasses import asdict
    
    try:
        mediainfo_config = config_mgr.get_config('mediainfo', MediainfoConfig)
        
        if profile_name not in mediainfo_config.mediainfo_profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False
        
        # Create updated profiles dict without the deleted profile
        updated_profiles = {}
        for k, v in mediainfo_config.mediainfo_profiles.items():
            if k != profile_name:
                if hasattr(v, '__dataclass_fields__'):
                    updated_profiles[k] = asdict(v)
                else:
                    updated_profiles[k] = v
        
        # Use replace_config_section to replace the entire mediainfo_profiles dict
        config_mgr.replace_config_section('mediainfo', 'mediainfo_profiles', updated_profiles)
        
        logger.info(f"Deleted mediainfo profile: {profile_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting mediainfo profile '{profile_name}': {str(e)}")
        return False


def apply_ffprobe_profile(profile_data):
    """
    Apply an FFprobe profile to the current spex configuration.
    
    Replaces the existing ffmpeg_values section in spex config.
    
    IMPORTANT - ConfigManager compatibility note:
        SpexConfig.ffmpeg_values is typed as Dict[str, Union[...]].
        ConfigManager._handle_dict does NOT auto-deserialize Union values,
        so ffmpeg_values entries are plain dicts (not dataclass instances).
        Therefore, this function must write plain dicts via asdict().
    
    Args:
        profile_data: FfprobeProfile dataclass instance or dict with
                     nested 'video_stream', 'audio_stream', 'format' sections
    """
    from dataclasses import asdict
    
    logger.debug(f"==== APPLYING FFPROBE PROFILE ====")

    # Ensure spex config is loaded into cache
    config_mgr.get_config('spex', SpexConfig)
    
    # Convert profile data to dictionary if it's a dataclass
    if hasattr(profile_data, '__dataclass_fields__'):
        profile_dict = asdict(profile_data)
        logger.debug("Converting FfprobeProfile dataclass to dict")
    else:
        profile_dict = profile_data
        logger.debug("Using profile data as dict directly")
    
    # Build the ffmpeg_values structure matching SpexConfig format.
    ffmpeg_values = {
        'video_stream': profile_dict.get('video_stream', {}),
        'audio_stream': profile_dict.get('audio_stream', {}),
        'format': profile_dict.get('format', {})
    }
    
    logger.debug(f"Video stream fields: {len(ffmpeg_values['video_stream'])}")
    logger.debug(f"Audio stream fields: {len(ffmpeg_values['audio_stream'])}")
    logger.debug(f"Format fields: {len(ffmpeg_values['format'])}")
    
    # Replace the entire ffmpeg_values section
    config_mgr.replace_config_section('spex', 'ffmpeg_values', ffmpeg_values)
    
    # Force a refresh to ensure changes are persisted
    config_mgr.refresh_configs()
    
    # Verify changes persisted
    final_config = config_mgr.get_config('spex', SpexConfig)
    logger.debug("Final verification after refresh: FFprobe values updated")
    
    # Verify we can read the sections back
    ff_values = final_config.ffmpeg_values
    if isinstance(ff_values, dict) and 'video_stream' in ff_values:
        logger.debug(f"Verified video_stream has {len(ff_values['video_stream'])} fields")
    
    return True


def get_ffprobe_profile(profile_name: str):
    """
    Get an FFprobe profile by name from the configuration.
    
    Args:
        profile_name (str): The name of the profile to retrieve
        
    Returns:
        FfprobeProfile or None: The requested profile or None if not found
    """
    try:
        config_mgr_instance = ConfigManager()
        ffprobe_config = config_mgr_instance.get_config('ffprobe', FfprobeConfig)
        
        if profile_name in ffprobe_config.ffprobe_profiles:
            return ffprobe_config.ffprobe_profiles[profile_name]
    except Exception as e:
        logger.warning(f"Could not retrieve ffprobe profile '{profile_name}': {str(e)}")
    
    return None


def get_available_ffprobe_profiles() -> List[str]:
    """
    Get a list of all available FFprobe profile names.
    
    Returns:
        List[str]: List of profile names
    """
    try:
        config_mgr_instance = ConfigManager()
        ffprobe_config = config_mgr_instance.get_config('ffprobe', FfprobeConfig)
        
        if hasattr(ffprobe_config, 'ffprobe_profiles'):
            return list(ffprobe_config.ffprobe_profiles.keys())
    except Exception as e:
        logger.warning(f"Could not retrieve ffprobe profiles: {str(e)}")
    
    return []


def save_ffprobe_profile(profile_name: str, profile_data) -> bool:
    """
    Save an FFprobe profile to the configuration.
    
    Follows the same pattern as save_mediainfo_profile().
    
    Args:
        profile_name (str): Name for the profile
        profile_data: FfprobeProfile dataclass or dict with profile data
        
    Returns:
        bool: True if successful, False otherwise
    """
    from dataclasses import asdict
    
    logger.debug(f"=== SAVING FFPROBE PROFILE ===")
    logger.debug(f"Profile name: {profile_name}")
    
    try:
        # Get current ffprobe config or create new one
        try:
            ffprobe_config = config_mgr.get_config('ffprobe', FfprobeConfig)
            logger.debug(f"Current profiles before save: {list(ffprobe_config.ffprobe_profiles.keys())}")
        except:
            # Config doesn't exist yet, create it
            ffprobe_config = FfprobeConfig()
            # Place in cache so replace_config_section can operate on it
            config_mgr._configs['ffprobe'] = ffprobe_config
            logger.debug("Creating new ffprobe config")
        
        # Ensure ffprobe_profiles dict exists
        if not hasattr(ffprobe_config, 'ffprobe_profiles'):
            ffprobe_config.ffprobe_profiles = {}
        
        # Create updated profiles dict
        updated_profiles = {}
        
        # Add existing profiles
        for name, existing_profile in ffprobe_config.ffprobe_profiles.items():
            if hasattr(existing_profile, '__dataclass_fields__'):
                updated_profiles[name] = asdict(existing_profile)
            else:
                updated_profiles[name] = existing_profile
        
        # Add the new profile
        if hasattr(profile_data, '__dataclass_fields__'):
            updated_profiles[profile_name] = asdict(profile_data)
        else:
            updated_profiles[profile_name] = profile_data
        
        logger.debug(f"Updated profiles dict will have: {list(updated_profiles.keys())}")
        
        # Use replace_config_section to replace the entire ffprobe_profiles dict
        config_mgr.replace_config_section('ffprobe', 'ffprobe_profiles', updated_profiles)
        
        logger.info(f"Successfully saved ffprobe profile: {profile_name}")
        
        # Verify the save worked
        verification_config = config_mgr.get_config('ffprobe', FfprobeConfig)
        if profile_name in verification_config.ffprobe_profiles:
            logger.debug(f"Verification: Profile '{profile_name}' confirmed saved")
            return True
        else:
            logger.error(f"Verification failed: Profile '{profile_name}' not found after save")
            return False
        
    except Exception as e:
        logger.error(f"Error saving ffprobe profile '{profile_name}': {str(e)}")
        import traceback
        traceback.print_exc()
        return False


def delete_ffprobe_profile(profile_name: str) -> bool:
    """
    Delete an FFprobe profile from the configuration.
    
    Args:
        profile_name (str): Name of the profile to delete
        
    Returns:
        bool: True if successful, False otherwise
    """
    from dataclasses import asdict
    
    try:
        ffprobe_config = config_mgr.get_config('ffprobe', FfprobeConfig)
        
        if profile_name not in ffprobe_config.ffprobe_profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False
        
        # Create updated profiles dict without the deleted profile
        updated_profiles = {}
        for k, v in ffprobe_config.ffprobe_profiles.items():
            if k != profile_name:
                if hasattr(v, '__dataclass_fields__'):
                    updated_profiles[k] = asdict(v)
                else:
                    updated_profiles[k] = v
        
        # Use replace_config_section to replace the entire ffprobe_profiles dict
        config_mgr.replace_config_section('ffprobe', 'ffprobe_profiles', updated_profiles)
        
        logger.info(f"Deleted ffprobe profile: {profile_name}")
        return True
        
    except Exception as e:
        logger.error(f"Error deleting ffprobe profile '{profile_name}': {str(e)}")
        return False


# Profile definitions with boolean values
profile_step1 = {
    "validate_filename": True,
    "tools": {
        "exiftool": {
            "check_tool": True,
            "run_tool": True
        },
        "ffprobe": {
            "check_tool": True,
            "run_tool": True
        },
        "mediaconch": {
            "mediaconch_policy": "JPC_FFV1-MKV_Preservation_Policy_20260709.xml",
            "run_mediaconch": True
        },
        "mediainfo": {
            "check_tool": True,
            "run_tool": True
        },
        "mediatrace": {
            "check_tool": True,
            "run_tool": True
        },
        "mkvalidator": {
            "check_tool": False,
            "run_tool": False
        },
        "qctools": {
            "run_tool": False
        },
        "qct_parse": {
            "run_tool": False,
            "barsDetection": False,
            "evaluateBars": False,
            "thumbExport": False,
            "audio_analysis": False,
            "detect_clamped_levels": False
        },
        "clams_detection": {
            "run_tool": False,
            "bars": {
                "threshold": 0.7,
                "sample_ratio": 30,
                "stop_at_frame": 9000,
                "min_frame_count": 10,
                "stop_after_one": True
            },
            "tone": {
                "tolerance": 1.0,
                "min_tone_duration_ms": 2000,
                "stop_at_seconds": 3600
            }
        }
    },
    "outputs": {
        "access_file": False,
        "report": False,
        "qctools_ext": "qctools.xml.gz",
        "frame_analysis": {
            "enable_border_detection": False,
            "enable_brng_analysis": False,
            "enable_signalstats": False
        }
    },
    "fixity": {
        "check_fixity": False,
        "validate_stream_fixity": False,
        "embed_stream_fixity": True,
        "output_fixity": True,
        "overwrite_stream_fixity": False
    }
}

profile_step2 = {
    "validate_filename": True,
    "tools": {
        "exiftool": {
            "check_tool": True,
            "run_tool": False
        },
        "ffprobe": {
            "check_tool": True,
            "run_tool": False
        },
        "mediaconch": {
            "mediaconch_policy": "JPC_FFV1-MKV_Preservation_Policy_20260709.xml",
            "run_mediaconch": True
        },
        "mediainfo": {
            "check_tool": True,
            "run_tool": False
        },
        "mediatrace": {
            "check_tool": True,
            "run_tool": False
        },
        "mkvalidator": {
            "check_tool": False,
            "run_tool": False
        },
        "qctools": {
            "run_tool": True
        },
        "qct_parse": {
            "run_tool": True,
            "barsDetection": True,
            "evaluateBars": True,
            "thumbExport": True,
            "audio_analysis": True,
            "detect_clamped_levels": True
        },
        "clams_detection": {
            "run_tool": True,
            "bars": {
                "threshold": 0.7,
                "sample_ratio": 30,
                "stop_at_frame": 9000,
                "min_frame_count": 10,
                "stop_after_one": True
            },
            "tone": {
                "tolerance": 1.0,
                "min_tone_duration_ms": 2000,
                "stop_at_seconds": 3600
            }
        }
    },
    "outputs": {
        "access_file": False,
        "report": True,
        "qctools_ext": "qctools.xml.gz",
        "frame_analysis": {
            "enable_bitplane_check": True,
            "enable_border_detection": True,
            "enable_brng_analysis": True,
            "enable_signalstats": True
        }
    },
    "fixity": {
        "check_fixity": True,
        "validate_stream_fixity": True,
        "embed_stream_fixity": False,
        "output_fixity": False,
        "overwrite_stream_fixity": False
    }
}

profile_allOff = {
    "validate_filename": False,
    "tools": {
        "exiftool": {
            "check_tool": False,
            "run_tool": False
        },
        "ffprobe": {
            "check_tool": False,
            "run_tool": False
        },
        "mediaconch": {
            "mediaconch_policy": "JPC_FFV1-MKV_Preservation_Policy_20260709.xml",
            "run_mediaconch": False
        },
        "mediainfo": {
            "check_tool": False,
            "run_tool": False
        },
        "mediatrace": {
            "check_tool": False,
            "run_tool": False
        },
        "mkvalidator": {
            "check_tool": False,
            "run_tool": False
        },
        "qctools": {
            "run_tool": False
        },
        "qct_parse": {
            "run_tool": False,
            "barsDetection": False,
            "evaluateBars": False,
            "thumbExport": False,
            "audio_analysis": False,
            "detect_clamped_levels": False
        },
        "clams_detection": {
            "run_tool": False,
            "bars": {
                "threshold": 0.7,
                "sample_ratio": 30,
                "stop_at_frame": 9000,
                "min_frame_count": 10,
                "stop_after_one": True
            },
            "tone": {
                "tolerance": 1.0,
                "min_tone_duration_ms": 2000,
                "stop_at_seconds": 3600
            }
        }
    },
    "outputs": {
        "access_file": False,
        "report": False,
        "qctools_ext": "qctools.xml.gz",
        "frame_analysis": {
            "enable_border_detection": False,
            "enable_brng_analysis": False,
            "enable_signalstats": False
        }
    },
    "fixity": {
        "check_fixity": False,
        "validate_stream_fixity": False,
        "embed_stream_fixity": False,
        "output_fixity": False,
        "overwrite_stream_fixity": False
    }
}

profile_vendor = {
    "validate_filename": True,
    "video_file_extension": "mkv",
    "tools": {
        "exiftool": {
            "check_tool": False,
            "run_tool": True
        },
        "ffprobe": {
            "check_tool": False,
            "run_tool": True
        },
        "mediaconch": {
            "mediaconch_policy": "JPC_FFV1-MKV_Preservation_Policy_20260709.xml",
            "run_mediaconch": True
        },
        "mediainfo": {
            "check_tool": False,
            "run_tool": True
        },
        "mediatrace": {
            "check_tool": False,
            "run_tool": True
        },
        "mkvalidator": {
            "check_tool": False,
            "run_tool": False
        },
        "qctools": {
            "run_tool": False
        },
        "qct_parse": {
            "run_tool": False,
            "barsDetection": False,
            "evaluateBars": False,
            "thumbExport": False,
            "audio_analysis": False,
            "detect_clamped_levels": False
        },
        "clams_detection": {
            "run_tool": False,
            "bars": {
                "threshold": 0.7,
                "sample_ratio": 30,
                "stop_at_frame": 9000,
                "min_frame_count": 10,
                "stop_after_one": True
            },
            "tone": {
                "tolerance": 1.0,
                "min_tone_duration_ms": 2000,
                "stop_at_seconds": 3600
            }
        }
    },
    "outputs": {
        "access_file": False,
        "report": True,
        "qctools_ext": "qctools.xml.gz",
        "frame_analysis": {
            "enable_bitplane_check": False,
            "enable_border_detection": False,
            "enable_brng_analysis": False,
            "enable_signalstats": False,
            "enable_dropped_sample_detection": False,
            "enable_duplicate_frame_detection": False
        }
    },
    "fixity": {
        "check_fixity": False,
        "validate_stream_fixity": False,
        "embed_stream_fixity": True,
        "output_fixity": False,
        "overwrite_stream_fixity": False,
        "stream_hash_algorithm": "md5"
    }
}

# Signal flow profiles remain unchanged as they don't use boolean values
JPC_AV_SVHS = {
    "Source_VTR": ["SVO5800", "SN 122345", "composite", "analog balanced"], 
    "TBC_Framesync": ["DPS575 with flash firmware h2.16", "SN 15230", "SDI", "audio embedded"], 
    "ADC": ["DPS575 with flash firmware h2.16", "SN 15230", "SDI"], 
    "Capture_Device": ["Black Magic Ultra Jam", "SN B022159", "Thunderbolt"],
    "Computer": ["2023 Mac Mini", "Apple M2 Pro chip", "SN H9HDW53JMV", "OS 14.5", "vrecord v2023-08-07", "ffmpeg"]
}

BVH3100 = {
    "Source_VTR": ["Sony BVH3100", "SN 10525", "composite", "analog balanced"],
    "TBC_Framesync": ["Sony BVH3100", "SN 10525", "composite", "analog balanced"],
    "ADC": ["Leitch DPS575 with flash firmware h2.16", "SN 15230", "SDI", "embedded"],
    "Capture_Device": ["Blackmagic Design UltraStudio 4K Extreme", "SN B022159", "Thunderbolt"],
    "Computer": ["2023 Mac Mini", "Apple M2 Pro chip", "SN H9HDW53JMV", "OS 14.5", "vrecord v2023-08-07", "ffmpeg"]
}