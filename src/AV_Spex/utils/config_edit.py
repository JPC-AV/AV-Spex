from dataclasses import asdict, dataclass
from typing import Callable, List, Dict, Union, Optional

import json
import os

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_setup import (
    ChecksConfig, SpexConfig, FilenameProfile, FilenameValues,
    FilenameSection, FilenameConfig, SignalflowConfig, SignalflowProfile,
    ChecksProfile, ChecksProfilesConfig,
    ExiftoolConfig, ExiftoolProfile, MediainfoConfig, MediainfoProfile,
    FfprobeConfig, FfprobeProfile, is_mkv_extension, is_protected_profile
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


# --- Active spex profiles ---------------------------------------------------
# The name of the profile last applied for each spex domain is recorded on
# SpexConfig.active_profiles, so the GUI can show the selection directly
# instead of reverse-matching current values against every saved profile.

PROFILE_DOMAINS = ('filename', 'mediainfo', 'exiftool', 'ffprobe', 'signalflow')

_SIGNALFLOW_STAGES = ("Source_VTR", "TBC_Framesync", "ADC", "Capture_Device", "Computer")


@dataclass
class ActiveProfileState:
    """Reported state of a domain's stored active profile.

    name: the stored profile name, or None if none recorded.
    exists: the named profile is still present in the domain's profile config.
    modified: current spex values differ from the stored profile's values.
    """
    name: Optional[str]
    exists: bool
    modified: bool


def set_active_profile(domain: str, profile_name: Optional[str]) -> None:
    """Record (or clear, with None) the profile last applied for a domain."""
    spex_config = config_mgr.get_config('spex', SpexConfig)
    updated = dict(spex_config.active_profiles)
    if profile_name is None:
        updated.pop(domain, None)
    else:
        updated[domain] = profile_name
    # replace_config_section is required here: update_config's deep merge
    # cannot add keys that are absent from the target dict.
    config_mgr.replace_config_section('spex', 'active_profiles', updated)


def get_active_profile(domain: str) -> Optional[str]:
    """Return the stored active profile name for a domain, or None."""
    spex_config = config_mgr.get_config('spex', SpexConfig)
    return (spex_config.active_profiles or {}).get(domain)


def get_domain_profiles(domain: str) -> Dict:
    """Return the {name: profile} dict for a spex profile domain."""
    spec = PROFILE_DOMAIN_REGISTRY.get(domain)
    if spec is None:
        raise ValueError(f"Unknown profile domain: {domain}")
    config = config_mgr.get_config(spec.config_key, spec.config_class)
    return dict(getattr(config, spec.profiles_attr, None) or {})


def _as_plain_dict(obj) -> dict:
    """Best-effort conversion of a dataclass / dict / object to a plain dict."""
    if obj is None:
        return {}
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    return {k: v for k, v in getattr(obj, '__dict__', {}).items() if not k.startswith('_')}


def _section_matches(current_section, profile_section, skip_keys=()) -> bool:
    """True if every profile field present in the current section matches it."""
    current_section = _as_plain_dict(current_section)
    profile_section = _as_plain_dict(profile_section)
    for key, profile_value in profile_section.items():
        if key in skip_keys:
            continue
        if key in current_section and current_section[key] != profile_value:
            return False
    return True


def _exiftool_matches(spex_config, profile) -> bool:
    return _section_matches(spex_config.exiftool_values, profile)


def _mediainfo_matches(spex_config, profile) -> bool:
    profile_dict = _as_plain_dict(profile)
    current = _as_plain_dict(spex_config.mediainfo_values)
    # Profile sections are named general/video/audio; spex uses expected_*.
    section_mapping = {
        'general': 'expected_general',
        'video': 'expected_video',
        'audio': 'expected_audio',
    }
    return all(
        _section_matches(current.get(spex_key), profile_dict.get(profile_key))
        for profile_key, spex_key in section_mapping.items()
    )


def _ffprobe_matches(spex_config, profile) -> bool:
    profile_dict = _as_plain_dict(profile)
    current = _as_plain_dict(spex_config.ffmpeg_values)
    # 'tags' is owned by the signal-flow system, so it is excluded here.
    return all(
        _section_matches(current.get(key), profile_dict.get(key), skip_keys=('tags',))
        for key in ('video_stream', 'audio_stream', 'format')
    )


def _filename_matches(spex_config, profile) -> bool:
    profile_dict = _as_plain_dict(profile)
    current_dict = _as_plain_dict(spex_config.filename_values)
    profile_sections = {k: _as_plain_dict(v) for k, v in (profile_dict.get('fn_sections') or {}).items()}
    current_sections = {k: _as_plain_dict(v) for k, v in (current_dict.get('fn_sections') or {}).items()}
    if profile_sections != current_sections:
        return False
    expected_ext = profile_dict.get('FileExtension')
    return not expected_ext or expected_ext == current_dict.get('FileExtension')


def _signalflow_matches(spex_config, profile) -> bool:
    profile_dict = _as_plain_dict(profile)
    encoder_settings = _as_plain_dict(spex_config.mediatrace_values.ENCODER_SETTINGS)
    for stage in _SIGNALFLOW_STAGES:
        profile_list = profile_dict.get(stage)
        if profile_list is None:
            continue
        if list(encoder_settings.get(stage) or []) != list(profile_list):
            return False
    return True


@dataclass(frozen=True)
class ProfileDomain:
    """Everything that varies between the spex profile domains.

    All five domains store named profiles in their own config file and are
    compared against a section of the spex config. Three of them — exiftool,
    mediainfo and ffprobe — are structurally identical beyond these fields, so
    they share the generic CRUD implementation further down instead of each
    carrying a private copy of the same five operations.

    Filename and signalflow are registered here for profile lookup and
    active-state reporting only. Their save/delete/apply paths are genuinely
    different shapes (signalflow has a lossy-tolerant loader and applies to
    ENCODER_SETTINGS; filename replaces fn_sections wholesale), so forcing them
    through the generic CRUD would mean modelling the exceptions rather than
    the rule. They leave spex_section and to_spex_values unset.
    """
    name: str
    label: str
    config_key: str
    config_class: type
    profiles_attr: str
    matches: Callable
    # Set only for the domains served by the generic CRUD functions below.
    spex_section: Optional[str] = None
    to_spex_values: Optional[Callable] = None

    @property
    def supports_crud(self) -> bool:
        """True if this domain is served by the generic CRUD functions."""
        return self.spex_section is not None and self.to_spex_values is not None


def _exiftool_spex_values(profile_dict: dict) -> dict:
    """ExifTool profiles are flat — the profile *is* the expected-values section."""
    return profile_dict


def _mediainfo_spex_values(profile_dict: dict) -> dict:
    """Profiles name sections general/video/audio; spex uses expected_* keys."""
    return {
        'expected_general': profile_dict.get('general', {}),
        'expected_video': profile_dict.get('video', {}),
        'expected_audio': profile_dict.get('audio', {}),
    }


def _ffprobe_spex_values(profile_dict: dict) -> dict:
    """FFprobe profile section names already match spex's ffmpeg_values keys."""
    return {
        key: profile_dict.get(key, {})
        for key in ('video_stream', 'audio_stream', 'format')
    }


PROFILE_DOMAIN_REGISTRY: Dict[str, ProfileDomain] = {
    'exiftool': ProfileDomain(
        name='exiftool',
        label='ExifTool',
        config_key='exiftool',
        config_class=ExiftoolConfig,
        profiles_attr='exiftool_profiles',
        matches=_exiftool_matches,
        spex_section='exiftool_values',
        to_spex_values=_exiftool_spex_values,
    ),
    'mediainfo': ProfileDomain(
        name='mediainfo',
        label='MediaInfo',
        config_key='mediainfo',
        config_class=MediainfoConfig,
        profiles_attr='mediainfo_profiles',
        matches=_mediainfo_matches,
        spex_section='mediainfo_values',
        to_spex_values=_mediainfo_spex_values,
    ),
    'ffprobe': ProfileDomain(
        name='ffprobe',
        label='FFprobe',
        config_key='ffprobe',
        config_class=FfprobeConfig,
        profiles_attr='ffprobe_profiles',
        matches=_ffprobe_matches,
        spex_section='ffmpeg_values',
        to_spex_values=_ffprobe_spex_values,
    ),
    'filename': ProfileDomain(
        name='filename',
        label='Filename',
        config_key='filename',
        config_class=FilenameConfig,
        profiles_attr='filename_profiles',
        matches=_filename_matches,
    ),
    'signalflow': ProfileDomain(
        name='signalflow',
        label='Signal flow',
        config_key='signalflow',
        config_class=SignalflowConfig,
        profiles_attr='signalflow_profiles',
        matches=_signalflow_matches,
    ),
}


def get_active_profile_state(domain: str) -> ActiveProfileState:
    """Report the stored active profile for a domain and whether the current
    spex values still match it.

    This compares only the single stored profile against current values —
    it is state reporting, not a matching heuristic.
    """
    name = get_active_profile(domain)
    if not name:
        return ActiveProfileState(name=None, exists=False, modified=False)
    profiles = get_domain_profiles(domain)
    if name not in profiles:
        return ActiveProfileState(name=name, exists=False, modified=False)
    spex_config = config_mgr.get_config('spex', SpexConfig)
    try:
        matches = PROFILE_DOMAIN_REGISTRY[domain].matches(spex_config, profiles[name])
    except Exception as e:
        logger.warning(f"Could not compare {domain} values against profile '{name}': {e}")
        matches = True
    return ActiveProfileState(name=name, exists=True, modified=not matches)


def apply_filename_profile(selected_profile: FilenameProfile, profile_name: Optional[str] = None):
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

    if profile_name:
        set_active_profile('filename', profile_name)


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

def apply_signalflow_profile(selected_profile, profile_name: Optional[str] = None):
    """
    Apply a SignalflowProfile dataclass or dict to the current configuration.

    Completely replaces the existing signalflow configuration with the selected profile,
    ensuring all sections are properly saved and persisted.

    Args:
        selected_profile (SignalflowProfile or dict): The signalflow profile to apply
        profile_name: When given, recorded as the domain's active profile
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

    if profile_name:
        set_active_profile('signalflow', profile_name)


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


# apply_exiftool_profile now lives with the other per-domain wrappers,
# below the generic CRUD implementation they all share.


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


# ---------------------------------------------------------------------------
# Generic profile CRUD
#
# One implementation of get / list / save / delete / apply, parameterised by
# the ProfileDomain descriptor. The per-domain public names below are thin
# wrappers so every existing CLI, GUI and import caller is unaffected; adding a
# fourth expected-value domain now means registering a descriptor rather than
# writing five more functions.
# ---------------------------------------------------------------------------


def _crud_domain(domain_name: str) -> ProfileDomain:
    """Return the descriptor for a domain served by the generic CRUD."""
    domain = PROFILE_DOMAIN_REGISTRY.get(domain_name)
    if domain is None or not domain.supports_crud:
        raise ValueError(f"No generic profile CRUD registered for domain: {domain_name}")
    return domain


def _profiles_of(domain: ProfileDomain, domain_config) -> dict:
    """The {name: profile} dict held by a domain's config, never None."""
    return getattr(domain_config, domain.profiles_attr, None) or {}


def _profile_as_dict(profile):
    """Profiles round-trip through JSON, so they are stored as plain dicts."""
    if hasattr(profile, '__dataclass_fields__'):
        return asdict(profile)
    return profile


def get_profile(domain_name: str, profile_name: str):
    """Get a named profile from a domain's config, or None if absent."""
    try:
        domain = _crud_domain(domain_name)
        domain_config = config_mgr.get_config(domain.config_key, domain.config_class)
        return _profiles_of(domain, domain_config).get(profile_name)
    except Exception as e:
        logger.warning(f"Could not retrieve {domain_name} profile '{profile_name}': {str(e)}")
    return None


def get_available_profiles(domain_name: str) -> List[str]:
    """List the profile names available for a domain."""
    try:
        domain = _crud_domain(domain_name)
        domain_config = config_mgr.get_config(domain.config_key, domain.config_class)
        return list(_profiles_of(domain, domain_config).keys())
    except Exception as e:
        logger.warning(f"Could not retrieve {domain_name} profiles: {str(e)}")
    return []


def save_profile(domain_name: str, profile_name: str, profile_data) -> bool:
    """Save a named profile into a domain's config.

    Built-in profiles are refused here rather than in the GUI, so the CLI and
    config-import paths are covered by the same guard.
    """
    try:
        domain = _crud_domain(domain_name)
    except ValueError as e:
        logger.error(str(e))
        return False

    logger.debug(f"=== SAVING {domain.label.upper()} PROFILE: {profile_name} ===")

    if is_protected_profile(domain.name, profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be overwritten")
        return False

    try:
        try:
            domain_config = config_mgr.get_config(domain.config_key, domain.config_class)
        except Exception:
            # No config file yet. Create one and place it in the cache, so the
            # replace_config_section below has something to operate on.
            domain_config = domain.config_class()
            config_mgr._configs[domain.config_key] = domain_config
            logger.debug(f"Creating new {domain.name} config")

        updated_profiles = {
            name: _profile_as_dict(existing)
            for name, existing in _profiles_of(domain, domain_config).items()
        }
        updated_profiles[profile_name] = _profile_as_dict(profile_data)

        # replace_config_section, not update_config: a deep merge would leave
        # deleted or renamed profiles behind in the saved dict.
        config_mgr.replace_config_section(
            domain.config_key, domain.profiles_attr, updated_profiles
        )
        logger.info(f"Successfully saved {domain.name} profile: {profile_name}")

        verification_config = config_mgr.get_config(domain.config_key, domain.config_class)
        if profile_name in _profiles_of(domain, verification_config):
            logger.debug(f"Verification: profile '{profile_name}' confirmed saved")
            return True

        logger.error(f"Verification failed: profile '{profile_name}' not found after save")
        return False

    except Exception as e:
        logger.error(f"Error saving {domain.name} profile '{profile_name}': {str(e)}")
        return False


def delete_profile(domain_name: str, profile_name: str) -> bool:
    """Delete a named profile from a domain's config."""
    try:
        domain = _crud_domain(domain_name)
    except ValueError as e:
        logger.error(str(e))
        return False

    if is_protected_profile(domain.name, profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be deleted")
        return False

    try:
        domain_config = config_mgr.get_config(domain.config_key, domain.config_class)
        profiles = _profiles_of(domain, domain_config)

        if profile_name not in profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False

        updated_profiles = {
            name: _profile_as_dict(profile)
            for name, profile in profiles.items()
            if name != profile_name
        }

        config_mgr.replace_config_section(
            domain.config_key, domain.profiles_attr, updated_profiles
        )
        logger.info(f"Deleted {domain.name} profile: {profile_name}")
        return True

    except Exception as e:
        logger.error(f"Error deleting {domain.name} profile '{profile_name}': {str(e)}")
        return False


def apply_profile_values(domain_name: str, profile_data, profile_name: Optional[str] = None) -> bool:
    """Apply a profile's values to its section of the spex config.

    The whole section is replaced rather than merged, so a sparser profile
    cannot inherit leftover fields from the profile applied before it.

    Args:
        domain_name: registered domain key ('exiftool', 'mediainfo', 'ffprobe')
        profile_data: the domain's profile dataclass, or an equivalent dict
        profile_name: when given, recorded as the domain's active profile
    """
    domain = _crud_domain(domain_name)
    logger.debug(f"==== APPLYING {domain.label.upper()} PROFILE ====")

    # Ensure the spex config is in the cache before replacing a section of it.
    config_mgr.get_config('spex', SpexConfig)

    profile_dict = _profile_as_dict(profile_data)
    spex_values = domain.to_spex_values(profile_dict)
    logger.debug(f"Applying {len(spex_values)} section(s) to spex.{domain.spex_section}")

    config_mgr.replace_config_section('spex', domain.spex_section, spex_values)

    # Force a refresh so the change is persisted and visible to later readers.
    config_mgr.refresh_configs()

    if profile_name:
        set_active_profile(domain.name, profile_name)

    return True


# ---------------------------------------------------------------------------
# Per-domain wrappers
#
# Kept as named functions rather than generated bindings so that call sites,
# grep and IDE navigation all still work, and so each domain has somewhere to
# document its own quirks.
# ---------------------------------------------------------------------------


def apply_exiftool_profile(profile_data, profile_name: Optional[str] = None) -> bool:
    """Apply an ExifTool profile to spex.exiftool_values."""
    return apply_profile_values('exiftool', profile_data, profile_name)


def get_exiftool_profile(profile_name: str):
    """Get an ExifTool profile by name, or None if not found."""
    return get_profile('exiftool', profile_name)


def get_available_exiftool_profiles() -> List[str]:
    """List available ExifTool profile names."""
    return get_available_profiles('exiftool')


def save_exiftool_profile(profile_name: str, profile_data) -> bool:
    """Save an ExifTool profile."""
    return save_profile('exiftool', profile_name, profile_data)


def delete_exiftool_profile(profile_name: str) -> bool:
    """Delete an ExifTool profile."""
    return delete_profile('exiftool', profile_name)


def apply_mediainfo_profile(profile_data, profile_name: Optional[str] = None) -> bool:
    """Apply a MediaInfo profile to spex.mediainfo_values.

    ConfigManager compatibility note: SpexConfig.mediainfo_values is typed as
    Dict[str, Union[...]], and ConfigManager._handle_dict does not auto-
    deserialize Union values — so the section is written as plain dicts.
    """
    return apply_profile_values('mediainfo', profile_data, profile_name)


def get_mediainfo_profile(profile_name: str):
    """Get a MediaInfo profile by name, or None if not found."""
    return get_profile('mediainfo', profile_name)


def get_available_mediainfo_profiles() -> List[str]:
    """List available MediaInfo profile names."""
    return get_available_profiles('mediainfo')


def save_mediainfo_profile(profile_name: str, profile_data) -> bool:
    """Save a MediaInfo profile."""
    return save_profile('mediainfo', profile_name, profile_data)


def delete_mediainfo_profile(profile_name: str) -> bool:
    """Delete a MediaInfo profile."""
    return delete_profile('mediainfo', profile_name)


def apply_ffprobe_profile(profile_data, profile_name: Optional[str] = None) -> bool:
    """Apply an FFprobe profile to spex.ffmpeg_values.

    ConfigManager compatibility note: SpexConfig.ffmpeg_values is typed as
    Dict[str, Union[...]], and ConfigManager._handle_dict does not auto-
    deserialize Union values — so the section is written as plain dicts.

    The 'tags' key inside the sections is owned by the signal-flow system, not
    by FFprobe profiles; see _ffprobe_matches, which skips it when comparing.
    """
    return apply_profile_values('ffprobe', profile_data, profile_name)


def get_ffprobe_profile(profile_name: str):
    """Get an FFprobe profile by name, or None if not found."""
    return get_profile('ffprobe', profile_name)


def get_available_ffprobe_profiles() -> List[str]:
    """List available FFprobe profile names."""
    return get_available_profiles('ffprobe')


def save_ffprobe_profile(profile_name: str, profile_data) -> bool:
    """Save an FFprobe profile."""
    return save_profile('ffprobe', profile_name, profile_data)


def delete_ffprobe_profile(profile_name: str) -> bool:
    """Delete an FFprobe profile."""
    return delete_profile('ffprobe', profile_name)


def save_filename_profile(profile_name: str, profile_data) -> bool:
    """
    Save a filename profile to the configuration.

    Args:
        profile_name (str): Name for the profile
        profile_data: FilenameProfile dataclass or dict with profile data

    Returns:
        bool: True if successful, False otherwise
    """
    logger.debug(f"=== SAVING FILENAME PROFILE ===")
    logger.debug(f"Profile name: {profile_name}")

    if is_protected_profile('filename', profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be overwritten")
        return False

    try:
        filename_config = config_mgr.get_config('filename', FilenameConfig)

        updated_profiles = {}
        for name, existing_profile in (filename_config.filename_profiles or {}).items():
            updated_profiles[name] = _as_plain_dict(existing_profile)
        updated_profiles[profile_name] = _as_plain_dict(profile_data)

        config_mgr.replace_config_section('filename', 'filename_profiles', updated_profiles)

        verification_config = config_mgr.get_config('filename', FilenameConfig)
        if profile_name in verification_config.filename_profiles:
            logger.info(f"Successfully saved filename profile: {profile_name}")
            return True
        logger.error(f"Verification failed: Profile '{profile_name}' not found after save")
        return False

    except Exception as e:
        logger.error(f"Error saving filename profile '{profile_name}': {str(e)}")
        return False


def delete_filename_profile(profile_name: str) -> bool:
    """
    Delete a filename profile from the configuration.

    Args:
        profile_name (str): Name of the profile to delete

    Returns:
        bool: True if successful, False otherwise
    """
    if is_protected_profile('filename', profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be deleted")
        return False

    try:
        filename_config = config_mgr.get_config('filename', FilenameConfig)

        if profile_name not in filename_config.filename_profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False

        updated_profiles = {
            name: _as_plain_dict(profile)
            for name, profile in filename_config.filename_profiles.items()
            if name != profile_name
        }
        config_mgr.replace_config_section('filename', 'filename_profiles', updated_profiles)

        logger.info(f"Deleted filename profile: {profile_name}")
        return True

    except Exception as e:
        logger.error(f"Error deleting filename profile '{profile_name}': {str(e)}")
        return False


def save_signalflow_profile(profile_name: str, profile_data) -> bool:
    """
    Save a signal flow profile to the configuration.

    Args:
        profile_name (str): Name for the profile
        profile_data: SignalflowProfile dataclass or dict with profile data

    Returns:
        bool: True if successful, False otherwise
    """
    logger.debug(f"=== SAVING SIGNALFLOW PROFILE ===")
    logger.debug(f"Profile name: {profile_name}")

    if is_protected_profile('signalflow', profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be overwritten")
        return False

    try:
        signalflow_config = config_mgr.get_config('signalflow', SignalflowConfig)

        updated_profiles = {}
        for name, existing_profile in (signalflow_config.signalflow_profiles or {}).items():
            updated_profiles[name] = _as_plain_dict(existing_profile)

        new_profile = _as_plain_dict(profile_data)
        new_profile.setdefault('name', profile_name)
        updated_profiles[profile_name] = new_profile

        config_mgr.replace_config_section('signalflow', 'signalflow_profiles', updated_profiles)

        verification_config = config_mgr.get_config('signalflow', SignalflowConfig)
        if profile_name in verification_config.signalflow_profiles:
            logger.info(f"Successfully saved signalflow profile: {profile_name}")
            return True
        logger.error(f"Verification failed: Profile '{profile_name}' not found after save")
        return False

    except Exception as e:
        logger.error(f"Error saving signalflow profile '{profile_name}': {str(e)}")
        return False


def delete_signalflow_profile(profile_name: str) -> bool:
    """
    Delete a signal flow profile from the configuration.

    Args:
        profile_name (str): Name of the profile to delete

    Returns:
        bool: True if successful, False otherwise
    """
    if is_protected_profile('signalflow', profile_name):
        logger.warning(f"'{profile_name}' is a built-in profile and cannot be deleted")
        return False

    try:
        signalflow_config = config_mgr.get_config('signalflow', SignalflowConfig)

        if profile_name not in signalflow_config.signalflow_profiles:
            logger.warning(f"Profile '{profile_name}' not found, cannot delete")
            return False

        updated_profiles = {
            name: _as_plain_dict(profile)
            for name, profile in signalflow_config.signalflow_profiles.items()
            if name != profile_name
        }
        config_mgr.replace_config_section('signalflow', 'signalflow_profiles', updated_profiles)

        logger.info(f"Deleted signalflow profile: {profile_name}")
        return True

    except Exception as e:
        logger.error(f"Error deleting signalflow profile '{profile_name}': {str(e)}")
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
            "check_tool": True,
            "run_tool": True
        },
        "qctools": {
            "run_tool": False
        },
        "qct_parse": {
            "run_tool": False,
            "barsDetection": False,
            "evaluateBars": False,
            "thumbExport": False,
            "evaluateBarsReference": "detected",
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
        "save_console_pdf": False,
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
            "evaluateBarsReference": "detected",
            "audio_analysis": True,
            "detect_clamped_levels": True,
            "detect_chroma_phase_errors": True
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
        "save_console_pdf": False,
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
            "evaluateBarsReference": "detected",
            "audio_analysis": False,
            "detect_clamped_levels": False,
            "detect_chroma_phase_errors": False,
            "detect_tone_leak": False
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
        "save_console_pdf": False,
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
            "evaluateBarsReference": "detected",
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
        "save_console_pdf": True,
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