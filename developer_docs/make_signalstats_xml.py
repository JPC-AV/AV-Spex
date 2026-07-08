#!/usr/bin/env python3
"""
make_signalstats_xml.py — generate a QCTools-compatible signalstats sidecar for
video files that `qcli` (QCTools) refuses to analyze.

Background
----------
Some JPEG2000 MXF masters decode to an odd picture size (e.g. 720x243 — one
243-line field stored per frame). QCTools' `qcli` rejects such resolutions, so
AV Spex's QCTools step never runs and the whole qct-parse subsystem downstream of
it is skipped.

`ffprobe -f lavfi`, however, does not care about resolution. This script runs the
same lavfi signalstats/astats analysis QCTools would and writes a
`<video_id>.qctools.xml.gz` report in the QCTools XML shape qct-parse expects.
Drop the result into `<video_id>_qc_metadata/` (this script does that by default)
and a normal AV Spex run will discover it via `find_qctools_report()`, skip
`qcli`, and run qct-parse on it.

The audio pass of this script now has an in-app counterpart:
`src/AV_Spex/checks/audio_stream_stats.py` generates a per-stream audio stats
sidecar (`<video_id>.audio_stats.xml.gz`) for multi-stream inputs, where qcli
downmixes the streams. Keep the two in sync if the filter chain or the
QCTools-compat handling below changes.

Two QCTools-compat details this script handles:
  1. `ffprobe -of xml` nests tags inside a `<tags>...</tags>` element, but real
     QCTools (and qct-parse's `for t in list(frame)` readers) expect `<tag>` as
     direct children of `<frame>`. We strip the `<tags>` wrapper.
  2. `ffprobe -f lavfi` only supports one output sink, so video (signalstats) and
     audio (astats/aphasemeter/ebur128) are produced in two passes and merged into
     a single `<frames>` block. Both passes emit `pkt_dts_time`, which is the
     timestamp attribute qct-parse auto-detects from the first video frame and
     then reuses for audio frames.

Usage
-----
    python developer_docs/make_signalstats_xml.py FILE.mxf [FILE2.mxf ...]
    python developer_docs/make_signalstats_xml.py /path/to/dir   # all video files within
    python developer_docs/make_signalstats_xml.py FILE.mxf --video-only
    python developer_docs/make_signalstats_xml.py FILE.mxf --outdir /somewhere
    python developer_docs/make_signalstats_xml.py FILE.mxf --duration 30   # bounded test
"""

import argparse
import gzip
import os
import subprocess
import sys
from pathlib import Path

# Lines that delimit the ffprobe XML document/structure but must not appear
# repeated inside our merged document, plus the `<tags>` wrapper qct-parse can't
# read. Everything else (frame elements and their flat <tag> children) is kept.
SKIP_PREFIXES = (
    "<?xml",
    "<ffprobe>",
    "</ffprobe>",
    "<frames>",
    "</frames>",
    "<tags>",
    "</tags>",
)

# Video lavfi: per-frame signalstats. `stat=tout+vrep+brng` mirrors QCTools and
# makes ffmpeg compute the TOUT/VREP/BRNG ratios in addition to the default
# min/low/avg/high/max + diff + SAT/HUE stats.
VIDEO_FILTER = "signalstats=stat=tout+vrep+brng"

# Audio lavfi: astats MUST come first so it sees every channel before
# aphasemeter/ebur128 collapse the stream to a stereo downmix. Do NOT insert
# asetnsamples — it reframes the stream and drops per-channel astats metadata.
# ebur128's 100ms cadence drives the audio frame rate (~10 fps).
AUDIO_FILTER = "astats=metadata=1:reset=1,aphasemeter=video=0,ebur128=metadata=1"

# Probed by AV Spex's dependency checker too; kept simple here.
VIDEO_EXTENSIONS = (".mxf", ".mkv", ".mov", ".mp4", ".avi", ".dv", ".m2t", ".ts")

# astats reports a dB level of "-inf" for a frame of true digital silence
# (all-zero samples). Our per-frame (~0.1s) astats hits this on any silent span,
# whereas qcli's ~4.8s sliding window rarely does. qct-parse averages per-channel
# RMS_level arithmetically (checks/qct_parse.py:_write_imbalance_results), so a
# single "-inf" frame poisons a channel's mean to -inf and the channel is wrongly
# reported silent — even channels that carry audio elsewhere (e.g. a digitally
# silent leader/trailer). Emit a finite floor instead, representing digital
# silence as a very low dBFS level (like a noise floor). The floor sits well below
# qct-parse's silence/dropout/LTC thresholds (SILENCE_THRESHOLD_DB=-60,
# DROPOUT_SILENCE_FLOOR_DB=-55, _TC_ASTATS_RMS_LEVEL_MIN=-30), so genuinely silent
# channels are still flagged silent while audible channels survive the average.
SILENCE_FLOOR_DB = -120.0
_NEG_INF_TAG = 'value="-inf"'
_SILENCE_FLOOR_TAG = f'value="{SILENCE_FLOOR_DB:.6f}"'


