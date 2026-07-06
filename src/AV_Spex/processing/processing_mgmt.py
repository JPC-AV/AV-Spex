import os
import shutil
import subprocess
import time
import re
import cv2
from pathlib import Path

from AV_Spex.processing import run_tools
from AV_Spex.utils import dir_setup
from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig, VALID_QCTOOLS_EXTENSIONS, is_mkv_extension
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.generate_report import generate_final_report
from AV_Spex.checks.fixity_check import check_fixity, output_fixity
from AV_Spex.checks.mediainfo_check import parse_mediainfo
from AV_Spex.checks.mediatrace_check import parse_mediatrace, create_metadata_difference_report
from AV_Spex.checks.exiftool_check import parse_exiftool
from AV_Spex.checks.ffprobe_check import parse_ffprobe
from AV_Spex.checks.mkvalidator_check import parse_mkvalidator
from AV_Spex.checks.embed_fixity import validate_embedded_md5, process_embedded_fixity
from AV_Spex.checks.make_access import process_access_file, determine_excluded_audio_channels
from AV_Spex.checks.qct_parse import run_qctparse
from AV_Spex.checks import bars_detection_clams, tone_detection_clams
from AV_Spex.checks.bars_detection_clams import run_clams_bars_detection
from AV_Spex.checks.tone_detection_clams import run_clams_tone_detection
from AV_Spex.checks.mediaconch_check import find_mediaconch_policy, run_mediaconch_command, parse_mediaconch_output
from AV_Spex.checks.frame_analysis import analyze_frame_quality


