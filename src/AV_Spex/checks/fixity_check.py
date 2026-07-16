import os
import sys
import hashlib
import shutil
import re
from datetime import datetime
from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.utils.fixity_summary import update_fixity_summary


def get_checksum_algorithm():
    """Get the configured checksum algorithm from config."""
    config_mgr = ConfigManager()
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    return getattr(checks_config.fixity, 'checksum_algorithm', 'md5').lower()


def get_video_file_extension():
    """Get the configured input video container extension (without leading dot)."""
    config_mgr = ConfigManager()
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    return getattr(checks_config, 'video_file_extension', 'mkv').lower().lstrip('.')


def check_fixity(directory, video_id, actual_checksum=None, check_cancelled=None, signals=None):
    """
    Validate fixity of a video file against stored checksums.
    
    Args:
        directory: Directory containing the video and checksum files
        video_id: Video identifier (filename without extension)
        actual_checksum: Pre-calculated checksum (optional, will calculate if not provided)
        check_cancelled: Callable to check if operation was cancelled
        signals: Signal object for progress updates
    """
    if check_cancelled and check_cancelled():
        return None
    
    algorithm = get_checksum_algorithm()
    
    fixity_result_file = os.path.join(
        directory, 
        f'{video_id}_qc_metadata', 
        f'{video_id}_{datetime.now().strftime("%Y_%m_%d_%H_%M")}_fixity_check.txt'
    )

    # Store paths to checksum files: (path, date, algorithm)
    checksum_files = []  

    # Define file suffixes to look for - support both md5 and sha256
    valid_suffixes = ['_checksums.md5', '_fixity.txt', '_fixity.md5', '_checksums.sha256', '_fixity.sha256']

    # Walk files of the source directory looking for checksum files
    for root, dirs, files in os.walk(directory):
        for file in files:
            # Check if file ends with any of the valid suffixes
            has_valid_suffix = any(file.endswith(suffix) for suffix in valid_suffixes)
            if not has_valid_suffix:
                continue
                
            checksum_file_path = os.path.join(root, file)
            try:
                # Remove the suffix first
                base_name = file
                for suffix in valid_suffixes:
                    if file.endswith(suffix):
                        base_name = file.replace(suffix, '')
                        break
                
                # Use regex to find date patterns at the end of the base name
                # Pattern 1: YYYY_MM_DD_HH_MM at the end of string
                date_match = re.search(r'(\d{4}_\d{2}_\d{2}_\d{2}_\d{2})$', base_name)
                if date_match:
                    date_str = date_match.group(1)
                    file_date = datetime.strptime(date_str, "%Y_%m_%d_%H_%M").date()
                else:
                    # Pattern 2: YYYY_MM_DD at the end of string
                    date_match = re.search(r'(\d{4}_\d{2}_\d{2})$', base_name)
                    if date_match:
                        date_str = date_match.group(1)
                        file_date = datetime.strptime(date_str, "%Y_%m_%d").date()
                    else:
                        raise ValueError(f"No date pattern found in filename: {file}")
                
                # Determine the algorithm used for this checksum file
                if any(file.endswith(s) for s in ['_checksums.sha256', '_fixity.sha256']):
                    file_algorithm = 'sha256'
                else:
                    file_algorithm = 'md5'
                
                checksum_files.append((checksum_file_path, file_date, file_algorithm))
            
            except (ValueError, IndexError) as e:
                logger.warning(f"Skipping checksum file with invalid date format: {file}. Error: {str(e)}")

    # Sort checksum files by date (descending - newest first)
    checksum_files.sort(key=lambda x: x[1], reverse=True)

    if not checksum_files:
        logger.error(f"Unable to validate fixity against previous checksum. No checksum file found.\n")

    video_file_path = os.path.join(directory, f'{video_id}.{get_video_file_extension()}')

    if check_cancelled and check_cancelled():
        return None
    
    # If video file exists, then:
    if os.path.exists(video_file_path):
        # If checksum has not yet been calculated, then:
        if not checksum_files and actual_checksum is None:
            output_fixity(directory, video_file_path, check_cancelled=check_cancelled, signals=signals)
            return
        elif checksum_files and actual_checksum is None:
            # Read all checksum files once and cache the results to avoid duplicate logging
            checksum_cache = {}
            for checksum_file_path, file_date, file_algorithm in checksum_files:
                stored_checksum, detected_algo = read_checksum_from_file(checksum_file_path)
                checksum_cache[checksum_file_path] = (stored_checksum, detected_algo)
            
            # Filter checksum files to find those matching the configured algorithm
            matching_checksum_files = []
            for checksum_file_path, file_date, file_algorithm in checksum_files:
                _, detected_algo = checksum_cache[checksum_file_path]
                if detected_algo == algorithm:
                    matching_checksum_files.append((checksum_file_path, file_date, detected_algo))
            
            # Determine which files to validate against
            if matching_checksum_files:
                # Use files matching the configured algorithm
                files_to_validate = matching_checksum_files
                validation_algorithm = algorithm
                logger.debug(f'Found {len(matching_checksum_files)} checksum file(s) matching configured algorithm: {algorithm.upper()}')
            else:
                # No files match configured algorithm - fall back to most recent file's algorithm
                most_recent_checksum_path = checksum_files[0][0]
                _, detected_algorithm = checksum_cache[most_recent_checksum_path]
                
                if detected_algorithm is None:
                    # Fall back to extension-based detection if content detection fails
                    detected_algorithm = checksum_files[0][2]
                
                logger.warning(
                    f'No checksum files found matching configured algorithm ({algorithm.upper()}). '
                    f'Falling back to {detected_algorithm.upper()} from existing checksum files.\n'
                )
                # Re-filter for the detected algorithm
                files_to_validate = []
                for checksum_file_path, file_date, file_algorithm in checksum_files:
                    _, detected_algo = checksum_cache[checksum_file_path]
                    if detected_algo == detected_algorithm:
                        files_to_validate.append((checksum_file_path, file_date, detected_algo))
                
                if not files_to_validate:
                    # If still no matches, use all files (shouldn't happen, but safety fallback)
                    files_to_validate = [(f[0], f[1], detected_algorithm) for f in checksum_files]
                
                validation_algorithm = detected_algorithm
            
            # Calculate the checksum using the determined algorithm
            actual_checksum = calculate_checksum(
                video_file_path, 
                algorithm=validation_algorithm,
                check_cancelled=check_cancelled, 
                signals=signals
            )
        elif checksum_files and actual_checksum is not None:
            # Read all checksum files once and cache the results
            checksum_cache = {}
            for checksum_file_path, file_date, file_algorithm in checksum_files:
                stored_checksum, detected_algo = read_checksum_from_file(checksum_file_path)
                checksum_cache[checksum_file_path] = (stored_checksum, detected_algo)
            
            # Checksum was pre-calculated - detect its algorithm from length
            if len(actual_checksum) == 64:
                provided_algorithm = 'sha256'
            elif len(actual_checksum) == 32:
                provided_algorithm = 'md5'
            else:
                logger.warning(f'Could not detect algorithm from provided checksum length ({len(actual_checksum)}), assuming configured algorithm: {algorithm}')
                provided_algorithm = algorithm
            
            # Filter checksum files to match the provided checksum's algorithm
            files_to_validate = []
            for checksum_file_path, file_date, file_algorithm in checksum_files:
                _, detected_algo = checksum_cache[checksum_file_path]
                if detected_algo == provided_algorithm:
                    files_to_validate.append((checksum_file_path, file_date, detected_algo))
            
            if not files_to_validate:
                logger.warning(f'No checksum files found matching provided checksum algorithm ({provided_algorithm.upper()})')
                # Use all files as fallback, but comparison will likely fail if algorithms differ
                files_to_validate = checksum_files

            validation_algorithm = provided_algorithm
        else:
            # No checksum files and actual_checksum is provided - nothing to validate against
            logger.warning(f'No existing checksum files to validate against')
            return
    else:
        logger.critical(f'Video file not found: {video_file_path}')
        return

    # Initialize variables
    checksums_match = True  
    most_recent_checksum = None
    most_recent_checksum_date = None

    # Only validate against files with matching algorithm
    for checksum_file_path, file_date, file_algorithm in files_to_validate:
        # Get the checksum from the cache (no need to read again)
        expected_checksum, _ = checksum_cache[checksum_file_path]

        # Update most recent checksum if this one is newer
        if most_recent_checksum_date is None or file_date > most_recent_checksum_date:
            most_recent_checksum = expected_checksum
            most_recent_checksum_date = file_date

        if actual_checksum != expected_checksum:
            checksums_match = False

    if checksums_match:
        logger.info(f'Fixity check passed for {video_file_path}\n')
        result_file = open(fixity_result_file, 'w', encoding='utf-8')
        print(f'Fixity check passed for {video_file_path}\n', file=result_file)
        result_file.close()
    else:
        logger.critical(f'Fixity check failed for {video_file_path}\n')
        logger.critical(f'Checksum read from {most_recent_checksum_date} file is: {expected_checksum}\nChecksum created now from MKV file = {actual_checksum}\n')
        result_file = open(fixity_result_file, 'w', encoding='utf-8')
        print(f'Fixity check failed for {os.path.basename(video_file_path)} checksum read from file = {expected_checksum} checksum created from MKV file = {actual_checksum}\n', file=result_file)
        result_file.close()

    update_fixity_summary(directory, video_id, 'whole_file', {
        'result': 'passed' if checksums_match else 'failed',
        'algorithm': validation_algorithm,
        'computed_checksum': actual_checksum,
        'validated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'validated_against': [
            {
                'file': os.path.basename(checksum_file_path),
                'date': str(file_date),
                'checksum': checksum_cache[checksum_file_path][0],
            }
            for checksum_file_path, file_date, file_algorithm in files_to_validate
        ],
    })


