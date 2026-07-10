import subprocess
import os
import sys
from AV_Spex.utils.log_setup import logger, report_ffmpeg_stderr
from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.dir_setup import is_hidden_file

# Fraction of the file duration that channel-specific audible-timecode (LTC)
# regions must cover before the channel is excluded from the access copy. A
# channel with program audio and only brief TC contamination stays in.
LTC_COVERAGE_THRESHOLD = 0.75


def determine_excluded_audio_channels(audio_findings):
    """Decide which channels (1-based) to exclude from the access copy.

    audio_findings is the dict returned by run_qctparse() under 'audio_findings':
    silent_channels (1-based ints from channel imbalance analysis), num_channels,
    timecode_consensus_regions (audible-TC consensus regions with raw-seconds
    start_time/end_time and a channel label of "Channel 1", "Channel 2",
    "Both channels", or "Not channel-specific (mix-based)"), and duration.

    A channel is flagged if it is silent, or if its channel-specific LTC regions
    cover at least LTC_COVERAGE_THRESHOLD of the file duration. Mix-based
    regions name no channel and never trigger exclusion.

    The audio analysis only examines the first stereo pair (astats channels 1
    and 2), so audible-timecode findings are limited to channels 1-2; silence
    findings may name any analyzed channel (e.g. on a 4-channel source). If both
    analyzed program channels (1 and 2) end up flagged, nothing is excluded (the
    caller warns and keeps all audio) — without a surviving channel of the
    analyzed pair the access copy's program audio can't be reconstructed.
    Findings from audio analysis run in a previous pass (CSV reuse) are not
    consulted; the caller falls back to including all audio.

    On a source with more than 2 channels, any exclusion rebuilds the access
    audio from the stereo pair alone, dropping every channel beyond the pair.
    That is only allowed when the analysis positively verified each extra
    channel (3+) as silent; if any extra channel was not flagged silent,
    nothing is excluded and all audio is kept (the caller warns).

    Returns:
        tuple: (excluded_channels, reasons) — excluded_channels is a sorted
        list of 1-based ints, empty when nothing should be excluded; reasons
        describe every flagged channel and, when a safety empties
        excluded_channels, end with an explanation of why exclusion was
        skipped.
    """
    if not audio_findings:
        return [], []

    num_channels = audio_findings.get('num_channels')
    if num_channels is not None and num_channels < 2:
        logger.debug(
            f'Audio channel exclusion skipped: source has {num_channels} '
            f'channel(s); at least a stereo pair is required\n'
        )
        return [], []

    flagged = {}

    for ch in audio_findings.get('silent_channels') or []:
        flagged[ch] = f'channel {ch} is silent (mean RMS below silence threshold)'

    duration = audio_findings.get('duration')
    regions = audio_findings.get('timecode_consensus_regions') or []
    if duration and regions:
        for ch in (1, 2):
            labels = (f'channel {ch}', 'both channels')
            intervals = sorted(
                (r['start_time'], r['end_time']) for r in regions
                if (r.get('channel') or '').strip().lower() in labels
            )
            covered = 0.0
            merged_end = None
            for start, end in intervals:
                if merged_end is not None and start < merged_end:
                    start = merged_end
                if end > start:
                    covered += end - start
                    merged_end = end if merged_end is None else max(merged_end, end)
            coverage = covered / duration
            if coverage >= LTC_COVERAGE_THRESHOLD and ch not in flagged:
                flagged[ch] = (
                    f'channel {ch} carries audible timecode over '
                    f'{coverage:.0%} of the file duration'
                )

    reasons = [flagged[ch] for ch in sorted(flagged)]
    # Both analyzed program channels flagged → no surviving pair channel to
    # build the access audio from; keep all audio (the caller warns).
    if {1, 2}.issubset(flagged):
        reasons.append(
            'both analyzed program channels are flagged, leaving no channel '
            'to rebuild the access audio from'
        )
        return [], reasons

    # Any exclusion rebuilds the access audio from the stereo pair alone,
    # dropping every channel beyond the pair. Only allow that when each extra
    # channel was positively verified silent by the analysis; otherwise keep
    # all audio (the caller warns).
    if flagged and num_channels and num_channels > 2:
        silent = set(audio_findings.get('silent_channels') or [])
        unverified = [ch for ch in range(3, num_channels + 1) if ch not in silent]
        if unverified:
            unverified_str = ', '.join(str(ch) for ch in unverified)
            reasons.append(
                f'channel(s) {unverified_str} beyond the analyzed stereo pair '
                f'were not verified silent and would be dropped by the exclusion'
            )
            return [], reasons

    return sorted(flagged), reasons