class ProcessingManager:
    def __init__(self, signals=None, check_cancelled_fn=None):
        self.signals = signals
        self.check_cancelled = check_cancelled_fn or (lambda: False)
        # Force a reload of the config from disk
         # Store config manager as an instance attribute
        self.config_mgr = ConfigManager()
        self.config_mgr.refresh_configs()
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        self.spex_config = self.config_mgr.get_config('spex', SpexConfig)

    def process_fixity(self, source_directory, video_path, video_id):
        """
        Orchestrates the entire fixity process, including embedded and file-level operations.

        Args:
            source_directory (str): Directory containing source files
            video_path (str): Path to the video file
            video_id (str): Unique identifier for the video
        """
        
        if self.check_cancelled():
            return None

        # Embedded stream fixity (embed + validate) relies on mkvextract/
        # mkvpropedit, which only work on Matroska. If a non-MKV extension is
        # configured but a profile or stale config left these enabled, skip them
        # gracefully rather than letting mkvextract output fail XML parsing.
        embed_stream_fixity = self.checks_config.fixity.embed_stream_fixity
        validate_stream_fixity = self.checks_config.fixity.validate_stream_fixity
        if (embed_stream_fixity or validate_stream_fixity) and not is_mkv_extension(
            getattr(self.checks_config, 'video_file_extension', 'mkv')
        ):
            logger.warning(
                f"Input extension '{self.checks_config.video_file_extension}' is not MKV; "
                "embedded stream fixity only works on Matroska. Skipping embed/validate "
                "stream fixity for this file.\n"
            )
            embed_stream_fixity = False
            validate_stream_fixity = False

        # Embed stream fixity if required
        if embed_stream_fixity:
            if self.signals:
                self.signals.fixity_progress.emit("Embedding fixity...")
            if self.check_cancelled():
                return False
            process_embedded_fixity(video_path, check_cancelled=self.check_cancelled, signals=self.signals)
            if self.check_cancelled():
                return False
            # Mark checkbox
            if self.signals:
                self.signals.step_completed.emit("Embed Stream Fixity")

        # Validate stream hashes if required
        if validate_stream_fixity:
            if self.signals:
                self.signals.fixity_progress.emit("Validating embedded fixity...")
            if embed_stream_fixity:
                logger.critical("Embed stream fixity is turned on, which overrides validate_fixity. Skipping validate_fixity.\n")
            else:
                validation_result = validate_embedded_md5(video_path, check_cancelled=self.check_cancelled, signals=self.signals)
                # Only mark complete if validation succeeded (True)
                # Don't mark complete if failed (False) or cancelled (None)
                if validation_result is True:
                    if self.signals:
                        self.signals.step_completed.emit("Validate Stream Fixity")
                elif validation_result is False:
                    if self.signals:
                        self.signals.step_failed.emit("Validate Stream Fixity")

        # Initialize file_checksum variable
        file_checksum = None

        # Create checksum for video file and output results
        if self.checks_config.fixity.output_fixity:
            if self.signals:
                self.signals.fixity_progress.emit("Outputting fixity...")
            file_checksum = output_fixity(source_directory, video_path, check_cancelled=self.check_cancelled, signals=self.signals)
            if self.signals:
                self.signals.step_completed.emit("Output Fixity")

        # Verify stored checksum and write results  
        if self.checks_config.fixity.check_fixity:
            if self.signals:
                self.signals.fixity_progress.emit("Validating fixity...")
            check_fixity(source_directory, video_id, actual_checksum=file_checksum, check_cancelled=self.check_cancelled, signals=self.signals)
            if self.signals:
                self.signals.step_completed.emit("Validate Fixity")

        if self.check_cancelled():
            return None


    def validate_video_with_mediaconch(self, video_path, destination_directory, video_id):
        """
        Coordinate the entire MediaConch validation process.
        
        Args:
            video_path (str): Path to the input video file
            destination_directory (str): Directory to store output files
            video_id (str): Unique identifier for the video
            config_path (object): Configuration path object
            
        Returns:
            dict: Validation results from MediaConch policy check
        """
        # Check if MediaConch should be run
        if not self.checks_config.tools.mediaconch.run_mediaconch:
            logger.info(f"MediaConch validation skipped\n")
            return {}
        
        if self.signals:
            self.signals.mediaconch_progress.emit("Locating MediaConch policy...")
        if self.check_cancelled():
            return None
        
        # Find the policy file
        policy_name = self.checks_config.tools.mediaconch.mediaconch_policy
        policy_path = self.config_mgr.get_policy_path(policy_name)
        if not policy_path:
            return {}

        # Prepare output path
        mediaconch_output_path = os.path.join(destination_directory, f'{video_id}_mediaconch_output.csv')

        if self.signals:
            self.signals.mediaconch_progress.emit("Running MediaConch...")
        if self.check_cancelled():
            return None

        # Run MediaConch command
        if not run_mediaconch_command(
            'mediaconch', 
            video_path, 
            '-oc', 
            mediaconch_output_path, 
            policy_path
        ):
            return {}
        
        if self.check_cancelled():
            return None

        # Parse and validate MediaConch output
        validation_results = parse_mediaconch_output(mediaconch_output_path)

        return validation_results
    

    def process_video_metadata(self, video_path, destination_directory, video_id):
        """
        Main function to process video metadata using multiple tools.
        
        Args:
            video_path (str): Path to the input video file
            destination_directory (str): Directory to store output files
            video_id (str): Unique identifier for the video
            
        Returns:
            dict: Dictionary of metadata differences from various tools
        """
        if self.check_cancelled():
            return None
        
        tools = ['exiftool', 'mediainfo', 'mediatrace', 'ffprobe', 'mkvalidator']
        
        # Store differences for each tool
        metadata_differences = {}

        if self.signals:
            self.signals.metadata_progress.emit("Running metadata tools...")
        
        # Process each tool
        for tool in tools:
            if self.check_cancelled():
                return None
                
            # Run tool and get output path (signals lets mkvalidator emit progress)
            output_path = run_tools.run_tool_command(tool, video_path, destination_directory, video_id, signals=self.signals)
            
            # Check metadata and store differences
            differences = check_tool_metadata(tool, output_path)
            if differences:
                metadata_differences[tool] = differences
                
            if self.check_cancelled():
                return None
        
        return metadata_differences
    

    def process_video_outputs(self, video_path, source_directory, destination_directory, video_id, metadata_differences):
        """
        Coordinate the entire output processing workflow.
        """
        
        processing_results = {
            'metadata_diff_report': None,
            'qctools_output': None,
            'access_file': None,
            'html_report': None,
            'frame_analysis': None  # Add this
        }
        
        if self.check_cancelled():
            return None
        
        # Create report directory if report is enabled
        report_directory = None
        if self.checks_config.outputs.report:
            report_directory = dir_setup.make_report_dir(source_directory, video_id)
            # Process metadata differences report
            processing_results['metadata_diff_report'] = create_metadata_difference_report(
                metadata_differences, report_directory, video_id
            )
        else:
            processing_results['metadata_diff_report'] = None
        
        if self.signals:
            self.signals.output_progress.emit("Running QCTools and qct-parse...")
        if self.check_cancelled():
            return None
        
        # Process QCTools output and capture color bars detection
        qctools_results = process_qctools_output(
            video_path, source_directory, destination_directory, video_id,
            report_directory=report_directory,
            check_cancelled=self.check_cancelled, signals=self.signals
        )
        color_bars_end_time = qctools_results.get('color_bars_end_time') if qctools_results else None
        audio_findings = qctools_results.get('audio_findings') if qctools_results else None
        clams_bars_start_time = qctools_results.get('clams_bars_start_time') if qctools_results else None
        clams_bars_end_time = qctools_results.get('clams_bars_end_time') if qctools_results else None
        processing_results['clams_bars_start_time'] = clams_bars_start_time
        processing_results['clams_bars_end_time'] = clams_bars_end_time
        processing_results['clams_tones'] = qctools_results.get('clams_tones', []) if qctools_results else []

        # Check if frame analysis is enabled
        frame_config = self.checks_config.outputs.frame_analysis
        if any([
            frame_config.enable_bitplane_check,
            frame_config.enable_border_detection,
            frame_config.enable_brng_analysis,
            frame_config.enable_signalstats,
            frame_config.enable_dropped_sample_detection,
            getattr(frame_config, 'enable_duplicate_frame_detection', False)
        ]):
            if self.signals:
                self.signals.output_progress.emit("Performing enhanced frame analysis...")
                # Reset the detail progress bar before frame analysis begins
                self.signals.frame_analysis_progress.emit(0)

            # Run the new unified frame analysis, passing color bars info from qct-parse
            frame_analysis_results = self.process_frame_analysis(
                video_path, source_directory, destination_directory, video_id,
                color_bars_end_time=color_bars_end_time
            )

            processing_results['frame_analysis'] = frame_analysis_results

        if self.signals:
            self.signals.frame_analysis_progress.emit(0)
            self.signals.output_progress.emit("Creating access file...")
        if self.check_cancelled():
            return None

        # Pull a crop region from sophisticated border detection (BRNG-refined when
        # available). Skip simple detection — the user has opted not to crop in
        # that case because the bordered area is a fixed fallback, not a real
        # measurement. detection_method is 'sophisticated_refined' after BRNG
        # refinement, plain 'sophisticated' otherwise.
        access_crop_area = None
        frame_results = processing_results.get('frame_analysis')
        if frame_results:
            border = frame_results.get('border_results')
            if border:
                method = border.get('detection_method') or ''
                if method.startswith('sophisticated') and border.get('active_area'):
                    access_crop_area = border['active_area']

        # Generate access file (runs after qct-parse and frame analysis so it can
        # take advantage of details learned during those steps). When qct-parse
        # detected color bars, color_bars_end_time trims them off the access copy.
        # When sophisticated border detection produced an active area, crop_area
        # removes the borders. Both behaviors are gated by user-controlled
        # config flags so they can be disabled independently of the detection.
        outputs_cfg = self.checks_config.outputs
        trim_start = color_bars_end_time if outputs_cfg.access_file_trim_color_bars else None
        crop_for_access = access_crop_area if outputs_cfg.access_file_crop_borders else None

        # When enabled, a stereo channel flagged by qct-parse audio analysis
        # (silent, or carrying audible timecode) is excluded from the access
        # copy and the good channel output as dual-mono.
        excluded_audio = None
        if getattr(outputs_cfg, 'access_file_exclude_flagged_audio', False) and outputs_cfg.access_file:
            if audio_findings:
                excluded_audio, exclusion_reasons = determine_excluded_audio_channels(audio_findings)
                if excluded_audio:
                    logger.info(
                        f"Excluding flagged audio channel(s) from access copy: "
                        f"{'; '.join(exclusion_reasons)}\n"
                    )
                elif exclusion_reasons:
                    logger.warning(
                        f"All audio channels were flagged ({'; '.join(exclusion_reasons)}); "
                        f"keeping all audio in the access copy\n"
                    )
            else:
                logger.warning(
                    f"Access file option 'exclude flagged audio channel' is on, but "
                    f"qct-parse audio analysis did not run or produced no results; "
                    f"including all audio in the access copy\n"
                )

        processing_results['access_file'] = process_access_file(
            video_path, source_directory, video_id,
            check_cancelled=self.check_cancelled,
            signals=self.signals,
            color_bars_end_time=trim_start,
            crop_area=crop_for_access,
            crop_to_480=outputs_cfg.access_file_crop_to_480,
            excluded_audio_channels=excluded_audio
        )

        if self.signals:
            self.signals.output_progress.emit("Preparing report...")
        if self.check_cancelled():
            return None

        # Generate final HTML report
        processing_results['html_report'] = generate_final_report(
            video_id, source_directory, report_directory, destination_directory,
            video_path=video_path,
            check_cancelled=self.check_cancelled, 
            signals=self.signals
        )
        
        return processing_results
        
    def process_frame_analysis(self, video_path, source_directory, destination_directory, video_id, color_bars_end_time=None):
        """
        Process comprehensive frame analysis including border detection,
        BRNG violations, and optionally signalstats using the enhanced unified module.
        
        Args:
            video_path: Path to video file
            source_directory: Source directory containing video
            destination_directory: Output directory for results
            video_id: Video identifier
            color_bars_end_time: End time of color bars (from qct-parse), None if not detected
        """
        
        if self.check_cancelled():
            return None
        
        # Get frame analysis config
        frame_config = self.checks_config.outputs.frame_analysis
        
        # Check if any frame analysis sub-steps are enabled
        if not any([
            frame_config.enable_bitplane_check,
            frame_config.enable_border_detection,
            frame_config.enable_brng_analysis,
            frame_config.enable_signalstats,
            frame_config.enable_dropped_sample_detection,
            getattr(frame_config, 'enable_duplicate_frame_detection', False)
        ]):
            logger.info("No frame analysis sub-steps enabled")
            return None
        
        if self.signals:
            self.signals.output_progress.emit("Performing enhanced frame analysis...")
        
        # Run the enhanced frame analysis - color_bars_end_time passed from qct-parse
        try:
            analysis_results = analyze_frame_quality(
                video_path=video_path,
                output_dir=destination_directory,
                frame_config=frame_config,
                color_bars_end_time=color_bars_end_time,
                signals=self.signals,  # Pass signals to emit step completion as each step finishes
                check_cancelled=self.check_cancelled  # Pass cancel check for responsive cancellation
            )
        except Exception as e:
            logger.error(f"Error during frame analysis: {e}")
            return None
        
        if self.check_cancelled():
            return None
        
        # Process results
        if analysis_results:
            # Step completion signals are now emitted inside analyze_frame_quality
            # as each step completes, so we don't need to emit them here
            
            # Log summary
            if 'summary' in analysis_results:
                logger.debug(analysis_results['summary'] + "\n")
            
            # Format results for compatibility with existing code
            formatted_results = self._format_frame_analysis_results(analysis_results)
            
            return formatted_results
        
        return None
    
    def _format_frame_analysis_results(self, analysis_results):
        """
        Format the enhanced frame analysis results to match the expected structure
        for the rest of the processing pipeline.
        """
        formatted = {
            'border_results': None,
            'brng_results': None,
            'signalstats_results': None,
            'dropped_sample_results': None,
            'analysis_decisions': []
        }
        
        # Extract border results
        if 'final_borders' in analysis_results:
            borders = analysis_results['final_borders']
        elif 'initial_borders' in analysis_results:
            borders = analysis_results['initial_borders']
        else:
            borders = None
        
        if borders:
            formatted['border_results'] = {
                'active_area': borders.get('active_area'),
                'border_regions': borders.get('border_regions'),
                'detection_method': borders.get('detection_method'),
                'head_switching_artifacts': borders.get('head_switching_artifacts'),
                'quality_frame_hints': borders.get('quality_frame_hints', [])
            }
        
        # Extract BRNG results
        if 'final_brng_analysis' in analysis_results:
            brng = analysis_results['final_brng_analysis']
        elif 'brng_analysis' in analysis_results:
            brng = analysis_results['brng_analysis']
        else:
            brng = None
        
        if brng:
            formatted['brng_results'] = {
                'aggregate_patterns': brng.get('aggregate_patterns', {}),
                'actionable_report': brng.get('actionable_report', {}),
                'worst_frames': [v for v in brng.get('violations', [])[:5]],  # Top 5 violations
                'video_info': {
                    'content_start_time': analysis_results.get('color_bars_end_time', 0)
                }
            }
        
        # Extract signalstats results
        if 'signalstats' in analysis_results:
            stats = analysis_results['signalstats']
            formatted['signalstats_results'] = {
                'violation_percentage': stats.get('violation_percentage', 0),
                'max_brng': stats.get('max_brng', 0),
                'avg_brng': stats.get('avg_brng', 0),
                'diagnosis': stats.get('diagnosis', ''),
                'used_qctools': stats.get('used_qctools', False)
            }
        
        # Extract dropped sample detection results
        if 'dropped_sample_detection' in analysis_results:
            ds = analysis_results['dropped_sample_detection']
            formatted['dropped_sample_results'] = {
                'status': ds.get('status'),
                'message': ds.get('message', ''),
                'spike_count': ds.get('spike_count', 0),
                'duration_diff_ms': ds.get('duration_diff_ms', 0.0),
                'audio_duration': ds.get('audio_duration', 0.0),
                'video_duration': ds.get('video_duration', 0.0),
                'combined_score': ds.get('combined_score', 0.0),
                'estimated_loss_ms': ds.get('estimated_loss_ms', 0.0),
                'sample_rate': ds.get('sample_rate', 0),
                'spectrogram_path': ds.get('spectrogram_path'),
                'spike_timestamps': ds.get('spike_timestamps', [])
            }

        # Add any analysis decisions
        if 'refinement_iterations' in analysis_results:
            formatted['analysis_decisions'].append(
                f"Performed {analysis_results['refinement_iterations']} border refinement iterations"
            )
        
        return formatted

    