def output_fixity(source_directory, video_path, check_cancelled=None, signals=None):
    """
    Generate checksum files for a video using the configured algorithm.
    
    Args:
        source_directory: Directory to write checksum files
        video_path: Path to the video file
        check_cancelled: Callable to check if operation was cancelled
        signals: Signal object for progress updates
        
    Returns:
        str: The calculated checksum, or None if cancelled
    """
    algorithm = get_checksum_algorithm()
    
    # Parse video_id from video file path
    video_id = os.path.splitext(os.path.basename(video_path))[0]
    
    # Create fixity results files with appropriate extension
    timestamp = datetime.now().strftime("%Y_%m_%d_%H_%M")
    ext = 'sha256' if algorithm == 'sha256' else 'md5'
    
    fixity_result_file = os.path.join(source_directory, f'{video_id}_{timestamp}_fixity.txt')
    fixity_hash_file = os.path.join(source_directory, f'{video_id}_{timestamp}_fixity.{ext}')

    if check_cancelled and check_cancelled():
        return None
    
    # Calculate the checksum of the video file using configured algorithm
    checksum = calculate_checksum(
        video_path, 
        algorithm=algorithm, 
        check_cancelled=check_cancelled, 
        signals=signals
    )
    if checksum is None:  # Handle cancelled case
        return None
    
    if check_cancelled and check_cancelled():
        return None
    
    # Write checksum in standard format: 'checksum  filename'
    with open(fixity_result_file, 'w', encoding='utf-8') as result_file:
        print(f'{checksum}  {os.path.basename(video_path)}', file=result_file)

    shutil.copy(fixity_result_file, fixity_hash_file)
    logger.debug(f'{algorithm.upper()} checksum written to {fixity_result_file}\n')

    update_fixity_summary(source_directory, video_id, 'checksum_output', {
        'algorithm': algorithm,
        'checksum': checksum,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'checksum_file': os.path.basename(fixity_hash_file),
    })
    return checksum


