"""One-line human-readable summaries of the current spex values.

Used by the Spex tab's cards to show what each domain expects at a glance
(e.g. "FFV1 · 720×486 · Interlaced BFF"). Every function here is total:
inputs may be dataclasses or plain dicts (SpexConfig's mediainfo_values and
ffmpeg_values are plain dicts at runtime), fields may be missing or None,
and nothing raises — missing pieces render as an em dash.

This module is intentionally Qt-free so it can be tested headlessly.
"""

from dataclasses import asdict

SEPARATOR = " · "
MISSING = "—"


def _get(obj, key, default=None):
    """Fetch a field from a dataclass, dict, or plain object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _text(value):
    """Render a single value or list of alternatives as display text."""
    if value is None or value == "" or value == []:
        return MISSING
    if isinstance(value, list):
        return "/".join(str(v) for v in value)
    return str(value)


def _join(parts):
    """Join non-empty summary fragments, or return the missing marker."""
    parts = [p for p in parts if p and p != MISSING]
    return SEPARATOR.join(parts) if parts else MISSING


def filename_summary(filename_values) -> str:
    """E.g. 'JPC_AV_#####.mkv' — section values joined with underscores."""
    try:
        sections = _get(filename_values, "fn_sections") or {}
        values = [
            _text(_get(section, "value"))
            for _, section in sorted(sections.items())
        ]
        pattern = "_".join(v for v in values if v != MISSING)
        if not pattern:
            return MISSING
        extension = _get(filename_values, "FileExtension")
        return f"{pattern}.{extension}" if extension else pattern
    except Exception:
        return MISSING


def mediainfo_summary(mediainfo_values) -> str:
    """E.g. 'FFV1 · 720×486 · Interlaced BFF' from expected_video."""
    try:
        video = _get(mediainfo_values, "expected_video") or {}
        width = _get(video, "Width")
        height = _get(video, "Height")
        size = f"{_text(width)}×{_text(height)}" if width or height else None
        scan_type = _get(video, "ScanType")
        scan_order = _get(video, "ScanOrder")
        scan = " ".join(_text(s) for s in (scan_type, scan_order) if s) or None
        return _join([_text(_get(video, "Format")), size, scan])
    except Exception:
        return MISSING


def exiftool_summary(exiftool_values) -> str:
    """E.g. 'MKV · 720×486 · FLAC/A_PCM · 24-bit'."""
    try:
        width = _get(exiftool_values, "ImageWidth")
        height = _get(exiftool_values, "ImageHeight")
        size = f"{_text(width)}×{_text(height)}" if width or height else None
        bits = _get(exiftool_values, "AudioBitsPerSample")
        return _join([
            _text(_get(exiftool_values, "FileType")),
            size,
            _text(_get(exiftool_values, "VideoScanType")),
            f"{_text(bits)}-bit audio" if bits else None,
        ])
    except Exception:
        return MISSING


def ffprobe_summary(ffmpeg_values) -> str:
    """E.g. 'ffv1 · 720×486 · yuv422p10le · flac/pcm_s24le'."""
    try:
        video = _get(ffmpeg_values, "video_stream") or {}
        audio = _get(ffmpeg_values, "audio_stream") or {}
        width = _get(video, "width")
        height = _get(video, "height")
        size = f"{_text(width)}×{_text(height)}" if width or height else None
        return _join([
            _text(_get(video, "codec_name")),
            size,
            _text(_get(video, "pix_fmt")),
            _text(_get(audio, "codec_name")),
        ])
    except Exception:
        return MISSING


def signalflow_summary(mediatrace_values) -> str:
    """E.g. 'Sony BVH3100 → Leitch DPS575 → Blackmagic UltraStudio'.

    Each ENCODER_SETTINGS stage is a list whose first entry is the device
    name (subsequent entries are serial number, connections, etc.).
    """
    try:
        encoder_settings = _get(mediatrace_values, "ENCODER_SETTINGS")
        if encoder_settings is not None and not isinstance(encoder_settings, dict):
            if hasattr(encoder_settings, "__dataclass_fields__"):
                encoder_settings = asdict(encoder_settings)
            else:
                encoder_settings = dict(getattr(encoder_settings, "__dict__", {}) or {})
        devices = []
        for stage in ("Source_VTR", "TBC_Framesync", "ADC", "Capture_Device"):
            entries = _get(encoder_settings, stage) or []
            if isinstance(entries, str):
                entries = [entries]
            if entries:
                # First entry is the device; strip any trailing ", SN ..." form.
                devices.append(str(entries[0]).split(",")[0].strip())
        if not devices:
            return "No signal flow set"
        return " → ".join(devices)
    except Exception:
        return MISSING


def qct_parse_summary(qct_parse_values) -> str:
    """E.g. '5 of 38 content thresholds set · SMPTE bars limits configured'."""
    try:
        full_tag_list = _get(qct_parse_values, "fullTagList")
        if full_tag_list is not None and not isinstance(full_tag_list, dict):
            full_tag_list = asdict(full_tag_list) if hasattr(full_tag_list, "__dataclass_fields__") \
                else dict(getattr(full_tag_list, "__dict__", {}) or {})
        full_tag_list = full_tag_list or {}
        set_count = sum(1 for v in full_tag_list.values() if v is not None)
        parts = [f"{set_count} of {len(full_tag_list)} content thresholds set"]
        smpte = _get(qct_parse_values, "smpte_color_bars")
        if smpte is not None:
            parts.append("SMPTE bars limits configured")
        return SEPARATOR.join(parts)
    except Exception:
        return MISSING
