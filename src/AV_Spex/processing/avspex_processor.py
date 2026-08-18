#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
# /opt/homebrew/bin before /usr/local/bin so Apple Silicon native binaries
# are preferred over any leftover x86_64 ones (which would run under Rosetta
# and stall on cross-arch sync primitives).
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
import time
from art import art, text2art
from dataclasses import asdict

from AV_Spex.processing.processing_mgmt import ProcessingManager
from AV_Spex.utils import dir_setup
from AV_Spex.utils.log_setup import logger, start_file_log, stop_file_log, format_elapsed
from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig
from AV_Spex.utils.config_manager import ConfigManager

def display_processing_banner(video_id=None):
    """
    Display ASCII art banners before and after processing.
    
    Args:
        video_id (str, optional): Video identifier for additional banner
    """
    tape_icon = art('cassette1')
    banner = f'\n{tape_icon}    {tape_icon}    {tape_icon}    {tape_icon}    {tape_icon}\n'
    print(banner)

    if video_id:
        ascii_video_id = text2art(video_id, font='small')
        logger.warning(f'Processing complete:\n{ascii_video_id}\n')
        # Get current time as a time structure
        local_time = time.localtime()
        # Format the time into a custom string (Year-Month-Day Hour:Minute:Second)
        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
        logger.info(f'Current local time: {formatted_time}\n')


def print_av_spex_logo():
    avspex_icon = text2art("A-V Spex", font='5lineoblique')
    print(f'{avspex_icon}\n')


def print_nmaahc_logo():
    nmaahc_icon = text2art("nmaahc",font='tarty1')
    print(f'{nmaahc_icon}\n')


def log_overall_time(overall_start_time, overall_end_time):
    logger.warning(f'All files processed!\n')
    overall_total_time = overall_end_time - overall_start_time
    # format_elapsed() rather than strftime/gmtime, which wraps a >24 hour run
    # back to 00:xx:xx (batches of full-length tapes routinely run past a day)
    formatted_overall_time = format_elapsed(overall_total_time)
    logger.info(f"Overall processing time for all directories: {formatted_overall_time}\n")

    return formatted_overall_time