def read_checksum_from_file(file_path):
    """
    Read a checksum from a file, supporting both MD5 (32 chars) and SHA256 (64 chars).
    
    Args:
        file_path: Path to the checksum file
        
    Returns:
        tuple: (checksum, algorithm) where algorithm is 'md5', 'sha256', or None if not found
    """
    # Read the file in binary mode first to handle encoding issues
    try:
        with open(file_path, 'rb') as file:
            content_bytes = file.read()
    except Exception as e:
        logger.critical(f'Error reading file {file_path}: {e}\n')
        return (None, None)
    
    # Try to decode with utf-8 first, with error reporting
    try:
        content = content_bytes.decode('utf-8')
    except UnicodeDecodeError as e:
        logger.warning(f'UTF-8 decoding error in {file_path}: {e}')
        # Try with latin-1 as a fallback, which can handle any byte
        try:
            content = content_bytes.decode('latin-1')
            logger.warning(f'Used latin-1 encoding as fallback for {file_path}\n')
        except Exception as e2:
            logger.error(f'Failed to decode {file_path} with fallback encoding: {e2}\n')
            return (None, None)

    # Try to find the checksum in the content
    # Support both MD5 (32 hex chars) and SHA256 (64 hex chars)
    checksum_parts = content.split()
    for part in checksum_parts:
        # Check for SHA256 (64 characters)
        if len(part) == 64 and all(c in '0123456789abcdefABCDEF' for c in part):
            logger.info(f'SHA256 checksum found in {os.path.basename(file_path)}: {part}\n')
            return (part, 'sha256')
        # Check for MD5 (32 characters)
        elif len(part) == 32 and all(c in '0123456789abcdefABCDEF' for c in part):
            logger.info(f'MD5 checksum found in {os.path.basename(file_path)}: {part}\n')
            return (part, 'md5')

    logger.critical(f'Checksum not found in {file_path}\n')
    return (None, None)