def get_duration(video_path):
    """Return the file duration in seconds as a string, or None if unavailable.

    Tries the container (format) duration first, then falls back to the first
    video stream's duration — some MKVs report 'N/A' for format=duration.
    Returns None when neither is available so callers can skip progress math.
    """
    def _probe(entries, stream_args=()):
        command = [
            'ffprobe', '-v', 'error', *stream_args,
            '-show_entries', entries, '-of', 'csv=p=0', video_path,
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE)
        return result.stdout.decode().strip()

    for duration in (
        _probe('format=duration'),
        _probe('stream=duration', ('-select_streams', 'v:0')),
    ):
        if duration and duration != 'N/A':
            return duration
    return None


def get_video_dimensions(video_path):
    """Return (width, height) of the first video stream, or (None, None) on failure."""
    command = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height',
        '-of', 'csv=p=0:s=x',
        video_path
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = result.stdout.decode().strip()
        if not out or 'x' not in out:
            return None, None
        w_str, h_str = out.split('x', 1)
        return int(w_str), int(h_str)
    except (ValueError, subprocess.SubprocessError):
        return None, None


def get_audio_stream_channels(video_path):
    """Return the channel count of every audio stream, in order.

    e.g. ``[2]`` for a single stereo stream (typical MKV) or ``[1, 1]`` for two
    separate mono streams (typical broadcast MXF, where each track is its own
    mono PCM stream). Returns an empty list if it can't be determined.
    """
    command = [
        'ffprobe',
        '-v', 'error',
        '-select_streams', 'a',
        '-show_entries', 'stream=channels',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        video_path,
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out = result.stdout.decode().strip()
        return [int(x) for x in out.split() if x.strip()]
    except (ValueError, OSError, subprocess.SubprocessError):
        return []


def make_access_file(video_path, output_path, check_cancelled=None, signals=None, start_time=None, crop_area=None, crop_to_480=True, excluded_audio_channels=None):
    """Create access file using ffmpeg.

    If start_time (seconds) is provided and > 0, the input is seeked past that
    point so head content (e.g. color bars detected by qct-parse) is excluded
    from the access copy.

    If excluded_audio_channels names a channel (1-based), only the first audio
    stream is mapped and the access copy is rebuilt from the good channel(s) of
    the analyzed stereo pair (channels 1-2) via the pan filter: a single
    surviving pair channel is output as dual-mono stereo, two surviving pair
    channels as straight stereo. The pan reads the wanted channels by index, so
    sources with extra channels — e.g. a 4-channel stream — are handled
    correctly; the extra channels are dropped. determine_excluded_audio_channels()
    only produces exclusions for a >2-channel source when every extra channel
    was verified silent, so a caller using it never drops program audio here.

    Output dimensions for NTSC sources are controlled by crop_to_480:
      * crop_to_480=True (default): NTSC (720x486 input) → 720x480 by trimming
        3 lines top and bottom. With a crop_area, the active region is cropped
        and then scaled to 720x480.
      * crop_to_480=False: NTSC stays at native 720x486. With a crop_area, the
        active region is cropped and then scaled to 720x486.
    PAL (720x576 input) is left at native height regardless of crop_to_480.
    """

    logger.debug(f'Running ffmpeg on {os.path.basename(video_path)} to create access copy {os.path.basename(output_path)}\n')

    duration_str = get_duration(video_path)

    ffmpeg_command = ['ffmpeg', '-n', '-vsync', '0',
        '-hide_banner', '-progress', 'pipe:1', '-nostats', '-loglevel', 'error']

    if start_time and start_time > 0:
        logger.info(f'Trimming first {start_time:.2f}s from access copy (color bars detected by qct-parse)\n')
        ffmpeg_command.extend(['-ss', f'{start_time:.3f}'])

    in_w, in_h = get_video_dimensions(video_path)

    # Pick the target output size based on the source standard.
    if in_h == 576:
        out_w, out_h = 720, 576  # PAL
    elif in_h == 486:
        # NTSC: 720x480 when crop_to_480, else native 720x486.
        out_w, out_h = (720, 480) if crop_to_480 else (720, 486)
    else:
        # Unknown standard — fall back to the input size so we don't distort.
        out_w, out_h = in_w, in_h
        logger.warning(
            f'Unexpected input height {in_h}; access copy will use input dimensions {in_w}x{in_h}\n'
        )

    vf_filters = []
    if crop_area:
        x, y, w, h = (int(v) for v in crop_area)
        # yuv420p requires even dimensions
        if w % 2:
            w -= 1
        if h % 2:
            h -= 1
        vf_filters.append(f'crop={w}:{h}:{x}:{y}')
        if out_w and out_h:
            vf_filters.append(f'scale={out_w}:{out_h}')
            logger.info(
                f'Cropping access copy to active area {w}x{h} at offset ({x},{y}), '
                f'then scaling to {out_w}x{out_h}\n'
            )
        else:
            logger.info(f'Cropping access copy to active area {w}x{h} at offset ({x},{y})\n')
    elif in_h == 486 and crop_to_480:
        # NTSC 720x486: drop top 3 and bottom 3 lines to produce 720x480
        vf_filters.append('crop=720:480:0:3')
        logger.info('Cropping top/bottom 3 lines of access copy to produce 720x480 output\n')
    # PAL (576), NTSC with crop_to_480 disabled, and unknown sources pass
    # through with no default crop.
    vf_filters.extend(['yadif=1', 'format=yuv420p'])

    # Determine the source audio layout. A single stream carrying the analyzed
    # stereo pair (typical MKV, possibly with extra channels) is handled with the
    # pan filter on that one stream. Separate mono streams (typical broadcast MXF,
    # one PCM stream per track) are handled by mapping/merging whole streams,
    # since each analyzed channel N is then its own stream a:(N-1).
    stream_channels = get_audio_stream_channels(video_path)
    multi_mono = len(stream_channels) > 1 and all(c == 1 for c in stream_channels)

    # The audio analysis only examines the first stereo pair (channels 1-2), so
    # the access copy is rebuilt from the good channel(s) of that pair; any extra
    # (unanalyzed) channels/streams — e.g. on a 4-channel file — are dropped.
    # pan channel indices are 0-based (ch1 -> c0).
    excluded = set(excluded_audio_channels or [])
    good = [ch for ch in (1, 2) if ch not in excluded]
    if excluded and good:
        excluded_str = ', '.join(str(ch) for ch in sorted(excluded))
        if multi_mono:
            # Each analyzed channel is its own mono stream (channel N -> a:(N-1)).
            if len(good) == 1:
                keep = good[0]
                # The kept mono stream has a single channel (c0); duplicate it to
                # both output channels for dual-mono stereo.
                audio_args = ['-map', f'0:a:{keep - 1}?', '-af', 'pan=stereo|c0=c0|c1=c0']
                logger.info(
                    f'Excluding audio channel(s) {excluded_str} from access copy; '
                    f'output is dual-mono from channel {keep} (stream a:{keep - 1}).\n'
                )
            else:
                # Both analyzed channels survive — merge their two mono streams
                # into one stereo track; any extra mono streams are dropped.
                fc = f'[0:a:{good[0] - 1}][0:a:{good[1] - 1}]amerge=inputs=2[aout]'
                audio_args = ['-filter_complex', fc, '-map', '[aout]']
                logger.info(
                    f'Excluding audio channel(s) {excluded_str} from access copy; '
                    f'output is stereo from channels {good[0]} and {good[1]} '
                    f'(streams a:{good[0] - 1} and a:{good[1] - 1}).\n'
                )
        else:
            # Single stream containing the analyzed pair (and maybe extra channels).
            if len(good) == 1:
                keep = good[0]
                pan = f'pan=stereo|c0=c{keep - 1}|c1=c{keep - 1}'
                logger.info(
                    f'Excluding audio channel(s) {excluded_str} from access copy; '
                    f'output is dual-mono from channel {keep}. '
                    f'Only the first audio stream is included.\n'
                )
            else:
                pan = f'pan=stereo|c0=c{good[0] - 1}|c1=c{good[1] - 1}'
                logger.info(
                    f'Excluding audio channel(s) {excluded_str} from access copy; '
                    f'output is stereo from channels {good[0]} and {good[1]}. '
                    f'Only the first audio stream is included.\n'
                )
            audio_args = ['-map', '0:a:0?', '-af', pan]
    elif multi_mono:
        # No exclusions: merge every mono stream into a single track so the access
        # copy keeps all audio (matching the single-stream "-map 0:a?" behavior).
        inputs = ''.join(f'[0:a:{i}]' for i in range(len(stream_channels)))
        fc = f'{inputs}amerge=inputs={len(stream_channels)}[aout]'
        audio_args = ['-filter_complex', fc, '-map', '[aout]']
        logger.info(
            f'Merging {len(stream_channels)} mono audio streams into a single '
            f'track for the access copy.\n'
        )
    else:
        # No exclusions, or no surviving channel of the analyzed pair — keep all
        # audio. determine_excluded_audio_channels() already empties the
        # exclusion list when both analyzed channels are flagged.
        audio_args = ['-map', '0:a?']

    ffmpeg_command.extend([
        '-i', video_path,
        '-movflags', 'faststart', '-map', '0:v:0', *audio_args, '-c:v', 'libx264',
        '-vf', ','.join(vf_filters), '-crf', '18', '-preset', 'fast', '-maxrate', '1000k', '-bufsize', '1835k',
        '-c:a', 'aac', '-strict', '-2', '-b:a', '192k', '-f', 'mp4', output_path
    ])

    # Total output duration in microseconds, used to compute progress percent.
    # None when the duration couldn't be probed (e.g. ffprobe returned 'N/A');
    # in that case we still run ffmpeg, just without a percentage readout.
    try:
        total_duration = float(duration_str) if duration_str else None
    except (TypeError, ValueError):
        total_duration = None
    if total_duration is not None and start_time and start_time > 0:
        total_duration = max(0.001, total_duration - start_time)
    duration_ms = total_duration * 1000000 if total_duration else None

    try:
        ffmpeg_process = subprocess.Popen(ffmpeg_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration_prefix = 'out_time_ms='  # prefix of ffmpeg microsecond progress output
        while True:
            ff_output = ffmpeg_process.stdout.readline()
            if not ff_output:
                break
            if ff_output.startswith(duration_prefix):
                if check_cancelled():
                    ffmpeg_process.terminate()
                    return
                if duration_ms:
                    current_frame_str = ff_output.split(duration_prefix)[1].strip()
                    try:
                        current_frame_ms = float(current_frame_str)
                    except ValueError:
                        # ffmpeg emits out_time_ms=N/A before a timestamp is
                        # established; skip those lines rather than aborting.
                        continue
                    percent_complete = (current_frame_ms / duration_ms) * 100
                    if signals:
                        # Make doubly sure we're emitting an integer percentage in range 0-100
                        safe_percent = min(100, max(0, int(percent_complete)))
                        signals.access_file_progress.emit(safe_percent)
                    else:
                        print(f"\rFFmpeg Access Copy Progress: {percent_complete:.2f}%", end='', flush=True)
        ffmpeg_stderr = ffmpeg_process.stderr.read()
        ffmpeg_process.wait()
        report_ffmpeg_stderr(ffmpeg_stderr, "access copy",
                             failure=ffmpeg_process.returncode not in (0, None))
    except Exception as e:
        logger.error(f"Error during ffmpeg process: {str(e)}")
    print("\n")


def process_access_file(video_path, source_directory, video_id, check_cancelled=None, signals=None, color_bars_end_time=None, crop_area=None, crop_to_480=True, excluded_audio_channels=None):
    """
    Generate access file if configured and not already existing.

    Args:
        video_path (str): Path to the input video file
        source_directory (str): Source directory for the video
        video_id (str): Unique identifier for the video
        check_cancelled (callable, optional): Function to check if operation was cancelled
        signals (object, optional): Signal object for progress updates
        color_bars_end_time (float, optional): End time of color bars in seconds, as
            detected by qct-parse. When provided, the access copy is trimmed to start
            after the bars.
        crop_area (tuple, optional): (x, y, w, h) active picture area from sophisticated
            border detection (BRNG-refined when available). When provided, borders are
            cropped off the access copy.
        crop_to_480 (bool): When True (default), NTSC sources are output at 720x480.
            When False, NTSC sources keep their native 720x486 height.
        excluded_audio_channels (list, optional): 1-based channel numbers flagged
            by audio analysis (silent or carrying audible timecode), as decided by
            determine_excluded_audio_channels(). When provided, the access copy is
            rebuilt from the surviving channel(s) of the analyzed stereo pair
            (dual-mono from one, or stereo from two); extra channels on a
            multi-channel source stream are dropped.

    Returns:
        str or None: Path to the created access file, or None
    """
    config_mgr = ConfigManager()
    checks_config = config_mgr.get_config('checks', ChecksConfig)

    # Check if access file should be generated
    if not checks_config.outputs.access_file:
        return None

    access_output_path = os.path.join(source_directory, f'{video_id}_access.mp4')

    try:
        # Check if access file already exists
        for filename in os.listdir(source_directory):
            if not is_hidden_file(filename) and filename.lower().endswith('mp4'):
                logger.critical(f"Access file already exists, not running ffmpeg\n")
                if signals:
                    signals.step_completed.emit("Generate Access File")
                return None
        if os.path.isfile(access_output_path):
            logger.critical(f"Access file already exists, not running ffmpeg\n")
            if signals:
                signals.step_completed.emit("Generate Access File")
            return None

        # Generate access file
        make_access_file(
            video_path, access_output_path,
            check_cancelled=check_cancelled, signals=signals,
            start_time=color_bars_end_time,
            crop_area=crop_area,
            crop_to_480=crop_to_480,
            excluded_audio_channels=excluded_audio_channels
        )
        if signals:
            signals.step_completed.emit("Generate Access File")
        return access_output_path

    except Exception as e:
        logger.critical(f"Error creating access file: {e}")
        return None