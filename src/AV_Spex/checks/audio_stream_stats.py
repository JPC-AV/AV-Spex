"""Generate a QCTools-compatible audio-stats sidecar from a file's audio streams.

qct-parse's audio analysis (checks/qct_parse.py analyzeAudio) reads per-channel
astats and R128 tags from the QCTools report's audio frames. For inputs with
multiple audio streams (e.g. broadcast MXF, where each channel is a discrete
mono stream) qcli downmixes all streams into a single analysis signal, so the
report's audio frames don't describe the real per-stream audio.

This module produces a substitute sidecar, ``{video_id}.audio_stats.xml.gz``,
by decoding the streams with ``ffprobe -f lavfi`` and merging them (amerge) into
one N-channel signal before astats runs — so astats channel N is audio stream N,
the same 1-based channel ↔ stream mapping make_access.py's multi-mono handling
assumes. The output is the QCTools XML shape analyzeAudio expects and is passed
to it in place of the QCTools report; nothing downstream changes.

Derived from developer_docs/make_signalstats_xml.py (audio pass), which serves
the same role for files qcli rejects outright.
"""

import gzip
import os
import subprocess

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils import ffprobe_probe

AUDIO_STATS_SUFFIX = ".audio_stats.xml.gz"

# Lines that delimit the ffprobe XML document/structure but must not appear in
# the merged document, plus the `<tags>` wrapper: ffprobe nests tags inside
# <tags>...</tags>, but qct-parse's readers (`for t in list(frame)`) expect
# <tag> elements as direct children of <frame>. Everything else (frame elements
# and their flat <tag> children) is kept.
SKIP_PREFIXES = (
    "<?xml",
    "<ffprobe>",
    "</ffprobe>",
    "<frames>",
    "</frames>",
    "<tags>",
    "</tags>",
)

# astats MUST come first so it sees every channel before aphasemeter/ebur128
# collapse the stream to a stereo downmix. Do NOT insert asetnsamples — it
# reframes the stream and drops per-channel astats metadata. ebur128's 100ms
# cadence drives the audio frame rate (~10 fps).
AUDIO_FILTER = "astats=metadata=1:reset=1,aphasemeter=video=0,ebur128=metadata=1"

# astats reports a dB level of "-inf" for a frame of true digital silence
# (all-zero samples). Per-frame (~0.1s) astats hits this on any silent span,
# whereas qcli's much longer analysis window rarely does. qct-parse averages
# per-channel RMS_level arithmetically (checks/qct_parse.py
# _write_imbalance_results), so a single "-inf" frame poisons a channel's mean
# to -inf and the channel is wrongly reported silent — even channels that carry
# audio elsewhere (e.g. a digitally silent leader/trailer). Emit a finite floor
# instead, representing digital silence as a very low dBFS level (like a noise
# floor). The floor sits well below qct-parse's silence/dropout/LTC thresholds
# (SILENCE_THRESHOLD_DB=-60, DROPOUT_SILENCE_FLOOR_DB=-55,
# _TC_ASTATS_RMS_LEVEL_MIN=-30), so genuinely silent channels are still flagged
# silent while audible channels survive the average.
SILENCE_FLOOR_DB = -120.0
_NEG_INF_TAG = 'value="-inf"'
_SILENCE_FLOOR_TAG = f'value="{SILENCE_FLOOR_DB:.6f}"'

# How often (in output lines) to poll check_cancelled during generation.
_CANCEL_POLL_LINES = 500


def audio_stats_path(output_dir, video_id):
    """Return the sidecar path for video_id inside output_dir."""
    return os.path.join(output_dir, f"{video_id}{AUDIO_STATS_SUFFIX}")


def _escape_movie_path(path):
    """Escape a path for use inside the lavfi amovie source argument.

    lavfi treats ':', '\\' and '\\'' specially inside the quoted filename.
    """
    return path.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")



def probe_audio_streams(video_path):
    """[(absolute_stream_index, channel_count), ...] in stream order.

    Empty list if it can't be determined, matching the callers that treat
    "no audio streams" and "probe failed" the same way here.
    """
    streams = ffprobe_probe.audio_streams(video_path)
    return [(s.index, s.channels) for s in (streams or [])]