def calculate_checksum(filename, algorithm='md5', check_cancelled=None, signals=None):
    """
    Calculate a checksum using the specified algorithm.
    
    Args:
        filename: Path to the file to checksum
        algorithm: 'md5' or 'sha256' (default: 'md5')
        check_cancelled: Callable to check if operation was cancelled
        signals: Signal object for progress updates
        
    Returns:
        str: Hexadecimal checksum string, or None if cancelled
    """
    if check_cancelled and check_cancelled():
        return None
    
    # Select the hash algorithm
    algorithm = algorithm.lower()
    if algorithm == 'sha256':
        hash_object = hashlib.sha256()
        algo_name = 'SHA256'
    else:
        hash_object = hashlib.md5()
        algo_name = 'MD5'
    
    read_size = 0
    last_percent_done = 0
    total_size = os.path.getsize(filename)
    logger.debug(f'Generating {algo_name} checksum for {os.path.basename(filename)}:')
    
    with open(str(filename), 'rb') as file_object:
        while True:
            if check_cancelled and check_cancelled():
                logger.warning("Checksum calculation cancelled.")
                return None
            
            buf = file_object.read(2**20)  # Read 1MB at a time
            if not buf:
                break
            read_size += len(buf)
            hash_object.update(buf)
            
            # Calculate percentage (0-100)
            percent_done = int((read_size * 100) / total_size)
            percent_done = min(100, percent_done)  # Cap at 100
            
            if percent_done > last_percent_done:
                if signals:
                    safe_percent = min(100, max(0, int(percent_done)))
                    signals.md5_progress.emit(safe_percent)
                else:
                    sys.stdout.write('[%d%%]\r' % percent_done)
                    sys.stdout.flush()
                    
                last_percent_done = percent_done
                
    checksum_output = hash_object.hexdigest()
    logger.info(f'Calculated {algo_name} checksum is {checksum_output}\n')
    return checksum_output


# Attribution note:
# The calculate_checksum function above is derived from the hashlib_md5 function 
# in the open-source project IFIscripts
# More here: https://github.com/Irish-Film-Institute/IFIscripts/blob/master/scripts/copyit.py
# IFIscripts license information below:
# The MIT License (MIT)
# Copyright (c) 2015-2018 Kieran O'Leary for the Irish Film Institute.
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the 'Software'), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python fixity_check.py <directory>")
        sys.exit(1)
    file_path = sys.argv[1]
    if not os.path.isdir(file_path):
        print(f"Error: {file_path} is not a directory.")
        sys.exit(1)
    check_fixity(file_path)