class AVSpexProcessor:
    def __init__(self, signals=None):
        # signals are connected in setup_signal_connections() function in gui_main_window
        # passed to AVSpexProcessor from ProcessingWorker
        self.signals = signals
        self._cancelled = False
        self._cancel_emitted = False 

        self.config_mgr = ConfigManager()
        # logger.debug("==== PROCESSOR INITIALIZATION DEBUGGING ====")
        
        # Get the config before refresh
        pre_refresh_spex = self.config_mgr.get_config('spex', SpexConfig, use_last_used=True)
        logger.debug(f"Pre-refresh spex config has {len(pre_refresh_spex.filename_values.fn_sections)} sections")
        
        self.config_mgr.refresh_configs()
        
        # Get configs after refresh
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        self.spex_config = self.config_mgr.get_config('spex', SpexConfig)
        
        # Log details about the refreshed config
        logger.debug(f"Post-refresh spex config has {len(self.spex_config.filename_values.fn_sections)} sections")
        for idx, (key, section) in enumerate(sorted(self.spex_config.filename_values.fn_sections.items()), 1):
            logger.debug(f"  Section {idx}: {key} = {section.value} ({section.section_type})")

    def cancel(self):
        self._cancelled = True

    def check_cancelled(self):
        """Check if processing was cancelled and emit signal if needed"""
        if self._cancelled and self.signals and not self._cancel_emitted:
            self.signals.cancelled.emit()
            self._cancel_emitted = True
        return self._cancelled

    def process_directories(self, source_directories):
        if self.check_cancelled():
            return False

        overall_start_time = time.time()
        total_dirs = len(source_directories)

        for idx, source_directory in enumerate(source_directories, 1):
            if self.check_cancelled():
                return False

            if self.signals:
                self.signals.file_started.emit(source_directory, idx, total_dirs)
            
            source_directory = os.path.normpath(source_directory)
            self.process_single_directory(source_directory)

        overall_end_time = time.time()
        formatted_time =  log_overall_time(overall_start_time, overall_end_time)

        if self.signals:
            # Signal that all processing is complete
            self.signals.step_completed.emit("All Processing")
            
        return formatted_time

    def process_single_directory(self, source_directory):
        if self.check_cancelled():
            return False

        init_dir_result = dir_setup.initialize_directory(source_directory)
        if init_dir_result is None:
            if self.signals:
                self.signals.error.emit(f"Failed to initialize directory: {source_directory}")
            return False

        video_path, video_id, destination_directory, access_file_found = init_dir_result
        
        # START PER-FILE LOGGING
        # Capture all logs for this specific file to the qc_metadata directory
        per_file_log_path = start_file_log(destination_directory, video_id)
        logger.debug(f"Per-file log started: {per_file_log_path}")
        
        try:
            # Wrap all processing in try/finally to ensure we always stop the file log
            return self._process_directory_contents(
                source_directory, video_path, video_id, 
                destination_directory, access_file_found
            )
        finally:
            # STOP PER-FILE LOGGING
            # Always stop the per-file log, even if processing fails or is cancelled
            stop_file_log()

            # Tell the GUI this file is done and where its qc_metadata dir is,
            # so the console output can be saved alongside the per-file log.
            # Emitted even on failure/cancel, matching the per-file log, which
            # keeps whatever was written up to that point.
            if self.signals:
                self.signals.file_completed.emit(video_id, destination_directory)

    def _process_directory_contents(self, source_directory, video_path, video_id, 
                                     destination_directory, access_file_found):
        """
        Internal method that handles the actual processing logic.
        Separated from process_single_directory to allow proper try/finally 
        handling of per-file logging.
        """
        processing_mgmt = ProcessingManager(signals=self.signals, check_cancelled_fn=self.check_cancelled)

        if self.check_cancelled():
            return False

        # Check if fixity is enabled in config (now using booleans)
        fixity_config = self.checks_config.fixity
        
        # Check each relevant attribute directly with boolean logic
        fixity_enabled = (
            fixity_config.check_fixity or 
            fixity_config.validate_stream_fixity or 
            fixity_config.embed_stream_fixity or 
            fixity_config.output_fixity
        )
            
        if fixity_enabled:
            if self.signals:
                self.signals.tool_started.emit("Fixity...\n")
            processing_mgmt.process_fixity(source_directory, video_path, video_id)
            if self.signals:
                self.signals.tool_completed.emit("Fixity processing complete")
                

        if self.check_cancelled():
            return False

        # Check if mediaconch is enabled (now using boolean)
        if self.checks_config.tools.mediaconch.run_mediaconch:
            if self.signals:
                self.signals.tool_started.emit("MediaConch")
                
            mediaconch_results = processing_mgmt.validate_video_with_mediaconch(
                video_path, destination_directory, video_id
            )
            
            if self.signals:
                self.signals.tool_completed.emit("MediaConch validation complete")
                self.signals.step_completed.emit("MediaConch Validation")

        if self.check_cancelled():
            return False

        # Process metadata tools (mediainfo, ffprobe, exiftool, etc.)
        tools_config = self.checks_config.tools
        
        # Check if any metadata tools are enabled (now using booleans)
        tools_to_check = ['mediainfo', 'mediatrace', 'exiftool', 'ffprobe', 'mkvalidator']
        metadata_tools_enabled = False

        for tool_name in tools_to_check:
            tool = getattr(tools_config, tool_name, None)
            if tool and (tool.check_tool or tool.run_tool):
                metadata_tools_enabled = True
                break
                    
        # Initialize metadata_differences
        # Needed for process_video_outputs, if not created in process_video_metadata
        metadata_differences = None

        if metadata_tools_enabled:
            if self.signals:
                self.signals.tool_started.emit("Metadata Tools")
            
            metadata_differences = processing_mgmt.process_video_metadata(
                video_path, destination_directory, video_id
            )
            
            if self.signals:
                self.signals.tool_completed.emit("Metadata tools complete")
                # Emit signals for each completed metadata tool
                tools_to_signal = [
                    ('mediainfo', 'Mediainfo'),
                    ('mediatrace', 'Mediatrace'),
                    ('exiftool', 'Exiftool'),
                    ('ffprobe', 'FFprobe'),
                    ('mkvalidator', 'mkvalidator')
                ]
                
                for tool_name, display_name in tools_to_signal:
                    tool = getattr(tools_config, tool_name)
                    if tool.check_tool or tool.run_tool:
                        self.signals.step_completed.emit(display_name)

            if self.signals:
                self.signals.clear_status.emit()

        if self.check_cancelled():
            return False

        # Process output tools (QCTools, report generation, etc.)
        # Now using boolean checks
        # Also check if any frame analysis sub-steps are enabled
        frame_config = self.checks_config.outputs.frame_analysis
        outputs_enabled = (
            self.checks_config.outputs.access_file or
            self.checks_config.outputs.report or
            self.checks_config.tools.qctools.run_tool or
            self.checks_config.tools.qct_parse.run_tool or
            self.checks_config.tools.clams_detection.run_tool or
            frame_config.enable_bitplane_check or
            frame_config.enable_border_detection or
            frame_config.enable_brng_analysis or
            frame_config.enable_signalstats or
            frame_config.enable_dropped_sample_detection or
            getattr(frame_config, 'enable_duplicate_frame_detection', False)
        )
        
        if outputs_enabled:
            if self.signals:
                self.signals.tool_started.emit("Output Processing\n")
            
            processing_results = processing_mgmt.process_video_outputs(
                video_path, source_directory, destination_directory,
                video_id, metadata_differences
            )
            
            if self.signals:
                self.signals.tool_completed.emit("Outputs complete\n")

        if self.check_cancelled():
            return False
        
        if self.signals:
            self.signals.tool_completed.emit("All processing for this directory complete\n")
        if self.signals:
            self.signals.step_completed.emit("All Processing")
            time.sleep(0.1) # pause for a ms to let the list update before the QMessage box pops up
        
        logger.debug('Please note that any warnings on metadata are just used to help any issues with your file. If they are not relevant at this point in your workflow, just ignore this. Thanks!\n')

        if self.signals:
            self.signals.clear_status.emit()
        
        display_processing_banner(video_id)
        return True