def find_qctools_report(source_directory, video_id):
    """
    Search for existing qctools files in both _qc_metadata and _vrecord_metadata folders.
    
    Args:
        source_directory (str): Path to the source directory containing metadata folders
        video_id (str): Video identifier (e.g., "JPC_AV_01581")
        
    Returns:
        str or None: Path to the qctools report if found, None otherwise
    """
    source_path = Path(source_directory)
    
    # Define the folders to search in
    search_folders = [
        source_path / f"{video_id}_qc_metadata",
        source_path / f"{video_id}_vrecord_metadata"
    ]
    
    # Search patterns for qctools files
    qctools_patterns = [
        "*.qctools.xml.gz",
        "*.qctools.mkv"
    ]
    
    # Search in each folder
    for folder in search_folders:
        if folder.exists() and folder.is_dir():
            for pattern in qctools_patterns:
                matches = list(folder.glob(pattern))
                if matches:
                    return str(matches[0])  # Return the first match
    
    return None


def process_qctools_output(video_path, source_directory, destination_directory, video_id, report_directory=None, check_cancelled=None, signals=None):
    """
    Process QCTools output, including running QCTools and optional parsing.
    Now searches for existing QCTools reports in both _qc_metadata and _vrecord_metadata folders.
    
    Args:
        video_path (str): Path to the input video file
        source_directory (str): Source directory for the video
        destination_directory (str): Directory to store output files
        video_id (str): Unique identifier for the video
        report_directory (str, optional): Directory to save reports
        check_cancelled (callable, optional): Function to check if operation was cancelled
        signals (object, optional): Signal object for progress updates
        
    Returns:
        dict: Processing results and paths
    """
    config_mgr = ConfigManager()
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    
    results = {
        'qctools_output_path': None,
        'qctools_check_output': None,
        'color_bars_end_time': None,
        'clams_bars_start_time': None,
        'clams_bars_end_time': None,
        'clams_tones': [],
        'audio_findings': None,
    }

    if check_cancelled and check_cancelled():
        return None

    # Get configuration settings
    qct_run_tool = getattr(checks_config.tools.qctools, 'run_tool')
    qct_parse_run_tool = getattr(checks_config.tools.qct_parse, 'run_tool')
    
    # Always search for existing QCTools reports first
    existing_qctools_path = find_qctools_report(source_directory, video_id)
    
    if existing_qctools_path:
        logger.info(f"Found existing QCTools report: {existing_qctools_path}\n")
        results['qctools_output_path'] = existing_qctools_path
    
    # Handle QCTools generation (only if configured and no existing report)
    if qct_run_tool:
        if existing_qctools_path:
            # Mark step as completed since we found an existing report
            if signals:
                signals.step_completed.emit("QCTools")
        else:
            # No existing report found, create a new one in the destination directory
            qctools_ext = checks_config.outputs.qctools_ext
            if qctools_ext not in VALID_QCTOOLS_EXTENSIONS:
                logger.warning(f"Invalid QCTools extension '{qctools_ext}', defaulting to 'qctools.xml.gz'")
                qctools_ext = "qctools.xml.gz"
            qctools_output_path = os.path.join(destination_directory, f'{video_id}.{qctools_ext}')
            
            # Check if we already created one in the destination directory
            if os.path.exists(qctools_output_path):
                logger.warning("QCTools report already exists in destination directory, not overwriting...")
                results['qctools_output_path'] = qctools_output_path
            else:
                # Create new QCTools report
                logger.info(f"No existing QCTools report found. Creating new report: {qctools_output_path}\n")
                run_qctools_command('qcli -i', video_path, '-o', qctools_output_path, check_cancelled=check_cancelled, signals=signals)
                logger.debug('')  # Add new line for cleaner terminal output
                results['qctools_output_path'] = qctools_output_path
            
            if signals:
                signals.step_completed.emit("QCTools")

    # ---- CLAMS detection (runs BEFORE qct-parse so its results can guide
    # additional qct-parse bars scans). Tone detection runs first — it's fast
    # over the whole file and good at finding bars hidden mid-file. Bars
    # detection always runs at the beginning and additionally at any tone-
    # flagged regions. Cross-validation second passes run as before.
    clams_regions = []

    clams_cfg = getattr(checks_config.tools, 'clams_detection', None)
    if clams_cfg and getattr(clams_cfg, 'run_tool', False):
        if check_cancelled and check_cancelled():
            return results
        if not report_directory:
            report_directory = dir_setup.make_report_dir(source_directory, video_id)
        logger.info("CLAMS detection: starting (tone first, then bars)")
        if signals and hasattr(signals, 'clams_detection_progress'):
            signals.clams_detection_progress.emit(0)

        bars_params = clams_cfg.bars
        tone_params = clams_cfg.tone

        def _merge_adjacent_spans(spans, gap_seconds):
            """Merge (start, end) tuples whose gap is <= gap_seconds."""
            if not spans:
                return []
            sorted_spans = sorted(spans, key=lambda t: t[0])
            merged = [sorted_spans[0]]
            for start, end in sorted_spans[1:]:
                prev_start, prev_end = merged[-1]
                if start - prev_end <= gap_seconds:
                    merged[-1] = (prev_start, max(prev_end, end))
                else:
                    merged.append((start, end))
            return merged

        bars_merge_gap = getattr(bars_params, 'merge_gap_seconds', 1.0)
        tone_merge_gap = getattr(tone_params, 'merge_gap_seconds', 5.0)

        # Load audio once so tone passes reuse the same decoded buffer.
        audio_samples = tone_detection_clams.load_audio_mono_16k(video_path)

        # ---- Phase 1: tone detection (whole file, fast) ----
        primary_tones = run_clams_tone_detection(
            video_path=video_path,
            report_directory=report_directory,
            video_id=video_id,
            tolerance=tone_params.tolerance,
            min_tone_duration_ms=tone_params.min_tone_duration_ms,
            stop_at_seconds=tone_params.stop_at_seconds,
            samples=audio_samples,
            check_cancelled=check_cancelled,
            signals=signals,
        )
        if primary_tones:
            before = len(primary_tones)
            primary_tones = _merge_adjacent_spans(primary_tones, gap_seconds=tone_merge_gap)
            if len(primary_tones) < before:
                logger.info(
                    f"CLAMS tone detection: merged {before} adjacent fragment(s) "
                    f"into {len(primary_tones)} tone(s) (gap ≤ {tone_merge_gap:.1f}s)"
                )
        results['clams_tones'] = primary_tones
        if primary_tones:
            logger.info(
                f"CLAMS tone detection: finished — {len(primary_tones)} tone(s) detected"
            )
        else:
            logger.info("CLAMS tone detection: finished — no tones met the minimum duration threshold")

        if check_cancelled and check_cancelled():
            bars_detection_clams.write_durations_csv(report_directory, [])
            tone_detection_clams.write_durations_csv(
                report_directory,
                [("primary", s, e) for s, e in primary_tones],
            )
            return results

        # Probe fps once for bars detection frame calculations.
        fps = bars_detection_clams.get_video_fps(video_path)

        # ---- Phase 2: bars detection at the beginning (always) ----
        primary_bars = run_clams_bars_detection(
            video_path=video_path,
            report_directory=report_directory,
            video_id=video_id,
            threshold=bars_params.threshold,
            sample_ratio=bars_params.sample_ratio,
            stop_at_frame=bars_params.stop_at_frame,
            min_frame_count=bars_params.min_frame_count,
            stop_after_one=bars_params.stop_after_one,
            fps=fps,
            pass_label="primary",
            check_cancelled=check_cancelled,
            signals=signals,
        )
        if primary_bars:
            before = len(primary_bars)
            primary_bars = _merge_adjacent_spans(primary_bars, gap_seconds=bars_merge_gap)
            if len(primary_bars) < before:
                logger.info(
                    f"CLAMS bars detection: merged {before} adjacent run(s) "
                    f"into {len(primary_bars)} (gap ≤ {bars_merge_gap:.1f}s)"
                )
            first_start, first_end = primary_bars[0]
            results['clams_bars_start_time'] = first_start
            results['clams_bars_end_time'] = first_end
            logger.info(
                f"CLAMS bars detection: finished — bars detected, "
                f"ending at {first_end:.1f}s"
            )
        else:
            logger.info("CLAMS bars detection: finished — no color bars run met the minimum frame count")

        # ---- Phase 3: cross-validation second passes ----
        WINDOW_SLACK_SECONDS = 5.0
        SECONDARY_BARS_THRESHOLD = 0.6
        SECONDARY_BARS_SAMPLE_RATIO = 5
        SECONDARY_TONE_TOLERANCE = 0.7
        SECONDARY_TONE_MIN_DURATION_MS = 500

        def _spans_overlap(a_start, a_end, b_start, b_end, slack):
            return a_start - slack <= b_end and b_start - slack <= a_end

        def _overlaps_any(start, end, spans, slack):
            return any(_spans_overlap(start, end, s, e, slack) for s, e in spans)

        # Bars second-pass: scan tone regions not covered by primary bars
        secondary_bars = []
        if primary_tones and fps and fps > 0:
            uncovered_tones = [
                (s, e) for s, e in primary_tones
                if not _overlaps_any(s, e, primary_bars, WINDOW_SLACK_SECONDS)
            ]
            padded_windows = [
                (max(0.0, s - WINDOW_SLACK_SECONDS), e + WINDOW_SLACK_SECONDS)
                for s, e in uncovered_tones
            ]
            scan_windows = _merge_adjacent_spans(padded_windows, gap_seconds=0.0)
            if len(scan_windows) < len(uncovered_tones):
                logger.info(
                    f"CLAMS bars second-pass: clustered {len(uncovered_tones)} "
                    f"tone trigger(s) into {len(scan_windows)} scan window(s)"
                )
            for window_start, window_stop in scan_windows:
                if check_cancelled and check_cancelled():
                    break
                window_start_frame = max(0, int(window_start * fps))
                window_stop_frame = int(window_stop * fps)
                logger.info(
                    f"CLAMS bars second-pass: scanning "
                    f"{window_start:.1f}s–{window_stop:.1f}s (relaxed threshold)"
                )
                hits = run_clams_bars_detection(
                    video_path=video_path,
                    report_directory=report_directory,
                    video_id=video_id,
                    threshold=SECONDARY_BARS_THRESHOLD,
                    sample_ratio=SECONDARY_BARS_SAMPLE_RATIO,
                    start_frame=window_start_frame,
                    stop_at_frame=window_stop_frame,
                    min_frame_count=bars_params.min_frame_count,
                    stop_after_one=False,
                    fps=fps,
                    pass_label="second-pass",
                    append_ssim_csv=True,
                    check_cancelled=check_cancelled,
                    signals=signals,
                )
                if hits:
                    logger.info(
                        f"CLAMS bars second-pass: found {len(hits)} additional run(s) "
                        f"in tone-anchored window"
                    )
                secondary_bars.extend(hits)

        if secondary_bars:
            before = len(secondary_bars)
            secondary_bars = _merge_adjacent_spans(secondary_bars, gap_seconds=bars_merge_gap)
            if len(secondary_bars) < before:
                logger.info(
                    f"CLAMS bars second-pass: merged {before} overlapping run(s) "
                    f"into {len(secondary_bars)} run(s) (gap ≤ {bars_merge_gap:.1f}s)"
                )

        # Tone second-pass: scan bars regions not covered by primary tones
        secondary_tones = []
        if primary_bars and audio_samples is not None:
            for bars_start, bars_end in primary_bars:
                if check_cancelled and check_cancelled():
                    break
                if _overlaps_any(bars_start, bars_end, primary_tones, WINDOW_SLACK_SECONDS):
                    continue
                window_start = max(0.0, bars_start - WINDOW_SLACK_SECONDS)
                window_stop = int(bars_end + WINDOW_SLACK_SECONDS)
                logger.info(
                    f"CLAMS tone second-pass: scanning "
                    f"{bars_start:.1f}s–{bars_end:.1f}s (relaxed thresholds)"
                )
                hits = run_clams_tone_detection(
                    video_path=video_path,
                    report_directory=report_directory,
                    video_id=video_id,
                    tolerance=SECONDARY_TONE_TOLERANCE,
                    min_tone_duration_ms=SECONDARY_TONE_MIN_DURATION_MS,
                    start_at_seconds=window_start,
                    stop_at_seconds=window_stop,
                    samples=audio_samples,
                    check_cancelled=check_cancelled,
                    signals=signals,
                )
                if hits:
                    logger.info(
                        f"CLAMS tone second-pass: found {len(hits)} additional tone(s) "
                        f"in bars-anchored window"
                    )
                secondary_tones.extend(hits)

        if secondary_tones:
            before = len(secondary_tones)
            secondary_tones = _merge_adjacent_spans(secondary_tones, gap_seconds=tone_merge_gap)
            if len(secondary_tones) < before:
                logger.info(
                    f"CLAMS tone second-pass: merged {before} fragments into "
                    f"{len(secondary_tones)} tone(s) (gap ≤ {tone_merge_gap:.1f}s)"
                )

        # Compose primary + second-pass into the on-disk durations CSVs.
        bars_detection_clams.write_durations_csv(
            report_directory,
            [("primary", s, e) for s, e in primary_bars]
            + [("second-pass", s, e) for s, e in secondary_bars],
        )
        tone_detection_clams.write_durations_csv(
            report_directory,
            [("primary", s, e) for s, e in primary_tones]
            + [("second-pass", s, e) for s, e in secondary_tones],
        )

        if signals and hasattr(signals, 'step_completed'):
            signals.step_completed.emit("CLAMS Detection")
        logger.info("")  # blank line for readability

        # Build clams_regions for qct-parse: all bars and tone regions found.
        for s, e in (primary_bars + secondary_bars):
            clams_regions.append((s, e, "bars"))
        for s, e in (primary_tones + secondary_tones):
            clams_regions.append((s, e, "tone"))
        clams_regions.sort(key=lambda r: r[0])

    # ---- qct-parse (runs AFTER CLAMS, receives CLAMS regions for
    # additional windowed bars scans beyond the head area) ----
    qctparse_head_end = None

    if qct_parse_run_tool:
        if not results['qctools_output_path'] or not os.path.isfile(results['qctools_output_path']):
            logger.critical(f"Unable to check qctools report. No QCTools report file found in input directory.\n")
        else:
            if not report_directory:
                report_directory = dir_setup.make_report_dir(source_directory, video_id)

            logger.info(f"Running qct-parse on: {results['qctools_output_path']}")
            if signals and hasattr(signals, 'qctparse_progress'):
                signals.qctparse_progress.emit(0)
            qctparse_result = run_qctparse(
                video_path, results['qctools_output_path'], report_directory,
                check_cancelled=check_cancelled, signals=signals,
                clams_regions=clams_regions,
            )
            if signals:
                signals.step_completed.emit("QCT Parse")

            if qctparse_result:
                qctparse_head_end = qctparse_result.get('head_bars_end')
                results['audio_findings'] = qctparse_result.get('audio_findings')

            # Fallback: read CSV if run_qctparse returned None (cancelled/error)
            if qctparse_head_end is None:
                colorbars_csv = Path(report_directory) / "qct-parse_colorbars_durations.csv"
                if colorbars_csv.exists():
                    _, end_seconds, _ = parse_colorbars_duration_csv(str(colorbars_csv))
                    qctparse_head_end = end_seconds

            if qctparse_head_end:
                logger.debug(f"qct-parse head bars detected, ending at {qctparse_head_end:.1f}s\n")

    # ---- Merge: "longest/latest wins" for head bars ----
    HEAD_BARS_START_THRESHOLD = 10.0
    clams_head_end = None
    if results.get('clams_bars_start_time') is not None:
        if results['clams_bars_start_time'] < HEAD_BARS_START_THRESHOLD:
            clams_head_end = results['clams_bars_end_time']

    head_candidates = [t for t in [qctparse_head_end, clams_head_end] if t is not None]
    if head_candidates:
        results['color_bars_end_time'] = max(head_candidates)
        logger.info(
            f"Merged head bars end time: {results['color_bars_end_time']:.1f}s "
            f"(qct-parse={qctparse_head_end}, CLAMS={clams_head_end})\n"
        )
    else:
        results['color_bars_end_time'] = None

    return results

