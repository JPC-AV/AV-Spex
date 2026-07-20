#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
os.environ["NUMEXPR_MAX_THREADS"] = "11" # troubleshooting goofy numbpy related error "Note: NumExpr detected 11 cores but "NUMEXPR_MAX_THREADS" not set, so enforcing safe limit of 8. # NumExpr defaulting to 8 threads."

import csv
from base64 import b64encode
import json
import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from concurrent.futures import ThreadPoolExecutor, as_completed

from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr

config_mgr = ConfigManager()

# Read CSV files and convert them to HTML tables
def csv_to_html_table(csv_file, style_mismatched=False, mismatch_color="#ff9999", match_color="#d2ffed", check_fail=False):
    try:
        # Try UTF-8 first
        try:
            with open(csv_file, newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                rows = list(reader)
        except UnicodeDecodeError:
            # If UTF-8 fails, try latin-1
            with open(csv_file, newline='', encoding='latin-1') as f:
                logger.warning(f"Used latin-1 encoding as fallback for CSV file {csv_file}")
                reader = csv.reader(f)
                rows = list(reader)

        # Rest of the function remains the same
        table_html = '<table>\n'
        header = rows[0]
        table_html += '  <tr>\n'
        for cell in header:
            table_html += f'    <th>{cell}</th>\n'
        table_html += '  </tr>\n'

        for row in rows[1:]:
            table_html += '  <tr>\n'
            for i, cell in enumerate(row):
                if check_fail and cell.lower() == "fail":
                    table_html += f'    <td class="cell-mismatch">{cell}</td>\n'
                elif check_fail and cell.lower() == "pass":
                    table_html += f'    <td class="cell-match">{cell}</td>\n'
                elif style_mismatched and i == 2 and row[2] != '' and row[1] != row[2]:
                    table_html += f'    <td class="cell-match">{cell}</td>\n'
                elif style_mismatched and i == 3 and row[2] != '' and row[1] != row[2]:
                    table_html += f'    <td class="cell-mismatch">{cell}</td>\n'
                else:
                    table_html += f'    <td>{cell}</td>\n'
            table_html += '  </tr>\n'
        table_html += '</table>\n'
        return table_html
    except Exception as e:
        logger.error(f"Error processing CSV file {csv_file}: {e}")
        return f"<p>Error processing CSV file: {e}</p>"


def read_text_file(text_file_path):
    try:
        # First try UTF-8
        with open(text_file_path, 'r', encoding='utf-8') as file:
            return file.read()
    except UnicodeDecodeError:
        try:
            # If UTF-8 fails, try latin-1 which can handle any byte
            with open(text_file_path, 'r', encoding='latin-1') as file:
                logger.warning(f"Used latin-1 encoding as fallback for {text_file_path}")
                return file.read()
        except Exception as e:
            logger.error(f"Failed to decode {text_file_path} with fallback encoding: {e}")
            return f"[Error reading file: {e}]"


def prepare_file_section(file_path, process_function=None):
    if file_path:
        if process_function:
            file_content = process_function(file_path)
        else:
            file_content = read_text_file(file_path)
        file_name = os.path.basename(file_path)
    else:
        file_content = ''
        file_name = ''
    return file_content, file_name


def image_to_data_uri(image_path, mime_type='image/png'):
    """Read a local image file and return it as a base64 data URI so the
    generated HTML renders self-contained when shared off this machine."""
    try:
        with open(image_path, 'rb') as f:
            encoded = b64encode(f.read()).decode('ascii')
        return f"data:{mime_type};base64,{encoded}"
    except Exception as e:
        logger.warning(f"Could not embed image at {image_path}: {e}")
        return ""


def image_file_to_jpeg_data_uri(image_path, quality=85):
    """Read a local image file, re-encode it as JPEG, and return it as a
    base64 data URI for embedding in the report.

    Photographic thumbnails (video frames) are often written to disk as
    lossless PNG, which inflates the self-contained HTML report. Re-encoding
    to JPEG here keeps the on-disk sidecars lossless while shrinking the
    embedded copy dramatically. Falls back to embedding the original bytes
    if decoding/encoding fails. Mirrors the spectrogram embed below."""
    try:
        import cv2
        img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("cv2 could not decode image")
        ok, jpeg_bytes = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("cv2.imencode failed")
        encoded = b64encode(jpeg_bytes.tobytes()).decode('ascii')
        return f"data:image/jpeg;base64,{encoded}"
    except Exception as e:
        logger.warning(f"Could not re-encode image at {image_path} as JPEG, embedding original: {e}")
        ext = os.path.splitext(str(image_path))[1].lower()
        mime = 'image/png' if ext == '.png' else 'image/jpeg'
        return image_to_data_uri(image_path, mime_type=mime)


def parse_timestamp(timestamp_str):
    if not timestamp_str:
        return (9999, 99, 99, 99, 9999)  # Return a placeholder tuple for non-timestamp entries
    
    try:
        # Split timestamp into hours, minutes, and seconds
        parts = timestamp_str.split(':')
        if len(parts) != 3:
            return (9999, 99, 99, 99, 9999)  # Invalid format
        
        hours = int(parts[0])
        minutes = int(parts[1])
        
        # Handle seconds with decimals
        seconds_parts = parts[2].split('.')
        seconds = int(seconds_parts[0])
        
        # Handle milliseconds if present
        if len(seconds_parts) > 1:
            # Pad or truncate to 4 digits for consistency
            milliseconds = int(seconds_parts[1].ljust(4, '0')[:4])
        else:
            milliseconds = 0
        
        return (hours, minutes, seconds, milliseconds)
        
    except (ValueError, IndexError):
        # Return placeholder if parsing fails
        return (9999, 99, 99, 99, 9999)


def parse_profile(profile_name):
    # Define a custom order for the profile names
    profile_order = {
        "color_bars_detection": 0,
        "color_bars_evaluation": 1,
        "threshold_profile": 2, 
        "tag_check": 3
    }

    for key in profile_order:
        if profile_name.startswith(key):
            return profile_order[key]
    return 99  # Default order if profile_name does not match any known profiles


def find_qct_thumbs(report_directory):
    thumbs_dict = {}
    thumb_exports_dir = os.path.join(report_directory, 'ThumbExports')

    if os.path.isdir(thumb_exports_dir):
        for file in os.listdir(thumb_exports_dir):
            file_path = os.path.join(thumb_exports_dir, file)
            if os.path.isfile(file_path) and not file.startswith('.DS_Store'):
                qct_thumb_path = file_path
                filename_segments = file.split('.')
                if len(filename_segments) >= 5:
                    profile_name = filename_segments[1]
                    tag_name = filename_segments[2]
                    tag_value = filename_segments[3]
                    
                    # Find the timestamp pattern in the filename
                    # It should be HH.MM.SS.ssss before the file extension
                    # We need to reconstruct it as HH:MM:SS.ssss

                    # The timestamp starts after tag_value (index 4) and goes until the extension
                    # For a file like: JPC_AV_01663.color_bars_evaluation.YMAX.940.0.00.00.53.7870.jpg
                    # segments[4:] would be ['0', '00', '00', '53', '7870', 'jpg']
                    # We want to reconstruct 00:00:53.7870

                    timestamp_parts = filename_segments[4:-1]  # Exclude extension
                    
                    if len(timestamp_parts) >= 4:
                        # Assuming format is always HH.MM.SS.milliseconds
                        hours = timestamp_parts[1].zfill(2)    # Second element (skip the first '0')
                        minutes = timestamp_parts[2].zfill(2)
                        seconds = timestamp_parts[3].zfill(2)
                        milliseconds = timestamp_parts[4] if len(timestamp_parts) > 4 else '0000'
                        timestamp_as_string = f"{hours}:{minutes}:{seconds}.{milliseconds}"
                    else:
                        # Fallback
                        timestamp_as_string = ':'.join(timestamp_parts)
                    
                    if profile_name == 'color_bars_detection':
                        qct_thumb_name = f'First frame of color bars\n\nAt timecode: {timestamp_as_string}'
                    else:
                        qct_thumb_name = f'Failed frame \n\n{tag_name}:{tag_value}\n\n{timestamp_as_string}'
                    thumbs_dict[qct_thumb_name] = (qct_thumb_path, tag_name, timestamp_as_string)
                else:
                    qct_thumb_name = ':'.join(filename_segments)
                    thumbs_dict[qct_thumb_name] = (qct_thumb_path, "", "")

    # Sort thumbs_dict by timestamp if possible
    sorted_thumbs_dict = {}
    for key in sorted(thumbs_dict.keys(), key=lambda x: (parse_profile(thumbs_dict[x][1]), parse_timestamp(thumbs_dict[x][2]))):
        sorted_thumbs_dict[key] = thumbs_dict[key]

    return sorted_thumbs_dict

def find_frame_analysis_outputs(source_directory, destination_directory, video_id):
    """
    Find frame analysis output files (border detection, BRNG analysis, signalstats).
    
    Args:
        source_directory (str): Source directory for the video
        destination_directory (str): Destination directory for outputs
        video_id (str): Video identifier
        
    Returns:
        dict: Paths to frame analysis output files
    """
    frame_outputs = {
        'border_visualization': None,
        'border_data': None,
        'brng_analysis': None,
        'brng_thumbnails': [],
        'signalstats_analysis': None,
        'enhanced_frame_analysis': None,
        'dropped_sample_spectrogram': None,
        'dropped_sample_detection': None,
        'duplicate_frame_detection': None
    }
    
    # Check for border detection outputs
    border_viz = os.path.join(destination_directory, f"{video_id}_border_detection.jpg")
    if os.path.exists(border_viz):
        frame_outputs['border_visualization'] = border_viz
    
    border_data = os.path.join(destination_directory, f"{video_id}_border_data.json")
    if os.path.exists(border_data):
        frame_outputs['border_data'] = border_data
    
    # Check for BRNG analysis outputs
    brng_analysis = os.path.join(destination_directory, f"{video_id}_active_brng_analysis.json")
    if os.path.exists(brng_analysis):
        frame_outputs['brng_analysis'] = brng_analysis
    
    # Check for BRNG diagnostic thumbnails
    brng_thumbs_dir = os.path.join(destination_directory, "brng_thumbnails")
    if os.path.exists(brng_thumbs_dir):
        for file in os.listdir(brng_thumbs_dir):
            if file.endswith('.jpg') or file.endswith('.png'):
                frame_outputs['brng_thumbnails'].append(os.path.join(brng_thumbs_dir, file))
    
    # Check for enhanced frame analysis JSON (contains signalstats data)
    enhanced_analysis = os.path.join(destination_directory, f"{video_id}_enhanced_frame_analysis.json")
    if os.path.exists(enhanced_analysis):
        frame_outputs['enhanced_frame_analysis'] = enhanced_analysis
        # Extract signalstats data from enhanced frame analysis
        try:
            with open(enhanced_analysis, 'r') as f:
                enhanced_data = json.load(f)
            # Check for signalstats in main results or final_signalstats (from refinement)
            if enhanced_data.get('final_signalstats'):
                frame_outputs['signalstats_analysis'] = enhanced_data['final_signalstats']
            elif enhanced_data.get('signalstats'):
                frame_outputs['signalstats_analysis'] = enhanced_data['signalstats']
            
            # Extract BRNG analysis from enhanced JSON if standalone file doesn't exist
            if not frame_outputs['brng_analysis']:
                brng_data = enhanced_data.get('final_brng_analysis') or enhanced_data.get('brng_analysis')
                if brng_data:
                    frame_outputs['brng_analysis'] = brng_data  # Store as dict directly
            
            # Extract bitplane check data from enhanced JSON
            if enhanced_data.get('bitplane_check'):
                frame_outputs['bitplane_check'] = enhanced_data['bitplane_check']

            # Extract border refinement data from enhanced JSON
            if enhanced_data.get('refinement_iterations'):
                frame_outputs['refinement_iterations'] = enhanced_data['refinement_iterations']
                frame_outputs['refinement_history'] = enhanced_data.get('refinement_history', [])
                frame_outputs['initial_borders'] = enhanced_data.get('initial_borders')
                frame_outputs['initial_brng_analysis'] = enhanced_data.get('initial_brng_analysis')
                frame_outputs['final_borders'] = enhanced_data.get('final_borders')
                
                # Check for refinement comparison visualization
                comparison_path = os.path.join(
                    destination_directory, f"{video_id}_border_refinement_comparison.jpg"
                )
                if os.path.exists(comparison_path):
                    frame_outputs['refinement_comparison'] = comparison_path
                
                # Check for per-iteration refined visualizations
                frame_outputs['refinement_visualizations'] = []
                for i in range(1, enhanced_data['refinement_iterations'] + 1):
                    iter_path = os.path.join(
                        destination_directory, f"{video_id}_border_detection_refined_iter{i}.jpg"
                    )
                    if os.path.exists(iter_path):
                        frame_outputs['refinement_visualizations'].append(iter_path)
            
            # Extract initial borders (even without refinement) for methodology info
            if not frame_outputs.get('initial_borders') and enhanced_data.get('initial_borders'):
                frame_outputs['initial_borders'] = enhanced_data['initial_borders']
            
            # Extract QCTools violation info
            if enhanced_data.get('qctools_violations_found'):
                frame_outputs['qctools_violations_found'] = enhanced_data['qctools_violations_found']
            if enhanced_data.get('color_bars_end_time'):
                frame_outputs['color_bars_end_time'] = enhanced_data['color_bars_end_time']

            # Extract dropped sample detection data
            if enhanced_data.get('dropped_sample_detection'):
                frame_outputs['dropped_sample_detection'] = enhanced_data['dropped_sample_detection']
                # Check for spectrogram image
                spec_path = enhanced_data['dropped_sample_detection'].get('spectrogram_path')
                if spec_path and os.path.exists(spec_path):
                    frame_outputs['dropped_sample_spectrogram'] = spec_path

            # Extract duplicate frame detection data (always present when the
            # step ran, even if no runs were detected — section is rendered
            # with header + table either way).
            if enhanced_data.get('duplicate_frame_detection') is not None:
                frame_outputs['duplicate_frame_detection'] = enhanced_data['duplicate_frame_detection']

        except Exception as e:
            logger.warning(f"Could not read signalstats from enhanced frame analysis: {e}")
    
    # Also check for standalone signalstats file (legacy support)
    if not frame_outputs['signalstats_analysis']:
        signalstats = os.path.join(destination_directory, f"{video_id}_signalstats_analysis.json")
        if os.path.exists(signalstats):
            frame_outputs['signalstats_analysis'] = signalstats

    # Check for spectrogram image (if not already found from enhanced JSON)
    if not frame_outputs['dropped_sample_spectrogram']:
        spectrogram = os.path.join(destination_directory, f"{video_id}_spectrogram.png")
        if os.path.exists(spectrogram):
            frame_outputs['dropped_sample_spectrogram'] = spectrogram

    return frame_outputs

def find_report_csvs(report_directory):

    qctools_colorbars_duration_output = None
    qctools_bars_eval_check_output = None
    qctools_bars_eval_timestamps = None
    colorbars_values_output = None
    windowed_colorbars_values = []
    qctools_content_check_outputs = []
    qctools_profile_check_output = None
    qctools_profile_timestamps = None
    profile_fails_csv = None
    tags_check_output = None
    tag_fails_csv = None
    colorbars_eval_fails_csv = None
    audio_clipping_csv = None
    channel_imbalance_csv = None
    audible_timecode_csv = None
    audio_dropout_csv = None
    clamped_levels_csv = None
    clamped_traces_csv = None
    chroma_phase_summary_csv = None
    chroma_phase_events_csv = None
    tone_leak_summary_csv = None
    tone_leak_events_csv = None
    difference_csv = None

    if os.path.isdir(report_directory):
        for file in os.listdir(report_directory):
            file_path = os.path.join(report_directory, file)
            if os.path.isfile(file_path) and not file.startswith('.DS_Store'):
                if file.startswith("qct-parse_"):
                    if "qct-parse_colorbars_durations" in file:
                        qctools_colorbars_duration_output = file_path
                    elif "qct-parse_colorbars_eval_summary" in file:
                        qctools_bars_eval_check_output = file_path
                    elif "qct-parse_colorbars_values" in file:
                        if file == "qct-parse_colorbars_values.csv":
                            colorbars_values_output = file_path
                        else:
                            windowed_colorbars_values.append(file_path)
                    elif "qct-parse_contentFilter" in file:
                        qctools_content_check_outputs.append(file_path)
                    elif "qct-parse_profile_summary" in file:
                        qctools_profile_check_output = file_path
                    elif "qct-parse_profile_failures" in file:
                        profile_fails_csv = file_path
                    elif "qct-parse_tags_summary.csv" in file:
                        tags_check_output = file_path
                    elif "qct-parse_tags_failures" in file:
                        tag_fails_csv = file_path
                    elif "qct-parse_colorbars_eval_failures" in file:
                        colorbars_eval_fails_csv = file_path
                    elif "qct-parse_audio_clipping" in file:
                        audio_clipping_csv = file_path
                    elif "qct-parse_channel_imbalance" in file:
                        channel_imbalance_csv = file_path
                    elif "qct-parse_audible_timecode" in file:
                        audible_timecode_csv = file_path
                    elif "qct-parse_audio_dropout" in file:
                        audio_dropout_csv = file_path
                    elif "qct-parse_clamped_traces" in file:
                        clamped_traces_csv = file_path
                    elif "qct-parse_clamped_levels" in file:
                        clamped_levels_csv = file_path
                    elif "qct-parse_chroma_phase_summary" in file:
                        chroma_phase_summary_csv = file_path
                    elif "qct-parse_chroma_phase_events" in file:
                        chroma_phase_events_csv = file_path
                    elif "qct-parse_tone_leak_summary" in file:
                        tone_leak_summary_csv = file_path
                    elif "qct-parse_tone_leak_events" in file:
                        tone_leak_events_csv = file_path
                elif "metadata_difference" in file:
                    difference_csv = file_path

    return qctools_colorbars_duration_output, qctools_bars_eval_check_output, colorbars_values_output, windowed_colorbars_values, qctools_content_check_outputs, qctools_profile_check_output, profile_fails_csv, tags_check_output, tag_fails_csv, colorbars_eval_fails_csv, audio_clipping_csv, channel_imbalance_csv, audible_timecode_csv, audio_dropout_csv, clamped_levels_csv, clamped_traces_csv, chroma_phase_summary_csv, chroma_phase_events_csv, tone_leak_summary_csv, tone_leak_events_csv, difference_csv


def read_xml_file(xml_file_path):
    """
    Read XML file content with proper encoding handling.
    
    Args:
        xml_file_path (str): Path to the XML file
        
    Returns:
        str: XML content or error message
    """
    try:
        # First try UTF-8
        with open(xml_file_path, 'r', encoding='utf-8') as file:
            return file.read()
    except UnicodeDecodeError:
        try:
            # If UTF-8 fails, try latin-1 which can handle any byte
            with open(xml_file_path, 'r', encoding='latin-1') as file:
                logger.warning(f"Used latin-1 encoding as fallback for {xml_file_path}")
                return file.read()
        except Exception as e:
            logger.error(f"Failed to decode {xml_file_path} with fallback encoding: {e}")
            return f"[Error reading XML file: {e}]"


def _get_video_duration(video_path):
    """
    Probe the video duration in seconds using ffprobe.
    
    Returns:
        float or None: Duration in seconds, or None on failure.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not probe video duration: {e}")
        return None


def _extract_frame_at(video_path, timestamp, output_path, height):
    """
    Fast-seek to *timestamp* and extract a single frame scaled to *height*.

    Placing ``-ss`` before ``-i`` triggers keyframe seeking, which jumps
    directly to the nearest keyframe without decoding intermediate frames.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-ss", str(timestamp),
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale=-1:{height}",
        "-q:v", "5",
        output_path,
    ]
    subprocess.run(cmd, check=True)


def generate_color_strip_base64(video_path, num_frames=40, strip_height=120, max_workers=4, signals=None, progress_start=22, progress_end=25):
    """
    Generate a frame-strip image from a video and return it as a
    base64-encoded JPEG string.

    Instead of decoding the full video with an ``fps`` filter, this function
    fast-seeks (``-ss`` before ``-i``) to *num_frames* evenly spaced
    timestamps in parallel, extracts one frame at each position, then tiles
    them into a single horizontal row with a second ffmpeg call.

    Args:
        video_path (str): Path to the source video file.
        num_frames (int): Number of frames to sample across the video.
        strip_height (int): Height of each frame tile in pixels.
        max_workers (int): Number of parallel ffmpeg seek processes.

    Returns:
        str or None: Base64-encoded PNG data (no ``data:`` prefix), or None
        on failure.
    """
    logger.info("Creating screenshot spacer")
    try:
        duration = _get_video_duration(video_path)
        if duration is None or duration <= 0:
            logger.warning("Color strip: could not determine video duration — skipping")
            return None

        # Evenly space timestamps, avoiding the very start and end
        timestamps = [
            duration * (i + 0.5) / num_frames
            for i in range(num_frames)
        ]

        with TemporaryDirectory() as tmpdir:
            # ── Step 1: extract frames via parallel keyframe seeks ──
            frame_paths = []
            future_to_index = {}
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                for i, ts in enumerate(timestamps):
                    frame_path = str(Path(tmpdir) / f"frame_{i:04d}.jpg")
                    frame_paths.append(frame_path)
                    fut = pool.submit(_extract_frame_at, video_path, ts, frame_path, strip_height)
                    future_to_index[fut] = i
                completed_count = 0
                progress_range = progress_end - progress_start
                for fut in as_completed(future_to_index):
                    fut.result()  # raises on failure
                    completed_count += 1
                    if signals:
                        pct = progress_start + int(progress_range * completed_count / num_frames)
                        signals.report_progress.emit(pct)

            # Drop any frames that failed to extract
            valid_paths = [p for p in frame_paths if os.path.isfile(p)]
            if not valid_paths:
                logger.warning("Color strip: no frames extracted — skipping")
                return None

            # ── Step 2: tile the extracted frames into a single row ──
            output_path = str(Path(tmpdir) / "frame_strip.jpg")
            tile_count = len(valid_paths)
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-i", str(Path(tmpdir) / "frame_%04d.jpg"),
                "-frames:v", "1",
                "-vf", f"tile={tile_count}x1",
                "-q:v", "3",
                output_path,
            ]
            subprocess.run(cmd, check=True)

            if not os.path.isfile(output_path):
                logger.warning("Color strip: ffmpeg tile produced no output — skipping")
                return None

            with open(output_path, "rb") as f:
                b64 = b64encode(f.read()).decode("utf-8")

        logger.info("Screenshot spacer completed")
        if signals:
            signals.report_progress.emit(progress_end)
        return b64

    except Exception as e:
        logger.warning(f"Screenshot spacer generation failed: {e}")
        return None


