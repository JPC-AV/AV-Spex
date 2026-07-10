import os
import sys
import shutil
import re
from dataclasses import asdict

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig
from AV_Spex.utils.config_manager import ConfigManager

config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)


def convert_wildcards_to_regex(pattern):
    r'''
    Converts a pattern with custom wildcards to a regex pattern.
    
    Custom wildcards:
    - @ : any letter (no numbers) => [a-zA-Z]
    - # : any number (no letters) => \d
    - * : any letter or number => [a-zA-Z0-9]
    '''
    if not isinstance(pattern, str):
        raise TypeError("Pattern must be a string")
        
    # First escape any special regex characters except our wildcards
    escaped_pattern = ''
    for char in pattern:
        if char in '@#*':
            escaped_pattern += char
        elif char in '.[]{}()\\+?^$':
            escaped_pattern += '\\' + char
        else:
            escaped_pattern += char
            
    # Now handle the wildcards
    pattern = escaped_pattern
    pattern = re.sub(r'#+', lambda m: rf'\d{{{len(m.group())}}}', pattern)
    pattern = pattern.replace('@', '[a-zA-Z]')
    pattern = pattern.replace('*', '[a-zA-Z0-9]')
    
    return pattern


def is_valid_filename(video_filename):
    '''
    Validates a filename against a configurable pattern with 1-8 sections.
    Provides detailed error messages about which part of the filename doesn't match the pattern.
    
    Parameters:
    - video_filename: The filename to validate
    
    Returns:
    - Tuple[bool, str]: (is_valid, error_message)
    '''
    # Add this debugging code at the beginning of the function
    logger.debug(f"Validating filename: {video_filename}\n")

    # Force refresh to ensure we have the latest config
    config_mgr.refresh_configs()
    
    # Get the latest checks config to check if filename validation is enabled
    current_checks = config_mgr.get_config('checks', ChecksConfig, use_last_used=True)
    
    # If filename validation is disabled, return True immediately
    if not current_checks.validate_filename:
        logger.debug(f"Filename validation is disabled, accepting filename: {video_filename}\n")
        return True
    
    # Get the LATEST spex_config (critical to use use_last_used=True)
    current_spex = config_mgr.get_config('spex', SpexConfig, use_last_used=True)
    
    # Use current_spex instead of the module-level spex_config
    # This ensures we're using the latest config loaded from disk
    
    # Log details about the current config
    # logger.debug(f"Filename validation using config with {len(current_spex.filename_values.fn_sections)} sections")
    # for idx, (key, section) in enumerate(sorted(current_spex.filename_values.fn_sections.items()), 1):
    #     logger.debug(f"  Section {idx}: {key} = {section.value} ({section.section_type})")

    base_filename = os.path.basename(video_filename)
    name_without_ext, file_ext = os.path.splitext(base_filename)
    file_ext = file_ext[1:]  # Remove the leading dot
    
    # Validate extension first - USE current_spex INSTEAD OF spex_config
    if file_ext.lower() != current_spex.filename_values.FileExtension.lower():
        logger.critical(f"Invalid file extension: Expected '{current_spex.filename_values.FileExtension}', got '{file_ext}'")
        return False
    
    # Extract section configurations - USE current_spex INSTEAD OF spex_config
    fn_sections = current_spex.filename_values.fn_sections
    
    # Validate number of sections
    if not fn_sections:
        logger.critical("No filename sections defined in configuration.")
        return False
    
    if len(fn_sections) < 1 or len(fn_sections) > 8:
        logger.critical(f"Invalid number of sections in configuration: {len(fn_sections)}. Must be between 1 and 8.")
        return False
    
    # Split the filename into sections
    filename_parts = name_without_ext.split('_')
    if len(filename_parts) != len(fn_sections):
        logger.critical(f"Invalid number of sections in filename: Expected {len(fn_sections)}, got {len(filename_parts)}")
        return False 
    
    # Validate each section
    for i, (part, section_key) in enumerate(zip(filename_parts, sorted(fn_sections.keys())), 1):
        section = fn_sections[section_key]
        
        if section.section_type == "literal":
            if part != section.value:
                logger.critical(f"Section {i} mismatch: Expected '{section.value}', got '{part}'")
                return False
                
        elif section.section_type == "wildcard":
            # Convert the wildcard pattern to regex for this section only
            section_pattern = convert_wildcards_to_regex(section.value)
            if not re.match(f"^{section_pattern}$", part, re.IGNORECASE):
                expected_format = section.value.replace('@', '[letter]').replace('#', '[digit]').replace('*', '[alphanumeric]')
                logger.critical(f"Section {i} format mismatch: Expected format '{expected_format}', got '{part}'")
                return False
                
        elif section.section_type == "regex":
            if not re.match(f"^{section.value}$", part, re.IGNORECASE):
                logger.critical(f"Section {i} doesn't match required pattern: '{section.value}'")
                return False
    
    logger.debug("Filename is valid\n")
    return True