def run_qctools_command(command, input_path, output_type, output_path, check_cancelled=None, signals=None):
    
    if check_cancelled():
        return None
    
    env = os.environ.copy()
    # Prefer Apple Silicon Homebrew (/opt/homebrew/bin) over Intel
    # (/usr/local/bin). Without this, a leftover x86_64 qcli at
    # /usr/local/bin/qcli runs under Rosetta and stalls on cross-arch
    # mutex/semaphore translation (~0.2% CPU, S+) while the arm64
    # /opt/homebrew/bin/qcli runs at 100%+ CPU natively.
    env['PATH'] = '/opt/homebrew/bin:/usr/local/bin:' + env.get('PATH', '')
    full_command = f"{command} \"{input_path}\" {output_type} \"{output_path}\""
    logger.debug(f'Running command: {full_command}\n')

    # qcli writes its progress bar with carriage returns ("\r"), not newlines.
    # Read in 4KB chunks (not readline, not byte-at-a-time) and split on both
    # \r and \n. readline() would block forever waiting for a \n that never
    # comes; byte-at-a-time is too slow to drain the pipe during bursts, which
    # causes qcli's write() to block and stalls its analysis thread.
    # Do NOT pass stdin=subprocess.DEVNULL — qcli (ffmpeg-based) treats
    # immediate EOF on stdin as a quit signal and exits early.
    process = subprocess.Popen(
        full_command,
        shell=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    READ_CHUNK = 4096
    LINE_SEPARATORS = (ord('\r'), ord('\n'))
    buf = bytearray()

    try:
        while True:
            if check_cancelled():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                return None

            chunk = process.stdout.read(READ_CHUNK)
            if not chunk:
                if process.poll() is not None:
                    break
                continue

            for byte in chunk:
                if byte in LINE_SEPARATORS:
                    if buf:
                        line = buf.decode('utf-8', errors='replace').strip()
                        buf.clear()
                        if line:
                            percentage = extract_percentage(line, signals=signals)
                            if percentage is not None and signals:
                                signals.qctools_progress.emit(min(100, max(0, int(percentage))))
                else:
                    buf.append(byte)

    except Exception as e:
        logger.error(f"Error reading QCTools output: {str(e)}")

    # Flush any trailing bytes that didn't end with \r or \n
    if buf:
        line = buf.decode('utf-8', errors='replace').strip()
        if line:
            percentage = extract_percentage(line, signals=signals)
            if percentage is not None and signals:
                signals.qctools_progress.emit(min(100, max(0, int(percentage))))

    return_code = process.wait()

    if signals:
        signals.qctools_progress.emit(100)

    return return_code

def extract_percentage(output_line, signals=None):
    """
    Extract percentage value from QCTools output line.
    Handles QCTools specific format: "dots + spaces + X of 100 %"
    """
    # QCTools specific pattern: any number of dots, then spaces, then "X of 100 %"
    pattern = r'\.+\s+(\d+)\s+of\s+100\s+%'

    match = re.search(pattern, output_line)
    if match:
        try:
            percentage = int(match.group(1))
            if signals:
                # logger.debug(f"QCTools emitting progress: {percentage}%")
                return percentage
            elif signals is None:
                print(f"\rQCTools progress: {percentage}%", end='', flush=True)
                return percentage
        except (ValueError, IndexError):
            pass
    else:
        # Try without dots (for early progress like 1%)
        pattern2 = r'(\d+)\s+of\s+100\s+%'
        match2 = re.search(pattern2, output_line)
        if match2:
            try:
                percentage = int(match2.group(1))
                if signals:
                    # logger.debug(f"QCTools emitting progress: {percentage}%")
                    return percentage
                elif signals is None:
                    print(f"\rQCTools progress: {percentage}%", end='', flush=True)
                    return percentage
            except (ValueError, IndexError):
                pass

def check_tool_metadata(tool_name, output_path):
    """
    Check metadata for a specific tool if configured.
    
    Args:
        tool_name (str): Name of the tool
        output_path (str): Path to the tool's output file
        
    Returns:
        dict or None: Differences found by parsing the tool's output, or None
    """
    config_mgr = ConfigManager()
    checks_config = config_mgr.get_config('checks', ChecksConfig)

    # Mapping of tool names to their parsing functions
    parse_functions = {
        'exiftool': parse_exiftool,
        'mediainfo': parse_mediainfo,
        'mediatrace': parse_mediatrace,
        'ffprobe': parse_ffprobe,
        'mkvalidator': parse_mkvalidator
    }

    # Check if tool metadata checking is enabled
    tool = getattr(checks_config.tools, tool_name)
    if output_path and tool.check_tool:
        parse_function = parse_functions.get(tool_name)
        if parse_function:
            return parse_function(output_path)
    
    return None


def setup_mediaconch_policy(user_policy_path: str = None) -> str:
    """
    Set up MediaConch policy file, either using user-provided policy or default.
    
    Args:
        user_policy_path (str, optional): Path to user-provided policy file
        
    Returns:
        str: Name of the policy file that will be used
    """
    config_mgr = ConfigManager()
    
    if not user_policy_path:
        # Return current policy file name from config
        current_config = config_mgr.get_config('checks', ChecksConfig)
        return current_config.tools.mediaconch.mediaconch_policy
        
    try:
        # Verify user policy file exists
        if not os.path.exists(user_policy_path):
            logger.critical(f"User provided policy file not found: {user_policy_path}")
            return None
            
        # Get policy file name
        policy_filename = os.path.basename(user_policy_path)
        
        # Copy policy file to user policies directory
        user_policy_dest = os.path.join(config_mgr._user_policies_dir, policy_filename)
        
        # Copy policy file, overwriting if file exists
        shutil.copy2(user_policy_path, user_policy_dest, follow_symlinks=False)
        logger.info(f"Copied user policy file to user policies directory: {policy_filename}")
        
        # Get current config to preserve run_mediaconch value
        current_config = config_mgr.get_config('checks', ChecksConfig)
        run_mediaconch = current_config.tools.mediaconch.run_mediaconch
        
        # Update config to use new policy file while preserving run_mediaconch
        config_mgr.update_config('checks', {
            'tools': {
                'mediaconch': {
                    'mediaconch_policy': policy_filename,
                    'run_mediaconch': run_mediaconch
                }
            }
        })
        logger.info(f"Updated config to use new policy file: {policy_filename}")
        
        return policy_filename
        
    except Exception as e:
        logger.critical(f"Error setting up MediaConch policy: {str(e)}")
        return None
    
def parse_colorbars_duration_csv(csv_path):
    """
    Parse the qct-parse color bars duration CSV to extract start and end times.

    Supports both the legacy 2-column format and the 3-column labeled format.

    Args:
        csv_path (str): Path to qct-parse_colorbars_durations.csv

    Returns:
        tuple: (head_start_seconds, head_end_seconds, all_regions)
               where all_regions is a list of (label, start_seconds, end_seconds).
               Returns (None, None, []) if no bars found.
    """
    import csv

    if not os.path.exists(csv_path):
        return None, None, []

    def timestamp_to_seconds(ts_str):
        parts = ts_str.strip().split(':')
        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        return 0

    try:
        with open(csv_path, 'r') as csvfile:
            reader = csv.reader(csvfile)
            rows = list(reader)

            if not rows or "color bars found" not in rows[0][0]:
                return None, None, []

            all_regions = []
            for row in rows[1:]:
                if len(row) >= 3:
                    label = row[0]
                    start = timestamp_to_seconds(row[1])
                    end = timestamp_to_seconds(row[2])
                    all_regions.append((label, start, end))
                elif len(row) >= 2:
                    start = timestamp_to_seconds(row[0])
                    end = timestamp_to_seconds(row[1])
                    all_regions.append(("head", start, end))

            head_region = next((r for r in all_regions if r[0] in ("head", "head-relaxed")), None)
            if head_region:
                return head_region[1], head_region[2], all_regions
            elif all_regions:
                return all_regions[0][1], all_regions[0][2], all_regions

    except Exception as e:
        logger.warning(f"Could not parse color bars duration CSV: {e}")

    return None, None, []


def calculate_border_regions(x, y, w, h, video_width, video_height):
    """Helper function to calculate border regions"""
    regions = {}
    
    left_border_width = x
    if left_border_width > 0:
        regions['left_border'] = (0, 0, left_border_width, video_height)
    else:
        regions['left_border'] = None
    
    right_border_start = x + w
    right_border_width = video_width - right_border_start
    if right_border_width > 0:
        regions['right_border'] = (right_border_start, 0, right_border_width, video_height)
    else:
        regions['right_border'] = None
    
    top_border_height = y
    if top_border_height > 0:
        regions['top_border'] = (0, 0, video_width, top_border_height)
    else:
        regions['top_border'] = None
    
    bottom_border_start = y + h
    bottom_border_height = video_height - bottom_border_start
    if bottom_border_height > 0:
        regions['bottom_border'] = (0, bottom_border_start, video_width, bottom_border_height)
    else:
        regions['bottom_border'] = None
    
    return regions