def _get_audio_channel_count(video_path):
    """Get the number of audio channels in the first audio stream using ffprobe.

    Returns:
        int or None: Number of channels, or None if unavailable.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=channels",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return int(result.stdout.strip())
    except Exception as e:
        logger.warning(f"Could not probe audio channel count: {e}")
        return None


def _get_audio_stream_channels(video_path):
    """Get the channel count of every audio stream, in order, using ffprobe.

    Returns a list with one entry per audio stream, e.g. ``[2]`` for a single
    stereo stream (typical MKV) or ``[1, 1]`` for two separate mono streams
    (typical broadcast MXF, where each track is its own mono PCM stream).

    Returns:
        list[int] or None: Per-stream channel counts, or None if unavailable
        or there are no audio streams.
    """
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=channels",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        counts = [int(x) for x in result.stdout.split() if x.strip()]
        return counts or None
    except Exception as e:
        logger.warning(f"Could not probe audio channel count: {e}")
        return None


# Colors assigned per channel for waveform visualization
_WAVEFORM_CHANNEL_COLORS = [
    "#378d6a",  # green (channel 1 / left)
    "#bf971b",  # gold (channel 2 / right)
    "#5b8abf",  # blue (channel 3)
    "#c45a3c",  # red-orange (channel 4)
    "#8e6bbf",  # purple (channel 5)
    "#bf6b8e",  # pink (channel 6)
    "#6bbfb5",  # teal (channel 7)
    "#bfad6b",  # tan (channel 8)
]


def _build_waveform_filter(stream_channels, width, height):
    """Build an FFmpeg filter_complex string that renders each audio channel
    as a separate waveform strip stacked vertically.

    Handles both common source layouts transparently:
      * a single multi-channel stream (e.g. one stereo stream in an MKV), which
        is split into its channels with ``channelsplit``; and
      * multiple separate streams (e.g. two mono PCM streams in a broadcast
        MXF), where each stream contributes its channel(s) directly.

    A thin separator line is inserted between channels so they are easy to
    distinguish regardless of the channel count or source layout.

    Args:
        stream_channels (list[int]): Channel count per audio stream, in order.
            e.g. ``[2]`` (one stereo stream) or ``[1, 1]`` (two mono streams).
        width (int): Image width.
        height (int): Height per channel in pixels.

    Returns:
        str: filter_complex string for FFmpeg.
    """
    colors = _WAVEFORM_CHANNEL_COLORS
    opacity = 0.8
    separator_height = 2
    separator_color = "333333"

    total_channels = sum(stream_channels)

    if total_channels <= 1:
        # Single mono channel — one waveform, no split needed
        return f"[0:a]showwavespic=s={width}x{height}:colors={colors[0]}@{opacity}:draw=full:scale=sqrt"

    # Map channel count to a standard FFmpeg layout name for channelsplit
    layout_map = {2: "stereo", 3: "2.1", 4: "quad", 6: "5.1", 8: "7.1"}

    # Build one filtergraph label per channel, in order. Mono streams are used
    # directly; multi-channel streams are split into their channels.
    lines = []
    channel_labels = []
    for s, count in enumerate(stream_channels):
        if count <= 1:
            channel_labels.append(f"[0:a:{s}]")
        else:
            layout_arg = layout_map.get(count, f"{count}c")
            outs = "".join(f"[s{s}c{j}]" for j in range(count))
            lines.append(f"[0:a:{s}]channelsplit=channel_layout={layout_arg}{outs};")
            channel_labels.extend(f"[s{s}c{j}]" for j in range(count))

    n = len(channel_labels)

    # Create a thin separator strip
    lines.append(
        f"color=c=#{separator_color}:s={width}x{separator_height}:d=1[sep];"
    )

    for i, label in enumerate(channel_labels):
        color = colors[i % len(colors)]
        lines.append(
            f"{label}showwavespic=s={width}x{height}:colors={color}@{opacity}:draw=full:scale=sqrt[w{i}];"
        )

    # Interleave waveform strips with separator copies, then vstack.
    # We need (n - 1) copies of the separator.
    if n > 2:
        lines.append(f"[sep]split={n - 1}" + "".join(f"[s{i}]" for i in range(n - 1)) + ";")
    else:
        # For two channels, the single [sep] can be used directly
        lines.append("[sep]copy[s0];")

    # Build the vstack input list: w0, s0, w1, s1, w2, ..., w(N-1)
    vstack_inputs = ""
    for i in range(n):
        vstack_inputs += f"[w{i}]"
        if i < n - 1:
            vstack_inputs += f"[s{i}]"

    total_inputs = n + (n - 1)  # waveforms + separators
    lines.append(f"{vstack_inputs}vstack=inputs={total_inputs}")

    return "\n".join(lines)


def generate_audio_waveform_base64(video_path, width=1200, height=80, signals=None, progress_start=25, progress_end=95):
    """
    Generate a compact audio waveform image from a video file and return it
    as a base64-encoded JPEG string.

    Uses FFmpeg's ``showwavespic`` filter, which renders the entire audio
    stream into a single image in one pass — no frame-by-frame extraction.
    Automatically detects the number of audio channels and renders each
    channel in a distinct color, layered on top of each other.

    Args:
        video_path (str): Path to the source video file.
        width (int): Width of the output image in pixels.
        height (int): Height of the output image in pixels.

    Returns:
        str or None: Base64-encoded JPEG data (no ``data:`` prefix), or None
        on failure.
    """
    logger.info("Creating audio waveform spacer")
    if signals:
        signals.report_progress.emit(progress_start)
    try:
        duration = _get_video_duration(video_path)
        if duration is None or duration <= 0:
            logger.warning("Audio waveform: could not determine video duration — skipping")
            return None

        stream_channels = _get_audio_stream_channels(video_path)
        if not stream_channels:
            logger.warning("Audio waveform: could not determine audio channel count — skipping")
            return None

        filter_complex = _build_waveform_filter(stream_channels, width, height)

        progress_range = progress_end - progress_start

        with TemporaryDirectory() as tmpdir:
            output_path = str(Path(tmpdir) / "waveform.jpg")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-i", video_path,
                "-filter_complex",
                filter_complex,
                "-frames:v", "1",
                "-q:v", "5",
                output_path,
            ]
            # Drive progress from Python using elapsed wall-clock time vs. an
            # estimated processing duration (~100x realtime for showwavespic).
            # We don't try to parse ffmpeg's output: showwavespic is a single-
            # output-frame filter so `-progress` reports output time stuck at
            # 0, and `-stats` emits `\r`-terminated lines that libc holds in
            # its stdio buffer over a pipe. Same Python-driven pattern used
            # by `generate_color_strip_base64`.
            if duration and duration > 0:
                estimated_s = max(duration / 100.0, 4.0)
            else:
                estimated_s = 30.0
            cap_fraction = 0.9  # leave room for the final emit at progress_end
            poll_interval = 0.3
            last_pct = progress_start

            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, text=True)
            start_t = time.time()

            while True:
                if process.poll() is not None:
                    break
                elapsed = time.time() - start_t
                if signals:
                    fraction = min(cap_fraction, elapsed / estimated_s)
                    pct = progress_start + int(progress_range * fraction)
                    if pct > last_pct:
                        signals.report_progress.emit(pct)
                        last_pct = pct
                time.sleep(poll_interval)

            stderr_text = process.stderr.read() if process.stderr else ''
            report_ffmpeg_stderr(stderr_text, "audio waveform",
                                 failure=process.returncode not in (0, None))

            if not os.path.isfile(output_path):
                logger.warning("Audio waveform: ffmpeg produced no output — skipping")
                return None

            with open(output_path, "rb") as f:
                b64 = b64encode(f.read()).decode("utf-8")

        logger.info("Audio waveform spacer completed")
        if signals:
            signals.report_progress.emit(progress_end)
        return b64

    except Exception as e:
        logger.warning(f"Audio waveform generation failed: {e}")
        return None


def find_qc_metadata(destination_directory):
    """
    Find QC metadata files including MediaConch policy.
    
    Returns:
        tuple: Paths to various metadata files and policy content
    """
    exiftool_output_path = None
    ffprobe_output_path = None
    mediainfo_output_path = None
    mediaconch_csv = None
    fixity_sidecar = None
    fixity_summary_json = None
    mediaconch_policy_content = None
    mediaconch_policy_name = None

    if os.path.isdir(destination_directory):
        for file in os.listdir(destination_directory):
            file_path = os.path.join(destination_directory, file)
            if os.path.isfile(file_path) and not file.startswith('.DS_Store'):
                if "_exiftool_output" in file:
                    exiftool_output_path = file_path
                if "_ffprobe_output" in file:
                    ffprobe_output_path = file_path
                if "_mediainfo_output" in file:
                    mediainfo_output_path = file_path
                if "_mediaconch_output" in file:
                    mediaconch_csv = file_path
                if "_fixity_summary" in file and file.endswith('.json'):
                    fixity_summary_json = file_path

    if os.path.isdir(os.path.dirname(destination_directory)):
        parent_dir = os.path.dirname(destination_directory) 
        for file in os.listdir(parent_dir):
            file_path = os.path.join(parent_dir, file)
            if file.endswith('fixity.txt'):
                fixity_sidecar = file_path

    # Get MediaConch policy file if MediaConch was run
    if mediaconch_csv:
        try:
            # Get policy file path from config manager
            policy_name = config_mgr.get_config('checks', ChecksConfig).tools.mediaconch.mediaconch_policy
            if policy_name:
                policy_path = config_mgr.get_policy_path(policy_name)
                if policy_path and os.path.isfile(policy_path):
                    mediaconch_policy_content = read_xml_file(policy_path)
                    mediaconch_policy_name = policy_name
                else:
                    logger.warning(f"MediaConch policy file not found: {policy_name}")
        except Exception as e:
            logger.error(f"Error retrieving MediaConch policy: {e}")

    return (exiftool_output_path, ffprobe_output_path, mediainfo_output_path,
            mediaconch_csv, fixity_sidecar, fixity_summary_json,
            mediaconch_policy_content, mediaconch_policy_name)


def make_fixity_section_html(fixity_summary_json, fixity_file_content):
    """
    Render the Fixity report section from the structured fixity summary JSON
    written by the fixity steps (checks/fixity_check.py, checks/embed_fixity.py).

    Falls back to the raw checksum sidecar content for runs that predate the
    summary file. Returns None when there is nothing to show.
    """
    summary = None
    if fixity_summary_json:
        try:
            with open(fixity_summary_json, 'r', encoding='utf-8') as f:
                summary = json.load(f)
            if not isinstance(summary, dict):
                summary = None
        except Exception as e:
            logger.warning(f"Could not read fixity summary {fixity_summary_json}: {e}")
            summary = None

    if not summary:
        if fixity_file_content:
            return f'<pre>{fixity_file_content}</pre>'
        return None

    whole_file = summary.get('whole_file')
    checksum_output = summary.get('checksum_output')
    stream_fixity = summary.get('stream_fixity')

    if not (whole_file or checksum_output or stream_fixity):
        if fixity_file_content:
            return f'<pre>{fixity_file_content}</pre>'
        return None

    hash_style = 'font-family: monospace; word-break: break-all;'

    def result_badge(result):
        if result == 'passed':
            return ('<span style="background-color: #d2ffed; color: #0a5738; border: 1px solid #0a5738; '
                    'border-radius: 4px; padding: 2px 10px; font-weight: bold;">&#10003; Passed</span>')
        return ('<span style="background-color: #ffbaba; color: #8b0000; border: 1px solid #8b0000; '
                'border-radius: 4px; padding: 2px 10px; font-weight: bold;">&#10007; Failed</span>')

    blocks = []

    # --- Whole-file fixity ---
    if whole_file:
        algorithm = (whole_file.get('algorithm') or '').upper()
        computed = whole_file.get('computed_checksum') or ''
        block = '<h4 style="margin-bottom: 6px;">Whole-file checksum</h4>'
        block += (
            f'<p>{result_badge(whole_file.get("result"))} '
            f'&nbsp;{algorithm} fixity check on {whole_file.get("validated_at", "unknown date")}</p>'
        )
        validated_against = whole_file.get('validated_against') or []
        if validated_against:
            rows = ''
            for entry in validated_against:
                stored = entry.get('checksum') or ''
                match = bool(computed) and stored == computed
                match_class = 'cell-match' if match else 'cell-mismatch'
                match_text = 'match' if match else 'MISMATCH'
                rows += (
                    '<tr>'
                    f'<td>{entry.get("file", "")}</td>'
                    f'<td>{entry.get("date", "")}</td>'
                    f'<td style="{hash_style}">{stored}</td>'
                    f'<td style="{hash_style}">{computed}</td>'
                    f'<td class="{match_class}">{match_text}</td>'
                    '</tr>'
                )
            block += (
                '<table>'
                '<tr><th>Checksum file</th><th>Created</th><th>Stored checksum</th><th>Computed checksum</th><th>Result</th></tr>'
                f'{rows}'
                '</table>'
            )
        else:
            block += (
                f'<p>Checksum computed from the video file: '
                f'<span style="{hash_style}">{computed}</span></p>'
            )
        if checksum_output:
            block += (
                f'<p style="font-size: 13px;">A new {(checksum_output.get("algorithm") or "").upper()} '
                f'checksum sidecar was also written this run: '
                f'{checksum_output.get("checksum_file", "")} ({checksum_output.get("created_at", "")})</p>'
            )
        blocks.append(block)
    elif checksum_output:
        algorithm = (checksum_output.get('algorithm') or '').upper()
        block = '<h4 style="margin-bottom: 6px;">Whole-file checksum</h4>'
        block += (
            f'<p>{algorithm} checksum created on {checksum_output.get("created_at", "unknown date")}</p>'
            '<table>'
            '<tr><th>Checksum file</th><th>Created</th><th>Checksum</th></tr>'
            '<tr>'
            f'<td>{checksum_output.get("checksum_file", "")}</td>'
            f'<td>{checksum_output.get("created_at", "")}</td>'
            f'<td style="{hash_style}">{checksum_output.get("checksum", "")}</td>'
            '</tr>'
            '</table>'
            '<p style="font-size: 13px;">No validation against a previously stored checksum was performed this run.</p>'
        )
        blocks.append(block)

    # --- Embedded stream fixity ---
    if stream_fixity:
        algorithm = (stream_fixity.get('algorithm') or '').upper()
        block = '<h4 style="margin-bottom: 6px;">Embedded stream fixity</h4>'
        block += (
            f'<p>{result_badge(stream_fixity.get("result"))} '
            f'&nbsp;{algorithm} stream hash validation on {stream_fixity.get("validated_at", "unknown date")}</p>'
        )
        rows = ''
        for stream_name in ('video', 'audio'):
            stream = stream_fixity.get(stream_name)
            if not stream:
                continue
            match = stream.get('match')
            match_class = 'cell-match' if match else 'cell-mismatch'
            match_text = 'match' if match else 'MISMATCH'
            rows += (
                '<tr>'
                f'<td>{stream_name.capitalize()}</td>'
                f'<td style="{hash_style}">{stream.get("embedded_hash", "")}</td>'
                f'<td style="{hash_style}">{stream.get("computed_hash", "")}</td>'
                f'<td class="{match_class}">{match_text}</td>'
                '</tr>'
            )
        block += (
            '<table>'
            '<tr><th>Stream</th><th>Embedded hash</th><th>Computed hash</th><th>Result</th></tr>'
            f'{rows}'
            '</table>'
        )
        blocks.append(block)

    section_body = ''.join(blocks)
    return (
        '<div style="background-color: #f5e9e3; border: 1px solid #4d2b12; border-radius: 4px; '
        'padding: 10px 30px 20px 30px; margin: 10px 0 20px 0;">'
        f'{section_body}'
        '</div>'
    )


def generate_thumbnail_for_failure(video_path, tag, tagValue, timestamp, profile_name, thumbPath):
    """
    Generates a thumbnail for a specific failure based on the summarized failures.
    
    Parameters:
        video_path (str): Path to the video file.
        tag (str): The tag that failed.
        tagValue (float): The value of the tag at failure.
        timestamp (str): Timestamp in HH:MM:SS.ssss format.
        profile_name (str): Name of the profile (e.g., 'tag_check', 'color_bars_evaluation').
        thumbPath (str): Directory to save the thumbnail.
    
    Returns:
        str: Path to the generated thumbnail, or None if generation failed.
    """
    if not os.path.isfile(video_path):
        logger.error(f"Video file not found: {video_path}")
        return None
    
    video_basename = os.path.basename(video_path)
    video_id = os.path.splitext(video_basename)[0]
    
    # Create filename matching existing convention
    outputFramePath = os.path.join(thumbPath, f"{video_id}.{profile_name}.{tag}.{tagValue}.{timestamp}.jpg")
    ffoutputFramePath = outputFramePath.replace(":", ".")

    # Windows drive letter fix
    match = re.search(r"[A-Z]\.\/", ffoutputFramePath)
    if match:
        ffoutputFramePath = ffoutputFramePath.replace(".", ":", 1)

    # Generate appropriate ffmpeg command based on tag
    if tag == "TOUT":
        ffmpegString = f'ffmpeg -ss {timestamp} -i "{video_path}" -vf signalstats=out=tout:color=yellow -vframes 1 -s 720x486 -q:v 3 -y "{ffoutputFramePath}"'
    elif tag == "VREP":
        ffmpegString = f'ffmpeg -ss {timestamp} -i "{video_path}" -vf signalstats=out=vrep:color=pink -vframes 1 -s 720x486 -q:v 3 -y "{ffoutputFramePath}"'
    else:
        ffmpegString = f'ffmpeg -ss {timestamp} -i "{video_path}" -vf signalstats=out=brng:color=cyan -vframes 1 -s 720x486 -q:v 3 -y "{ffoutputFramePath}"'
    
    try:
        logger.debug(f"Generating thumbnail for {tag} failure at {timestamp}\n")
        result = subprocess.run(ffmpegString, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)
        if result.returncode == 0 and os.path.isfile(ffoutputFramePath):
            return ffoutputFramePath
        else:
            logger.warning(f"Failed to generate {tag} thumbnail at {timestamp} (exit code {result.returncode})")
            report_ffmpeg_stderr(result.stderr, f"{tag} thumbnail", failure=True)
            return None
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
        return None


def summarize_failures(failure_csv_path):  # Change parameter to accept CSV file path
    """
    Summarizes the failure information from the CSV file, prioritizing tags 
    with the greatest difference between tag value and threshold, ensuring
    selected frames are at least 4 seconds apart.

    Args:
        failure_csv_path (str): The path to the CSV file containing failure details.

    Returns:
        dict: A dictionary with timestamps as keys and failure details as values.
    """
    
    def timestamp_to_seconds(timestamp_str):
        """Convert HH:MM:SS.ssss to total seconds"""
        parts = timestamp_str.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    
    def are_timestamps_far_enough(ts1, ts2, min_gap=4.0):
        """Check if two timestamps are at least min_gap seconds apart"""
        seconds1 = timestamp_to_seconds(ts1)
        seconds2 = timestamp_to_seconds(ts2)
        return abs(seconds1 - seconds2) >= min_gap
    
    failureInfo = {}
    # 0. Read the failure information from the CSV
    try:
        # Try UTF-8 first
        try:
            with open(failure_csv_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    timestamp = row['Timestamp']
                    if timestamp not in failureInfo:
                        failureInfo[timestamp] = []
                    failureInfo[timestamp].append({
                        'tag': row['Tag'],
                        'tagValue': float(row['Tag Value']),  # Convert to float
                        'over': float(row['Threshold'])     # Convert to float
                    })
        except UnicodeDecodeError:
            # If UTF-8 fails, try latin-1
            with open(failure_csv_path, 'r', encoding='latin-1') as csvfile:
                logger.warning(f"Used latin-1 encoding as fallback for CSV file {failure_csv_path}")
                reader = csv.DictReader(csvfile)
                for row in reader:
                    timestamp = row['Timestamp']
                    if timestamp not in failureInfo:
                        failureInfo[timestamp] = []
                    failureInfo[timestamp].append({
                        'tag': row['Tag'],
                        'tagValue': float(row['Tag Value']),  # Convert to float
                        'over': float(row['Threshold'])     # Convert to float
                    })
    except Exception as e:
        logger.error(f"Error reading failure CSV file {failure_csv_path}: {e}")
        return {}  # Return empty dictionary on error

    # 1. Collect all unique tags and count their occurrences
    tag_counts = {}
    for info_list in failureInfo.values():
        for info in info_list:
            tag = info['tag']
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # 2. Determine the maximum number of frames per tag
    num_tags = len(tag_counts)
    max_frames_per_tag = 5 if num_tags <= 2 else 3

    # 3. Flatten the failureInfo dictionary into a list of tuples
    all_failures = []
    for timestamp, info_list in failureInfo.items():
        for info in info_list:
            all_failures.append((timestamp, info))  # Store as (timestamp, info) tuples

    # 4. Sort the flattened list based on tag value difference
    all_failures.sort(key=lambda x: abs(x[1]['tagValue'] - x[1]['over']), reverse=True)

    # 5. Limit the number of frames per tag with 4-second spacing
    limited_failures = []
    selected_timestamps_per_tag = {}  # Track selected timestamps for each tag
    
    for timestamp, info in all_failures:
        tag = info['tag']
        
        # Initialize list for this tag if needed
        if tag not in selected_timestamps_per_tag:
            selected_timestamps_per_tag[tag] = []
        
        # Check if we've already selected enough frames for this tag
        if len(selected_timestamps_per_tag[tag]) >= max_frames_per_tag:
            continue
        
        # Check if this timestamp is far enough from all previously selected timestamps for this tag
        is_far_enough = True
        for selected_ts in selected_timestamps_per_tag[tag]:
            if not are_timestamps_far_enough(timestamp, selected_ts):
                is_far_enough = False
                break
        
        # If far enough from all selected timestamps, add it
        if is_far_enough:
            limited_failures.append((timestamp, info))
            selected_timestamps_per_tag[tag].append(timestamp)

    # 6. Group the limited failures back into a dictionary by timestamp
    summary_dict = {}
    for timestamp, info in limited_failures:
        if timestamp not in summary_dict:
            summary_dict[timestamp] = []
        summary_dict[timestamp].append(info)

    return summary_dict


def make_audio_clipping_html(audio_clipping_csv):
    """
    Generates an HTML section summarizing audio clipping detection results.

    Args:
        audio_clipping_csv (str): Path to the audio clipping CSV file.

    Returns:
        str: HTML string with audio clipping results, or None if file cannot be read.
    """
    if not audio_clipping_csv or not os.path.isfile(audio_clipping_csv):
        return None

    try:
        with open(audio_clipping_csv, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as e:
        logger.error(f"Error reading audio clipping CSV: {e}")
        return None

    if len(rows) < 8:
        return None

    # Parse summary rows
    threshold = rows[1][1] if len(rows[1]) > 1 else "N/A"
    total_frames = rows[2][1] if len(rows[2]) > 1 else "N/A"
    clipped_frames = rows[3][1] if len(rows[3]) > 1 else "N/A"
    clipped_pct = rows[4][1] if len(rows[4]) > 1 else "N/A"
    max_peak = rows[5][1] if len(rows[5]) > 1 else "N/A"
    max_flat_factor = rows[6][1] if len(rows[6]) > 1 else "N/A"
    clipping_detected = rows[7][1] if len(rows[7]) > 1 else "N/A"

    if clipping_detected == "Yes":
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = "Audio Clipping Detected"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Audio Clipping Detected"

    html = f'''
    <a id="link_clipping_methodology" href="javascript:void(0);"
       onclick="toggleContent('clipping_methodology', 'What is audio clipping detection? ▼', 'What is audio clipping detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is audio clipping detection? ▼</a>
    <div id="clipping_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Audio clipping detection</strong> scans the audio frames of the QCTools report to
            identify moments where the audio signal reaches or exceeds digital full scale, indicating
            that the original analog signal may have been too hot during digitization.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Metrics used:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>Peak Level (dBFS)</strong> &mdash; the peak sample
                value per audio frame, expressed in decibels relative to full scale. A value of 0.0 dBFS
                means the signal has hit the absolute digital maximum. Frames with a peak level at or above
                the configured threshold ({threshold} dBFS) are flagged as clipped.</li>
            <li style="margin-bottom: 4px;"><strong>Flat Factor</strong> &mdash; measures how many
                consecutive audio samples share the same value. When the signal clips, it is clamped at
                the digital ceiling, producing runs of identical samples. A Flat Factor of 1&ndash;10 is
                normal in any audio. Values above 100 at near-peak levels indicate sustained clipping where
                the waveform is being flattened for extended periods.</li>
        </ul>
        <p style="margin: 0;">
            Both metrics are derived from FFmpeg's <code>astats</code> filter as recorded in the QCTools
            report. Peak Level identifies <em>whether</em> clipping occurred; Flat Factor indicates
            <em>how severe</em> it is.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Threshold (dBFS)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{threshold}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Total Audio Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{total_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Clipped Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{clipped_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Clipped Frames (%)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{clipped_pct}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Max Peak Level (dBFS)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{max_peak}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Max Flat Factor</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{max_flat_factor}</td></tr>
    </table>
    '''

    # Add clipping events table if there are any
    clipping_events = [r for r in rows[9:] if len(r) >= 2]
    if clipping_events:
        html += f'''
        <a href="javascript:void(0);" onclick="toggleContent('audio_clipping_events', 'Show clipping events ({len(clipping_events)}) ▼', 'Hide clipping events ▲')" style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block;">Show clipping events ({len(clipping_events)}) ▼</a>
        <div id="audio_clipping_events" style="display: none;">
        <table style="border-collapse: collapse; margin: 10px 0;">
            <tr><th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Timestamp</th><th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Peak Level (dBFS)</th><th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Flat Factor</th></tr>
        '''
        for event in clipping_events:
            ff_val = event[2] if len(event) > 2 else "N/A"
            html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;">{event[0]}</td><td style="padding: 4px 12px; border: 1px solid #ddd;">{event[1]}</td><td style="padding: 4px 12px; border: 1px solid #ddd;">{ff_val}</td></tr>\n'
        html += '</table></div>\n'

    return html


def _imbalance_status_colors(characterization):
    """Return (text_color, bg_color, border_color) for an imbalance characterization."""
    if characterization in ("Balanced", "Mono (single channel)"):
        return "#155724", "#d4edda", "#c3e6cb"
    elif characterization in ("Slight imbalance",):
        return "#856404", "#fff3cd", "#ffeeba"
    elif characterization in ("Moderate imbalance",):
        return "#856404", "#fff3cd", "#ffeeba"
    else:
        return "#721c24", "#f8d7da", "#f5c6cb"


def make_channel_imbalance_html(channel_imbalance_csv):
    """
    Generates an HTML section summarizing channel imbalance analysis results.
    Handles mono, stereo, and multi-channel CSV formats.

    Args:
        channel_imbalance_csv (str): Path to the channel imbalance CSV file.

    Returns:
        str: HTML string with channel imbalance results, or None if file cannot be read.
    """
    if not channel_imbalance_csv or not os.path.isfile(channel_imbalance_csv):
        return None

    try:
        with open(channel_imbalance_csv, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as e:
        logger.error(f"Error reading channel imbalance CSV: {e}")
        return None

    if len(rows) < 3:
        return None

    # Build a lookup dict from the CSV rows for flexible parsing
    csv_data = {}
    for row in rows:
        if len(row) >= 2:
            csv_data[row[0]] = row[1]

    total_frames = csv_data.get("Total Audio Frames", "N/A")
    num_channels_str = csv_data.get("Number of Channels", "2")
    try:
        num_channels = int(num_channels_str)
    except (ValueError, TypeError):
        num_channels = 2
    frames_analyzed = csv_data.get("Frames Analyzed", "N/A")
    overall_characterization = csv_data.get("Overall Characterization", csv_data.get("Characterization", "N/A"))

    # Collect per-channel mean RMS values
    channel_means = {}
    for key, val in csv_data.items():
        match = re.match(r'Channel (\d+) Mean RMS \(dBFS\)', key)
        if match:
            channel_means[int(match.group(1))] = val

    silent_channels = csv_data.get("Silent Channels", "")

    status_color, status_bg, status_border = _imbalance_status_colors(overall_characterization)
    status_text = overall_characterization

    # For stereo, add louder channel info
    louder_channel = csv_data.get("Louder Channel")
    if louder_channel and louder_channel != "Neither":
        status_text += f" ({louder_channel} is louder)"

    if silent_channels:
        status_text += f" — silent channel(s) detected: {silent_channels}"

    # Methodology description adapts to channel count
    if num_channels == 1:
        methodology_text = """
            <strong>Channel imbalance analysis</strong> reports the average loudness of each audio channel
            across the entire audio program. This file has a single audio channel (mono), so no
            inter-channel comparison is performed.
        """
    else:
        methodology_text = """
            <strong>Channel imbalance analysis</strong> compares the average loudness of each audio channel
            across the entire audio program to characterize any level differences between channels.
        """

    html = f'''
    <a id="link_imbalance_methodology" href="javascript:void(0);"
       onclick="toggleContent('imbalance_methodology', 'What is channel imbalance analysis? ▼', 'What is channel imbalance analysis? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is channel imbalance analysis? ▼</a>
    <div id="imbalance_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            {methodology_text}
        </p>
        <p style="margin: 0 0 10px 0;">
            The analysis uses the <strong>RMS level</strong> (Root Mean Square, in dBFS) from each channel,
            as recorded per audio frame by FFmpeg's <code>astats</code> filter in the QCTools report. The
            mean RMS for each channel is computed across all audio frames, and the difference between each
            pair of channels is reported in decibels.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Characterization:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>Balanced</strong> &mdash; less than 1 dB difference between channels.</li>
            <li style="margin-bottom: 4px;"><strong>Slight imbalance</strong> &mdash; 1&ndash;3 dB difference. Common with analog sources and generally not a concern.</li>
            <li style="margin-bottom: 4px;"><strong>Moderate imbalance</strong> &mdash; 3&ndash;6 dB difference. May indicate a level calibration issue with the playback or capture equipment.</li>
            <li style="margin-bottom: 4px;"><strong>Significant imbalance</strong> &mdash; greater than 6 dB difference. Could indicate a hardware fault, bad cable, or a mono source recorded to only one channel.</li>
        </ul>
        <p style="margin: 0;">
            It is not uncommon for one channel to be somewhat louder than the other on analog source
            material. This analysis is informational &mdash; it characterizes the file rather than
            flagging an error.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Total Audio Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{total_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Number of Channels</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{num_channels}</td></tr>
    '''

    if num_channels > 1:
        html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Frames Analyzed</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{frames_analyzed}</td></tr>\n'

    for ch in sorted(channel_means.keys()):
        html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Channel {ch} Mean RMS (dBFS)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{channel_means[ch]}</td></tr>\n'

    if silent_channels:
        html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Silent Channels</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd; color: #dc3545;"><strong>{silent_channels}</strong></td></tr>\n'

    # For stereo, show simple difference and louder channel
    if num_channels == 2:
        mean_diff = csv_data.get("Mean Difference (dB)", "N/A")
        louder = csv_data.get("Louder Channel", "N/A")
        html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Mean Difference (dB)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{mean_diff}</td></tr>\n'
        html += f'<tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Louder Channel</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{louder}</td></tr>\n'

    html += '</table>\n'

    # For >2 channels, add pairwise comparison table
    if num_channels > 2:
        # Find pairwise rows in CSV (look for "Pairwise Comparisons" header followed by data rows)
        pairwise_rows = []
        in_pairwise = False
        for row in rows:
            if len(row) >= 1 and row[0] == "Pairwise Comparisons":
                in_pairwise = True
                continue
            if in_pairwise:
                if len(row) >= 5 and row[0].startswith("Channel"):
                    pairwise_rows.append(row)
                elif len(row) >= 5 and row[0] == "Channel A":
                    continue  # skip header row
                elif len(row) == 0 or (len(row) == 1 and row[0] == ""):
                    break  # end of pairwise section

        if pairwise_rows:
            html += '''
            <h4 style="margin-top: 16px;">Pairwise Comparisons</h4>
            <table style="border-collapse: collapse; margin: 10px 0;">
                <tr>
                    <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel A</th>
                    <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel B</th>
                    <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Mean Diff (dB)</th>
                    <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Characterization</th>
                    <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Louder Channel</th>
                </tr>
            '''
            for row in pairwise_rows:
                html += f'''<tr>
                    <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[0]}</td>
                    <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[1]}</td>
                    <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[2]}</td>
                    <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[3]}</td>
                    <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[4]}</td>
                </tr>\n'''
            html += '</table>\n'

    return html


def _tc_consensus_channel_summary(consensus_rows):
    """Aggregate the per-region channel labels (column index 3, already
    human-readable: 'Channel 1', 'Both channels', 'Not channel-specific
    (mix-based)') into one top-level 'Timecode Channel(s)' label. If regions
    disagree on a single channel, or any region spans both, report both
    channels; if only mix-based regions exist, say so."""
    labels = {r[3].strip() for r in consensus_rows if len(r) > 3 and r[3].strip()}
    specific = {l for l in labels if l.startswith("Channel")}
    if any("Both" in l for l in labels) or len(specific) > 1:
        return "Both channels"
    if len(specific) == 1:
        return next(iter(specific))
    return "Not channel-specific (mix-based)"


def make_audible_timecode_html(audible_timecode_csv):
    """
    Generates an HTML section summarizing audible timecode detection results.

    Args:
        audible_timecode_csv (str): Path to the audible timecode CSV file.

    Returns:
        str: HTML string with audible timecode results, or None if file cannot be read.
    """
    if not audible_timecode_csv or not os.path.isfile(audible_timecode_csv):
        return None

    try:
        with open(audible_timecode_csv, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as e:
        logger.error(f"Error reading audible timecode CSV: {e}")
        return None

    if len(rows) < 6:
        return None

    # Parse summary rows
    metric_type = rows[1][1] if len(rows[1]) > 1 else "N/A"
    metric_type = {
        "both": "both R128 and astats",
        "r128": "R128",
        "astats": "astats",
    }.get(metric_type.strip().lower(), metric_type)
    duration = rows[3][1] if len(rows[3]) > 1 else "N/A"
    tc_detected = rows[4][1] if len(rows[4]) > 1 else "No"

    # Read the consensus regions table — the header row carries "Detection
    # Methods" as its last column, distinguishing it from the per-method
    # forensic section that follows. Collect rows up to the next blank line.
    consensus_rows = []
    header_idx = None
    for i, r in enumerate(rows):
        if r and r[0] == "Start Time" and len(r) >= 6 and r[5] == "Detection Methods":
            header_idx = i
            break
    if header_idx is not None:
        for r in rows[header_idx + 1:]:
            if not r or not r[0].strip() or r[0] == "Per-Method Detections":
                break
            if len(r) >= 6:
                consensus_rows.append(r)

    tc_channel = _tc_consensus_channel_summary(consensus_rows) if consensus_rows else "—"

    if tc_detected == "Yes":
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = "Audible Timecode Detected"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Audible Timecode Detected"

    html = f'''
    <a id="link_timecode_methodology" href="javascript:void(0);"
       onclick="toggleContent('timecode_methodology', 'What is audible timecode detection? ▼', 'What is audible timecode detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is audible timecode detection? ▼</a>
    <div id="timecode_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Audible timecode detection</strong> scans the audio frames of the QCTools report to
            identify the presence of Linear Timecode (LTC) artifacts &mdash; a biphase-modulated square
            wave (~2400 Hz for 30fps NTSC) that was recorded on an audio track during the original
            production or dubbing process.
        </p>
        <p style="margin: 0 0 10px 0;">
            The analysis uses rolling windows over the audio measurements to detect the characteristic
            statistical fingerprint of LTC: steady RMS level, low crest factor (square wave), narrow
            dynamic range, and a zero-crossing rate consistent with the LTC carrier frequency.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Detection criteria:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>Stable mix at TC level</strong> &mdash; mix-loudness sits steadily at LTC level with narrow dynamic range. R128 measures the channel mix, not individual channels, so this fires for both-channels-TC <em>and</em> one-channel-TC-plus-quiet-channel; check the astats rows to see which channels actually carry timecode.</li>
            <li style="margin-bottom: 4px;"><strong>TC + silence</strong> &mdash; one channel carries timecode while the other is near-silent (large gap between momentary and integrated loudness).</li>
            <li style="margin-bottom: 4px;"><strong>TC + program audio</strong> &mdash; timecode is present alongside program audio on separate channels (divergence between M and S loudness, high M variance).</li>
        </ul>
        <p style="margin: 10px 0 6px 0; font-weight: bold;">Reading the <em>Details</em> column:</p>
        <p style="margin: 0 0 6px 0;">
            Each detection lists the measurements that landed in the expected ranges for LTC.
            "Stability +/- X" is the standard deviation across the rolling window &mdash; lower means
            the value held steady, which is itself a signature of timecode.
        </p>
        <table style="border-collapse: collapse; margin: 6px 0 10px 0; font-size: 12px;">
            <tr><th style="padding: 4px 8px; border: 1px solid #ddd; background-color: #efe9e0; text-align: left;">Metric</th><th style="padding: 4px 8px; border: 1px solid #ddd; background-color: #efe9e0; text-align: left;">What it measures</th><th style="padding: 4px 8px; border: 1px solid #ddd; background-color: #efe9e0; text-align: left;">TC vs non-TC ranges</th></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Loudness stability</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">Window-to-window variation in EBU R128 momentary loudness (M).</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: &lt; 2 dB (often &lt; 0.5). Program audio: many dB. Silence: very low but at -70+ LUFS.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Avg loudness</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">Mean R128 momentary loudness across the window, in LUFS.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: roughly -16 to -22 LUFS on broadcast tapes. Silence: &lt; -60 LUFS.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Loudness range</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">EBU R128 LRA &mdash; spread of loudness across the signal so far, in LU.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: low (&lt; ~8). Music/dialogue: 10-25+. LRA is cumulative from file start, so it stays inflated for ~30-60 s after a silence-to-TC transition.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Momentary vs integrated gap</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">|M - I|: how far the current loudness sits above the program-integrated loudness. Big gap means one channel is much louder than the other.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">TC + silence: &gt; 20 LU. Both channels active: small.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Momentary vs short-term gap</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">|M - S|: divergence between momentary and short-term loudness. Marks the channel asymmetry of TC sharing a mix with program audio.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">TC + program audio: &gt; 1.5 LU, often &gt; 2.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Loud-side level</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">R128 LRA.high: the top of the loudness distribution. Used to pin down the TC level when the other channel drags the mix down.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: -10 to -30 LUFS, stable +/- &lt; 1.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Signal level</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">Per-channel RMS in dBFS, from ffmpeg <code>astats</code>.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: -10 to -30 dBFS. Silence: &lt; -55. Loud program: &gt; -10.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Crest factor</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">Ratio of peak to RMS. Square-wave-like signals have a low crest factor; sparse signals have a high one.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC biphase: ~1.6-2.2. 1 kHz sine tone: ~1.4. Speech/music: ~3+.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Zero-crossing rate</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">Fraction of samples per frame where the waveform crosses zero. A proxy for dominant frequency.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC carrier: ~0.05-0.10. Sub-kHz tone: much lower. Silence/noise: erratic, often &gt; 0.4.</td></tr>
            <tr><td style="padding: 4px 8px; border: 1px solid #ddd;"><strong>Spectral entropy</strong></td><td style="padding: 4px 8px; border: 1px solid #ddd;">How spread-out the frame's frequency content is.</td><td style="padding: 4px 8px; border: 1px solid #ddd;">LTC: ~0.6-0.8 (broadband biphase). Pure tone: ~0.3. Silence: ~0.3.</td></tr>
        </table>
        <p style="margin: 0;">
            Detections must persist across multiple consecutive windows to be reported, reducing
            false positives from transient audio events. The same stretch of timecode usually
            triggers several methods at once, so the overlapping detections are collapsed into the
            <strong>consensus regions</strong> shown below &mdash; one row per contiguous span of
            audible timecode. The per-channel <code>astats</code> boundaries are authoritative
            (they resolve true breaks where the TC carrier stutters or one channel drops out, which
            the mix-based R128 meter smooths over); R128 corroborates. When the QCTools report
            carries per-channel <code>astats</code> data, an R128 detection that <code>astats</code>
            never corroborated is discarded &mdash; loudness statistics alone can't tell LTC from
            other steady-loudness audio (e.g. heavily compressed music). The <em>Detection Methods</em>
            column lists which methods agreed on each region.
        </p>
        <p style="margin: 10px 0 0 0;">
            Region <strong>start and end positions are shown as non-drop-frame timecode</strong>
            (<code>HH:MM:SS:FF</code>) at the video frame rate, so they match what an NLE displays;
            the <em>Duration</em> column is elapsed time. (NDF timecode runs ~0.1% behind wall-clock
            time, so it reads a couple of seconds lower than elapsed seconds late in a long file.)
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Timecode Channel(s)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{tc_channel}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Duration</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{duration}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Metric Type</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{metric_type}</td></tr>
    </table>
    '''

    # Add the consensus regions table if there are any. These are the merged
    # regions agreed on across detection methods — the per-method detections
    # they were built from are kept in the CSV but intentionally not shown.
    if consensus_rows:
        def conf_badge(conf):
            c = (conf or "").strip().lower()
            color = {"high": "#0a5f1c", "medium": "#b8860b"}.get(c, "#666")
            label = c.capitalize() if c else "—"
            return (
                f'<span style="display: inline-block; padding: 2px 10px; border-radius: 10px; '
                f'color: #fff; background-color: {color}; font-size: 12px;">{label}</span>'
            )

        # Plain-language summary: region count, overall span, highest confidence.
        # Rows are sorted by start, so the span runs from the first start to the
        # last end — shown verbatim as the NDF timecodes from the CSV.
        conf_rank = {"high": 3, "medium": 2, "low": 1}
        confidences = [(r[4] or "").strip().lower() for r in consensus_rows]
        top = max(confidences, key=lambda c: conf_rank.get(c, 0)) if confidences else ""
        top_conf = top.capitalize() if top else "N/A"
        span = f"{consensus_rows[0][0]}–{consensus_rows[-1][1]}"
        summary_text = (f"Audible timecode detected in {len(consensus_rows)} region(s), "
                        f"{span}; highest confidence: {top_conf}.")
        html += (
            f'<p style="margin: 10px 0; padding: 10px 14px; background-color: #f5e9e3; '
            f'border-radius: 4px; color: #4d2b12;">{summary_text}</p>'
        )

        html += '''
        <table style="border-collapse: collapse; margin: 10px 0;">
            <tr>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Start Time</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">End Time</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Duration</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Confidence</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Detection Methods</th>
            </tr>
        '''
        for row in consensus_rows:
            html += f'''<tr>
                <td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{row[0]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{row[1]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace; text-align: right;">{row[2]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[3]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{conf_badge(row[4])}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{row[5]}</td>
            </tr>\n'''
        html += '</table>\n'

    return html


def make_audio_dropout_html(audio_dropout_csv):
    """
    Generates an HTML section summarizing audio dropout detection results.

    Args:
        audio_dropout_csv (str): Path to the audio dropout CSV file.

    Returns:
        str: HTML string with audio dropout results, or None if file cannot be read.
    """
    if not audio_dropout_csv or not os.path.isfile(audio_dropout_csv):
        return None

    try:
        with open(audio_dropout_csv, 'r') as f:
            reader = csv.reader(f)
            rows = list(reader)
    except Exception as e:
        logger.error(f"Error reading audio dropout CSV: {e}")
        return None

    if len(rows) < 8:
        return None

    # Parse summary rows
    window_size = rows[1][1] if len(rows[1]) > 1 else "N/A"
    rms_threshold = rows[2][1] if len(rows[2]) > 1 else "N/A"
    silence_floor = rows[3][1] if len(rows[3]) > 1 else "N/A"
    total_frames = rows[4][1] if len(rows[4]) > 1 else "N/A"
    events_detected = rows[5][1] if len(rows[5]) > 1 else "N/A"
    frames_flagged = rows[6][1] if len(rows[6]) > 1 else "N/A"
    dropout_detected = rows[7][1] if len(rows[7]) > 1 else "N/A"

    if dropout_detected == "Yes":
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = "Audio Dropout Detected"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Audio Dropout Detected"

    html = f'''
    <a id="link_dropout_methodology" href="javascript:void(0);"
       onclick="toggleContent('dropout_methodology', 'What is audio dropout detection? ▼', 'What is audio dropout detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is audio dropout detection? ▼</a>
    <div id="dropout_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Audio dropout detection</strong> identifies moments where the audio signal level
            drops suddenly and significantly, which is characteristic of tape dropout during analog
            playback. A rolling window of audio frames is used to establish a local baseline, and
            frames that fall far below that baseline are flagged.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Metrics used:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>RMS Level (dBFS)</strong> &mdash; the primary trigger.
                A frame is flagged when its RMS level drops more than {rms_threshold} dB below the rolling
                median of the preceding {window_size} frames. Frames where the median itself is below
                {silence_floor} dBFS are ignored to avoid false positives in naturally quiet content.</li>
            <li style="margin-bottom: 4px;"><strong>Max Difference</strong> &mdash; the maximum sample-to-sample
                jump within a frame. A spike above 2x the rolling median suggests a click or discontinuity
                at the dropout boundary.</li>
            <li style="margin-bottom: 4px;"><strong>RMS Difference</strong> &mdash; the RMS of sample-to-sample
                differences. A spike corroborates signal discontinuity.</li>
            <li style="margin-bottom: 4px;"><strong>Zero Crossings Rate</strong> &mdash; the proportion of
                sign changes in the audio signal. Very low values indicate silence; very high values may
                indicate noise bursts.</li>
        </ul>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Confidence levels:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>High</strong> &mdash; RMS drop plus two or more corroborating metrics.</li>
            <li style="margin-bottom: 4px;"><strong>Medium</strong> &mdash; RMS drop plus one corroborating metric.</li>
            <li style="margin-bottom: 4px;"><strong>Low</strong> &mdash; RMS drop only, no corroboration.</li>
        </ul>
        <p style="margin: 0;">
            All metrics are derived from FFmpeg's <code>astats</code> filter as recorded in the QCTools
            report. Detection is performed per audio channel to catch single-channel dropouts.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Total Audio Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{total_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Dropout Events</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{events_detected}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Frames Flagged</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{frames_flagged}</td></tr>
    </table>
    '''

    # Add dropout events table if there are any
    dropout_events = [r for r in rows[9:] if len(r) >= 7 and r[0] != "Timestamp Start"]
    if dropout_events:
        confidence_colors = {
            'high': '#dc3545',
            'medium': '#fd7e14',
            'low': '#ffc107',
        }
        html += f'''
        <a href="javascript:void(0);" onclick="toggleContent('audio_dropout_events', 'Show dropout events ({len(dropout_events)}) ▼', 'Hide dropout events ▲')" style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block;">Show dropout events ({len(dropout_events)}) ▼</a>
        <div id="audio_dropout_events" style="display: none;">
        <table style="border-collapse: collapse; margin: 10px 0;">
            <tr>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Timestamp Start</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Timestamp End</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Worst RMS (dBFS)</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Median RMS (dBFS)</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Drop (dB)</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Confidence</th>
                <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Corroborating Metrics</th>
            </tr>
        '''
        for event in dropout_events:
            conf = event[6].strip().lower() if len(event) > 6 else 'low'
            conf_color = confidence_colors.get(conf, '#ffc107')
            corr = event[7] if len(event) > 7 else ""
            html += f'''<tr>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[0]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[1]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[2]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[3]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[4]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{event[5]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd; color: {conf_color}; font-weight: bold;">{event[6]}</td>
                <td style="padding: 4px 12px; border: 1px solid #ddd;">{corr}</td>
            </tr>\n'''
        html += '</table></div>\n'

    return html


def make_clamped_levels_html(clamped_levels_csv, clamped_traces_csv=None):
    """
    Generates an HTML section summarizing clamped-levels detection results.
    Always renders when the CSV is present, including when no clamping was
    found — so the report shows that the check was run.

    Args:
        clamped_levels_csv (str): Path to the clamped-levels summary CSV.
        clamped_traces_csv (str): Optional path to per-frame trace CSV; when
            present, one plotly graph is rendered per clamped channel/direction.

    Returns:
        str: HTML string, or None if the file cannot be read.
    """
    if not clamped_levels_csv or not os.path.isfile(clamped_levels_csv):
        return None

    try:
        with open(clamped_levels_csv, 'r') as f:
            rows = list(csv.reader(f))
    except Exception as e:
        logger.error(f"Error reading clamped levels CSV: {e}")
        return None

    if len(rows) < 9:
        return None

    bit_depth = rows[1][1] if len(rows[1]) > 1 else "N/A"
    total_frames = rows[2][1] if len(rows[2]) > 1 else "N/A"
    tolerance = rows[3][1] if len(rows[3]) > 1 else "0"
    any_clamp = rows[6][1] if len(rows[6]) > 1 else "N/A"

    # Findings table starts at row 8 (header) then rows 9+
    findings = [r for r in rows[9:] if len(r) >= 8]

    if any_clamp == "Yes":
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = "Clamped Levels Detected"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Clamped Levels Detected"

    def verdict_color(verdict):
        if verdict == "Clamped":
            return "#dc3545"
        if verdict == "Not Clamped":
            return "#155724"
        return "#856404"  # Inconclusive

    rows_html = ""
    for r in findings:
        channel, direction, limit, extreme, hits, hit_pct, beyond, verdict = r[:8]
        v_color = verdict_color(verdict)
        rows_html += (
            f'<tr>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{channel}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{direction}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{limit}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{extreme}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{hits}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{hit_pct}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{beyond}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; color: {v_color}; font-weight: bold;">{verdict}</td>'
            f'</tr>\n'
        )

    graphs_html = _make_clamped_traces_graphs(clamped_traces_csv, bit_depth)

    html = f'''
    <a id="link_clamp_methodology" href="javascript:void(0);"
       onclick="toggleContent('clamp_methodology', 'What is clamped-levels detection? ▼', 'What is clamped-levels detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is clamped-levels detection? ▼</a>
    <div id="clamp_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Clamped-levels detection</strong> flags analog-to-digital converters that truncate
            the video signal at the broadcast (legal) range limits. A clamped channel will pile up at
            (or just inside) the limit value and never exceed it, whereas an unclamped source will show
            excursions past the legal range caused by sync pulses, noise, or peak whites/superblacks.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Verdicts:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>Clamped</strong> &mdash; enough frames sit at or near
                the broadcast limit (within the bit-depth tolerance) with zero excursions past it;
                indicates the ADC is truncating the signal.</li>
            <li style="margin-bottom: 4px;"><strong>Not Clamped</strong> &mdash; one or more frames went
                past the limit; the signal is free to exceed broadcast range.</li>
            <li style="margin-bottom: 4px;"><strong>Inconclusive</strong> &mdash; the signal never reached
                the limit, so clamping cannot be determined from this content.</li>
        </ul>
        <p style="margin: 0;">
            Limits are derived from SMPTE broadcast-range values (bit-depth aware): 10-bit Y 64&ndash;940,
            U/V 64&ndash;960; 8-bit Y 16&ndash;235, U/V 16&ndash;240. The tolerance window scales with
            bit depth (8-bit: exact match required; 10-bit: &plusmn;2 codes). Measurements come from FFmpeg's
            <code>signalstats</code> filter as recorded in the QCTools report.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Bit Depth</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{bit_depth}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Total Video Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{total_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Tolerance Window (codes)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{tolerance}</td></tr>
    </table>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Direction</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Limit</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Global Extreme</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Frames at/near Limit</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Hit %</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Frames Beyond Limit</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Verdict</th>
        </tr>
        {rows_html}
    </table>
    {graphs_html}
    '''
    return html


def _make_clamped_traces_graphs(clamped_traces_csv, bit_depth):
    """
    Render one plotly graph per clamped channel/direction from the trace CSV.
    Each graph shows the per-frame extreme value over time with a dashed line
    at the broadcast limit and shaded tolerance window. Returns "" when no
    trace CSV is present (no clamps, or feature disabled).
    """
    if not clamped_traces_csv or not os.path.isfile(clamped_traces_csv):
        return ""

    try:
        import plotly.graph_objs as go
    except ImportError as e:
        logger.warning(f"plotly unavailable; skipping clamped-levels graphs: {e}")
        return ""

    try:
        with open(clamped_traces_csv, 'r') as f:
            reader = csv.DictReader(f)
            traces = {}
            for row in reader:
                key = (row['channel'], row['direction'])
                if key not in traces:
                    traces[key] = {'limit': float(row['limit']), 'time': [], 'value': []}
                try:
                    traces[key]['time'].append(float(row['time_seconds']))
                    traces[key]['value'].append(float(row['value']))
                except ValueError:
                    continue
    except Exception as e:
        logger.error(f"Error reading clamped traces CSV: {e}")
        return ""

    if not traces:
        return ""

    try:
        bit_depth_int = int(bit_depth)
    except (TypeError, ValueError):
        bit_depth_int = 8
    code_max = 1023 if bit_depth_int == 10 else 255
    tolerance = 2 if bit_depth_int == 10 else 0

    graphs = []
    for (channel, direction), t in traces.items():
        limit = t['limit']
        metric_name = f"{channel}{'MIN' if direction == 'floor' else 'MAX'}"
        title = f"{metric_name} over time — broadcast {direction} = {limit:g} ({bit_depth_int}-bit)"

        if direction == 'floor':
            shaded_y0, shaded_y1 = 0, limit
            tol_y0, tol_y1 = limit, min(limit + tolerance, code_max)
            extreme_value = min(t['value']) if t['value'] else limit
            y_lo = max(0, min(extreme_value, limit) - 8)
            y_hi = min(code_max, limit + max(20, tolerance + 10))
        else:
            shaded_y0, shaded_y1 = limit, code_max
            tol_y0, tol_y1 = max(0, limit - tolerance), limit
            extreme_value = max(t['value']) if t['value'] else limit
            y_hi = min(code_max, max(extreme_value, limit) + 8)
            y_lo = max(0, limit - max(20, tolerance + 10))

        x_min = t['time'][0] if t['time'] else 0
        x_max = t['time'][-1] if t['time'] else 1

        fig = go.Figure()

        fig.add_shape(
            type='rect', xref='x', yref='y',
            x0=x_min, x1=x_max, y0=shaded_y0, y1=shaded_y1,
            fillcolor='rgba(220, 53, 69, 0.08)', line=dict(width=0), layer='below'
        )
        if tolerance > 0:
            fig.add_shape(
                type='rect', xref='x', yref='y',
                x0=x_min, x1=x_max, y0=tol_y0, y1=tol_y1,
                fillcolor='rgba(255, 193, 7, 0.18)', line=dict(width=0), layer='below'
            )

        fig.add_trace(go.Scattergl(
            x=t['time'], y=t['value'], mode='lines',
            name=metric_name, line=dict(color='#378d6a', width=1),
            hovertemplate='%{x:.2f}s<br>' + metric_name + '=%{y}<extra></extra>',
        ))

        fig.add_hline(
            y=limit, line=dict(color='#dc3545', width=2, dash='dash'),
            annotation_text=f"Broadcast {direction}: {limit:g}",
            annotation_position='top right',
            annotation_font=dict(color='#dc3545', size=11),
        )

        fig.update_layout(
            title=dict(text=title, font=dict(size=14)),
            xaxis_title='Time (seconds)',
            yaxis_title=f'{metric_name} (code value)',
            yaxis=dict(range=[y_lo, y_hi]),
            margin=dict(l=60, r=20, t=50, b=50),
            height=320,
            showlegend=False,
            plot_bgcolor='#fafafa',
        )

        config = {
            'toImageButtonOptions': {
                'format': 'png',
                'filename': f'clamped_{channel}_{direction}',
                'height': 400, 'width': 900, 'scale': 1,
            }
        }
        graphs.append(fig.to_html(full_html=False, include_plotlyjs='cdn', config=config))

    if not graphs:
        return ""

    return (
        '<h4 style="margin-top: 20px;">Clamped Channel Traces</h4>'
        '<p style="font-size: 13px; color: #555; margin: 0 0 10px 0;">'
        'Per-frame extreme over time for each channel/direction flagged as clamped. '
        'The dashed red line marks the broadcast limit; the shaded red region is past the limit '
        '(no excursions = a wall). Long files are downsampled to ~5,000 points per trace; '
        'each point is the per-bucket extreme so brief wall-hit events are preserved.'
        '</p>'
        + '\n'.join(f'<div style="margin: 10px 0;">{g}</div>' for g in graphs)
    )


def _hue_label(hue_deg):
    """Map a hue angle (degrees) to a short colour name for the report.
    Based on the YUV/signalstats hue convention as displayed in QCTools."""
    try:
        h = float(hue_deg) % 360
    except (TypeError, ValueError):
        return ""
    # 12-segment wheel, centred on canonical hue angles
    bins = [
        (15, "red"), (45, "orange"), (75, "yellow"),
        (105, "yellow-green"), (135, "green"), (165, "green-cyan"),
        (195, "cyan"), (225, "blue-cyan"), (255, "blue"),
        (285, "blue-magenta"), (315, "magenta"), (345, "red-magenta"),
        (360, "red"),
    ]
    for boundary, label in bins:
        if h < boundary:
            return label
    return ""


def make_chroma_phase_html(chroma_phase_summary_csv, chroma_phase_events_csv):
    """
    Generates an HTML section summarizing chroma phase error detection results.
    Always renders when the summary CSV is present, including when no events
    were found - so the report shows that the check was run.

    Args:
        chroma_phase_summary_csv (str): Path to the summary CSV.
        chroma_phase_events_csv (str): Path to the events CSV (per-event rows).

    Returns:
        str: HTML string, or None if the summary CSV cannot be read.
    """
    if not chroma_phase_summary_csv or not os.path.isfile(chroma_phase_summary_csv):
        return None

    try:
        with open(chroma_phase_summary_csv, 'r') as f:
            summary_rows = list(csv.reader(f))
    except Exception as e:
        logger.error(f"Error reading chroma phase summary CSV: {e}")
        return None

    summary = {}
    for r in summary_rows:
        if len(r) >= 2:
            summary[r[0]] = r[1]

    bit_depth = summary.get("Bit Depth", "N/A")
    total_frames = summary.get("Total Video Frames", "N/A")
    bars_skipped = summary.get("Frames Skipped (color bars region)", "0")
    flagged_frames = summary.get("Flagged Frames", "0")
    flagged_pct = summary.get("Flagged %", "0")
    event_count = summary.get("Detected Events", "0")
    env_low = summary.get("Envelope Low (U/V min)", "")
    env_high = summary.get("Envelope High (U/V max)", "")
    satmax_high = summary.get("SATMAX High", "")

    try:
        event_count_int = int(event_count)
    except ValueError:
        event_count_int = 0

    events = []
    if chroma_phase_events_csv and os.path.isfile(chroma_phase_events_csv):
        try:
            with open(chroma_phase_events_csv, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 9:
                        events.append(row[:9])
        except Exception as e:
            logger.error(f"Error reading chroma phase events CSV: {e}")

    if event_count_int > 0:
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = f"Chroma Phase Errors Detected: {event_count_int} event(s), {flagged_frames} flagged frame(s)"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Chroma Phase Errors Detected"

    rows_html = ""
    for r in events:
        event_idx, start, end, dur, frames, rule, peak_sat, peak_time, hue = r
        hue_name = _hue_label(hue)
        hue_display = f"{hue}" + (f" ({hue_name})" if hue_name else "")
        rows_html += (
            f'<tr>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{event_idx}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{start}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{end}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{dur}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{frames}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{rule}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{peak_sat}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{peak_time}</td>'
            f'<td style="padding: 4px 12px; border: 1px solid #ddd;">{hue_display}</td>'
            f'</tr>\n'
        )

    events_table = ""
    if events:
        events_table = f'''
    <a href="javascript:void(0);" onclick="toggleContent('chroma_phase_events', 'Show chroma phase events ({len(events)}) ▼', 'Hide chroma phase events ▲')" style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block;">Show chroma phase events ({len(events)}) ▼</a>
    <div id="chroma_phase_events" style="display: none;">
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Event</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Start</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">End</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Duration (s)</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Frames</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Rule</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Peak SATMAX</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Peak Time</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Hue at Peak (deg)</th>
        </tr>
        {rows_html}
    </table>
    </div>'''

    # Thumbnail gallery: chroma_phase analyzer writes PNGs into
    # ChromaPhaseThumbs/ for the top events by frame count (max
    # CHROMA_MAX_THUMBS = 10). Embed them inline as base64.
    thumbs_html = _make_chroma_phase_thumbs_html(chroma_phase_summary_csv, events)

    html = f'''
    <a id="link_chroma_phase_methodology" href="javascript:void(0);"
       onclick="toggleContent('chroma_phase_methodology', 'What is chroma phase detection? ▼', 'What is chroma phase detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is chroma phase detection? ▼</a>
    <div id="chroma_phase_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Chroma phase detection</strong> flags frames where the chroma signal has collapsed
            toward a single hue (typically cyan or magenta), usually caused by helical-scan tracking
            failures on tape source. The artifact is often accompanied by horizontal image displacement
            and brief picture "swerve" at onset.
        </p>
        <p style="margin: 0 0 10px 0; font-weight: bold;">Two flagging rules:</p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>envelope</strong> &mdash; within a single frame, both
                U and V span nearly the full chroma range (UMIN &lt; {env_low}, UMAX &gt; {env_high},
                VMIN &lt; {env_low}, VMAX &gt; {env_high} for 10-bit; scaled /4 for 8-bit).
                This is the strongest single-frame signature.</li>
            <li style="margin-bottom: 4px;"><strong>satmax</strong> &mdash; SATMAX exceeds {satmax_high}
                (10-bit) or its 8-bit equivalent. Catches partial events where only a portion of the
                frame is affected.</li>
        </ul>
        <p style="margin: 0 0 10px 0;">
            Consecutive flagged frames within ~10 frames are merged into a single event. Events shorter
            than 2 flagged frames are suppressed to filter isolated transients (scene cuts, motion blur
            into saturated content) which would otherwise be false positives.
        </p>
        <p style="margin: 0;">
            Color bars at the head of the tape (when detected by qct-parse) are skipped automatically.
            Measurements come from FFmpeg's <code>signalstats</code> filter as recorded in the QCTools
            report. Hue values are the median hue (HUEMED) at the event's peak SATMAX frame; ~180&deg;
            is cyan, ~315&deg; magenta.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Bit Depth</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{bit_depth}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Total Video Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{total_frames}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Frames Skipped (color bars)</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{bars_skipped}</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Flagged Frames</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{flagged_frames} ({flagged_pct}%)</td></tr>
        <tr><td style="padding: 4px 12px; border: 1px solid #ddd;"><strong>Detected Events</strong></td><td style="padding: 4px 12px; border: 1px solid #ddd;">{event_count}</td></tr>
    </table>
    {thumbs_html}
    {events_table}
    '''
    return html


def _make_chroma_phase_thumbs_html(chroma_phase_summary_csv, events):
    """
    Render the chroma-phase thumbnail gallery as base64-embedded <img> tags.
    Pulls PNGs from {report_dir}/ChromaPhaseThumbs/event_NN.png where NN is
    the event index (1-based). Returns "" when no thumbnails are present.

    Args:
        chroma_phase_summary_csv (str): Path to the summary CSV (used only to
            derive the sibling ChromaPhaseThumbs/ directory).
        events (list): The parsed event rows from the events CSV.

    Returns:
        str: HTML string, or "" if no thumbnails are present.
    """
    if not chroma_phase_summary_csv or not events:
        return ""

    report_dir = os.path.dirname(chroma_phase_summary_csv)
    thumb_dir = os.path.join(report_dir, "ChromaPhaseThumbs")
    if not os.path.isdir(thumb_dir):
        return ""

    # Build a quick lookup of event_idx -> event row, then iterate in
    # event-index order so the gallery's reading order matches the table.
    by_idx = {}
    for r in events:
        try:
            idx = int(r[0])
            by_idx[idx] = r
        except (ValueError, IndexError):
            continue

    items_html = ""
    found = 0
    for idx in sorted(by_idx.keys()):
        thumb_path = os.path.join(thumb_dir, f"event_{idx:02d}.png")
        if not os.path.isfile(thumb_path):
            continue
        data_uri = image_file_to_jpeg_data_uri(thumb_path)
        if not data_uri:
            logger.warning(f"Failed to read chroma-phase thumbnail {thumb_path}")
            continue
        found += 1
        row = by_idx[idx]
        # row: event_idx, start, end, dur_s, frames, rule, peak_sat, peak_time, hue
        start = row[1] if len(row) > 1 else ""
        frames = row[4] if len(row) > 4 else ""
        peak_time = row[7] if len(row) > 7 else ""
        items_html += f'''
        <div style="text-align: center; margin: 5px;">
            <img src="{data_uri}"
                 style="width: 300px; height: auto; border: 1px solid #4d2b12; cursor: pointer;"
                 onclick="openImage(this.src, 'Chroma Phase Event {idx} - peak at {peak_time}')"
                 title="Click to enlarge" />
            <p style="font-size: 12px; margin: 4px 0 0 0;"><strong>Event {idx}</strong> &mdash; starts {start}</p>
            <p style="font-size: 11px; margin: 1px 0 0 0; color: #777;">peak at {peak_time} &middot; {frames} frame(s)</p>
        </div>'''

    if found == 0:
        return ""

    return f'''
    <h4 style="margin-top: 20px;">Event Thumbnails</h4>
    <p style="font-size: 13px; margin: 0 0 8px 0; color: #555;">
        Frame captured at each event's peak SATMAX (max {found} shown; if more than 10 events were detected,
        the largest events by frame count are shown).
    </p>
    <div style="display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0;">
        {items_html}
    </div>'''


def make_tone_leak_html(tone_leak_summary_csv, tone_leak_events_csv):
    """
    Generates an HTML section summarizing reference-tone leak detection results.
    Always renders when the summary CSV is present, including when no channel
    was flagged - so the report shows that the check was run.

    Args:
        tone_leak_summary_csv (str): Path to the per-channel summary CSV.
        tone_leak_events_csv (str): Path to the events CSV (flagged regions).

    Returns:
        str: HTML string, or None if the summary CSV cannot be read.
    """
    if not tone_leak_summary_csv or not os.path.isfile(tone_leak_summary_csv):
        return None

    try:
        with open(tone_leak_summary_csv, 'r') as f:
            summary_rows = list(csv.reader(f))
    except Exception as e:
        logger.error(f"Error reading tone leak summary CSV: {e}")
        return None

    # The summary CSV is parameter rows (2 columns), a blank row, then the
    # per-channel table whose header row starts with "Stream".
    params = {}
    channel_header = None
    channel_rows = []
    for r in summary_rows:
        if not r:
            continue
        if channel_header is None:
            if r[0] == "Stream":
                channel_header = r
            elif len(r) >= 2:
                params[r[0]] = r[1]
        else:
            channel_rows.append(r)

    harmonics = params.get("Harmonics Scored (Hz)", "")
    comb_threshold = params.get("Comb Score Threshold (dB)", "")
    window_sec = params.get("Window Size (seconds)", "")

    flagged_rows = [r for r in channel_rows if r and r[-1] == "Yes"]
    if flagged_rows:
        flagged_desc = ", ".join(f"stream {r[0]} channel {r[1]}" for r in flagged_rows)
        status_color = "#dc3545"
        status_bg = "#f8d7da"
        status_border = "#f5c6cb"
        status_text = f"Reference-Tone Leak Detected: {flagged_desc}"
    else:
        status_color = "#155724"
        status_bg = "#d4edda"
        status_border = "#c3e6cb"
        status_text = "No Reference-Tone Leak Detected"

    channel_table = ""
    if channel_header and channel_rows:
        header_html = "".join(
            f'<th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">{h}</th>'
            for h in channel_header
        )
        rows_html = ""
        for r in channel_rows:
            detected = r and r[-1] == "Yes"
            row_style = ' background-color: #f8d7da;' if detected else ''
            rows_html += (
                '<tr>'
                + "".join(
                    f'<td style="padding: 4px 12px; border: 1px solid #ddd;{row_style}">{v}</td>'
                    for v in r
                )
                + '</tr>\n'
            )
        channel_table = f'''
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr>{header_html}</tr>
        {rows_html}
    </table>'''

    events = []
    if tone_leak_events_csv and os.path.isfile(tone_leak_events_csv):
        try:
            with open(tone_leak_events_csv, 'r') as f:
                reader = csv.reader(f)
                header = next(reader, None)
                for row in reader:
                    if len(row) >= 8:
                        events.append(row[:8])
        except Exception as e:
            logger.error(f"Error reading tone leak events CSV: {e}")

    events_table = ""
    if events:
        event_rows_html = ""
        for r in events:
            start, end, stream, channel, dur, mean_comb, peak_comb, level = r
            event_rows_html += (
                f'<tr>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{start}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; font-family: monospace;">{end}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{stream}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{channel}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{dur}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{mean_comb}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{peak_comb}</td>'
                f'<td style="padding: 4px 12px; border: 1px solid #ddd; text-align: right;">{level}</td>'
                f'</tr>\n'
            )
        events_table = f'''
    <a href="javascript:void(0);" onclick="toggleContent('tone_leak_events', 'Show tone leak regions ({len(events)}) ▼', 'Hide tone leak regions ▲')" style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block;">Show tone leak regions ({len(events)}) ▼</a>
    <div id="tone_leak_events" style="display: none;">
    <table style="border-collapse: collapse; margin: 10px 0;">
        <tr>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Start</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">End</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Stream</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Channel</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Duration (s)</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Mean Comb (dB)</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Peak Comb (dB)</th>
            <th style="padding: 4px 12px; border: 1px solid #ddd; background-color: #f2f2f2;">Median 1kHz Level (dBFS)</th>
        </tr>
        {event_rows_html}
    </table>
    </div>'''

    html = f'''
    <a id="link_tone_leak_methodology" href="javascript:void(0);"
       onclick="toggleContent('tone_leak_methodology', 'What is tone leak detection? ▼', 'What is tone leak detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is tone leak detection? ▼</a>
    <div id="tone_leak_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Tone leak detection</strong> flags a continuous ~1 kHz calibration/reference tone
            leaking from the transfer chain into the recorded audio &mdash; heard as a faint
            high-pitched whine or squeak during quiet passages. Because the leaked tone is distorted,
            it leaves a harmonic comb at exact multiples of 1 kHz ({harmonics} Hz) that stands far
            above the surrounding spectral floor even when the tone is well below program level.
        </p>
        <p style="margin: 0 0 10px 0;">
            Audio is decoded directly from the video file and analyzed per channel in
            {window_sec}-second FFT windows. Each window's <strong>comb score</strong> is the average
            level of the harmonics above their local spectral floor; a channel is flagged when enough
            windows score at or above {comb_threshold} dB. Digitally silent stretches are excluded.
            The flagged regions below use the file's own timecode.
        </p>
        <p style="margin: 0;">
            In the tables, <strong>Stream</strong> is the audio stream (track) within the video
            container, numbered from 0, and <strong>Channel</strong> is the channel within that
            stream &mdash; e.g. a file with one 4-channel stream shows Stream 0, Channels 0&ndash;3,
            while a file with two stereo streams shows Streams 0&ndash;1, each with Channels
            0&ndash;1. Every stream and channel is analyzed independently.
        </p>
    </div>
    <div style="background-color: {status_bg}; padding: 15px; border: 1px solid {status_border}; margin: 10px 0; border-radius: 5px;">
        <p style="margin: 0; color: {status_color};"><strong>{status_text}</strong></p>
    </div>
    {channel_table}
    {events_table}
    '''
    return html


def make_color_bars_graphs(video_id, qctools_colorbars_duration_output, colorbars_values_output, sorted_thumbs_dict, duration_override=None, bars_label=None, thumb_profile_filter=None):
    """
    Creates HTML visualizations for color bars analysis, including bar charts comparing
    SMPTE color bars with the video's color bars values.

    Args:
        video_id (str): The identifier for the video being analyzed.
        qctools_colorbars_duration_output (str or None): Path to CSV file containing color bars duration data.
        colorbars_values_output (str): Path to CSV file containing color bars values data.
        sorted_thumbs_dict (dict): Dictionary containing thumbnail information with keys as descriptions
                                 and values as tuples of (path, profile_name, timestamp).
                                 Output find_qct_thumbs().
        duration_override (str or None): When set, use this string as the duration annotation
                                         instead of reading the duration CSV.
        bars_label (str or None): Label for the detected bars trace in the chart
                                  (default: '{video_id} Colorbars').
        thumb_profile_filter (str or None): When set, only match thumbnails whose
                                            profile_name contains this substring.

    Returns:
        str or None: HTML string containing the visualization if successful, None if there are errors.
    """
    if not colorbars_values_output:
        logger.warning("Missing required colorbar values file")
        return None

    if not duration_override:
        if not qctools_colorbars_duration_output:
            logger.warning("Missing required colorbar files - duration or values file is None")
            return None
        try:
            if not os.path.isfile(qctools_colorbars_duration_output):
                logger.critical(f"Cannot open color bars duration csv file: {qctools_colorbars_duration_output}")
                return None
        except (TypeError, AttributeError) as e:
            logger.critical(f"Invalid path for color bars duration file: {e}")
            return None
        
    try:
        if not os.path.isfile(colorbars_values_output):
            logger.critical(f"Cannot open color bars values csv file: {colorbars_values_output}")
            return None
    except Exception as e:
        logger.critical(f"Error reading colorbars CSV file: {e}")
        return None
    
    try:
        import plotly.graph_objs as go
    except ImportError as e:
        logger.critical(f"Error importing required libraries for graphs: {e}")
        return None
    
    if not bars_label:
        bars_label = f'{video_id} Colorbars'

    try:
        with open(colorbars_values_output, 'r') as file:
            csv_reader = csv.DictReader(file)
            colorbar_csv_data = {
                'QCTools Fields': [],
                'SMPTE Colorbars': [],
                'detected': []
            }
            for row in csv_reader:
                colorbar_csv_data['QCTools Fields'].append(row['QCTools Fields'])
                colorbar_csv_data['SMPTE Colorbars'].append(float(row['SMPTE Colorbars']))
                colorbar_csv_data['detected'].append(float(row[f'{video_id} Colorbars']))
    except Exception as e:
        logger.critical(f"Error reading colorbars CSV file: {e}")
        return None

     # Initialize duration_text with default value
    duration_text = "Colorbars duration: Not available"

    if duration_override:
        duration_text = "Colorbars duration: " + duration_override
    else:
        try:
            with open(qctools_colorbars_duration_output, 'r') as file:
                duration_lines = file.readlines()

                if len(duration_lines) <= 1:
                    logger.critical(f"The csv file {qctools_colorbars_duration_output} does not match the expected format")
                    return None

                # Labeled 3-column row: drop the internal pass label
                # ("head", "head-relaxed", ...) — the section header already
                # says which region this is. Legacy rows are [start, end].
                row = next(csv.reader([duration_lines[1].strip()]))
                if len(row) >= 3:
                    duration_text = f"Colorbars duration: {row[1]} - {row[2]}"
                elif len(row) >= 2:
                    duration_text = f"Colorbars duration: {row[0]} - {row[1]}"
                else:
                    duration_text = "Colorbars duration: " + duration_lines[1].strip()
        except Exception as e:
            logger.critical(f"Error processing duration file: {e}")
            return None

    try:
        colorbars_fig = go.Figure(data=[
            go.Bar(name='SMPTE Colorbars',
                x=colorbar_csv_data['QCTools Fields'],
                y=colorbar_csv_data['SMPTE Colorbars'],
                marker=dict(color='#378d6a')),
            go.Bar(name=bars_label,
                x=colorbar_csv_data['QCTools Fields'],
                y=colorbar_csv_data['detected'],
                marker=dict(color='#bf971b'))
        ])
        colorbars_fig.update_layout(
            barmode='group',
            width=450,
            height=350,
            margin=dict(l=50, r=20, t=30, b=50),
            legend=dict(
                orientation='h',
                yanchor='bottom',
                y=1.02,
                xanchor='left',
                x=0,
            ),
        )

        config = {
            'toImageButtonOptions': {
                'format': 'png',
                'filename': f'{video_id}_colorbars_comparison',
                'height': 500,
                'width': 800,
                'scale': 1
            },
            'responsive': False,
        }
        colorbars_barchart_html = colorbars_fig.to_html(full_html=False, include_plotlyjs='cdn', config=config)
    except Exception as e:
        logger.critical(f"Error creating colorbars chart: {e}")
        return None

    # Add annotations for the thumbnail
    thumbnail_html = ''
    for thumb_name, (thumb_path, profile_name, timestamp) in sorted_thumbs_dict.items():
        if "bars_found.first_frame" not in thumb_path:
            continue
        if thumb_profile_filter and thumb_profile_filter not in thumb_path:
            continue
        if not thumb_profile_filter and "additional_bars" in thumb_path:
            continue
        thumb_name_with_breaks = thumb_name.replace("\n", "<br>")
        # Embed as a base64 JPEG data URI (rather than a raw local file path) so
        # the thumbnail still renders when the report is opened off the machine
        # that generated it.
        thumb_data_uri = image_file_to_jpeg_data_uri(thumb_path)
        if not thumb_data_uri:
            continue
        thumbnail_html = f'''
            <img src="{thumb_data_uri}" alt="{thumb_name}" style="width:200px; height:auto;">
            <p>{thumb_name_with_breaks}</p>
        '''
        break

    # Create the complete HTML with the duration text and the thumbnail/barchart side-by-side.
    # The caption column is pinned to the thumbnail width so long captions wrap
    # instead of widening the column and pushing the fixed-width chart into the
    # section's overflow clip.
    colorbars_html = f'''
    <div style="background-color: #f5e9e3; padding: 20px 30px; border-radius: 6px;">
        <div style="display: flex; align-items: center; justify-content: center;">
            <div style="flex: 0 0 200px; max-width: 200px; overflow-wrap: break-word;">
                {thumbnail_html}
                <p>{duration_text}</p>
            </div>
            <div style="margin-left: 20px; min-width: 0; flex: 1;">
                {colorbars_barchart_html}
            </div>
        </div>
    </div>
    '''

    return colorbars_html


def _parse_bars_durations_csv(csv_path):
    """
    Parse a bars-detection durations CSV.

    Two row schemas are supported:
      * qct-parse: row 0 sentinel + a single row of [start_ts, end_ts].
      * CLAMS: row 0 sentinel + N rows of [pass_label, start_ts, end_ts].

    Returns:
        List of (pass_label, start_seconds, end_seconds) tuples. The qct-parse
        single-row format is reported with pass_label="primary". Empty list
        when no bars were detected or the file is unreadable.
    """
    if not csv_path or not os.path.isfile(csv_path):
        return []

    def to_seconds(ts):
        try:
            h, m, s = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except (ValueError, AttributeError):
            return None

    runs = []
    try:
        with open(csv_path, "r") as f:
            rows = list(csv.reader(f))
        if rows and rows[0] and "color bars found" in rows[0][0]:
            for row in rows[1:]:
                if len(row) >= 3:
                    pass_label = row[0]
                    start = to_seconds(row[1])
                    end = to_seconds(row[2])
                elif len(row) >= 2:
                    pass_label = "primary"
                    start = to_seconds(row[0])
                    end = to_seconds(row[1])
                else:
                    continue
                if start is not None and end is not None:
                    runs.append((pass_label, start, end))
    except Exception as e:
        logger.warning(f"Could not parse bars durations CSV {csv_path}: {e}")
    return runs


_BARS_TONE_PASS_LABELS = {
    "head": "Head bars",
    "head-relaxed": "Head bars (relaxed)",
    "head-second-pass": "Head bars (relaxed re-check)",
    "primary": "Primary scan",
    "second-pass": "Targeted re-scan",
    "windowed": "Mid-file scan",
}


def _humanize_pass_label(label):
    """Map an internal bars/tone pass label to a human-readable phrase."""
    if label in _BARS_TONE_PASS_LABELS:
        return _BARS_TONE_PASS_LABELS[label]
    return str(label).replace("-", " ").replace("_", " ").capitalize()


def _short_ts(seconds):
    """Format seconds compactly for prose summaries: M:SS, or H:MM:SS past an hour."""
    if seconds is None:
        return "—"
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def make_bars_detection_comparison_html(qct_csv_path, clams_csv_path, agreement_tolerance_s=1.0):
    """
    Render a unified table comparing qct-parse and CLAMS SSIM bars detections.

    Each detection becomes a row tagged with its source and pass. Rows are
    shown chronologically (by start time), so the head bars from both methods
    appear together, followed by any additional bars. Head candidates are
    classified by time, matching the processing consensus: the earliest run
    starting within the head window, regardless of which pass found it.

    Returns None when neither detector produced output (so the section is
    omitted from the report entirely).
    """
    qct_run = bool(qct_csv_path and os.path.isfile(qct_csv_path))
    clams_run = bool(clams_csv_path and os.path.isfile(clams_csv_path))
    if not qct_run and not clams_run:
        return None

    qct_runs = _parse_bars_durations_csv(qct_csv_path)
    clams_runs = _parse_bars_durations_csv(clams_csv_path)

    # Mirrors HEAD_BARS_START_THRESHOLD in processing_mgmt.py
    HEAD_WINDOW_S = 30.0

    def fmt(seconds):
        if seconds is None:
            return "—"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - (h * 3600) - (m * 60)
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    # Separate qct-parse head from windowed (CLAMS-guided) rows.
    qct_head = next((r for r in qct_runs if r[0] in ("head", "head-relaxed")), None)
    # Legacy 2-column rows come through with label "primary"
    if qct_head is None:
        qct_head = next((r for r in qct_runs if r[0] == "primary"), None)
    qct_windowed = [r for r in qct_runs if r is not qct_head]

    # CLAMS head candidate: earliest run starting within the head window.
    # The primary scan can find only mid-file bars (its first hit isn't
    # necessarily at the head), and the relaxed head re-check writes a
    # "head-second-pass" row — so classify by time, not by pass label.
    clams_head_runs = [r for r in clams_runs if r[1] is not None and r[1] < HEAD_WINDOW_S]
    clams_head = min(clams_head_runs, key=lambda r: r[1]) if clams_head_runs else None
    clams_additional = [r for r in clams_runs if r is not clams_head]

    # Agreement: qct-parse head vs CLAMS head candidate.
    agreement_label = "—"
    agreement_color = "#666"
    if qct_run and clams_run:
        qct_end = qct_head[2] if qct_head else None
        clams_end = clams_head[2] if clams_head else None
        if qct_end is not None and clams_end is not None:
            delta = abs(qct_end - clams_end)
            if delta <= agreement_tolerance_s:
                agreement_label = f"Agree — end times within {delta:.2f}s"
                agreement_color = "#0a5f1c"
            else:
                agreement_label = f"Disagree — end times differ by {delta:.2f}s"
                agreement_color = "#a02020"
        elif qct_end is None and clams_end is None:
            agreement_label = "Agree — both found no bars"
            agreement_color = "#0a5f1c"
        else:
            which = "qct-parse" if qct_end is not None else "CLAMS"
            agreement_label = f"Disagree — only {which} found bars"
            agreement_color = "#a02020"

    # Build one row per detection. Source/pass go in the first two cells.
    # Each row carries raw seconds so the renderer can compute duration.
    body_rows = []
    if qct_run:
        if qct_head:
            _, qs, qe = qct_head
            body_rows.append(("qct-parse", qct_head[0], True, qs, qe, False))
        else:
            body_rows.append(("qct-parse", "head", False, None, None, False))
        for label, ws, we in qct_windowed:
            body_rows.append(("qct-parse", label, True, ws, we, True))
    if clams_run:
        if clams_head:
            body_rows.append(("CLAMS SSIM", clams_head[0], True, clams_head[1], clams_head[2], False))
        else:
            body_rows.append(("CLAMS SSIM", "head", False, None, None, False))
        for label, ss, se in clams_additional:
            body_rows.append(("CLAMS SSIM", label, True, ss, se, True))

    # Chronological order: detected segments by start time (head bars from
    # both methods together, then additional bars); "not detected" rows last.
    body_rows.sort(key=lambda r: (r[3] is None, r[3] if r[3] is not None else 0.0))

    cell = "padding: 6px 12px; border: 1px solid #e0d0c0;"

    def render_row(source, pass_label, found, start_sec, end_sec, is_secondary):
        bg = "background-color: #fff3cd;" if is_secondary else ""
        if found:
            detected = '<span style="color: #0a5f1c; font-weight: bold;">✓ Detected</span>'
            dur = f"{end_sec - start_sec:.1f}s" if (start_sec is not None and end_sec is not None) else "—"
        else:
            detected = '<span style="color: #999;">Not detected</span>'
            dur = "—"
        return (
            f'<tr style="{bg}">'
            f'<td style="{cell}">{source}</td>'
            f'<td style="{cell}">{_humanize_pass_label(pass_label)}</td>'
            f'<td style="{cell}">{detected}</td>'
            f'<td style="{cell} font-family: monospace;">{fmt(start_sec)}</td>'
            f'<td style="{cell} font-family: monospace;">{fmt(end_sec)}</td>'
            f'<td style="{cell} text-align: right;">{dur}</td>'
            f'</tr>'
        )

    rows_html = "".join(render_row(*r) for r in body_rows)

    # Plain-language summary line above the grid.
    head_candidates = [r for r in (qct_head, clams_head) if r]
    head_starts = [r[1] for r in head_candidates if r[1] is not None]
    head_ends = [r[2] for r in head_candidates if r[2] is not None]
    if head_starts and head_ends:
        hs, he = min(head_starts), max(head_ends)
        summary_text = (f"Color bars detected at the start of the file, "
                        f"{_short_ts(hs)}–{_short_ts(he)} ({he - hs:.1f}s).")
    else:
        summary_text = "No color bars detected at the start of the file."
    mid_count = len(qct_windowed) + len(clams_additional)
    if mid_count:
        summary_text += f" {mid_count} additional mid-file detection(s) shown below (report-only)."
    summary_html = (
        f'<p style="margin: 10px 0; padding: 10px 14px; background-color: #f5e9e3; '
        f'border-radius: 4px; color: #4d2b12;">{summary_text}</p>'
    )

    note_lines = [
        "The authoritative head bars end time is the cross-validated consensus "
        "of the two detectors: when their end times disagree, the disputed span "
        "is arbitrated against the CLAMS SSIM scores. Additional bars "
        "(highlighted) never drive access-file trimming, but every detected "
        "bars region is excluded from BRNG, signalstats, and duplicate-frame "
        "analysis, and from the color-bars evaluation.",
    ]
    has_secondary = clams_additional or qct_windowed
    if has_secondary:
        note_lines.append(
            "Targeted re-scans are triggered when one detector finds a span the "
            "other missed: CLAMS rows fire from tone detections outside the primary "
            "bars window; qct-parse rows are CLAMS-guided scans beyond the detected "
            "head bars."
        )
    note = "".join(
        f'<p style="font-size: 13px; color: #4d2b12; margin: 10px 0 0 0;">{line}</p>'
        for line in note_lines
    )

    legend = ""
    if has_secondary:
        legend = (
            '<p style="font-size: 13px; color: #4d2b12; margin: 8px 0 0 0;">'
            '<span style="display: inline-block; width: 12px; height: 12px; '
            'background-color: #fff3cd; border: 1px solid #e0d0c0; vertical-align: middle; '
            'margin-right: 6px;"></span>'
            '= targeted re-scan (report-only)</p>'
        )

    agreement_html = ""
    if qct_run and clams_run:
        agreement_html = f'''
    <p style="margin: 12px 0 0 0;">
        <span style="font-weight: bold;">Agreement:</span>
        <span style="display: inline-block; padding: 2px 10px; border-radius: 10px; color: #fff; background-color: {agreement_color}; font-size: 13px;">{agreement_label}</span>
    </p>'''

    html = f"""
    {summary_html}
    <table style="border-collapse: collapse; margin-top: 10px;">
        <thead>
            <tr style="background-color: #f5e9e3;">
                <th style="text-align: left; {cell}">Source</th>
                <th style="text-align: left; {cell}">Pass</th>
                <th style="text-align: left; {cell}">Bars detected</th>
                <th style="text-align: left; {cell}">Start</th>
                <th style="text-align: left; {cell}">End</th>
                <th style="text-align: left; {cell}">Duration</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
    {agreement_html}
    {legend}
    {note}
    """
    return html


def _parse_tone_detection_csv(csv_path):
    """
    Parse a CLAMS tone detection durations CSV.

    Row 0 col 0 either contains "tones found" (followed by one row per tone)
    or "no tones" (no further rows). Each tone row is
    [pass_label, start_ts, end_ts].

    Returns:
        List of (pass_label, start_seconds, end_seconds) tuples, or [] when
        no tones. Legacy two-column rows are reported with pass_label="primary".
    """
    if not csv_path or not os.path.isfile(csv_path):
        return []

    def to_seconds(ts):
        try:
            h, m, s = ts.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
        except (ValueError, AttributeError):
            return None

    tones = []
    try:
        with open(csv_path, "r") as f:
            rows = list(csv.reader(f))
        if rows and rows[0] and "tones found" in rows[0][0]:
            for row in rows[1:]:
                if len(row) >= 3:
                    pass_label = row[0]
                    s = to_seconds(row[1])
                    e = to_seconds(row[2])
                elif len(row) >= 2:
                    pass_label = "primary"
                    s = to_seconds(row[0])
                    e = to_seconds(row[1])
                else:
                    continue
                if s is not None and e is not None:
                    tones.append((pass_label, s, e))
    except Exception as e:
        logger.warning(f"Could not parse tone detection CSV {csv_path}: {e}")
    return tones


def make_tone_detection_html(tone_csv_path):
    """
    Render the CLAMS tone detection results as an HTML table.

    Returns None when the CSV is missing (so the section is omitted from the
    report). When the detector ran but found nothing, returns a short
    "no tones detected" notice.
    """
    if not tone_csv_path or not os.path.isfile(tone_csv_path):
        return None

    tones = _parse_tone_detection_csv(tone_csv_path)

    def fmt(seconds):
        if seconds is None:
            return "—"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds - (h * 3600) - (m * 60)
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    if not tones:
        return (
            '<div style="background-color: #f5e9e3; padding: 10px;">'
            '<p style="margin: 0;">CLAMS tone detector ran on the audio track '
            'and found no monotonic spans meeting the minimum duration threshold.</p>'
            '</div>'
        )

    has_secondary = any(p != "primary" for p, _, _ in tones)

    cell = "padding: 6px 12px; border: 1px solid #e0d0c0;"

    def render_row(i, pass_label, s, e):
        bg = "background-color: #fff3cd;" if pass_label != "primary" else ""
        return (
            f'<tr style="{bg}">'
            f'<td style="{cell} text-align: right;">{i + 1}</td>'
            f'<td style="{cell}">{_humanize_pass_label(pass_label)}</td>'
            f'<td style="{cell} font-family: monospace;">{fmt(s)}</td>'
            f'<td style="{cell} font-family: monospace;">{fmt(e)}</td>'
            f'<td style="{cell} text-align: right;">{(e - s):.1f}s</td>'
            f'</tr>'
        )

    rows = "".join(render_row(i, p, s, e) for i, (p, s, e) in enumerate(tones))

    # Plain-language summary line above the grid.
    primary_tones = [t for t in tones if t[0] == "primary"]
    secondary_count = len(tones) - len(primary_tones)
    if len(primary_tones) == 1:
        p0 = primary_tones[0]
        summary_text = (f"Reference tone detected, {_short_ts(p0[1])}–{_short_ts(p0[2])} "
                        f"({p0[2] - p0[1]:.1f}s).")
    elif primary_tones:
        summary_text = f"{len(primary_tones)} reference tone spans detected."
    else:
        summary_text = f"{len(tones)} tone span(s) detected."
    if secondary_count:
        summary_text += f" {secondary_count} targeted re-scan detection(s) shown below."
    summary_html = (
        f'<p style="margin: 10px 0; padding: 10px 14px; background-color: #f5e9e3; '
        f'border-radius: 4px; color: #4d2b12;">{summary_text}</p>'
    )

    note_lines = [
        "Adapted from the CLAMS tonedetection app: cross-correlation of "
        "consecutive 250 ms audio chunks at 16 kHz mono.",
    ]
    if has_secondary:
        note_lines.append(
            "Targeted re-scans are triggered by bars detections that fell outside the "
            "primary tone window, run with relaxed thresholds (tolerance 0.7, min duration 500 ms)."
        )
    note = "".join(
        f'<p style="font-size: 13px; color: #4d2b12; margin: 10px 0 0 0;">{line}</p>'
        for line in note_lines
    )

    legend = ""
    if has_secondary:
        legend = (
            '<p style="font-size: 13px; color: #4d2b12; margin: 8px 0 0 0;">'
            '<span style="display: inline-block; width: 12px; height: 12px; '
            'background-color: #fff3cd; border: 1px solid #e0d0c0; vertical-align: middle; '
            'margin-right: 6px;"></span>'
            '= targeted re-scan (report-only)</p>'
        )

    return f"""
    {summary_html}
    <table style="border-collapse: collapse; margin-top: 10px;">
        <thead>
            <tr style="background-color: #f5e9e3;">
                <th style="text-align: left; {cell}">#</th>
                <th style="text-align: left; {cell}">Pass</th>
                <th style="text-align: left; {cell}">Start</th>
                <th style="text-align: left; {cell}">End</th>
                <th style="text-align: left; {cell}">Duration</th>
            </tr>
        </thead>
        <tbody>
            {rows}
        </tbody>
    </table>
    {legend}
    {note}
    """


def make_profile_piecharts(qctools_profile_check_output, sorted_thumbs_dict, failureInfoSummary, video_id, failure_csv_path=None, check_cancelled=None):
    """
    Creates HTML visualizations showing pie charts of profile check results with thumbnails 
    and detailed failure information for each failed profile check.

    Args:
        qctools_profile_check_output (str): Path to CSV file containing profile check results.
        sorted_thumbs_dict (dict): Dictionary containing thumbnail information with keys as descriptions
                                 and values as tuples of (path, profile_name, timestamp). Output of find_qct_thumbs().
        failureInfoSummary (dict): Dictionary containing detailed failure information, with timestamps
                                 as keys and lists of failure details as values. Output of summarize_failures().
        video_id (str): The identifier for the video being analyzed.
        failure_csv_path (str, optional): Path to the full failures CSV file.
        check_cancelled (callable, optional): Function to check if processing should be cancelled.
                                           Defaults to None.

    Returns:
        str or None: HTML string containing the visualizations if successful, None if there are errors.
    """
    try:
        import plotly.graph_objs as go
    except ImportError as e:
        logger.critical(f"Error importing required libraries for graphs: {e}")
        return None
    
    # Read the full failure data if CSV path is provided
    all_failures = {}
    if failure_csv_path and os.path.isfile(failure_csv_path):
        try:
            with open(failure_csv_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    tag = row['Tag']
                    if tag not in all_failures:
                        all_failures[tag] = []
                    all_failures[tag].append({
                        'timestamp': row['Timestamp'],
                        'tagValue': row['Tag Value'],
                        'threshold': row['Threshold']
                    })
        except Exception as e:
            logger.error(f"Error reading full failures CSV: {e}")
    
    # Read and validate CSV file
    try:
        # Check if file exists
        if not os.path.isfile(qctools_profile_check_output):
            logger.critical(f"Profile check CSV file not found: {qctools_profile_check_output}")
            return None
    
        # First, get the total frames from the third line
        with open(qctools_profile_check_output, 'r') as file:
            lines = file.readlines()
            total_frames_line = lines[2].strip()
            _, total_frames = total_frames_line.split(',')
            total_frames = int(total_frames)
            
            # Read the profile summary data, skipping the first three lines
            profile_data = []
            csv_reader = csv.DictReader(lines[3:])
            for row in csv_reader:
                profile_data.append(row)

    except Exception as e:
        logger.critical(f"Error processing profile check CSV: {e}")
        return None

    # Initialize profile summary html
    profile_summary_html = None
    profile_summary_pie_charts = []

    # Create a reverse lookup dictionary for thumbnails by timestamp and tag
    thumb_lookup = {}
    for thumb_name, (thumb_path, tag_name, timestamp) in sorted_thumbs_dict.items():
        if timestamp and tag_name:  # Only process thumbnails with valid timestamps and tags
            key = (timestamp, tag_name)
            thumb_lookup[key] = (thumb_path, thumb_name)

    # Add JavaScript for toggling tables
    javascript_code = """
    <script>
    function openImage(imgData, caption) {
        var newWindow = window.open('', '_blank');
        newWindow.document.write('<html><head><title>' + caption + '</title></head><body style="margin:0; background:#000; display:flex; align-items:center; justify-content:center; height:100vh;">');
        newWindow.document.write('<img src="' + imgData + '" style="max-width:100%; max-height:100%; object-fit:contain;">');
        newWindow.document.write('</body></html>');
        newWindow.document.close();
    }

    function toggleTable(tagId) {
        var table = document.getElementById('table_' + tagId);
        var link = document.getElementById('link_' + tagId);
        if (table.style.display === 'none') {
            table.style.display = 'block';
            link.textContent = 'Hide all failures ▲';
        } else {
            table.style.display = 'none';
            link.textContent = 'Show all failures ▼';
        }
    }
    </script>
    """

    # Create pie charts for the profile summary
    tag_counter = 0  # Counter to create unique IDs
    for row in profile_data:
        if check_cancelled and check_cancelled():
            logger.warning("HTML report cancelled.")
            return profile_summary_html

        tag = row['Tag']
        failed_frames = int(row['Number of failed frames'])
        percentage = float(row['Percentage of failed frames'])

        if tag != 'Total':
            tag_id = f"tag_{tag_counter}"  # Create unique ID for this tag
            tag_counter += 1
            
            if percentage > 0:
                # Initialize variables for summary data
                failure_entries_html = []

                # Get failure details for this tag
                for timestamp, info_list in failureInfoSummary.items():
                    for info in info_list:
                        if info['tag'] == tag:
                            # Look for thumbnail for this specific timestamp and tag
                            thumb_html = ""
                            lookup_key = (timestamp, tag)
                            
                            if lookup_key in thumb_lookup:
                                thumb_path, thumb_name = thumb_lookup[lookup_key]
                                thumb_data_uri = image_file_to_jpeg_data_uri(thumb_path)
                                # Create caption for the new window
                                caption = f"{tag} at {timestamp} - Value: {info['tagValue']}, Threshold: {info['over']}"
                                # Make thumbnail clickable with JavaScript
                                thumb_html = f'''<img src="{thumb_data_uri}"
                                                onclick="openImage(this.src, '{caption}')"
                                                style="width: 100px; height: auto; vertical-align: middle; margin-left: 10px; cursor: pointer; border: 1px solid #ccc;"
                                                title="Click to enlarge" />'''
                            
                            # Create entry with or without thumbnail
                            entry_html = f'''
                            <div style="margin-bottom: 10px;">
                                <span><b>Timestamp: {timestamp}</b> | <b>Value:</b> {info['tagValue']} | <b>Threshold:</b> {info['over']}</span>
                                {thumb_html}
                            </div>
                            '''
                            failure_entries_html.append(entry_html)

                # Create formatted failure summary with all thumbnails
                formatted_failures = "".join(failure_entries_html)
                
                # Create the full failures table (hidden by default)
                full_table_html = ""
                if tag in all_failures:
                    table_rows = []
                    for failure in all_failures[tag]:
                        table_rows.append(f"""
                        <tr>
                            <td>{failure['timestamp']}</td>
                            <td>{failure['tagValue']}</td>
                            <td>{failure['threshold']}</td>
                        </tr>
                        """)
                    
                    full_table_html = f"""
                    <div id="table_{tag_id}" style="display: none; margin-top: 10px;">
                        <table style="border-collapse: collapse; width: 100%; border: 1px solid #4d2b12;">
                            <tr style="background-color: #fbe4eb;">
                                <th style="border: 1px solid #4d2b12; padding: 8px;">Timestamp</th>
                                <th style="border: 1px solid #4d2b12; padding: 8px;">Value</th>
                                <th style="border: 1px solid #4d2b12; padding: 8px;">Threshold</th>
                            </tr>
                            {''.join(table_rows)}
                        </table>
                    </div>
                    """
                
                summary_html = f"""
                <div style="display: flex; flex-direction: column; align-items: flex-start; background-color: #f5e9e3; padding: 10px; max-height: 400px; overflow-y: auto;">
                    <p><b>Peak Values outside of Threshold for {tag}:</b></p>
                    {formatted_failures}
                    <a id="link_{tag_id}" href="javascript:void(0);" onclick="toggleTable('{tag_id}')" style="color: #378d6a; text-decoration: underline; margin-top: 10px;">Show all failures ▼</a>
                    {full_table_html}
                </div>
                """
            else:
                # For 0% failures, show a simple message without thumbnails
                summary_html = f"""
                <div style="display: flex; flex-direction: column; align-items: flex-start; background-color: #f5e9e3; padding: 10px;">
                    <p><b>All values within specified threshold</b></p>
                </div>
                """

            # Generate Pie chart (same for both cases)
            pie_fig = go.Figure(data=[go.Pie(
                labels=['Failed Frames', 'Other Frames'],
                values=[failed_frames, total_frames - failed_frames],
                hole=.3,
                marker=dict(colors=['#ffbaba', '#d2ffed'])
            )])
            pie_fig.update_layout(
                title=f"{video_id}<br>{tag} - {percentage:.2f}% ({failed_frames} frames)", 
                height=400, 
                width=400,
                paper_bgcolor='#f5e9e3'
            )

            # plotly config values for custo. png file name
            config = {
                'toImageButtonOptions': {
                    'format': 'png',
                    'filename': f'{video_id}_{tag}',
                    'height': 400,
                    'width': 400,
                    'scale': 1
                }
            }

            # Wrap everything in one div
            pie_chart_html = f"""
            <div style="display: flex; flex-direction: column; align-items: start; background-color: #f5e9e3; padding: 10px;"> 
                <div style="width: 400px;">{pie_fig.to_html(full_html=False, include_plotlyjs='cdn', config=config)}</div>
                {summary_html}
            </div>
            """

            profile_summary_pie_charts.append(f"""
            <div style="display:inline-block; margin-right: 10px; padding-bottom: 20px;">  
                {pie_chart_html}
            </div>
            """)

    # Arrange pie charts horizontally with JavaScript
    profile_piecharts_html = ''.join(profile_summary_pie_charts)

    profile_summary_html = f'''
    {javascript_code}
    <div>
        {profile_piecharts_html}
    </div>
    '''

    return profile_summary_html

def _seconds_to_display(seconds):
    """Convert seconds to a human-readable timecode string (HH:MM:SS.s)"""
    if seconds is None:
        return "N/A"
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:04.1f}"
    return f"{minutes}:{secs:04.1f}"


def generate_bitplane_html(frame_outputs):
    """
    Generate HTML section for bitplane check results.

    Rendered separately from generate_frame_analysis_html so the bitplane
    section can be placed side-by-side with duplicate frame detection in the
    report layout.

    Args:
        frame_outputs (dict): Dictionary of frame analysis output paths/data.

    Returns:
        str: HTML fragment, or empty string if no bitplane data.
    """
    if not frame_outputs:
        return ""
    bitplane_data = frame_outputs.get('bitplane_check')
    if not bitplane_data:
        return ""

    status = bitplane_data.get('status', 'unknown')
    message = bitplane_data.get('message', '')
    frames_sampled = bitplane_data.get('frames_sampled', 0)
    overall_avgs = bitplane_data.get('overall_bitplane_averages', {})
    channels = bitplane_data.get('channels', {})

    if status == 'truncated':
        status_color = '#cc0000'
        status_icon = '&#x26A0;'
    elif status == 'partial_truncation':
        status_color = '#cc6600'
        status_icon = '&#x26A0;'
    elif status == 'valid':
        status_color = '#0a5f1c'
        status_icon = '&#x2705;'
    else:
        status_color = '#666666'
        status_icon = '&#x2753;'

    html = "<h3 style='color: #bf971b;'>Bitplane Check (7th–10th Bit Verification)</h3>"
    html += f"""
    <p style="font-size: 14px; color: {status_color}; font-weight: bold;">
        {status_icon} {message}
    </p>
    <p style="font-size: 13px; color: #555;">Frames sampled: {frames_sampled} (evenly spaced across the full video duration)</p>
    """

    if overall_avgs:
        html += """
        <table style="border-collapse: collapse; margin: 10px 0; font-size: 13px;">
            <tr style="background-color: #f0ebe4;">
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: left;">Bitplane</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Avg Noise (all channels)</th>
            </tr>
        """
        for bp_name, avg in overall_avgs.items():
            val_str = f"{avg:.6f}" if avg is not None else "N/A"
            html += f"""
            <tr>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">{bp_name}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{val_str}</td>
            </tr>
            """
        html += "</table>"

    bit_order = bitplane_data.get('bit_order_check')
    if bit_order:
        bo_status = bit_order.get('status', '')
        bo_message = bit_order.get('message', '')
        avg_lsb = bit_order.get('avg_9th_10th', 0)
        avg_msb = bit_order.get('avg_7th_8th', 0)
        if bo_status == 'expected':
            bo_color = '#0a5f1c'
            bo_icon = '&#x2705;'
        else:
            bo_color = '#cc6600'
            bo_icon = '&#x26A0;'
        html += f"""
        <p style="font-size: 13px; color: {bo_color}; margin: 8px 0;">
            {bo_icon} {bo_message}
        </p>
        <p style="font-size: 13px; color: #555; margin: 4px 0 12px 0;">
            Avg noise — 9th/10th bits: {avg_lsb:.6f} | 7th/8th bits: {avg_msb:.6f}
        </p>
        """

    if channels:
        html += """
        <a id="link_bitplane_detail" href="javascript:void(0);"
           onclick="toggleContent('bitplane_detail', 'Per-channel detail ▼', 'Per-channel detail ▲')"
           style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block; font-size: 13px;">
           Per-channel detail ▼</a>
        <div id="bitplane_detail" style="display: none; margin: 0 0 16px 0;">
        <table style="border-collapse: collapse; font-size: 13px;">
            <tr style="background-color: #f0ebe4;">
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0;">Channel</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0;">Bitplane</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">Status</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Avg Noise</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Max Noise</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Zero Frames</th>
            </tr>
        """
        for ch_name, bp_data in channels.items():
            for bp_name, bp_result in bp_data.items():
                bp_status = bp_result.get('status', 'unknown')
                avg_noise = bp_result.get('average_noise', 0)
                max_noise = bp_result.get('max_noise', 0)
                zero_pct = bp_result.get('zero_percentage', 0)
                row_color = '#fce4e4' if bp_status == 'empty' else ''
                style = f' style="background-color: {row_color};"' if row_color else ''
                html += f"""
                <tr{style}>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">{ch_name}</td>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">{bp_name}</td>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">
                        {'&#x274C; empty' if bp_status == 'empty' else '&#x2705; active'}
                    </td>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{avg_noise:.6f}</td>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{max_noise:.6f}</td>
                    <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{zero_pct:.1f}%</td>
                </tr>
                """
        html += "</table></div>"

    return html


def generate_frame_analysis_html(frame_outputs, video_id):
    """
    Generate HTML section for frame analysis results.

    Args:
        frame_outputs (dict): Dictionary of frame analysis output paths
        video_id (str): Video identifier

    Returns:
        str: HTML string for frame analysis section
    """
    has_content = (
        frame_outputs.get('border_visualization') or
        frame_outputs.get('border_data') or
        frame_outputs.get('brng_analysis') or
        frame_outputs.get('signalstats_analysis')
    )
    if not has_content:
        return ""

    html = """
    <div class="frame-analysis-section" id="section-frame-analysis">
        <h2 style="color: #0a5f1c; text-decoration: underline; margin-top: 30px;">Frame Analysis Results</h2>
    """

    # Border Detection Section
    if frame_outputs['border_visualization'] or frame_outputs['border_data']:
        html += "<h3 id='section-border-detection' style='color: #bf971b;'>Border Detection</h3>"
        
        # Methodology explanation (collapsible)
        html += """
        <a id="link_border_methodology" href="javascript:void(0);" 
           onclick="toggleContent('border_methodology', 'What is border detection? ▼', 'What is border detection? ▲')" 
           style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
           What is border detection? ▼</a>
        <div id="border_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px; 
             margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
            <p style="margin: 0 0 10px 0;">
                <strong>Border detection</strong> identifies the active picture area within the video frame, 
                excluding non-content regions such as blanking intervals, head switching noise, and 
                pillarboxing/letterboxing borders. Accurately identifying borders is essential because 
                pixels in these regions are often outside broadcast range but do not represent actual 
                content violations.
            </p>
            <p style="margin: 0 0 10px 0; font-weight: bold;">Detection methods:</p>
            <ul style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>Sophisticated (quality-based)</strong> — samples 
                    multiple frames across the video, selecting high-quality frames with good contrast. 
                    Analyzes luminance gradients at frame edges to find where active picture content begins. 
                    Also detects head switching artifacts in the bottom rows of the frame. If the
                    average head switching artifact height exceeds the luminance-based bottom border crop,
                    the bottom crop is expanded to match the artifact height.</li>
                <li style="margin-bottom: 4px;"><strong>Simple (fixed)</strong> — applies a uniform border 
                    crop (default 25 pixels) on all sides. Used as a fallback when sophisticated detection 
                    is not possible.</li>
            </ul>
            <p style="margin: 0 0 10px 0; font-weight: bold;">Iterative refinement:</p>
            <p style="margin: 0 0 10px 0;">
                After initial border detection, AV Spex runs BRNG (broadcast range) analysis on the detected 
                active area. If a high percentage of violations occur at the edges of the active area 
                (suggesting the borders were not cropped aggressively enough), the borders are automatically 
                expanded and analysis is re-run. This iterative refinement continues until edge violations 
                are reduced or a maximum number of iterations is reached. The goal is to separate true 
                content violations from border artifacts.
            </p>
        </div>
        """
        
        # Determine border data source: prefer enhanced JSON, fall back to standalone file
        border_data = None
        enhanced_data = None
        
        # Try enhanced JSON first (has more complete data)
        if frame_outputs.get('enhanced_frame_analysis'):
            try:
                with open(frame_outputs['enhanced_frame_analysis'], 'r') as f:
                    enhanced_data = json.load(f)
            except Exception:
                pass
        
        # Try standalone border_data.json
        if frame_outputs['border_data']:
            try:
                with open(frame_outputs['border_data'], 'r') as f:
                    border_data = json.load(f)
            except Exception as e:
                logger.error(f"Error reading border data: {e}")
        
        # Determine initial and final border data
        initial_borders = frame_outputs.get('initial_borders') or (enhanced_data or {}).get('initial_borders')
        final_borders = frame_outputs.get('final_borders') or (enhanced_data or {}).get('final_borders')
        refinement_iterations = frame_outputs.get('refinement_iterations', 0) or (enhanced_data or {}).get('refinement_iterations', 0)
        refinement_history = frame_outputs.get('refinement_history', []) or (enhanced_data or {}).get('refinement_history', [])
        
        # Use the best available border info for display
        display_borders = final_borders or initial_borders or border_data
        
        if display_borders:
            # Display detection method
            detection_method = display_borders.get('detection_method', 'unknown')
            method_label = "Simple (fixed borders)" if detection_method == 'simple_fixed' else "Sophisticated (quality-based detection)"
            if 'refined' in detection_method:
                method_label += " with iterative refinement"
            
            html += f"<p><strong>Method:</strong> {method_label}</p>"
            
            # Active area display
            active_area = display_borders.get('active_area')
            if active_area:
                if isinstance(active_area, (list, tuple)) and len(active_area) == 4:
                    x, y, w, h = active_area
                else:
                    x, y, w, h = 0, 0, 0, 0
                
                # Get video dimensions from border_data or enhanced data
                video_width, video_height = 0, 0
                if border_data and border_data.get('video_properties'):
                    video_width = border_data['video_properties']['width']
                    video_height = border_data['video_properties']['height']
                elif initial_borders and initial_borders.get('active_area'):
                    # Estimate from initial borders (initial area + borders = full frame)
                    init_area = initial_borders['active_area']
                    if isinstance(init_area, (list, tuple)) and len(init_area) == 4:
                        # Use border_regions if available
                        pass
                
                # Build active area HTML
                active_area_html = ""
                if video_width > 0 and video_height > 0:
                    active_percentage = (w * h) / (video_width * video_height) * 100
                    right_border = video_width - x - w
                    bottom_border = video_height - y - h

                    active_area_html = f"""
                        <table style="border-collapse: collapse; width: 100%; font-size: 14px;">
                            <tr>
                                <td style="padding: 4px 10px; font-weight: bold; width: 200px;">Active picture area</td>
                                <td style="padding: 4px 10px;">{w}×{h} pixels ({active_percentage:.1f}% of {video_width}×{video_height} frame)</td>
                            </tr>
                            <tr>
                                <td style="padding: 4px 10px; font-weight: bold;">Position</td>
                                <td style="padding: 4px 10px;">({x}, {y})</td>
                            </tr>
                            <tr>
                                <td style="padding: 4px 10px; font-weight: bold;">Borders</td>
                                <td style="padding: 4px 10px;">Left={x}px, Right={right_border}px, Top={y}px, Bottom={bottom_border}px</td>
                            </tr>
                        </table>
                    """
                else:
                    active_area_html = f"""
                        <p><strong>Active Picture Area:</strong> {w}×{h} pixels at ({x}, {y})</p>
                    """
            else:
                active_area_html = ""

            # Head switching artifacts
            hs_html = ""
            hs_data = display_borders.get('head_switching_artifacts')
            if hs_data and isinstance(hs_data, dict):
                severity = hs_data.get('severity', 'none')
                if severity not in ('none', 'error', None):
                    artifact_pct = hs_data.get('percentage', 0)
                    avg_height = hs_data.get('avg_height_px', 0)
                    height_info = ""
                    if avg_height:
                        height_info = f"<p>Average artifact height: {avg_height}px</p>"
                    hs_html = f"""
                        <p><strong>Head Switching Artifacts Detected</strong></p>
                        <p>Affected frames: {artifact_pct:.1f}%</p>
                        {height_info}
                    """

            # Render active area and head switching side by side using flexbox
            if active_area_html and hs_html:
                html += f"""
                <div style="display: flex; gap: 12px; margin: 10px 0; align-items: stretch;">
                    <div style="background-color: #f5e9e3; padding: 10px; border-radius: 4px; flex: 1; min-width: 0;">
                        {active_area_html}
                    </div>
                    <div style="background-color: #f5e9e3; padding: 10px; border-radius: 4px; flex: 1; min-width: 0;">
                        {hs_html}
                    </div>
                </div>
                """
            elif active_area_html:
                html += f"""
                <div style="background-color: #f5e9e3; padding: 10px; margin: 10px 0; border-radius: 4px;">
                    {active_area_html}
                </div>
                """
            elif hs_html:
                html += f"""
                <div style="background-color: #f5e9e3; padding: 10px; margin: 10px 0; border-radius: 4px;">
                    {hs_html}
                </div>
                """
        
        # Display initial border visualization image
        if frame_outputs['border_visualization']:
            viz_label = "Initial border detection" if refinement_iterations > 0 else "Border detection"
            try:
                with open(frame_outputs['border_visualization'], "rb") as img_file:
                    encoded_img = b64encode(img_file.read()).decode()
                html += f"""
                <div style="margin: 15px 0;">
                    <p style="font-size: 13px; color: #666; margin-bottom: 6px;"><em>{viz_label}</em></p>
                    <img src="data:image/jpeg;base64,{encoded_img}" 
                         style="max-width: 100%; height: auto; border: 1px solid #4d2b12;"
                         alt="{viz_label} visualization" />
                </div>
                """
            except Exception as e:
                logger.warning(f"Could not embed border visualization: {e}")
        
        # === Border Refinement Section ===
        if refinement_iterations and refinement_iterations > 0:
            html += f"""
            <div style="margin-top: 20px; padding: 14px 16px; background-color: #fff3cd; border: 1px solid #bf971b; border-radius: 4px;">
                <p style="margin: 0 0 10px 0; font-weight: bold; color: #856404;">
                    ⚠️ Border Refinement Performed ({refinement_iterations} iteration{'s' if refinement_iterations > 1 else ''})
                </p>
                <p style="margin: 0 0 10px 0; font-size: 13px;">
                    Initial BRNG analysis detected a high percentage of violations at the edges of the 
                    active area, indicating the initial border crop was insufficient. Borders were 
                    automatically expanded and analysis was re-run.
                </p>
            """
            
            # Show initial → final comparison
            if initial_borders and final_borders:
                init_area = initial_borders.get('active_area', [0,0,0,0])
                final_area = final_borders.get('active_area', [0,0,0,0])
                
                if isinstance(init_area, (list, tuple)) and len(init_area) == 4:
                    init_w, init_h = init_area[2], init_area[3]
                    final_w, final_h = final_area[2], final_area[3]
                    width_change = final_w - init_w
                    height_change = final_h - init_h
                    
                    html += f"""
                <table style="border-collapse: collapse; width: 100%; font-size: 13px; margin-bottom: 10px;">
                    <tr style="background-color: rgba(255,255,255,0.5);">
                        <th style="padding: 5px 10px; text-align: left; border-bottom: 1px solid #bf971b;"></th>
                        <th style="padding: 5px 10px; text-align: left; border-bottom: 1px solid #bf971b;">Initial</th>
                        <th style="padding: 5px 10px; text-align: left; border-bottom: 1px solid #bf971b;">Final</th>
                        <th style="padding: 5px 10px; text-align: left; border-bottom: 1px solid #bf971b;">Change</th>
                    </tr>
                    <tr>
                        <td style="padding: 4px 10px; font-weight: bold;">Active area</td>
                        <td style="padding: 4px 10px;">{init_w}×{init_h}</td>
                        <td style="padding: 4px 10px;">{final_w}×{final_h}</td>
                        <td style="padding: 4px 10px;">{width_change:+d}×{height_change:+d} px</td>
                    </tr>
                    <tr>
                        <td style="padding: 4px 10px; font-weight: bold;">Position</td>
                        <td style="padding: 4px 10px;">({init_area[0]}, {init_area[1]})</td>
                        <td style="padding: 4px 10px;">({final_area[0]}, {final_area[1]})</td>
                        <td style="padding: 4px 10px;"></td>
                    </tr>"""

                    # Extract borders
                    if video_width > 0 and video_height > 0:
                        init_borders_str = f"L={init_area[0]} R={video_width-init_area[0]-init_w} T={init_area[1]} B={video_height-init_area[1]-init_h}"
                        final_borders_str = f"L={final_area[0]} R={video_width-final_area[0]-final_w} T={final_area[1]} B={video_height-final_area[1]-final_h}"
                        html += f"""
                    <tr>
                        <td style="padding: 4px 10px; font-weight: bold;">Borders (px)</td>
                        <td style="padding: 4px 10px;">{init_borders_str}</td>
                        <td style="padding: 4px 10px;">{final_borders_str}</td>
                        <td style="padding: 4px 10px;"></td>
                    </tr>"""
                    
                    html += "</table>"
            
            # Show per-iteration details
            if refinement_history:
                for iteration in refinement_history:
                    iter_num = iteration.get('iteration', '?')
                    iter_area = iteration.get('active_area', [0,0,0,0])
                    area_change = iteration.get('area_change', {})
                    violations_before = iteration.get('violations_before', 0)
                    violations_after = iteration.get('violations_after', 0)
                    edge_pct = iteration.get('edge_violation_pct', 0)
                    
                    violation_delta = violations_after - violations_before
                    violation_delta_str = f"{violation_delta:+d}" if violation_delta != 0 else "no change"
                    
                    if isinstance(iter_area, (list, tuple)) and len(iter_area) == 4:
                        html += f"""
                <div style="background-color: rgba(255,255,255,0.4); padding: 6px 10px; margin: 6px 0; border-radius: 3px; font-size: 12px;">
                    <strong>Iteration {iter_num}:</strong> 
                    Active area → {iter_area[2]}×{iter_area[3]} 
                    (width {area_change.get('width', 0):+d}px, height {area_change.get('height', 0):+d}px) — 
                    Violation frames: {violations_before} → {violations_after} ({violation_delta_str}) — 
                    Edge violations: {edge_pct:.1f}%
                </div>"""
            
            html += "</div>"  # Close refinement container
            
            # Collect all refinement thumbnails for horizontal display
            refinement_thumbs = []
            
            # Add each refinement iteration visualization
            refinement_vizs = frame_outputs.get('refinement_visualizations', [])
            if refinement_vizs:
                for idx, viz_path in enumerate(refinement_vizs, 1):
                    try:
                        with open(viz_path, "rb") as img_file:
                            encoded_img = b64encode(img_file.read()).decode()
                        refinement_thumbs.append((f'Refinement iteration {idx}', encoded_img))
                    except Exception as e:
                        logger.warning(f"Could not embed refinement visualization {viz_path}: {e}")
            
            # Add before/after comparison
            comparison_path = frame_outputs.get('refinement_comparison')
            if comparison_path:
                try:
                    with open(comparison_path, "rb") as img_file:
                        encoded_img = b64encode(img_file.read()).decode()
                    refinement_thumbs.append(('Initial vs. final comparison', encoded_img))
                except Exception as e:
                    logger.warning(f"Could not embed refinement comparison: {e}")
            
            # Render horizontal thumbnail strip
            if refinement_thumbs:
                html += """
                <div style="margin: 15px 0;">
                    <p style="font-size: 13px; color: #666; margin-bottom: 8px;"><em>Border refinement visualizations</em> <span style="font-size: 11px;">(click to enlarge)</span></p>
                    <div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-start;">"""
                
                for caption, encoded_img in refinement_thumbs:
                    html += f"""
                        <div style="flex: 1 1 0; min-width: 180px; max-width: {max(100 // len(refinement_thumbs), 20)}%; text-align: center;">
                            <img src="data:image/jpeg;base64,{encoded_img}"
                                 style="width: 100%; height: auto; border: 1px solid #4d2b12; cursor: pointer; transition: opacity 0.2s;"
                                 onmouseover="this.style.opacity='0.85'" onmouseout="this.style.opacity='1'"
                                 onclick="openImage(this.src, '{caption}')"
                                 title="Click to enlarge"
                                 alt="{caption}" />
                            <p style="font-size: 11px; color: #888; margin: 4px 0 0 0;">{caption}</p>
                        </div>"""
                
                html += """
                    </div>
                </div>"""
    
    # Signalstats Analysis Section
    if frame_outputs['signalstats_analysis']:
        html += "<h3 id='section-signalstats' style='color: #bf971b;'>Signalstats Analysis</h3>"
        
        # Methodology explanation (collapsible)
        html += """
        <a id="link_signalstats_methodology" href="javascript:void(0);" 
           onclick="toggleContent('signalstats_methodology', 'What is signalstats analysis? ▼', 'What is signalstats analysis? ▲')" 
           style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
           What is signalstats analysis? ▼</a>
        <div id="signalstats_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px; 
             margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
            <p style="margin: 0 0 10px 0;">
                <strong>Signalstats analysis</strong> evaluates broadcast range compliance across sampled 
                time periods of the video. It reads the FFmpeg 
                <code style="background: #eee; padding: 1px 4px; border-radius: 2px;">signalstats</code> 
                BRNG metric, which counts the number of pixels in each frame that fall outside the 
                broadcast-legal range (luma &lt; 16 or &gt; 235, chroma &lt; 16 or &gt; 240 for 8-bit video) 
                and divides by the total pixel count to produce a ratio from 0.0 to 1.0. AV Spex 
                converts this ratio to a percentage for display.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Dual-source comparison:</p>
            <p style="margin: 0 0 6px 0;">
                When border detection has identified an active picture area, AV Spex runs two parallel 
                analyses for each period to distinguish border artifacts from actual content violations:
            </p>
            <ol style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>QCTools (full frame)</strong> — BRNG values parsed 
                    from the QCTools report for the time range, covering the entire frame including 
                    borders and blanking areas</li>
                <li style="margin-bottom: 4px;"><strong>FFprobe (active area only)</strong> — BRNG values 
                    computed by FFprobe with a crop filter applied to analyze only the detected active 
                    picture area, excluding borders</li>
            </ol>
            <p style="margin: 0 0 10px 0;">
                Comparing these two results reveals whether violations originate from border/blanking 
                regions or from the actual picture content. If the full frame shows significantly more 
                violations (>5%) than the active area, violations are classified as <em>border violations</em>. 
                If the active area itself shows >10% violations, they are classified as <em>content violations</em> 
                that may require correction.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Period selection priority:</p>
            <ol style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>QCTools violation clusters</strong> — periods targeting 
                    timestamps where QCTools detected the highest concentrations of BRNG activity</li>
                <li style="margin-bottom: 4px;"><strong>Border detection quality hints</strong> — timestamps 
                    flagged during border detection as having interesting signal characteristics</li>
                <li style="margin-bottom: 4px;"><strong>Even distribution</strong> — fallback to evenly 
                    spaced periods across the video content (after color bars)</li>
            </ol>
            <p style="margin: 0; color: #777;">
                The final diagnosis is based on active area results, which reflect the actual picture 
                content that would be seen in playback or broadcast.
            </p>
        </div>
        """
        
        try:
            # Handle both dict (from enhanced_frame_analysis.json) and file path (legacy)
            if isinstance(frame_outputs['signalstats_analysis'], dict):
                signalstats_data = frame_outputs['signalstats_analysis']
            else:
                with open(frame_outputs['signalstats_analysis'], 'r') as f:
                    signalstats_data = json.load(f)
            
            diagnosis = signalstats_data.get('diagnosis', 'Analysis complete')

            # Determine assessment styling from the severity field when present;
            # fall back to diagnosis keyword matching for results generated
            # before severity existed
            severity = signalstats_data.get('severity')
            if not severity:
                d = diagnosis.lower()
                if 'broadcast-compliant' in d or 'broadcast-safe' in d:
                    severity = 'ok'
                elif 'acceptable' in d or 'minor' in d:
                    severity = 'info'
                elif 'significant' in d or 'requires' in d or 'severe' in d:
                    severity = 'alert'
                elif 'review' in d or 'detected' in d:
                    severity = 'warning'

            if severity == 'ok':
                assessment_bg = '#d2ffed'
                assessment_border = '#378d6a'
                assessment_icon = '✅'
            elif severity == 'alert':
                assessment_bg = '#ffbaba'
                assessment_border = '#d32f2f'
                assessment_icon = '⛔'
            elif severity == 'warning':
                assessment_bg = '#fff3cd'
                assessment_border = '#bf971b'
                assessment_icon = '⚠️'
            elif severity == 'info':
                assessment_bg = '#e3f0ff'
                assessment_border = '#5c6bc0'
                assessment_icon = 'ℹ️'
            else:
                assessment_bg = '#f5e9e3'
                assessment_border = '#bf971b'
                assessment_icon = 'ℹ️'
            
            html += f"""
            <div style="background-color: {assessment_bg}; padding: 12px 16px; margin: 10px 0; 
                        border-left: 4px solid {assessment_border}; border-radius: 0 4px 4px 0;">
                <p style="margin: 0; font-size: 14px;"><strong>{assessment_icon} Diagnosis:</strong> {diagnosis}</p>
            </div>
            """
            
            # Overall results
            violation_pct = signalstats_data.get('violation_percentage')
            max_brng = signalstats_data.get('max_brng')
            avg_brng = signalstats_data.get('avg_brng')
            used_qctools = signalstats_data.get('used_qctools', False)
            
            if violation_pct is not None or max_brng is not None:
                # Label the heading with the region the stats were measured on
                # ('' for legacy results that didn't record it)
                analyzed_region = signalstats_data.get('analyzed_region', '')
                region_label = {
                    'active_area': ' (measured on the active picture area)',
                    'full_frame': ' (measured on the full frame — borders not excluded)',
                    'mixed': ' (mixed: some periods active picture area, some full frame)',
                }.get(analyzed_region, '')

                html += f"""
                <div style="margin: 16px 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">Overall Results{region_label}</p>
                    <table style="border-collapse: collapse; width: auto; margin: 0;">
                """

                stat_rows = []
                if violation_pct is not None:
                    stat_rows.append(("Frames with any out-of-range pixels",
                                      f"{violation_pct:.1f}% of analyzed frames"))
                if avg_brng is not None:
                    stat_rows.append(("Average out-of-range share",
                                      f"{avg_brng:.4f}% of pixels per analyzed frame"))
                if max_brng is not None:
                    stat_rows.append(("Worst frame",
                                      f"{max_brng:.4f}% of pixels out of range"))
                stat_rows.append(("Data source", "QCTools + FFprobe comparison" if used_qctools else "FFprobe signalstats"))
                
                for label, value in stat_rows:
                    html += f"""
                    <tr>
                        <td style="padding: 4px 12px 4px 0; color: #555; font-size: 13px; border: none; white-space: nowrap;">{label}</td>
                        <td style="padding: 4px 0; font-weight: bold; font-size: 13px; border: none;">{value}</td>
                    </tr>
                    """
                
                html += "</table></div>"
            
            # Display results for active area (legacy format)
            if signalstats_data.get('results', {}).get('active_area'):
                active_area = signalstats_data['results']['active_area']
                html += f"""
                <div style="margin: 16px 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">Active Area Results</p>
                    <table style="border-collapse: collapse; width: auto; margin: 0;">
                        <tr>
                            <td style="padding: 4px 12px 4px 0; color: #555; font-size: 13px; border: none;">Frames with violations</td>
                            <td style="padding: 4px 0; font-weight: bold; font-size: 13px; border: none;">{active_area['frames_with_violations']}/{active_area['frames_analyzed']} ({active_area['violation_percentage']:.1f}%)</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 12px 4px 0; color: #555; font-size: 13px; border: none;">Average BRNG</td>
                            <td style="padding: 4px 0; font-weight: bold; font-size: 13px; border: none;">{active_area['avg_brng']:.4f}%</td>
                        </tr>
                        <tr>
                            <td style="padding: 4px 12px 4px 0; color: #555; font-size: 13px; border: none;">Maximum BRNG</td>
                            <td style="padding: 4px 0; font-weight: bold; font-size: 13px; border: none;">{active_area['max_brng']:.4f}%</td>
                        </tr>
                    </table>
                </div>
                """
            
            # Per-period comparison cards
            comparison_results = signalstats_data.get('comparison_results', [])
            analysis_periods = signalstats_data.get('analysis_periods', [])
            
            if comparison_results:
                # Build time range display
                all_starts = []
                all_ends = []
                for comp in comparison_results:
                    tr = comp.get('time_range')
                    if tr and len(tr) >= 2:
                        all_starts.append(tr[0])
                        all_ends.append(tr[1])
                
                coverage_label = ""
                if all_starts and all_ends:
                    coverage_label = f" ({_seconds_to_display(min(all_starts))} – {_seconds_to_display(max(all_ends))})"
                
                html += f"""
                <div style="margin: 16px 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">
                        Period Comparison: {len(comparison_results)} periods{coverage_label}
                    </p>
                """
                
                for comp in comparison_results:
                    period_num = comp.get('period', '?')
                    time_range = comp.get('time_range', [0, 0])
                    start_display = _seconds_to_display(time_range[0]) if len(time_range) >= 1 else "?"
                    end_display = _seconds_to_display(time_range[1]) if len(time_range) >= 2 else "?"
                    
                    qc_data = comp.get('qctools_full_frame', {})
                    ff_data = comp.get('ffprobe_active_area', {})
                    period_diag = comp.get('diagnosis', '')
                    
                    # Diagnosis badge
                    if period_diag == 'border_violations':
                        diag_label = 'Border violations'
                        diag_color = '#bf971b'
                        diag_bg = '#fff3cd'
                    elif period_diag == 'content_violations':
                        diag_label = 'Content violations'
                        diag_color = '#d32f2f'
                        diag_bg = '#ffbaba'
                    elif period_diag == 'minimal_violations':
                        diag_label = 'Minimal'
                        diag_color = '#378d6a'
                        diag_bg = '#d2ffed'
                    else:
                        diag_label = ''
                        diag_color = ''
                        diag_bg = ''
                    
                    html += f"""
                    <div style="background-color: #f5e9e3; padding: 10px 14px; margin: 6px 0; 
                                border-radius: 4px; border: 1px solid #e0d0c0;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight: bold; color: #4d2b12;">
                                Period {period_num}: {start_display} – {end_display}
                            </span>
                    """
                    
                    if diag_label:
                        html += f"""
                            <span style="background: {diag_bg}; color: {diag_color}; padding: 2px 8px; 
                                        border-radius: 3px; font-size: 12px; font-weight: bold;">{diag_label}</span>
                        """
                    
                    html += "</div>"
                    
                    # Comparison bars
                    if qc_data or ff_data:
                        qc_pct = qc_data.get('violations_pct', 0)
                        ff_pct = ff_data.get('violations_pct', 0)
                        qc_frames = qc_data.get('frames_analyzed', 0)
                        qc_violations = qc_data.get('frames_with_violations', 0)
                        ff_frames = ff_data.get('frames_analyzed', 0)
                        ff_violations = ff_data.get('frames_with_violations', 0)
                        qc_max = qc_data.get('max_brng', 0)
                        ff_max = ff_data.get('max_brng', 0)
                        
                        # Scale bars to the larger value
                        max_pct = max(qc_pct, ff_pct, 0.1)
                        
                        if qc_data:
                            qc_bar_width = (qc_pct / max_pct * 100) if max_pct > 0 else 0
                            qc_bar_color = '#bf971b' if qc_pct > 10 else '#607d8b'
                            html += f"""
                            <div style="margin-bottom: 6px;">
                                <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 2px;">
                                    <span style="color: #555;">Full frame (QCTools)</span>
                                    <span style="color: #666;">{qc_violations}/{qc_frames} frames ({qc_pct:.1f}%) · max {qc_max:.4f}%</span>
                                </div>
                                <div style="background-color: #e8ddd5; border-radius: 3px; height: 10px; overflow: hidden;">
                                    <div style="background-color: {qc_bar_color}; height: 100%; width: {qc_bar_width:.1f}%; 
                                                min-width: {('2px' if qc_pct > 0 else '0')}; border-radius: 3px;"></div>
                                </div>
                            </div>
                            """
                        
                        if ff_data:
                            ff_bar_width = (ff_pct / max_pct * 100) if max_pct > 0 else 0
                            ff_bar_color = '#d32f2f' if ff_pct > 10 else '#378d6a'
                            html += f"""
                            <div style="margin-bottom: 4px;">
                                <div style="display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 2px;">
                                    <span style="color: #555;">Active area (FFprobe)</span>
                                    <span style="color: #666;">{ff_violations}/{ff_frames} frames ({ff_pct:.1f}%) · max {ff_max:.4f}%</span>
                                </div>
                                <div style="background-color: #e8ddd5; border-radius: 3px; height: 10px; overflow: hidden;">
                                    <div style="background-color: {ff_bar_color}; height: 100%; width: {ff_bar_width:.1f}%; 
                                                min-width: {('2px' if ff_pct > 0 else '0')}; border-radius: 3px;"></div>
                                </div>
                            </div>
                            """
                        
                        # Show delta if both sources present
                        if qc_data and ff_data:
                            delta = qc_pct - ff_pct
                            if abs(delta) > 0.5:
                                delta_label = f"Full frame has {delta:.1f}% more violations than active area" if delta > 0 else f"Active area has {abs(delta):.1f}% more violations than full frame"
                                html += f"""
                                <div style="font-size: 11px; color: #888; margin-top: 2px;">
                                    → {delta_label}
                                </div>
                                """
                    
                    html += "</div>"
                
                html += "</div>"
            
            elif analysis_periods:
                # Fallback: display analysis periods without comparison data
                periods = analysis_periods
                
                # Build time range display
                all_starts = []
                all_ends = []
                for period in periods:
                    if isinstance(period, (list, tuple)) and len(period) >= 2:
                        all_starts.append(period[0])
                        all_ends.append(period[0] + period[1])
                    elif isinstance(period, dict):
                        s = period.get('start_time', period.get('start', 0))
                        d = period.get('duration', 60)
                        all_starts.append(s)
                        all_ends.append(s + d)
                
                coverage_label = ""
                if all_starts and all_ends:
                    coverage_label = f" ({_seconds_to_display(min(all_starts))} – {_seconds_to_display(max(all_ends))})"
                
                html += f"""
                <div style="margin: 16px 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">
                        Analysis Periods: {len(periods)} periods{coverage_label}
                    </p>
                """
                
                for i, period in enumerate(periods[:5]):
                    if isinstance(period, (list, tuple)) and len(period) >= 2:
                        start, duration = period[0], period[1]
                    elif isinstance(period, dict):
                        start = period.get('start_time', period.get('start', 0))
                        duration = period.get('duration', 60)
                    else:
                        continue
                    
                    start_display = _seconds_to_display(start)
                    end_display = _seconds_to_display(start + duration)
                    
                    html += f"""
                    <div style="background-color: #f5e9e3; padding: 8px 14px; margin: 4px 0; 
                                border-radius: 4px; border: 1px solid #e0d0c0; font-size: 13px;">
                        <span style="font-weight: bold; color: #4d2b12;">Period {i+1}:</span> 
                        {start_display} – {end_display} ({duration}s duration)
                    </div>
                    """
                
                html += "</div>"
                
        except Exception as e:
            logger.error(f"Error reading signalstats analysis: {e}")
    
    # BRNG Analysis Section
    if frame_outputs['brng_analysis']:
        html += "<h3 id='section-brng-analysis' style='color: #bf971b;'>BRNG Violation Analysis</h3>"
        
        # Methodology explanation (collapsible)
        html += """
        <a id="link_brng_methodology" href="javascript:void(0);" 
           onclick="toggleContent('brng_methodology', 'What is BRNG analysis? ▼', 'What is BRNG analysis? ▲')" 
           style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
           What is BRNG analysis? ▼</a>
        <div id="brng_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px; 
             margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
            <p style="margin: 0 0 10px 0;">
                <strong>BRNG (Broadcast Range)</strong> measures whether pixel values fall outside the 
                broadcast-legal range (16–235 for luma, 16–240 for chroma in 8-bit video). Pixels outside 
                this range may be clipped during broadcast or indicate issues in the source material.
            </p>
            <p style="margin: 0 0 10px 0; font-weight: bold;">How violations are detected:</p>
            <p style="margin: 0 0 6px 0;">
                AV Spex uses <em>differential detection</em> to identify violations. For each analysis period, 
                two temporary video segments are created from the active picture area:
            </p>
            <ol style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>Highlighted version</strong> — rendered with FFmpeg's 
                    <code style="background: #eee; padding: 1px 4px; border-radius: 2px;">signalstats=out=brng:color=magenta</code>, 
                    which overlays magenta on out-of-range pixels</li>
                <li style="margin-bottom: 4px;"><strong>Original version</strong> — rendered without the filter 
                    (cropped to active area only)</li>
            </ol>
            <p style="margin: 0 0 6px 0;">
                Frames are then compared pixel-by-pixel using three independent detection methods that vote 
                on whether a pixel is a genuine violation:
            </p>
            <ol style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>BGR threshold</strong> — checks for magenta color signature 
                    (high red + blue, low green channel differences)</li>
                <li style="margin-bottom: 4px;"><strong>Ratio-based</strong> — verifies that red and blue 
                    channel increases are proportional (characteristic of magenta overlay)</li>
                <li style="margin-bottom: 4px;"><strong>HSV analysis</strong> — confirms magenta hue range with 
                    saturation increase in HSV color space</li>
            </ol>
            <p style="margin: 0 0 10px 0;">
                A pixel is classified as a violation only when <strong>at least 2 of 3 methods agree</strong>. 
                Small isolated pixel clusters (fewer than 10 connected pixels) are filtered out as noise.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Violation classification:</p>
            <p style="margin: 0 0 4px 0;">Each frame with detected violations is then classified by spatial pattern:</p>
            <ul style="margin: 4px 0 10px 16px; padding: 0;">
                <li style="margin-bottom: 3px;"><strong>Sub-black</strong> — violations concentrated in low-luma 
                    zones (pixels below broadcast black level)</li>
                <li style="margin-bottom: 3px;"><strong>Highlight clipping</strong> — violations in high-luma 
                    zones (pixels above broadcast white level)</li>
                <li style="margin-bottom: 3px;"><strong>Edge artifacts</strong> — violations concentrated within 
                    15px of frame edges, suggesting border/blanking issues</li>
                <li style="margin-bottom: 3px;"><strong>Linear blanking patterns</strong> — edge violations 
                    forming continuous horizontal or vertical lines</li>
                <li style="margin-bottom: 3px;"><strong>General broadcast range violations</strong> — violations 
                    that don't match a specific spatial pattern</li>
            </ul>
            <p style="margin: 0 0 6px 0; font-weight: bold;">How frames are sampled:</p>
            <p style="margin: 0 0 6px 0;">
                Each analysis period reports a sampling summary in the format:<br>
                <code style="background: #eee; padding: 1px 4px; border-radius: 2px;">QCTools targeted N frames → M mapped to period → T total samples analyzed</code>
            </p>
            <ul style="margin: 4px 0 10px 16px; padding: 0;">
                <li style="margin-bottom: 3px;"><strong>QCTools targeted</strong> — the number of BRNG violations 
                    found in the initial scan of the full QCTools report (capped at 100). These are the frames 
                    QCTools flagged as having out-of-range pixels anywhere in the video.</li>
                <li style="margin-bottom: 3px;"><strong>Mapped to period</strong> — how many of those violations 
                    have timestamps that fall within this specific analysis period's time window. Violations 
                    from other parts of the video are not relevant to this period.</li>
                <li style="margin-bottom: 3px;"><strong>Total samples analyzed</strong> — the actual number of 
                    frames examined by the differential detector. The sample count adapts based on signalstats 
                    findings for each period: periods with significant active-area violations receive denser 
                    sampling (~200 frames) while periods with negligible active-area BRNG use lighter sampling 
                    (~30 frames). When no upstream data is available, the default behavior targets ~50–100 frames 
                    per period.</li>
            </ul>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Adaptive detection:</p>
            <p style="margin: 0 0 10px 0;">
                When signalstats analysis is available, BRNG detection adapts per period based on the 
                signalstats diagnosis. Periods diagnosed as <em>border-dominated</em> or <em>minimal</em> 
                use stricter detection thresholds (requiring stronger evidence to classify a pixel as a 
                violation), reducing false positives in regions where actual content violations are unlikely. 
                Periods with <em>content violations</em> use standard sensitivity. When head switching 
                artifacts were detected during border detection, the bottom-edge analysis zone is 
                automatically widened to classify head switching noise as edge artifacts rather than 
                content violations.
            </p>
            <p style="margin: 0; color: #777;">
                Because the differential detector compares two decoded video frames and runs multi-method 
                computer vision analysis on every sample, this targeted approach keeps processing time 
                manageable while concentrating analysis on the frames most likely to contain violations.
            </p>
        </div>
        """
        
        try:
            # Handle both file path (string) and dict (from enhanced JSON)
            if isinstance(frame_outputs['brng_analysis'], str):
                with open(frame_outputs['brng_analysis'], 'r') as f:
                    brng_data = json.load(f)
            else:
                brng_data = frame_outputs['brng_analysis']
            
            # Get actionable report
            report = brng_data.get('actionable_report', {})
            aggregate = brng_data.get('aggregate_patterns', {})
            violations = brng_data.get('violations', [])
            period_summaries = brng_data.get('period_summaries', [])
            analysis_periods = brng_data.get('analysis_periods', [])
            
            # Overall assessment banner
            assessment = report.get('overall_assessment', 'Analysis complete')
            stats = report.get('summary_statistics', {})
            
            # Determine assessment styling
            edge_pct = aggregate.get('edge_violation_percentage', 0)
            avg_violation = stats.get('average_violation_percentage', 0)
            max_violation = stats.get('max_violation_percentage', 0)
            
            if avg_violation >= 10:
                assessment_bg = '#ffbaba'
                assessment_border = '#d32f2f'
                assessment_icon = '⛔'
            elif edge_pct > 50:
                assessment_bg = '#fff3cd'
                assessment_border = '#bf971b'
                assessment_icon = '⚠️'
            elif len(violations) > 0:
                assessment_bg = '#e8f4fd'
                assessment_border = '#1976d2'
                assessment_icon = 'ℹ️'
            else:
                assessment_bg = '#d2ffed'
                assessment_border = '#2e7d32'
                assessment_icon = '✅'
            
            html += f"""
            <div style="background-color: {assessment_bg}; padding: 12px 16px; margin: 10px 0; 
                        border-left: 4px solid {assessment_border}; border-radius: 0 4px 4px 0;">
                <p style="margin: 0; font-size: 14px;"><strong>{assessment_icon} Assessment:</strong> {assessment}</p>
            </div>
            """
            
            # ── Period-by-Period Analysis ──
            if period_summaries:
                # Calculate overall time range
                all_starts = [p.get('start_time', 0) for p in period_summaries]
                all_ends = [p.get('end_time', 0) for p in period_summaries]
                time_range_start = min(all_starts) if all_starts else 0
                time_range_end = max(all_ends) if all_ends else 0
                total_violations_across = sum(p.get('violations_found', 0) for p in period_summaries)
                
                html += f"""
                <div style="margin: 16px 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">
                        Analysis Coverage: {len(period_summaries)} periods 
                        ({_seconds_to_display(time_range_start)} – {_seconds_to_display(time_range_end)})
                    </p>
                """
                
                for ps in period_summaries:
                    p_num = ps.get('period_num', '?')
                    p_start = ps.get('start_time', 0)
                    p_end = ps.get('end_time', 0)
                    qct_targeted = ps.get('qctools_frames_targeted', 0)
                    frames_mapped = ps.get('frames_mapped', 0)
                    total_samples = ps.get('total_samples', 0)
                    checked = ps.get('frames_checked', 0)
                    found = ps.get('violations_found', 0)
                    ss_diagnosis = ps.get('signalstats_diagnosis', '')
                    sensitivity_used = ps.get('sensitivity_used', '')
                    ss_active_pct = ps.get('signalstats_active_area_pct', None)
                    
                    # Bar width as proportion of violations found vs frames checked
                    bar_pct = (found / checked * 100) if checked > 0 else 0
                    bar_color = '#d32f2f' if bar_pct > 50 else '#bf971b' if bar_pct > 20 else '#1976d2'
                    
                    html += f"""
                    <div style="background-color: #f5e9e3; padding: 10px 14px; margin: 6px 0; 
                                border-radius: 4px; border: 1px solid #e0d0c0;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                            <span style="font-weight: bold; color: #4d2b12;">
                                Period {p_num}: {_seconds_to_display(p_start)} – {_seconds_to_display(p_end)}
                            </span>
                            <span style="display: flex; align-items: center; gap: 6px;">
                    """
                    
                    # Signalstats diagnosis badge
                    if ss_diagnosis:
                        diag_labels = {
                            'border_violations': ('Border-dominated', '#bf971b', '#fff3cd'),
                            'content_violations': ('Content violations', '#d32f2f', '#ffbaba'),
                            'minimal_violations': ('Minimal signal', '#378d6a', '#d2ffed')
                        }
                        label, color, bg = diag_labels.get(ss_diagnosis, ('', '#666', '#eee'))
                        if label:
                            html += f"""
                                <span style="background: {bg}; color: {color}; padding: 2px 8px; 
                                            border-radius: 3px; font-size: 11px; font-weight: bold;"
                                      title="Signalstats diagnosis for this period">{label}</span>
                            """
                    
                    html += f"""
                                <span style="font-size: 13px; color: #666;">
                                    {found} violation{'s' if found != 1 else ''} / {checked} frames checked
                                </span>
                            </span>
                        </div>
                        <div style="background-color: #e8ddd5; border-radius: 3px; height: 14px; overflow: hidden; margin-bottom: 6px;">
                            <div style="background-color: {bar_color}; height: 100%; width: {bar_pct:.1f}%; 
                                        min-width: {('2px' if found > 0 else '0')}; border-radius: 3px; 
                                        transition: width 0.3s;"></div>
                        </div>
                        <div style="font-size: 12px; color: #777;">
                            QCTools targeted {qct_targeted} frames → {frames_mapped} mapped to period → {total_samples} total samples analyzed
                    """
                    
                    # Show sensitivity and signalstats context on a second line if available
                    context_parts = []
                    if sensitivity_used and sensitivity_used != 'normal':
                        context_parts.append(f"{sensitivity_used} detection sensitivity")
                    if ss_active_pct is not None and ss_active_pct > 0:
                        context_parts.append(f"signalstats active-area: {ss_active_pct:.1f}%")
                    if context_parts:
                        html += f"""
                            <br>{'  ·  '.join(context_parts)}
                        """
                    
                    html += """
                        </div>
                    </div>
                    """
                
                html += "</div>"
            
            elif analysis_periods:
                # Fall back to showing period ranges without per-period stats
                time_range_start = min(p[0] for p in analysis_periods)
                time_range_end = max(p[0] + p[1] for p in analysis_periods)
                html += f"""
                <p style="color: #666; font-size: 13px;">
                    Analyzed {len(analysis_periods)} periods spanning 
                    {_seconds_to_display(time_range_start)} – {_seconds_to_display(time_range_end)}
                </p>
                """
            
            # Flex wrapper so "Violation Types Detected" and "Violation
            # Statistics" render side-by-side.
            viol_flex_open = bool(violations) or bool(stats or aggregate)
            if viol_flex_open:
                html += (
                    '<div style="display: flex; flex-wrap: wrap; gap: 24px; '
                    'align-items: flex-start; margin: 16px 0;">'
                )

            # ── Violation Types Breakdown ──
            if violations:
                diagnostic_counts = {}
                edge_artifact_edges = set()
                
                for v in violations:
                    diags = v.get('diagnostics', [])
                    if diags:
                        for diag in diags:
                            if diag.startswith("Edge artifacts"):
                                diagnostic_counts["Edge artifacts"] = diagnostic_counts.get("Edge artifacts", 0) + 1
                                if "(" in diag and ")" in diag:
                                    edges_str = diag[diag.find("(")+1:diag.find(")")]
                                    for edge in edges_str.split(", "):
                                        edge_artifact_edges.add(edge.strip())
                            elif diag == "Border adjustment recommended":
                                continue
                            else:
                                diagnostic_counts[diag] = diagnostic_counts.get(diag, 0) + 1
                
                if diagnostic_counts:
                    total_v = len(violations)
                    refinement_iters = frame_outputs.get('refinement_iterations', 0)
                    
                    # Build initial-run diagnostics for comparison if refinement occurred
                    initial_diag_counts = {}
                    if refinement_iters > 0 and frame_outputs.get('initial_brng_analysis'):
                        initial_violations = frame_outputs['initial_brng_analysis'].get('violations', [])
                        for v in initial_violations:
                            for diag in v.get('diagnostics', []):
                                key = "Edge artifacts" if diag.startswith("Edge artifacts") else diag
                                if key == "Border adjustment recommended":
                                    continue
                                initial_diag_counts[key] = initial_diag_counts.get(key, 0) + 1

                    html += """
                    <div style="flex: 1 1 320px; min-width: 0;">
                        <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">Violation Types Detected</p>
                    """
                    
                    # Sort by count (descending)
                    priority_order = ["Sub-black detected", "Highlight clipping", "Edge artifacts", 
                                     "Linear blanking patterns",
                                     "General broadcast range violations"]
                    
                    sorted_diags = []
                    for diag_type in priority_order:
                        if diag_type in diagnostic_counts:
                            sorted_diags.append((diag_type, diagnostic_counts[diag_type]))
                    for diag_type, count in diagnostic_counts.items():
                        if diag_type not in priority_order:
                            sorted_diags.append((diag_type, count))
                    
                    # Type-specific colors
                    type_colors = {
                        "Sub-black detected": "#5c6bc0",
                        "Highlight clipping": "#ef6c00",
                        "Edge artifacts": "#bf971b",
                        "Linear blanking patterns": "#7b1fa2",
                        "Continuous edge artifacts": "#bf971b",
                        "General broadcast range violations": "#607d8b",
                        "Border detection likely missed blanking": "#d32f2f",
                        "Moderate blanking detected": "#f57c00"
                    }
                    
                    for diag_type, count in sorted_diags:
                        pct = (count / total_v) * 100
                        bar_color = type_colors.get(diag_type, '#90a4ae')
                        
                        # Show before → after if refinement happened
                        initial_count = initial_diag_counts.get(diag_type)
                        count_display = f"{count} frames ({pct:.1f}%)"
                        if initial_count is not None and refinement_iters > 0:
                            initial_total = len(initial_violations) if initial_violations else 1
                            initial_pct = (initial_count / initial_total) * 100
                            count_display = (
                                f'<span style="color: #999; text-decoration: line-through;">'
                                f'{initial_count} ({initial_pct:.1f}%)</span>'
                                f' → {count} frames ({pct:.1f}%)'
                            )
                        
                        # Build label
                        label = diag_type
                        if diag_type == "Edge artifacts" and edge_artifact_edges:
                            label = f"Edge artifacts ({', '.join(sorted(edge_artifact_edges))})"
                        
                        html += f"""
                        <div style="margin: 4px 0;">
                            <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 2px;">
                                <span style="color: #333;">{label}</span>
                                <span style="color: #666;">{count_display}</span>
                            </div>
                            <div style="background-color: #e8ddd5; border-radius: 3px; height: 10px; overflow: hidden;">
                                <div style="background-color: {bar_color}; height: 100%; width: {pct:.1f}%; 
                                            min-width: 2px; border-radius: 3px;"></div>
                            </div>
                        </div>
                        """
                    
                    # High edge percentage warning
                    if edge_pct > 50:
                        html += f"""
                        <div style="background-color: #fff3cd; padding: 8px 12px; margin-top: 8px; 
                                    border-left: 3px solid #bf971b; border-radius: 0 4px 4px 0; font-size: 13px;">
                            ⚠ High edge percentage ({edge_pct:.1f}%) suggests border detection may need adjustment
                        </div>
                        """
                    
                    html += "</div>"
            
            # ── Violation Statistics ──
            if stats or aggregate:
                continuous_pct = aggregate.get('continuous_edge_percentage', 0)
                linear_pct = aggregate.get('linear_pattern_percentage', 0)
                
                html += """
                <div style="flex: 1 1 320px; min-width: 0;">
                    <p style="font-weight: bold; margin-bottom: 8px; color: #4d2b12;">Violation Statistics</p>
                    <table style="border-collapse: collapse; width: auto; margin: 0;">
                """
                
                stat_rows = [
                    ("Frames with violations", f"{stats.get('total_violations', 0)}"),
                    ("Average BRNG", f"{avg_violation:.2f}%"),
                    ("Maximum BRNG", f"{max_violation:.2f}%"),
                    ("Edge violations (any)", f"{edge_pct:.1f}% of analyzed frames"),
                    ("Edge violations (solid line)", f"{continuous_pct:.1f}% of analyzed frames"),
                ]
                
                if linear_pct > 0:
                    stat_rows.append(("Linear patterns", f"{linear_pct:.1f}% of analyzed frames"))
                
                # Add note about scattered vs solid edges
                if continuous_pct == 0 and edge_pct > 95:
                    stat_rows.append(("", "→ Violations are scattered rather than forming a solid line"))
                
                for label, value in stat_rows:
                    if label:
                        html += f"""
                        <tr>
                            <td style="padding: 4px 12px 4px 0; color: #555; font-size: 13px; border: none; white-space: nowrap;">{label}</td>
                            <td style="padding: 4px 0; font-weight: bold; font-size: 13px; border: none;">{value}</td>
                        </tr>
                        """
                    else:
                        # Note row (spans both columns)
                        html += f"""
                        <tr>
                            <td colspan="2" style="padding: 4px 0; font-size: 12px; color: #888; border: none; font-style: italic;">{value}</td>
                        </tr>
                        """
                
                html += """
                    </table>
                </div>
                """

            if viol_flex_open:
                html += "</div>"

            # ── Content Start Detection ──
            skip_info = brng_data.get('skip_info', {})
            if skip_info and skip_info.get('total_skipped_seconds', 0) > 0:
                html += f"""
                <div style="background-color: #f5e9e3; padding: 10px; margin: 10px 0; border-radius: 4px;">
                    <p style="margin: 0; font-size: 13px;"><strong>Content Start Detection:</strong> 
                    Skipped first {skip_info['total_skipped_seconds']:.1f} seconds (test patterns/color bars)</p>
                </div>
                """
            
            # ── Recommendations ──
            if report.get('recommendations'):
                html += "<div style='margin: 16px 0;'><p style='font-weight: bold; margin-bottom: 8px; color: #4d2b12;'>Recommendations</p>"
                for rec in report['recommendations']:
                    severity = rec.get('severity', 'low')
                    if severity == 'high':
                        rec_bg = '#ffbaba'
                        rec_border = '#d32f2f'
                    elif severity == 'medium':
                        rec_bg = '#fff3cd'
                        rec_border = '#bf971b'
                    else:
                        rec_bg = '#e8f4fd'
                        rec_border = '#1976d2'
                    
                    html += f"""
                    <div style="background-color: {rec_bg}; padding: 8px 12px; margin: 4px 0; 
                                border-left: 3px solid {rec_border}; border-radius: 0 4px 4px 0; font-size: 13px;">
                        <strong>{rec.get('issue', 'Unknown issue')}</strong>
                    """
                    if rec.get('description'):
                        html += f"<br><span style='color: #555;'>{rec['description']}</span>"
                    html += "</div>"
                html += "</div>"
                
        except Exception as e:
            logger.error(f"Error reading BRNG analysis: {e}")
    
    # BRNG Diagnostic Thumbnails
    if frame_outputs['brng_thumbnails']:
        # Try to get violations data for thumbnail metadata
        thumb_violations_data = []
        try:
            if isinstance(frame_outputs['brng_analysis'], str):
                with open(frame_outputs['brng_analysis'], 'r') as f:
                    _brng_data = json.load(f)
            elif isinstance(frame_outputs['brng_analysis'], dict):
                _brng_data = frame_outputs['brng_analysis']
            else:
                _brng_data = {}
            thumb_violations_data = _brng_data.get('violations', [])
        except Exception:
            pass
        
        num_thumbs = len(frame_outputs['brng_thumbnails'])
        
        html += f"""
        <h3 style='color: #bf971b;'>BRNG Diagnostic Thumbnails</h3>
        <a id="link_brng_thumb_info" href="javascript:void(0);" 
           onclick="toggleContent('brng_thumb_info', 'How are thumbnails selected? ▼', 'How are thumbnails selected? ▲')" 
           style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
           How are thumbnails selected? ▼</a>
        <div id="brng_thumb_info" style="display: none; background-color: #f8f6f3; padding: 12px 14px; margin: 0 0 12px 0; 
                    border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
            <p style="margin: 0 0 6px 0;">
                <strong>Thumbnail selection:</strong> Up to 5 diagnostic thumbnails are chosen from the 
                frames with detected violations. The highest-scoring violation frame is always included. 
                Additional frames are selected in descending order of violation score, subject to a 
                <strong>minimum 10-second temporal separation</strong> from all previously selected frames. 
                This ensures thumbnails represent different moments in the video rather than clustering 
                around a single event. If fewer than 5 frames meet the separation requirement, remaining 
                slots are filled by the next highest-scoring frames regardless of spacing.
            </p>
            <p style="margin: 0; color: #777;">
                Each thumbnail is a 4-quadrant diagnostic image: <strong>Original</strong> (top-left), 
                <strong>BRNG Highlighted</strong> (top-right, magenta = out-of-range pixels), 
                <strong>Violations Only</strong> (bottom-left, yellow on black), and 
                <strong>Analysis Data</strong> (bottom-right, frame number, timestamp, BRNG %, pixel count, 
                and diagnostic classification).
            </p>
        </div>
        <div style="display: flex; flex-wrap: wrap; gap: 10px; margin: 10px 0;">
        """
        
        # Sort thumbnails by filename (which includes timecode)
        sorted_thumbs = sorted(frame_outputs['brng_thumbnails'])
        
        for thumb_idx, thumb_path in enumerate(sorted_thumbs[:6]):  # Limit to 6 thumbnails in report
            # Extract timecode from filename if possible
            filename = os.path.basename(thumb_path)
            parts = filename.split('_')
            timecode = "Unknown"
            timestamp_seconds = None
            if len(parts) >= 3:
                timecode = parts[-1].replace('.jpg', '').replace('.png', '').replace('-', ':')
                # Try to parse as seconds for matching
                try:
                    timestamp_seconds = float(timecode.replace('s', ''))
                except (ValueError, AttributeError):
                    pass
            
            # Try to find matching violation data for this thumbnail
            thumb_score = None
            thumb_diags = None
            thumb_brng_pct = None
            if timestamp_seconds is not None and thumb_violations_data:
                for v in thumb_violations_data:
                    v_ts = v.get('timestamp', 0)
                    if abs(v_ts - timestamp_seconds) < 0.2:  # Match within 0.2s
                        thumb_score = v.get('violation_score')
                        thumb_brng_pct = v.get('violation_percentage')
                        thumb_diags = v.get('diagnostics', [])
                        break
            
            with open(thumb_path, "rb") as img_file:
                encoded_thumb = b64encode(img_file.read()).decode()
            
            # Build caption
            caption_parts = [f"Frame at {timecode}"]
            if thumb_score is not None:
                caption_parts.append(f"score: {thumb_score:.4f}")
            if thumb_brng_pct is not None:
                caption_parts.append(f"BRNG: {thumb_brng_pct:.2f}%")
            
            caption_line1 = caption_parts[0]
            caption_line2 = ", ".join(caption_parts[1:]) if len(caption_parts) > 1 else ""
            
            # Diagnostic badge
            diag_html = ""
            if thumb_diags:
                diag_labels = [d for d in thumb_diags if d != "Border adjustment recommended"][:2]
                if diag_labels:
                    badges = ""
                    for dl in diag_labels:
                        badges += f'<span style="display: inline-block; background: #e8ddd5; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin: 1px 2px;">{dl}</span>'
                    diag_html = f'<div style="margin-top: 3px;">{badges}</div>'
            
            html += f"""
            <div style="text-align: center; margin: 5px;">
                <img src="data:image/jpeg;base64,{encoded_thumb}"
                     style="width: 300px; height: auto; border: 1px solid #4d2b12; cursor: pointer;"
                     onclick="openImage(this.src, 'BRNG Diagnostic - {caption_line1}')"
                     title="Click to enlarge" />
                <p style="font-size: 12px; margin: 4px 0 0 0;">{caption_line1}</p>
            """
            if caption_line2:
                html += f'<p style="font-size: 11px; margin: 1px 0 0 0; color: #777;">{caption_line2}</p>'
            html += diag_html
            html += "</div>"
        
        html += "</div>"
        
        if num_thumbs > 6:
            html += f"<p style='font-style: italic; font-size: 13px;'>Showing 6 of {num_thumbs} diagnostic thumbnails</p>"
    
    html += "</div>"
    return html


def generate_dropped_sample_html(frame_outputs):
    """
    Generates an HTML section for dropped sample detection results.

    Args:
        frame_outputs (dict): Dictionary of frame analysis output paths and data.

    Returns:
        str: HTML string with dropped sample detection section, or empty string if no data.
    """
    dropped_sample_data = frame_outputs.get('dropped_sample_detection')
    if not dropped_sample_data:
        return ""

    html = "<h3>Dropped Sample Detection</h3>"

    # Collapsible methodology explanation
    html += """
    <a id="link_dropped_sample_methodology" href="javascript:void(0);"
       onclick="toggleContent('dropped_sample_methodology', 'What is dropped sample detection? ▼', 'What is dropped sample detection? ▲')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is dropped sample detection? ▼</a>
    <div id="dropped_sample_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Dropped sample detection</strong> identifies potential audio sample drops caused by
            TBC/framesync devices or analog-to-digital converters during digitization. Two indicators are analyzed:
        </p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>Spectrogram spike analysis</strong> &mdash; A spectrogram
                of the full audio is generated using FFmpeg. Bright vertical lines spanning the entire frequency
                range indicate audible pops/clicks from dropped samples. The spectrogram image is analyzed
                programmatically to detect and count these spikes.</li>
            <li style="margin-bottom: 4px;"><strong>Audio/video duration mismatch</strong> &mdash; Dropped
                samples cause the audio stream to be slightly shorter than the video stream. Any measurable
                difference (&gt;0ms) between audio and video stream durations is flagged.</li>
        </ul>
        <p style="margin: 0;">
            Both signals are combined into a weighted risk score. When both indicators are present,
            the score is escalated to reflect higher confidence that samples were dropped.
        </p>
    </div>
    """

    status = dropped_sample_data.get('status', 'unknown')
    message = dropped_sample_data.get('message', '')
    spike_count = dropped_sample_data.get('spike_count', 0)
    duration_diff_ms = dropped_sample_data.get('duration_diff_ms', 0.0)
    audio_duration = dropped_sample_data.get('audio_duration', 0.0)
    video_duration = dropped_sample_data.get('video_duration', 0.0)
    combined_score = dropped_sample_data.get('combined_score', 0.0)
    estimated_loss_ms = dropped_sample_data.get('estimated_loss_ms', 0.0)
    sample_rate = dropped_sample_data.get('sample_rate', 0)
    spike_timestamps = dropped_sample_data.get('spike_timestamps', [])

    if status == 'critical':
        status_color = '#cc0000'
        status_icon = '&#x26A0;'
    elif status == 'warning':
        status_color = '#cc6600'
        status_icon = '&#x26A0;'
    elif status == 'clean':
        status_color = '#0a5f1c'
        status_icon = '&#x2705;'
    else:
        status_color = '#666666'
        status_icon = '&#x2753;'

    html += f"""
    <p style="font-size: 14px; color: {status_color}; font-weight: bold;">
        {status_icon} {message}
    </p>
    """

    # Embed spectrogram image. The on-disk PNG is kept lossless for cv2 spike
    # analysis in frame_analysis.py; here we transcode to JPEG in memory just
    # for the report, which cuts the embedded payload ~5–10x with no impact on
    # the analysis step.
    spectrogram_path = frame_outputs.get('dropped_sample_spectrogram')
    if spectrogram_path:
        try:
            import cv2
            img = cv2.imread(str(spectrogram_path))
            if img is None:
                raise ValueError(f"cv2 could not read spectrogram at {spectrogram_path}")
            ok, jpeg_bytes = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                raise ValueError("cv2.imencode failed for spectrogram JPEG")
            encoded_img = b64encode(jpeg_bytes.tobytes()).decode()
            html += f"""
            <p style="font-size: 13px; font-weight: bold; margin: 16px 0 6px 0;">Audio Spectrogram:</p>
            <img src="data:image/jpeg;base64,{encoded_img}"
                 style="max-width: 100%; height: auto; margin: 0 0 10px 0; border: 1px solid #d0c0b0;" />
            """
        except Exception as e:
            logger.warning(f"Could not embed spectrogram image: {e}")

    # Results table
    html += """
    <table style="border-collapse: collapse; margin: 10px 0; font-size: 13px;">
        <tr style="background-color: #f0ebe4;">
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: left;">Metric</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Value</th>
        </tr>
    """
    html += f"""
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Spectrogram spikes detected</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{spike_count}</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Audio/video duration difference</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{duration_diff_ms:.3f} ms</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Audio stream duration</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{audio_duration:.6f} s</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Video stream duration</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{video_duration:.6f} s</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Estimated loss from detected spikes</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{estimated_loss_ms:.4f} ms</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Audio sample rate</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{sample_rate} Hz</td>
        </tr>
        <tr>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">Combined risk score</td>
            <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{combined_score:.3f}</td>
        </tr>
    </table>
    """

    # Comparison note: measured duration difference vs estimated loss from spikes
    if estimated_loss_ms > 0 and duration_diff_ms > 0:
        ratio = duration_diff_ms / estimated_loss_ms
        if ratio > 10:
            html += f"""
            <p style="font-size: 13px; color: #cc6600; margin: 8px 0;">
                &#x26A0; The measured duration difference ({duration_diff_ms:.3f}ms) is {ratio:.0f}x larger than
                the {spike_count} detected spike(s) account for ({estimated_loss_ms:.4f}ms).
                Additional undetected drops or a systematic offset in the digitization chain is likely.
            </p>
            """
        elif ratio < 0.5:
            html += f"""
            <p style="font-size: 13px; color: #cc6600; margin: 8px 0;">
                &#x26A0; The measured duration difference ({duration_diff_ms:.3f}ms) is smaller than
                the estimated loss from {spike_count} spike(s) ({estimated_loss_ms:.4f}ms).
                Some detected spikes may be content transients rather than dropped samples.
            </p>
            """

    # Spike timestamps (collapsible, if any)
    if spike_timestamps:
        html += f"""
        <a id="link_spike_timestamps" href="javascript:void(0);"
           onclick="toggleContent('spike_timestamps', 'Estimated spike timestamps ({len(spike_timestamps)}) ▼', 'Estimated spike timestamps ▲')"
           style="color: #378d6a; text-decoration: underline; margin: 10px 0; display: block; font-size: 13px;">
           Estimated spike timestamps ({len(spike_timestamps)}) ▼</a>
        <div id="spike_timestamps" style="display: none; margin: 0 0 16px 0;">
        <table style="border-collapse: collapse; font-size: 13px;">
            <tr style="background-color: #f0ebe4;">
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0;">#</th>
                <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Timestamp (s)</th>
            </tr>
        """
        for i, ts in enumerate(spike_timestamps, 1):
            html += f"""
            <tr>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">{i}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{ts:.2f}</td>
            </tr>
            """
        html += "</table></div>"

    return html


def generate_duplicate_frame_html(frame_outputs):
    """
    Generate the HTML section for duplicate frame detection.

    Renders a header, methodology blurb, status line, and a table of detected
    runs (showing the first and last frame thumbnails of each freeze). When no
    runs were detected, still renders the header and a placeholder row so the
    report shows the step ran cleanly.

    Args:
        frame_outputs (dict): Output of locate_frame_analysis_outputs(). Returns
            empty string if duplicate frame detection didn't run for this video.

    Returns:
        str: HTML fragment, or empty string if step did not run.
    """
    duplicate_data = frame_outputs.get('duplicate_frame_detection')
    if duplicate_data is None:
        return ""

    runs = duplicate_data.get('runs') or []
    status = duplicate_data.get('status', 'unknown')
    message = duplicate_data.get('message', '')
    bit_depth_10 = duplicate_data.get('bit_depth_10', False)
    ydif_thresh = duplicate_data.get('ydif_threshold', 0)
    udif_thresh = duplicate_data.get('udif_threshold', 0)
    vdif_thresh = duplicate_data.get('vdif_threshold', 0)
    min_run_length = duplicate_data.get('min_run_length', 2)
    total_loss = duplicate_data.get('estimated_loss_seconds', 0.0)

    if status == 'critical':
        status_color = '#cc0000'
        status_icon = '&#x26A0;'
    elif status == 'warning':
        status_color = '#cc6600'
        status_icon = '&#x26A0;'
    elif status == 'clean':
        status_color = '#0a5f1c'
        status_icon = '&#x2705;'
    else:
        status_color = '#666666'
        status_icon = '&#x2753;'

    bit_depth_label = "10-bit" if bit_depth_10 else "8-bit"

    html = "<h3>Duplicate Frame Detection</h3>"

    # Collapsible methodology explanation
    html += f"""
    <a id="link_duplicate_frame_methodology" href="javascript:void(0);"
       onclick="toggleContent('duplicate_frame_methodology', 'What is duplicate frame detection? &#x25BC;', 'What is duplicate frame detection? &#x25B2;')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is duplicate frame detection? &#x25BC;</a>
    <div id="duplicate_frame_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>Duplicate frame detection</strong> identifies runs of repeated frames likely caused by
            TBC or framesync error concealment during digitization. The detection pipeline:
        </p>
        <ul style="margin: 4px 0 10px 20px; padding: 0;">
            <li style="margin-bottom: 4px;"><strong>QCTools candidate filter</strong> &mdash; The QCTools
                report is scanned for runs of consecutive frames whose YDIF, UDIF, and VDIF values all fall
                below bit-depth-aware thresholds. Color bars, detected black segments, and flat-field
                frames (the deck's synthetic black/mute output during signal loss, which is bit-identical
                frame to frame but is not frozen picture content) are excluded.</li>
            <li style="margin-bottom: 4px;"><strong>OpenCV verification</strong> &mdash; Each candidate is
                verified by reading the actual frames with OpenCV and computing the mean squared error
                against the preceding frame. Candidates that don't confirm as near-identical are dropped.</li>
            <li style="margin-bottom: 4px;"><strong>Minimum run length</strong> &mdash; A run of K
                consecutive low-diff frames represents a freeze of K+1 identical frames. The minimum run
                length is configurable (default {min_run_length}, freeze of &ge;{min_run_length + 1} frames)
                to suppress single-frame matches that occur naturally on static content.</li>
        </ul>
        <p style="margin: 0;">
            Detected file is {bit_depth_label}; thresholds in use:
            YDIF &lt; {ydif_thresh}, UDIF &lt; {udif_thresh}, VDIF &lt; {vdif_thresh}.
        </p>
    </div>
    """

    html += f"""
    <p style="font-size: 14px; color: {status_color}; font-weight: bold;">
        {status_icon} {message}
    </p>
    """

    if total_loss > 0:
        html += f"""
        <p style="font-size: 13px; color: #555; margin: 4px 0 12px 0;">
            Estimated total duration loss from freezes: <strong>{total_loss:.3f} s</strong>
        </p>
        """

    # Results table
    html += """
    <table style="border-collapse: collapse; margin: 10px 0; font-size: 13px;">
        <tr style="background-color: #f0ebe4;">
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: left;">#</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: left;">Start</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: left;">End</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Frozen frames</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">Est. loss (s)</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">YDIF avg</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">VREP</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">OpenCV MSE</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">First frame</th>
            <th style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">Last frame</th>
        </tr>
    """

    if not runs:
        html += """
        <tr>
            <td colspan="10" style="padding: 12px; border: 1px solid #d0c0b0;
                text-align: center; color: #666; font-style: italic;">
                No duplicate frame runs detected.
            </td>
        </tr>
        """
    else:
        def _fmt_tc(t):
            mins = int(t // 60)
            secs = t - (mins * 60)
            return f"{mins:02d}:{secs:05.2f}"

        def _embed_thumb(thumb_path):
            if not thumb_path or not os.path.exists(thumb_path):
                return '<span style="color:#999; font-size:12px;">n/a</span>'
            try:
                with open(thumb_path, 'rb') as fh:
                    encoded = b64encode(fh.read()).decode()
                return (
                    f'<img src="data:image/jpeg;base64,{encoded}" '
                    f'style="max-width: 220px; height: auto; border: 1px solid #d0c0b0;" />'
                )
            except Exception as e:
                logger.warning(f"Could not embed duplicate frame thumbnail {thumb_path}: {e}")
                return '<span style="color:#999; font-size:12px;">unavailable</span>'

        for i, run in enumerate(runs, 1):
            # Prefer the file-timecode labels (NDF/DF aware, start-TC offset);
            # fall back to raw seconds for results saved before they existed.
            start_tc = run.get('start_timecode') or _fmt_tc(run.get('start_time', 0.0))
            end_tc = run.get('end_timecode') or _fmt_tc(run.get('end_time', 0.0))
            frozen = run.get('frozen_frames', 0)
            est_loss = run.get('estimated_loss_seconds', 0.0)
            avg_ydif = run.get('avg_ydif', 0.0)
            avg_vrep = run.get('avg_vrep', 0.0)
            cv_mse = run.get('cv_mse')
            first_thumb = run.get('first_frame_thumbnail')
            last_thumb = run.get('last_frame_thumbnail')

            mse_cell = f"{cv_mse:.2f}" if isinstance(cv_mse, (int, float)) else "&mdash;"
            html += f"""
            <tr>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0;">{i}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; font-family: monospace;">{start_tc}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; font-family: monospace;">{end_tc}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{frozen}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{est_loss:.3f}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{avg_ydif:.3f}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{avg_vrep:.2f}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: right;">{mse_cell}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">{_embed_thumb(first_thumb)}</td>
                <td style="padding: 6px 12px; border: 1px solid #d0c0b0; text-align: center;">{_embed_thumb(last_thumb)}</td>
            </tr>
            """

    html += "</table>"
    return html


def make_content_summary_html(qctools_content_check_output, sorted_thumbs_dict, paper_bgcolor='#f5e9e3'):
    with open(qctools_content_check_output, 'r') as file:
        lines = file.readlines()

    # Find the line with content filter results
    content_filter_line_index = None
    for i, line in enumerate(lines):
        if line.startswith("Segments found within thresholds of content filter"):
            content_filter_line_index = i
            break

    if content_filter_line_index is None:
        return "Content filter results not found in CSV."

    content_filter_name = lines[content_filter_line_index].split()[-1].strip(':')
    time_ranges = lines[content_filter_line_index + 1:]

    matching_thumbs = [
        (thumb_name, thumb_path)
        for thumb_name, (thumb_path, profile_name, _) in sorted_thumbs_dict.items()
        if content_filter_name in thumb_path  # Simplified matching
    ]

    # Build HTML table
    table_rows = []
    for i, time_range in enumerate(time_ranges):
        thumbnail_html = ""
        if i < len(matching_thumbs):  
            thumb_name, thumb_path = matching_thumbs[i]
            with open(thumb_path, "rb") as image_file:
                encoded_string = b64encode(image_file.read()).decode()
            thumbnail_html = f"""<img src="data:image/jpeg;base64,{encoded_string}" style="width: 150px; height: auto;" />"""

        table_rows.append(f"""
            <tr>
                <td style="text-align: center; padding: 10px;">{thumbnail_html}</td>
                <td style="padding: 10px; white-space: nowrap;">{time_range}</td>  
            </tr>
        """)

    content_summary_html = f"""
    <table style="background-color: {paper_bgcolor}; margin-top: 20px; border-collapse: collapse;"> 
        <tr>
            <th colspan="2" style="padding: 10px;">Segments found within thresholds of content filter {content_filter_name}:</th>
        </tr>
        {''.join(table_rows)}
    </table>
    """

    return content_summary_html


def _read_mkvalidator_summary(summary_path):
    """
    Parse the mkvalidator summary sidecar for the report.

    Returns (verdict_text, is_valid, error_count). is_valid is True only when the
    verdict reads valid AND no ERR errors were reported; a missing/garbled verdict
    is treated as not-valid (conservative).
    """
    try:
        with open(summary_path, encoding='utf-8') as f:
            lines = [ln.rstrip('\n') for ln in f]
    except Exception as e:
        logger.error(f"Error reading mkvalidator summary {summary_path}: {e}")
        return ('mkvalidator summary unavailable', False, 0)

    verdict = lines[0].strip() if lines else ''
    error_count = 0
    for ln in lines:
        m = re.match(r'\s*Errors \(ERR\):\s*(\d+)', ln)
        if m:
            error_count = int(m.group(1))
            break

    v = verdict.lower()
    is_valid = (
        'valid' in v and 'invalid' not in v and 'not valid' not in v
        and error_count == 0
    )
    return (verdict, is_valid, error_count)


# Collapsible "What is mkvalidator?" explainer, styled to match the frame-analysis
# methodology blocks (e.g. duplicate frame detection). Static — no per-file values.
_MKVALIDATOR_METHODOLOGY_HTML = """
    <a id="link_mkvalidator_methodology" href="javascript:void(0);"
       onclick="toggleContent('mkvalidator_methodology', 'What is mkvalidator? &#x25BC;', 'What is mkvalidator? &#x25B2;')"
       style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
       What is mkvalidator? &#x25BC;</a>
    <div id="mkvalidator_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
         margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
        <p style="margin: 0 0 10px 0;">
            <strong>mkvalidator</strong> (from the Matroska project) parses the file's Matroska/EBML
            structure and reports whether it is well-formed. AV Spex surfaces that verdict as the
            <strong>Result</strong> below: <strong>Valid</strong> means the container conforms;
            <strong>Invalid</strong> means a hard structural error (an <code>ERR</code> code) was found.
            Warnings on their own do not make a file invalid.
        </p>
        <p style="margin: 0 0 10px 0;">
            <strong>WRN0C2 timecode warnings</strong> &mdash; mkvalidator emits a <code>WRN0C2</code> for
            each Matroska <em>Cluster</em> whose stored timecode does not increase relative to the previous
            cluster. It does not indicate file damage and does not fail validation. These are
            common in losslessly-encoded preservation MKVs.
        </p>
        <p style="margin: 0;">
            <strong>Byte offset &rarr; timecode</strong> &mdash; each warning reports the cluster's
            <em>byte offset</em> (its position within the file), not a media time. The
            <strong>Approx. timecode</strong> column estimates where that offset falls on the timeline by
            linear interpolation (offset &divide; file size &times; duration) and formats it as the file's
            own NDF/DF timecode. Because Matroska is variable-rate this is an
            estimate, shown with a leading &ldquo;&asymp;&rdquo;.
        </p>
    </div>
"""


def _mkvalidator_timecode_mapper(video_path):
    """
    Return a function mapping a WRN0C2 byte offset -> approximate file timecode str,
    or None if the inputs needed for the estimate are unavailable.

    A byte offset has no exact time (Matroska is variable-rate), so it is mapped to
    elapsed seconds by linear interpolation (offset / file_size * duration) and then
    formatted as the file's own NDF/DF timecode (with start-TC offset) so the value
    lines up with what the user would scrub to in their NLE. The estimate is
    approximate — callers should label it as such.
    """
    if not video_path or not os.path.isfile(video_path):
        return None
    try:
        file_size = os.path.getsize(video_path)
    except OSError:
        return None
    duration = _get_video_duration(video_path)
    if not file_size or not duration or duration <= 0:
        return None

    # Local import: qct_parse is heavy (numpy/scipy) and only needed when this
    # section actually renders a warnings table.
    from AV_Spex.checks.qct_parse import (
        _tc_format_timecode, _tc_parse_start_timecode,
        _get_video_frame_rate, _get_video_start_timecode,
    )
    fps = _get_video_frame_rate(video_path)
    nominal = max(1, int(round(fps))) if fps else 30
    start_frames, drop_frame = _tc_parse_start_timecode(
        _get_video_start_timecode(video_path), nominal
    )

    def to_timecode(byte_offset):
        seconds = byte_offset / file_size * duration
        return _tc_format_timecode(seconds, fps, start_frames, drop_frame)

    return to_timecode


def make_mkvalidator_html(summary_path, clusters_csv_path, video_path=None):
    """
    Render the mkvalidator report section.

    The focus is the validity verdict (Valid / Invalid). If the file has WRN0C2
    non-incrementing cluster timecode warnings, a compact table of their byte
    offsets follows. The section renders whenever mkvalidator's check ran (i.e. the
    summary sidecar exists), including for a clean valid file with no warnings.

    Each WRN0C2 "at <N>" value is a byte offset into the file. When video_path is
    given, an approximate file timecode (linear offset->time interpolation, formatted
    as NDF/DF to match the NLE) is shown alongside each offset.

    Returns an HTML string, or None if the summary sidecar is missing.
    """
    if not summary_path or not os.path.isfile(summary_path):
        return None

    verdict, is_valid, error_count = _read_mkvalidator_summary(summary_path)

    status_color = '#1f7a4d' if is_valid else '#b3261e'   # green / red
    status_text = 'Valid' if is_valid else 'Invalid'
    status_html = (
        f'<p style="margin-top: 0; font-size: 1.15em;">Result: '
        f'<strong style="color: {status_color};">{status_text}</strong></p>'
        f'<p style="margin: 0; color: #4d2b12;">{verdict}</p>'
    )

    # Optional WRN0C2 timecode-warnings table (only when the clusters CSV has rows).
    # Kept deliberately narrow — it is just two columns of short numbers.
    clusters_html = '<p style="margin-bottom: 0;">No timecode warnings.</p>'
    if clusters_csv_path and os.path.isfile(clusters_csv_path):
        try:
            with open(clusters_csv_path, newline='', encoding='utf-8') as f:
                rows = list(csv.reader(f))
        except Exception as e:
            logger.error(f"Error processing mkvalidator clusters CSV {clusters_csv_path}: {e}")
            rows = []
        data_rows = [r for r in rows[1:] if len(r) >= 2]
        if data_rows:
            # Optional approximate file-timecode column (only when we can derive it).
            to_timecode = _mkvalidator_timecode_mapper(video_path)
            td = 'padding: 4px 12px; border: 1px solid #4d2b12;'
            th = ('padding: 6px 12px; border: 1px solid #4d2b12; '
                  'background-color: #fbe4eb; position: sticky; top: 0;')

            def _row(cells):
                tc_cell = ''
                if to_timecode:
                    try:
                        tc_cell = f'<td style="{td}">&asymp; {to_timecode(int(cells[1]))}</td>'
                    except (ValueError, TypeError):
                        tc_cell = f'<td style="{td}">&mdash;</td>'
                return (f'<tr><td style="{td}">{cells[0]}</td>'
                        f'<td style="{td}">{cells[1]}</td>{tc_cell}</tr>')

            table_rows = ''.join(_row(cells) for cells in data_rows)
            tc_header = f'<th style="{th}">Approx. timecode</th>' if to_timecode else ''
            table_width = '460px' if to_timecode else '320px'
            clusters_html = f"""
        <p style="margin-bottom: 6px;"><strong>{len(data_rows)}</strong> non-incrementing
        cluster timecode warning(s) (<code>WRN0C2</code>, informational &mdash; may be intentional).</p>
        <div style="max-width: {table_width}; max-height: 360px; overflow-y: auto; border: 1px solid #4d2b12;">
            <table style="border-collapse: collapse; width: 100%; background-color: #ffffff;">
                <tr>
                    <th style="{th}">Cluster #</th>
                    <th style="{th}">Byte offset</th>
                    {tc_header}
                </tr>
                {table_rows}
            </table>
        </div>"""

    return f"""
    {_MKVALIDATOR_METHODOLOGY_HTML}
    <div style="background-color: #f5e9e3; padding: 20px 30px; margin-top: 10px; border-radius: 6px;">
        {status_html}
        <div style="margin-top: 16px;">
        {clusters_html}
        </div>
    </div>
    """


def generate_final_report(video_id, source_directory, report_directory, destination_directory,
                         video_path=None, check_cancelled=None, signals=None):
    """
    Generate final HTML report if configured.
    
    Args:
        video_id (str): Unique identifier for the video
        source_directory (str): Source directory for the video
        report_directory (str): Directory containing report files
        destination_directory (str): Destination directory for output files
        video_path (str, optional): Path to the video file for thumbnail generation
        check_cancelled (callable, optional): Function to check if cancelled
        signals (object, optional): Signals for GUI updates
        
    Returns:
        str or None: Path to the generated HTML report, or None
    """
    
    checks_config = config_mgr.get_config('checks', ChecksConfig)

    if not checks_config.outputs.report:
        return None

    try:
        html_report_path = os.path.join(source_directory, f'{video_id}_avspex_report.html')
        
        # Generate HTML report with video path (no frame_analysis parameter needed)
        write_html_report(video_id, report_directory, destination_directory, html_report_path,
                         video_path=video_path, check_cancelled=check_cancelled, signals=signals)
        
        logger.info(f"HTML report generated: {html_report_path}\n")
        if signals:
            signals.step_completed.emit("Generate Report")
        return html_report_path

    except Exception as e:
        logger.critical(f"Error generating HTML report: {e}")
        import traceback
        logger.critical(f"Traceback: {traceback.format_exc()}")
        return None


def write_html_report(video_id, report_directory, destination_directory, html_report_path, video_path=None, check_cancelled=None, signals=None):

    if signals:
        signals.report_progress.emit(0)

    qctools_colorbars_duration_output, qctools_bars_eval_check_output, colorbars_values_output, windowed_colorbars_values, qctools_content_check_outputs, qctools_profile_check_output, profile_fails_csv, tags_check_output, tag_fails_csv, colorbars_eval_fails_csv, audio_clipping_csv, channel_imbalance_csv, audible_timecode_csv, audio_dropout_csv, clamped_levels_csv, clamped_traces_csv, chroma_phase_summary_csv, chroma_phase_events_csv, tone_leak_summary_csv, tone_leak_events_csv, difference_csv = find_report_csvs(report_directory)

    # CLAMS bars-detection durations CSV (filename matches the writer in
    # checks/bars_detection_clams.py); present only when the parallel detector ran.
    clams_bars_durations_csv = os.path.join(report_directory, "clams_bars_colorbars_durations.csv")
    if not os.path.isfile(clams_bars_durations_csv):
        clams_bars_durations_csv = None

    # CLAMS tone-detection durations CSV (filename matches the writer in
    # checks/tone_detection_clams.py); present only when the detector ran.
    clams_tone_durations_csv = os.path.join(report_directory, "clams_tone_detection_durations.csv")
    if not os.path.isfile(clams_tone_durations_csv):
        clams_tone_durations_csv = None

    # mkvalidator sidecars (filenames match the writers in checks/mkvalidator_check.py).
    # Unlike the qct-parse/CLAMS sidecars these live in the _qc_metadata directory
    # (destination_directory), not the report dir, because the mkvalidator check runs
    # before the report dir exists. The summary sidecar holds the validity verdict and
    # is written whenever the check ran; the clusters CSV holds the WRN0C2 byte offsets
    # and is present only when at least one WRN0C2 was found.
    mkvalidator_summary_path = os.path.join(destination_directory, f"{video_id}_mkvalidator_summary.txt")
    if not os.path.isfile(mkvalidator_summary_path):
        mkvalidator_summary_path = None

    mkvalidator_clusters_csv = os.path.join(destination_directory, f"{video_id}_mkvalidator_clusters.csv")
    if not os.path.isfile(mkvalidator_clusters_csv):
        mkvalidator_clusters_csv = None

    if check_cancelled():
        return
    
    # Create thumbPath if it doesn't exist
    thumbPath = os.path.join(report_directory, "ThumbExports")
    if not os.path.exists(thumbPath):
        os.makedirs(thumbPath)
    
    # Collect all thumbnail tasks across all failure types, then generate with progress
    generated_thumbs = {}
    thumbnail_tasks = []

    if profile_fails_csv and video_path:
        profile_fails_csv_path = os.path.join(report_directory, profile_fails_csv)
        failureInfoSummary_profile = summarize_failures(profile_fails_csv_path)
        for timestamp, info_list in failureInfoSummary_profile.items():
            for info in info_list:
                thumbnail_tasks.append((info['tag'], info['tagValue'], timestamp, 'threshold_profile'))

    if tag_fails_csv and video_path:
        tag_fails_csv_path = os.path.join(report_directory, tag_fails_csv)
        failureInfoSummary_tags = summarize_failures(tag_fails_csv_path)
        for timestamp, info_list in failureInfoSummary_tags.items():
            for info in info_list:
                thumbnail_tasks.append((info['tag'], info['tagValue'], timestamp, 'tag_check'))

    if colorbars_eval_fails_csv and video_path:
        colorbars_eval_fails_csv_path = os.path.join(report_directory, colorbars_eval_fails_csv)
        failureInfoSummary_colorbars = summarize_failures(colorbars_eval_fails_csv_path)
        for timestamp, info_list in failureInfoSummary_colorbars.items():
            for info in info_list:
                thumbnail_tasks.append((info['tag'], info['tagValue'], timestamp, 'color_bars_evaluation'))

    total_thumbs = len(thumbnail_tasks)
    for i, (tag, tagValue, timestamp, profile_name) in enumerate(thumbnail_tasks):
        thumb_path = generate_thumbnail_for_failure(
            video_path,
            tag,
            tagValue,
            timestamp,
            profile_name,
            thumbPath
        )
        if thumb_path:
            thumb_key = f"Failed frame \n\n{tag}:{tagValue}\n\n{timestamp}"
            generated_thumbs[thumb_key] = (thumb_path, tag, timestamp)
        if signals and total_thumbs > 0:
            signals.report_progress.emit(1 + int(9 * (i + 1) / total_thumbs))

    # Merge with existing thumbs (for things like color bars detection)
    existing_thumbs = find_qct_thumbs(report_directory)
    thumbs_dict = {**existing_thumbs, **generated_thumbs}

    if signals:
        signals.report_progress.emit(10)

    # Sort thumbs_dict as before
    sorted_thumbs_dict = {}
    for key in sorted(thumbs_dict.keys(), key=lambda x: (parse_profile(thumbs_dict[x][1]), parse_timestamp(thumbs_dict[x][2]))):
        sorted_thumbs_dict[key] = thumbs_dict[key]

    if check_cancelled():
        return
    
    # Modified to get MediaConch policy content
    (exiftool_output_path, mediainfo_output_path, ffprobe_output_path,
     mediaconch_csv, fixity_sidecar, fixity_summary_json,
     mediaconch_policy_content,
     mediaconch_policy_name) = find_qc_metadata(destination_directory)

    if check_cancelled():
        return

    if signals:
        signals.report_progress.emit(15)

    # Find frame analysis outputs
    frame_outputs = find_frame_analysis_outputs(
        os.path.dirname(html_report_path),  # source_directory
        destination_directory,
        video_id
    )
    
    # Generate frame analysis HTML section
    frame_analysis_html = generate_frame_analysis_html(frame_outputs, video_id) if frame_outputs else ""

    # Initialize and create html from 
    mc_csv_html, mediaconch_csv_filename = prepare_file_section(mediaconch_csv, lambda path: csv_to_html_table(path, style_mismatched=False, mismatch_color="#ffbaba", match_color="#d2ffed", check_fail=True))
    diff_csv_html, difference_csv_filename = prepare_file_section(difference_csv, lambda path: csv_to_html_table(path, style_mismatched=True, mismatch_color="#ffbaba", match_color="#d2ffed", check_fail=False))
    exif_file_content, exif_file_filename = prepare_file_section(exiftool_output_path)
    mi_file_content, mi_file_filename = prepare_file_section(mediainfo_output_path)
    ffprobe_file_content, ffprobe_file_filename = prepare_file_section(ffprobe_output_path)
    fixity_file_content, fixity_file_filename = prepare_file_section(fixity_sidecar)
    fixity_html = make_fixity_section_html(fixity_summary_json, fixity_file_content)

    # Get qct-parse thumbs if they exists
    thumbs_dict = find_qct_thumbs(report_directory)

    if profile_fails_csv:
        profile_fails_csv_path = os.path.join(report_directory, profile_fails_csv)
        failureInfoSummary_profile = summarize_failures(profile_fails_csv_path)
    else:
        failureInfoSummary_profile = None

    if tag_fails_csv:
        tag_fails_csv_path = os.path.join(report_directory, tag_fails_csv)
        failureInfoSummary_tags = summarize_failures(tag_fails_csv_path)
    else:
        failureInfoSummary_tags = None

    if colorbars_eval_fails_csv:
        colorbars_eval_fails_csv_path = os.path.join(report_directory, colorbars_eval_fails_csv)
        failureInfoSummary_colorbars = summarize_failures(colorbars_eval_fails_csv_path)
    else:
        failureInfoSummary_colorbars = None

    if check_cancelled():
        return

    # Create graphs for all existing csv files (existing code...)
    if qctools_bars_eval_check_output and failureInfoSummary_colorbars:
        colorbars_eval_html = make_profile_piecharts(qctools_bars_eval_check_output, thumbs_dict, failureInfoSummary_colorbars, video_id, failure_csv_path=colorbars_eval_fails_csv_path, check_cancelled=check_cancelled)
    elif qctools_bars_eval_check_output and failureInfoSummary_colorbars is None:
       color_bars_segment = f"""
        <div style="display: flex; flex-direction: column; align-items: start; background-color: #f5e9e3; padding: 10px;"> 
            <p><b>All QCTools values of the video file are within the peak values of the color bars.</b></p>
        </div>
        """
       colorbars_eval_html = f"""
        <div style="display:inline-block; margin-right: 10px; padding-bottom: 20px;">  
            {color_bars_segment}
        </div>
        """
    else:
        colorbars_eval_html = None

    # Check if SMPTE fallback was used
    smpte_fallback = False
    if colorbars_values_output:
        try:
            with open(colorbars_values_output, 'r') as f:
                first_line = f.readline().strip()
            if first_line == "SMPTE_FALLBACK":
                smpte_fallback = True
                colorbars_html = """
                <div style="background-color: #fff3cd; padding: 15px; border: 1px solid #856404; margin: 10px 0; border-radius: 5px;">
                    <p style="margin: 0; color: #856404;"><strong>No color bars detected.</strong> Evaluation was performed using standard SMPTE color bar values.</p>
                </div>
                """
            elif first_line == "SMPTE_SELECTED":
                smpte_fallback = True
                colorbars_html = """
                <div style="background-color: #fff3cd; padding: 15px; border: 1px solid #856404; margin: 10px 0; border-radius: 5px;">
                    <p style="margin: 0; color: #856404;">Evaluation was performed against standard SMPTE color bar values (selected reference).</p>
                </div>
                """
            else:
                colorbars_html = make_color_bars_graphs(video_id, qctools_colorbars_duration_output, colorbars_values_output, thumbs_dict)
        except Exception as e:
            logger.error(f"Error reading colorbars values file: {e}")
            colorbars_html = None
    else:
        colorbars_html = None

    # Render bar graphs for windowed (mid-file) bars regions.
    # Build a lookup of region label → duration string from the qct-parse
    # bars durations CSV so each graph shows the correct timecodes.
    qct_duration_runs = _parse_bars_durations_csv(qctools_colorbars_duration_output)
    windowed_duration_lookup = {}
    for label, start_s, end_s in qct_duration_runs:
        def _fmt(seconds):
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = seconds - (h * 3600) - (m * 60)
            return f"{h:02d}:{m:02d}:{s:06.3f}"
        windowed_duration_lookup[label] = f"{_fmt(start_s)} - {_fmt(end_s)}"

    windowed_colorbars_html_list = []
    for wv_path in sorted(windowed_colorbars_values):
        try:
            region_name = os.path.basename(wv_path).replace("qct-parse_colorbars_values_", "").replace(".csv", "")
            dur_str = windowed_duration_lookup.get(region_name)
            idx_str = region_name.replace("additional-", "")
            wv_html = make_color_bars_graphs(
                video_id, qctools_colorbars_duration_output, wv_path, thumbs_dict,
                duration_override=dur_str,
                bars_label=f'{video_id} Additional Bars',
                thumb_profile_filter=f'additional_bars_{idx_str}',
            )
            if wv_html:
                windowed_colorbars_html_list.append((region_name, wv_html))
        except Exception as e:
            logger.error(f"Error rendering windowed bars graph for {wv_path}: {e}")

    if qctools_profile_check_output and failureInfoSummary_profile:
        profile_summary_html = make_profile_piecharts(qctools_profile_check_output, thumbs_dict, failureInfoSummary_profile, video_id, failure_csv_path=profile_fails_csv_path, check_cancelled=check_cancelled)
    else:
        profile_summary_html = None

    if qctools_content_check_outputs:
        content_summary_html_list = []
        for output_csv in qctools_content_check_outputs:
            content_summary_html = make_content_summary_html(output_csv, thumbs_dict, paper_bgcolor='#f5e9e3')
            content_summary_html_list.append(content_summary_html)
    else:
        content_summary_html_list = None

    if tags_check_output and failureInfoSummary_tags:
        tags_summary_html = make_profile_piecharts(tags_check_output, thumbs_dict, failureInfoSummary_tags, video_id, failure_csv_path=tag_fails_csv_path, check_cancelled=check_cancelled)
    else:
        tags_summary_html = None

    # Side-by-side comparison of qct-parse and CLAMS bars detectors. None
    # unless at least one of the two CSVs is present.
    bars_comparison_html = make_bars_detection_comparison_html(
        qctools_colorbars_duration_output,
        clams_bars_durations_csv,
    ) if clams_bars_durations_csv else None

    # CLAMS tone detection results.
    tone_detection_html = make_tone_detection_html(clams_tone_durations_csv)
    mkvalidator_html = make_mkvalidator_html(mkvalidator_summary_path, mkvalidator_clusters_csv, video_path)

    audio_clipping_html = make_audio_clipping_html(audio_clipping_csv) if audio_clipping_csv else None
    channel_imbalance_html = make_channel_imbalance_html(channel_imbalance_csv) if channel_imbalance_csv else None
    audible_timecode_html = make_audible_timecode_html(audible_timecode_csv) if audible_timecode_csv else None
    audio_dropout_html = make_audio_dropout_html(audio_dropout_csv) if audio_dropout_csv else None
    clamped_levels_html = make_clamped_levels_html(clamped_levels_csv, clamped_traces_csv) if clamped_levels_csv else None
    chroma_phase_html = make_chroma_phase_html(chroma_phase_summary_csv, chroma_phase_events_csv) if chroma_phase_summary_csv else None
    tone_leak_html = make_tone_leak_html(tone_leak_summary_csv, tone_leak_events_csv) if tone_leak_summary_csv else None
    dropped_sample_html = generate_dropped_sample_html(frame_outputs) if frame_outputs else ""
    duplicate_frame_html = generate_duplicate_frame_html(frame_outputs) if frame_outputs else ""
    bitplane_html = generate_bitplane_html(frame_outputs) if frame_outputs else ""

    existing_thumbs = find_qct_thumbs(report_directory)
    no_qct_parse_files = (
        not profile_fails_csv and
        not tag_fails_csv and
        not colorbars_eval_fails_csv and
        not audio_clipping_csv and
        not channel_imbalance_csv and
        not audible_timecode_csv and
        not audio_dropout_csv and
        not clamped_levels_csv and
        not chroma_phase_summary_csv and
        not tone_leak_summary_csv and
        not existing_thumbs
    )

    if check_cancelled():
        return

    # Embed logo as a data URI so the report renders self-contained.
    logo_image_path = image_to_data_uri(config_mgr.get_logo_path('av_spex_the_logo.png'))
    if signals:
        signals.report_progress.emit(20)

    # Generate a color strip from the video, fall back to the static eq image
    color_strip_b64 = None
    if video_path:
        color_strip_b64 = generate_color_strip_base64(video_path, signals=signals, progress_start=22, progress_end=25)
    if signals:
        signals.report_progress.emit(25)
 
    if color_strip_b64:
        # Store the data URI once in a hidden element, reference it via JS
        color_strip_src = f"data:image/jpeg;base64,{color_strip_b64}"
        color_strip_store = (
            f'<img id="color-strip-data" src="{color_strip_src}" '
            f'alt="Color strip" class="color-strip-divider">'
        )
        # Subsequent uses clone the src from the stored element
        color_strip_divider = (
            '<img class="color-strip-divider color-strip-clone" alt="Color strip">'
        )
        color_strip_init_script = """
        <script>
        (function() {
            var src = document.getElementById('color-strip-data').src;
            var clones = document.getElementsByClassName('color-strip-clone');
            for (var i = 0; i < clones.length; i++) { clones[i].src = src; }
        })();
        </script>"""
    else:
        # Fallback: use the static eq image as before
        eq_image_path = image_to_data_uri(config_mgr.get_logo_path('germfree_eq.png'))
        color_strip_src = eq_image_path
        color_strip_store = (
            f'<img src="{eq_image_path}" alt="AV Spex Graphic EQ Logo" '
            f'style="width: 10%">'
        )
        color_strip_divider = color_strip_store
        color_strip_init_script = ""

    # Generate audio waveform
    waveform_b64 = None
    if video_path:
        waveform_b64 = generate_audio_waveform_base64(video_path, signals=signals, progress_start=25, progress_end=95)

    if waveform_b64:
        # Note: Using class "waveform-data-store" to target via JS
        waveform_store = (
            f'<img class="waveform-data-store" src="data:image/jpeg;base64,{waveform_b64}" '
            f'style="display: none;">'
        )
        
        # Divider: The placeholder that will be cloned by the script
        waveform_divider = (
            '<div style="margin: 20px 0; text-align: center;">'
            '<img class="waveform-clone" style="width: 100%; height: auto; border: 1px solid #378d6a;">'
            '</div>'
        )
        
        # JavaScript: Finds all clones and populates them with the source data
        waveform_init_script = """
        <script>
        (function() {
            var src = document.querySelector('.waveform-data-store').src;
            var clones = document.querySelectorAll('.waveform-clone');
            for (var i = 0; i < clones.length; i++) {
                clones[i].src = src;
            }
        })();
        </script>"""
    else:
        # Fallback: If generation fails, use the static logo as a divider (similar to eq_image_path)
        eq_image_path = image_to_data_uri(config_mgr.get_logo_path('germfree_eq.png'))
        waveform_store = f'<img src="{eq_image_path}" alt="AV Spex Logo" style="width: 10%; display: none;">'
        waveform_divider = f'<div style="text-align: center;"><img src="{eq_image_path}" style="width: 10%;"></div>'
        waveform_init_script = ""

    # Build a "Jump to section" table of contents from the conditional flags
    # computed above. Each entry is (anchor_id, label). Order matches the
    # render order below, so sections are listed the way they appear.
    _has_audio_results = bool(
        audio_clipping_html or channel_imbalance_html
        or audible_timecode_html or audio_dropout_html
        or tone_leak_html
    )
    toc_entries = []
    if fixity_html:
        toc_entries.append(('section-fixity', 'Fixity'))
    if mediaconch_csv:
        toc_entries.append(('section-mediaconch-csv', 'MediaConch CSV'))
    if mediaconch_policy_content and mediaconch_policy_name:
        toc_entries.append(('section-mediaconch-policy', 'MediaConch Policy'))
    if mkvalidator_html:
        toc_entries.append(('section-mkvalidator', 'mkvalidator'))
    if frame_analysis_html:
        # Link to the individual frame-analysis subsections rather than the
        # section heading; only list the ones that were actually rendered
        frame_subsections = [
            ('section-border-detection', 'Border Detection'),
            ('section-signalstats', 'Signalstats Analysis'),
            ('section-brng-analysis', 'BRNG Violation Analysis'),
        ]
        rendered = [
            (anchor, label) for anchor, label in frame_subsections
            if f"id='{anchor}'" in frame_analysis_html
        ]
        if rendered:
            toc_entries.extend(rendered)
        else:
            toc_entries.append(('section-frame-analysis', 'Frame Analysis Results'))
    if bitplane_html:
        toc_entries.append(('section-bitplane', 'Bitplane Check'))
    if duplicate_frame_html:
        toc_entries.append(('section-duplicate-frame', 'Duplicate Frame Detection'))
    if no_qct_parse_files:
        toc_entries.append(('section-qct-parse-notice', 'QCT-Parse Analysis'))
    if colorbars_html:
        toc_entries.append(('section-colorbars', 'Color Bars Detection'))
    for region_name, _ in windowed_colorbars_html_list:
        toc_entries.append((f'section-colorbars-{region_name}', 'Additional Color Bars'))
    if bars_comparison_html or tone_detection_html:
        toc_entries.append(('section-clams-detection', 'CLAMS Detection'))
    if colorbars_eval_html:
        toc_entries.append(('section-colorbars-eval', 'Colorbars Threshold Evaluation'))
    if clamped_levels_html:
        toc_entries.append(('section-clamped-levels', 'Clamped Levels Detection'))
    if chroma_phase_html:
        toc_entries.append(('section-chroma-phase', 'Chroma Phase Detection'))
    if _has_audio_results:
        toc_entries.append(('section-audio-analysis', 'Audio Analysis Results'))
    if dropped_sample_html:
        toc_entries.append(('section-dropped-sample', 'Dropped Sample Detection'))
    if difference_csv:
        toc_entries.append(('section-difference-csv', 'Difference CSV'))
    if profile_summary_html:
        toc_entries.append(('section-profile-summary', 'QCT-Parse Profile Summary'))
    if tags_summary_html:
        toc_entries.append(('section-tags-summary', 'QCT-Parse Tag Check Summary'))
    if content_summary_html_list:
        toc_entries.append(('section-content-summary', 'QCT-Parse Content Detection'))
    if exiftool_output_path:
        toc_entries.append(('section-exiftool', 'ExifTool Output'))
    if mediainfo_output_path:
        toc_entries.append(('section-mediainfo', 'MediaInfo Output'))
    if ffprobe_output_path:
        toc_entries.append(('section-ffprobe', 'FFprobe Output'))

    if toc_entries:
        toc_links = ''.join(
            f'<li style="margin: 0;">'
            f'<a class="toc-pill" href="#{anchor}">{label}</a></li>'
            for anchor, label in toc_entries
        )
        toc_html = (
            '<nav aria-label="Report sections" '
            'style="background-color: #f5e9e3; border: 1px solid #4d2b12; '
            'border-radius: 4px; padding: 14px 18px; margin: 18px 0;">'
            '<p style="font-weight: bold; margin: 0 0 10px 0; color: #4d2b12; '
            'font-size: 14px;">Jump to section</p>'
            '<ul style="list-style: none; padding: 0; margin: 0; '
            'display: flex; flex-wrap: wrap; gap: 8px;">'
            f'{toc_links}'
            '</ul></nav>'
        )
    else:
        toc_html = ''

    # HTML template with JavaScript functions
    html_template = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AV Spex Report</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #fcfdff;
                color: #011054;
                margin: 30px;
            }}
            h1 {{
                font-size: 24px;
                text-align: center;
                margin-top: 20px;
                color: #378d6a;
            }}
            h2 {{
                font-size: 20px;
                font-weight: bold;
                margin-top: 30px;
                color: #0a5f1c;
                text-decoration: underline;
            }}
            h3 {{
                font-size: 18px;
                margin-top: 20px;
                color: #bf971b;
            }}
            table {{
                border-collapse: collapse;
                margin-top: 10px;
                margin-bottom: 20px;
                border: 2px solid #4d2b12;
            }}
            th, td {{
                border: 1.5px solid #4d2b12;
                padding: 8px;
                text-align: left;
            }}
            th {{
                background-color: #fbe4eb;
                font-weight: bold;
            }}
            pre {{
                background-color: #f5e9e3;
                border: 1px solid #4d2b12;
                padding: 10px;
                white-space: pre-wrap;
                word-wrap: break-word;
            }}
            .xml-content {{
                background-color: #f8f9fa;
                border: 1px solid #6c757d;
                padding: 15px;
                white-space: pre-wrap;
                word-wrap: break-word;
                font-family: 'Courier New', monospace;
                font-size: 12px;
                max-height: 400px;
                overflow-y: auto;
            }}
            .metadata-content {{
                background-color: #f5e9e3;
                border: 1px solid #4d2b12;
                padding: 10px;
                white-space: pre-wrap;
                word-wrap: break-word;
                font-family: monospace;
                max-height: 400px;
                overflow-y: auto;
            }}
            .color-strip-divider {{
                display: block;
                width: 100%;
                height: auto;
                margin: 25px 0;
                border-radius: 3px;
                image-rendering: pixelated;          /* Chrome/Edge */
                image-rendering: crisp-edges;         /* Firefox */
                -ms-interpolation-mode: nearest-neighbor; /* legacy IE */
            }}
            .cell-match {{
                background-color: #d2ffed;
            }}
            .cell-mismatch {{
                background-color: #ff9999;
            }}
            [id^="section-"] {{
                scroll-margin-top: 16px;
            }}
            .toc-pill {{
                display: inline-block;
                padding: 6px 14px;
                background-color: #fcfdff;
                color: #378d6a;
                border: 1px solid #378d6a;
                border-radius: 999px;
                text-decoration: none;
                font-size: 13px;
                font-weight: 500;
                transition: background-color 0.15s ease, color 0.15s ease;
            }}
            .toc-pill:hover,
            .toc-pill:focus {{
                background-color: #378d6a;
                color: #fcfdff;
                outline: none;
            }}
        </style>
        <script>
        function openImage(imgData, caption) {{
            var newWindow = window.open('', '_blank');
            newWindow.document.write('<html><head><title>' + caption + '</title></head><body style="margin:0; background:#000; display:flex; align-items:center; justify-content:center; height:100vh;">');
            newWindow.document.write('<img src="' + imgData + '" style="max-width:100%; max-height:100%; object-fit:contain;">');
            newWindow.document.write('</body></html>');
            newWindow.document.close();
        }}

        function toggleTable(tagId) {{
            var table = document.getElementById('table_' + tagId);
            var link = document.getElementById('link_' + tagId);
            if (table.style.display === 'none') {{
                table.style.display = 'block';
                link.textContent = 'Hide all failures ▲';
            }} else {{
                table.style.display = 'none';
                link.textContent = 'Show all failures ▼';
            }}
        }}

        function toggleContent(contentId, showText, hideText) {{
            var content = document.getElementById(contentId);
            var link = document.getElementById('link_' + contentId);
            if (content.style.display === 'none') {{
                content.style.display = 'block';
                link.textContent = hideText;
            }} else {{
                content.style.display = 'none';
                link.textContent = showText;
            }}
        }}
        </script>
        <img src="{logo_image_path}" alt="AV Spex Logo" style="display: block; margin-left: auto; margin-right: auto; width: 25%; margin-top: 20px;">
    </head>
    <body>
        <h1>AV Spex Report</h1>
        <h2>{video_id}</h2>
        {color_strip_store}
        {waveform_store}
        {toc_html}
    """

    if check_cancelled():
        return

    if fixity_html:
        html_template += f"""
        <h3 id="section-fixity">Fixity</h3>
        {fixity_html}
        """

    if mediaconch_csv:
        html_template += f"""
        <h3 id="section-mediaconch-csv">{mediaconch_csv_filename}</h3>
        {mc_csv_html}
        """

    # Add MediaConch policy section if available - NOW WITH COLLAPSIBLE FUNCTIONALITY
    if mediaconch_policy_content and mediaconch_policy_name:
        html_template += f"""
        <h3 id="section-mediaconch-policy">MediaConch Policy File: {mediaconch_policy_name}</h3>
        <a id="link_mediaconch_policy" href="javascript:void(0);" onclick="toggleContent('mediaconch_policy', 'Show policy content ▼', 'Hide policy content ▲')" style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block;">Show policy content ▼</a>
        <div id="mediaconch_policy" class="xml-content" style="display: none;">{mediaconch_policy_content}</div>
        """

    if mkvalidator_html:
        html_template += f"""
        <h3 id="section-mkvalidator">mkvalidator</h3>
        {mkvalidator_html}
        """

    if frame_analysis_html:
        html_template += frame_analysis_html

    # Bitplane check and duplicate frame detection render side-by-side so
    # each fills the vertical space of the taller section.
    if bitplane_html or duplicate_frame_html:
        html_template += (
            '<div style="display: flex; flex-wrap: wrap; gap: 24px; '
            'align-items: flex-start; margin-top: 20px;">'
        )
        if bitplane_html:
            html_template += (
                '<div id="section-bitplane" style="flex: 1 1 380px; min-width: 0;">'
                f'{bitplane_html}</div>'
            )
        if duplicate_frame_html:
            html_template += (
                '<div id="section-duplicate-frame" '
                'style="flex: 2 1 600px; min-width: 0; overflow-x: auto;">'
                f'{duplicate_frame_html}</div>'
            )
        html_template += '</div>'

    if frame_analysis_html or bitplane_html or duplicate_frame_html:
        html_template += color_strip_divider

    # Rest of the HTML template remains the same...
    if no_qct_parse_files:
        html_template += """
        <h3 id="section-qct-parse-notice">QCT-Parse Analysis</h3>
        <div style="background-color: #fff3cd; padding: 15px; border: 1px solid #856404; margin: 10px 0; border-radius: 5px;">
            <p style="margin: 0; color: #856404;"><strong>Information:</strong> QCT-Parse analysis was not performed for this video. Quality control analysis sections are not available in this report.</p>
        </div>
        """

    if colorbars_html or windowed_colorbars_html_list:
        html_template += '<div style="display: flex; flex-wrap: wrap; gap: 24px; align-items: flex-start;">'
        if colorbars_html:
            if smpte_fallback:
                colorbars_header = "Color Bars Detection"
            else:
                colorbars_header = f"SMPTE Colorbars vs {video_id} Colorbars"
            html_template += f"""
            <div id="section-colorbars" style="flex: 1 1 720px; max-width: 100%; min-width: 0; overflow: hidden;">
                <h3>{colorbars_header}</h3>
                {colorbars_html}
            </div>"""
        for region_name, wv_html in windowed_colorbars_html_list:
            html_template += f"""
            <div id="section-colorbars-{region_name}" style="flex: 1 1 720px; max-width: 100%; min-width: 0; overflow: hidden;">
                <h3>SMPTE Colorbars vs Additional {video_id} Colorbars</h3>
                {wv_html}
            </div>"""
        html_template += '</div>'

    if bars_comparison_html or tone_detection_html:
        html_template += '<h3 id="section-clams-detection">CLAMS Detection</h3>'
        html_template += """
        <a id="link_clams_methodology" href="javascript:void(0);"
           onclick="toggleContent('clams_methodology', 'What is CLAMS Detection? ▼', 'What is CLAMS Detection? ▲')"
           style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block; font-size: 13px;">
           What is CLAMS Detection? ▼</a>
        <div id="clams_methodology" style="display: none; background-color: #f8f6f3; padding: 14px 16px;
             margin: 0 0 16px 0; border: 1px solid #e0d0c0; border-radius: 4px; font-size: 13px; line-height: 1.5;">
            <p style="margin: 0 0 10px 0;">
                <strong>CLAMS</strong> (Computational Linguistics Applications for Multimedia Services) is an
                open-source project led by Brandeis University that builds reusable tools for analyzing
                audiovisual collections. AV Spex adapts two CLAMS apps —
                <a href="https://github.com/clamsproject/app-barsdetection" style="color: #378d6a;">app-barsdetection</a>
                and <a href="https://github.com/clamsproject/app-tonedetection" style="color: #378d6a;">app-tonedetection</a> —
                porting just their detection cores into the AV Spex pipeline. Both upstream apps are
                distributed under the Apache License 2.0.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">How bars detection works:</p>
            <p style="margin: 0 0 10px 0;">
                Frames are sampled (every 30th frame by default) and converted to grayscale. Each sample
                is compared to a bundled SMPTE color bars reference image using
                <strong>structural similarity (SSIM)</strong>. A frame is considered to match when its
                SSIM score exceeds the primary threshold (<code style="background:#eee; padding:1px 4px; border-radius:2px;">0.7</code>),
                and a run of consecutive matching samples becomes a detected bars span once it
                exceeds the minimum frame count.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">How tone detection works:</p>
            <p style="margin: 0 0 10px 0;">
                The audio track is decoded to 16 kHz mono via ffmpeg and split into consecutive 250 ms
                chunks. Adjacent chunks are compared using <strong>numpy cross-correlation</strong>;
                when their similarity stays at or above the tolerance
                (<code style="background:#eee; padding:1px 4px; border-radius:2px;">1.0</code> by default),
                the run is extended. Runs that survive a minimum-duration filter
                (<code style="background:#eee; padding:1px 4px; border-radius:2px;">2000 ms</code> by default)
                are reported as detected tones.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Two-pass cross-validation:</p>
            <p style="margin: 0 0 6px 0;">
                Color bars and reference tone are typically authored together at the head of a tape, so
                the two detectors should largely agree. AV Spex runs them in two passes:
            </p>
            <ol style="margin: 4px 0 10px 20px; padding: 0;">
                <li style="margin-bottom: 4px;"><strong>Primary pass</strong> — each detector scans the
                    file independently with its default thresholds.</li>
                <li style="margin-bottom: 4px;"><strong>Head re-check</strong> — if the primary bars pass
                    found nothing near the head of the file, the recorded SSIM scores over the first two
                    minutes are re-evaluated at the relaxed threshold
                    (<code style="background:#eee; padding:1px 4px; border-radius:2px;">0.6</code>).
                    This catches real but degraded head bars that score just under the primary
                    threshold.</li>
                <li style="margin-bottom: 4px;"><strong>Second pass</strong> — when one detector finds a
                    span the other missed, a targeted windowed scan is run on the other detector with
                    <em>relaxed</em> thresholds (bars: SSIM ≥
                    <code style="background:#eee; padding:1px 4px; border-radius:2px;">0.6</code>, sample
                    ratio <code style="background:#eee; padding:1px 4px; border-radius:2px;">5</code>;
                    tone: tolerance
                    <code style="background:#eee; padding:1px 4px; border-radius:2px;">0.7</code>,
                    min duration
                    <code style="background:#eee; padding:1px 4px; border-radius:2px;">500 ms</code>).
                    A ±5 s slack is added around the trigger window because bars and tone don't always
                    start and stop in lockstep.</li>
            </ol>
            <p style="margin: 0 0 10px 0;">
                Re-scan rows are highlighted in the result tables below. The head-bars end time used
                downstream (the BRNG-skip window, access-file trim) is a <strong>cross-validated
                consensus</strong> of qct-parse and CLAMS: when the two detectors disagree, the disputed
                span is checked against the recorded SSIM scores — a span whose scores never approach
                the bars threshold is rejected, and a head claim from one detector that the other
                decisively contradicts is treated as unconfirmed rather than trusted.
            </p>
            <p style="margin: 0 0 6px 0; font-weight: bold;">Fragment merging:</p>
            <p style="margin: 0 0 10px 0;">
                A continuous tone or bars span can dip below threshold for a brief chunk and be reported
                as several adjacent fragments. After detection, AV Spex coalesces fragments separated by
                less than the configured <code style="background:#eee; padding:1px 4px; border-radius:2px;">merge_gap_seconds</code>
                (defaults: 1 s for bars, 5 s for tone) back into a single span so the report reflects
                the underlying continuous signal.
            </p>
            <p style="margin: 0; color: #777;">
                Both detectors write a per-pass durations CSV alongside the report; the bars detector
                additionally writes a per-sampled-frame SSIM scores CSV
                (<code style="background:#eee; padding:1px 4px; border-radius:2px;">pass, frame,
                timestamp, ssim_score, exceeds_threshold</code>). Those recorded scores are also what
                the head re-check and the consensus arbitration read.
            </p>
        </div>
        """
        if bars_comparison_html:
            html_template += f"""
            <h4 style="font-size: 16px; margin-top: 16px; color: #4d2b12;">Bars Detection (qct-parse vs CLAMS SSIM)</h4>
            {bars_comparison_html}
            """
        if tone_detection_html:
            html_template += f"""
            <h4 style="font-size: 16px; margin-top: 16px; color: #4d2b12;">Tone Detection</h4>
            {tone_detection_html}
            """

    if colorbars_eval_html:
        if smpte_fallback:
            eval_header = "Values relative to SMPTE colorbar's thresholds"
        else:
            eval_header = "Values relative to colorbar's thresholds"
        html_template += f"""
        <h3 id="section-colorbars-eval">{eval_header}</h3>
        {colorbars_eval_html}
        """

    if clamped_levels_html:
        html_template += f"""
        <h3 id="section-clamped-levels">Clamped Levels Detection</h3>
        {clamped_levels_html}
        """

    if chroma_phase_html:
        html_template += f"""
        <h3 id="section-chroma-phase">Chroma Phase Detection</h3>
        {chroma_phase_html}
        """

    has_audio_results = bool(
        audio_clipping_html or channel_imbalance_html
        or audible_timecode_html or audio_dropout_html
        or tone_leak_html
    )

    if has_audio_results:
        html_template += (
            '<h2 id="section-audio-analysis" style="color: #0a5f1c; '
            'text-decoration: underline; margin-top: 30px;">'
            'Audio Analysis Results</h2>'
        )
        html_template += waveform_divider

        # Clipping + Channel Imbalance side-by-side
        if audio_clipping_html or channel_imbalance_html:
            html_template += (
                '<div style="display: flex; flex-wrap: wrap; gap: 24px; '
                'align-items: flex-start; margin: 16px 0;">'
            )
            if audio_clipping_html:
                html_template += f"""
                <div style="flex: 1 1 420px; min-width: 0;">
                    <h3>Audio Clipping Detection</h3>
                    {audio_clipping_html}
                </div>
                """
            if channel_imbalance_html:
                html_template += f"""
                <div style="flex: 1 1 420px; min-width: 0;">
                    <h3>Channel Imbalance Analysis</h3>
                    {channel_imbalance_html}
                </div>
                """
            html_template += '</div>'

        # Audible Timecode + Audio Dropout side-by-side
        if audible_timecode_html or audio_dropout_html:
            html_template += (
                '<div style="display: flex; flex-wrap: wrap; gap: 24px; '
                'align-items: flex-start; margin: 16px 0;">'
            )
            if audible_timecode_html:
                html_template += f"""
                <div style="flex: 1 1 420px; min-width: 0;">
                    <h3>Audible Timecode Detection</h3>
                    {audible_timecode_html}
                </div>
                """
            if audio_dropout_html:
                html_template += f"""
                <div style="flex: 1 1 420px; min-width: 0;">
                    <h3>Audio Dropout Detection</h3>
                    {audio_dropout_html}
                </div>
                """
            html_template += '</div>'

        # Tone Leak Detection (full width - the per-channel table is wide)
        if tone_leak_html:
            html_template += f"""
            <div id="section-tone-leak" style="margin: 16px 0;">
                <h3>Tone Leak Detection</h3>
                {tone_leak_html}
            </div>
            """

        html_template += waveform_divider

    if dropped_sample_html:
        html_template += f'<div id="section-dropped-sample">{dropped_sample_html}</div>'

    if difference_csv:
        html_template += f"""
        <h3 id="section-difference-csv">{difference_csv_filename}</h3>
        {diff_csv_html}
        """

    if profile_summary_html:
        html_template += f"""
        <h3 id="section-profile-summary">qct-parse Profile Summary</h3>
        <div style="white-space: nowrap;">
            {profile_summary_html}
        </div>
        """

    if tags_summary_html:
        html_template += f"""
        <h3 id="section-tags-summary">qct-parse Tag Check Summary</h3>
        <div style="white-space: nowrap;">
            {tags_summary_html}
        </div>
        """

    if content_summary_html_list:
        for idx, content_summary_html in enumerate(content_summary_html_list):
            # Only first content-detection block gets the anchor id so TOC
            # links resolve even when multiple blocks render.
            heading_id = ' id="section-content-summary"' if idx == 0 else ''
            html_template += f"""
            <h3{heading_id}>qct-parse Content Detection</h3>
            <div style="white-space: nowrap;">
                {content_summary_html}
            </div>
            """

    # Modified sections with collapsible functionality
    if exiftool_output_path:
        html_template += f"""
        <h3 id="section-exiftool">{exif_file_filename}</h3>
        <a id="link_exiftool" href="javascript:void(0);" onclick="toggleContent('exiftool', 'Show content ▼', 'Hide content ▲')" style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block;">Show content ▼</a>
        <div id="exiftool" class="metadata-content" style="display: none;">{exif_file_content}</div>
        """

    if mediainfo_output_path:
        html_template += f"""
        <h3 id="section-mediainfo">{mi_file_filename}</h3>
        <a id="link_mediainfo" href="javascript:void(0);" onclick="toggleContent('mediainfo', 'Show content ▼', 'Hide content ▲')" style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block;">Show content ▼</a>
        <div id="mediainfo" class="metadata-content" style="display: none;">{mi_file_content}</div>
        """

    if ffprobe_output_path:
        html_template += f"""
        <h3 id="section-ffprobe">{ffprobe_file_filename}</h3>
        <a id="link_ffprobe" href="javascript:void(0);" onclick="toggleContent('ffprobe', 'Show content ▼', 'Hide content ▲')" style="color: #378d6a; text-decoration: underline; margin-bottom: 10px; display: block;">Show content ▼</a>
        <div id="ffprobe" class="metadata-content" style="display: none;">{ffprobe_file_content}</div>
        """

    if check_cancelled():
        return

    html_template += color_strip_init_script
    html_template += waveform_init_script   
    html_template += """
    </body>
    </html>
    """

    if signals:
        signals.report_progress.emit(96)

    # Minify: collapse runs of whitespace between tags and strip leading whitespace on lines
    html_template = re.sub(r'>\s+<', '>\n<', html_template)
    html_template = re.sub(r'^\s+', '', html_template, flags=re.MULTILINE)

    # Write the HTML file
    with open(html_report_path, 'w') as f:
        f.write(html_template)

    if signals:
        signals.report_progress.emit(100)

    logger.info("HTML report generated successfully!\n")