def _escape_movie_path(path: str) -> str:
    """Escape a path for use inside the lavfi movie/amovie source argument.

    lavfi treats ':', '\\' and '\'' specially inside the quoted filename.
    """
    return path.replace("\\", "\\\\").replace("'", r"\'").replace(":", r"\:")


def _ffprobe_xml_lines(source_filter: str, input_path: str, duration):
    """Yield ffprobe XML output lines for a single lavfi pass (streaming).

    Raises CalledProcessError-style RuntimeError on a non-zero exit.
    """
    escaped = _escape_movie_path(input_path)
    cmd = [
        "ffprobe",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", f"{source_filter}='{escaped}'," + (
            VIDEO_FILTER if source_filter == "movie" else AUDIO_FILTER
        ),
    ]
    if duration:
        cmd += ["-read_intervals", f"%+{duration}"]
    cmd += ["-show_frames", "-of", "xml"]

    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    for line in proc.stdout:
        yield line
    proc.stdout.close()
    err = proc.stderr.read()
    proc.stderr.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(
            f"ffprobe failed (exit {rc}) for {input_path}:\n{err.strip()}"
        )


def _write_frames(out, source_filter, input_path, duration, label):
    """Stream one ffprobe pass into the open output, flattening <tags> and
    dropping document wrappers. Returns the number of <frame ...> opens seen.

    For the audio pass, digital-silence "-inf" dB levels are floored to
    SILENCE_FLOOR_DB so they don't poison qct-parse's per-channel RMS averages
    (see SILENCE_FLOOR_DB comment above)."""
    is_audio = label == "audio"
    frame_count = 0
    for line in _ffprobe_xml_lines(source_filter, input_path, duration):
        stripped = line.lstrip()
        if stripped.startswith(SKIP_PREFIXES):
            continue
        if stripped.startswith("<frame "):
            frame_count += 1
        elif is_audio and _NEG_INF_TAG in line:
            line = line.replace(_NEG_INF_TAG, _SILENCE_FLOOR_TAG)
        out.write(line)
    return frame_count


def generate_sidecar(input_path: Path, outdir: Path, video_only: bool, duration):
    """Generate one `<video_id>.qctools.xml.gz` for `input_path` into `outdir`."""
    outdir.mkdir(parents=True, exist_ok=True)
    video_id = input_path.stem
    out_path = outdir / f"{video_id}.qctools.xml.gz"

    print(f"[{video_id}] video pass (signalstats)...", flush=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as g:
        g.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        g.write("<ffprobe>\n<frames>\n")
        nv = _write_frames(g, "movie", str(input_path), duration, "video")
        print(f"[{video_id}]   {nv} video frames", flush=True)

        na = 0
        if not video_only:
            print(f"[{video_id}] audio pass (astats/aphasemeter/ebur128)...", flush=True)
            na = _write_frames(g, "amovie", str(input_path), duration, "audio")
            print(f"[{video_id}]   {na} audio frames", flush=True)

        g.write("</frames>\n</ffprobe>\n")

    print(f"[{video_id}] wrote {out_path} ({out_path.stat().st_size:,} bytes)", flush=True)
    return out_path


def _collect_inputs(paths):
    """Expand directories to the video files they contain; keep files as-is."""
    inputs = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            for child in sorted(path.iterdir()):
                if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                    inputs.append(child)
        elif path.is_file():
            inputs.append(path)
        else:
            print(f"WARNING: not found, skipping: {p}", file=sys.stderr)
    return inputs


def main():
    parser = argparse.ArgumentParser(
        description="Generate a QCTools-compatible signalstats sidecar "
                    "(<video_id>.qctools.xml.gz) for files qcli rejects."
    )
    parser.add_argument("inputs", nargs="+",
                        help="Video file(s) or directory(ies) to process.")
    parser.add_argument("--outdir", default=None,
                        help="Output directory. Default: "
                             "<file parent>/<video_id>_qc_metadata/")
    parser.add_argument("--video-only", action="store_true",
                        help="Skip the audio (astats/r128/aphasemeter) pass.")
    parser.add_argument("--duration", type=float, default=None,
                        help="Only analyze the first N seconds (for quick "
                             "bounded tests).")
    args = parser.parse_args()

    inputs = _collect_inputs(args.inputs)
    if not inputs:
        print("No input video files found.", file=sys.stderr)
        return 1

    failures = 0
    for input_path in inputs:
        if args.outdir:
            outdir = Path(args.outdir)
        else:
            outdir = input_path.parent / f"{input_path.stem}_qc_metadata"
        try:
            generate_sidecar(input_path, outdir, args.video_only, args.duration)
        except Exception as exc:  # keep going across a batch
            failures += 1
            print(f"ERROR processing {input_path}: {exc}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