def build_lavfi_graph(video_path, stream_indexes):
    """Build the lavfi graph merging the given audio streams through AUDIO_FILTER.

    stream_indexes are absolute (container) stream indexes. amovie's ``s=``
    option takes bare integer specifiers separated by '+'; ``a:N``-style
    specifiers can't be used there because their ':' fails the option parsing
    even escaped or quoted.
    """
    escaped = _escape_movie_path(str(video_path))
    if len(stream_indexes) == 1:
        return f"amovie='{escaped}':s={stream_indexes[0]},{AUDIO_FILTER}"
    s_opt = '+'.join(str(i) for i in stream_indexes)
    labels = ''.join(f'[a{n}]' for n in range(len(stream_indexes)))
    return (
        f"amovie='{escaped}':s={s_opt}{labels};"
        f"{labels}amerge=inputs={len(stream_indexes)},{AUDIO_FILTER}"
    )


def _filter_line(line):
    """Return the line to write, or None to drop it.

    Drops document wrappers and the <tags> wrapper, and floors digital-silence
    "-inf" dB values to SILENCE_FLOOR_DB (see comment above).
    """
    if line.lstrip().startswith(SKIP_PREFIXES):
        return None
    if _NEG_INF_TAG in line:
        line = line.replace(_NEG_INF_TAG, _SILENCE_FLOOR_TAG)
    return line


def generate_audio_stats_sidecar(video_path, output_dir, video_id, check_cancelled=None):
    """Generate ``{video_id}.audio_stats.xml.gz`` in output_dir.

    Decodes every audio stream of video_path once (single amovie instance),
    merges them into one N-channel signal, and writes the resulting
    astats/aphasemeter/ebur128 frames as a gzipped QCTools-shaped XML document.
    The write goes to a temporary name and is renamed into place only on
    success, so a failed or cancelled run never leaves a truncated sidecar for
    the reuse-if-present check to pick up.

    Returns:
        str or None: Path to the written sidecar, or None on failure/cancel.
    """
    streams = probe_audio_streams(video_path)
    if not streams:
        logger.error(f"Could not determine audio stream layout of {os.path.basename(video_path)}\n")
        return None

    channel_total = sum(ch for _, ch in streams)
    layout_desc = '+'.join(str(ch) for _, ch in streams)
    logger.info(
        f"Generating per-stream audio stats for {os.path.basename(video_path)}: "
        f"{len(streams)} audio streams ({layout_desc} channels) merged in stream "
        f"order -> analysis channels 1-{channel_total}\n"
    )

    graph = build_lavfi_graph(video_path, [idx for idx, _ in streams])
    command = [
        'ffprobe',
        '-hide_banner',
        '-loglevel', 'error',
        '-f', 'lavfi',
        '-i', graph,
        '-show_frames',
        '-of', 'xml',
    ]

    out_path = audio_stats_path(output_dir, video_id)
    tmp_path = out_path + '.part'
    proc = None
    frame_count = 0
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        line_count = 0
        with gzip.open(tmp_path, 'wt', encoding='utf-8') as g:
            g.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            g.write('<ffprobe>\n<frames>\n')
            for line in proc.stdout:
                line_count += 1
                if check_cancelled and line_count % _CANCEL_POLL_LINES == 0:
                    if check_cancelled():
                        logger.warning("Audio stats generation cancelled\n")
                        proc.kill()
                        return None
                out_line = _filter_line(line)
                if out_line is None:
                    continue
                if out_line.lstrip().startswith('<frame '):
                    frame_count += 1
                g.write(out_line)
            g.write('</frames>\n</ffprobe>\n')

        err = proc.stderr.read()
        rc = proc.wait()
        if rc != 0:
            logger.error(
                f"ffprobe audio stats pass failed (exit {rc}) for "
                f"{os.path.basename(video_path)}:\n{err.strip()}\n"
            )
            return None
        if frame_count == 0:
            logger.error(
                f"ffprobe audio stats pass produced no audio frames for "
                f"{os.path.basename(video_path)}\n"
            )
            return None

        os.replace(tmp_path, out_path)
        logger.info(
            f"Wrote {frame_count} audio stats frames to {os.path.basename(out_path)}\n"
        )
        return out_path
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"Audio stats generation failed for {os.path.basename(video_path)}: {e}\n")
        if proc and proc.poll() is None:
            proc.kill()
        return None
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
