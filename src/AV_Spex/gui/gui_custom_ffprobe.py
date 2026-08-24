from dataclasses import asdict

from PyQt6.QtWidgets import QMessageBox

from AV_Spex.utils.config_setup import (
    FfprobeProfile, FFmpegVideoStream, FFmpegAudioStream, FFmpegFormat
)
from AV_Spex.gui import gui_custom_profile_common as profile_common
from AV_Spex.gui.gui_custom_profile_common import ProfileSection
from AV_Spex.utils import ffprobe_import


# Each tuple: (field_name, label_text, default_values, tooltip)

_VIDEO_STREAM_FIELDS = (
    ("codec_name", "Codec Name", ["ffv1"], "Video codec short name (e.g., ffv1, v210, prores)"),
    ("codec_long_name", "Codec Long Name", ["FFmpeg video codec #1"], "Full codec name"),
    ("codec_type", "Codec Type", ["video"], "Stream type"),
    ("codec_tag_string", "Codec Tag String", ["FFV1"], "FourCC or codec tag string"),
    ("codec_tag", "Codec Tag", ["0x31564646"], "Hex codec tag value"),
    ("width", "Width", ["720"], "Video width in pixels"),
    ("height", "Height", ["486"], "Video height in pixels"),
    ("display_aspect_ratio", "Display Aspect Ratio", ["400:297"], "Display aspect ratio"),
    ("pix_fmt", "Pixel Format", ["yuv422p10le"], "Pixel format (e.g., yuv422p10le)"),
    ("color_space", "Color Space", ["smpte170m"], "Color space"),
    ("color_transfer", "Color Transfer", ["bt709"], "Transfer characteristics"),
    ("color_primaries", "Color Primaries", ["smpte170m"], "Color primaries"),
    ("field_order", "Field Order", ["bt"], "Field order (bb=BFF, bt=BFF, tt=TFF, progressive)"),
    ("bits_per_raw_sample", "Bits Per Raw Sample", ["10"], "Bit depth of raw samples"),
)

_AUDIO_STREAM_FIELDS = (
    ("codec_name", "Codec Name", ["flac", "pcm_s24le"], "Audio codec(s) — add multiple for alternatives"),
    ("codec_long_name", "Codec Long Name", ["FLAC (Free Lossless Audio Codec)", "PCM signed 24-bit little-endian"], "Full codec name(s)"),
    ("codec_type", "Codec Type", ["audio"], "Stream type"),
    ("codec_tag", "Codec Tag", ["0x0000"], "Hex codec tag value"),
    ("sample_fmt", "Sample Format", ["s32"], "Audio sample format"),
    ("sample_rate", "Sample Rate", ["48000"], "Audio sample rate in Hz"),
    ("channels", "Channels", ["2"], "Number of audio channels"),
    ("channel_layout", "Channel Layout", ["stereo"], "Channel layout"),
    ("bits_per_raw_sample", "Bits Per Raw Sample", ["24"], "Audio bit depth"),
)

_FORMAT_FIELDS = (
    ("format_name", "Format Name", ["matroska webm"], "Container format short name"),
    ("format_long_name", "Format Long Name", ["Matroska / WebM"], "Container format full name"),
)

# FFmpegFormat.tags is a free-form dict the form does not expose, so a profile
# built here carries the key skeleton AV Spex expects downstream.
_FORMAT_TAGS_DEFAULT = {
    'creation_time': None,
    'ENCODER': None,
    'TITLE': None,
    'ENCODER_SETTINGS': None,
    'DESCRIPTION': None,
    'ORIGINAL MEDIA TYPE': None,
    'ENCODED_BY': None,
}


class CustomFfprobeDialog(profile_common.SectionedProfileDialog):
    """Create or edit a custom FFprobe profile.

    Three sections — video stream, audio stream, format — matching FfprobeProfile.
    """

    TOOL_LABEL = "FFprobe"
    NEW_DESCRIPTION = (
        "Define expected FFprobe values for file validation. "
        "Fields are organized by Video Stream, Audio Stream, and Format sections."
    )
    NAME_PLACEHOLDER = "e.g., Custom MKV FFV1 FFprobe Profile"
    MIN_SIZE = (750, 850)

    SECTIONS = (
        ProfileSection('video_stream', 'Video Stream', _VIDEO_STREAM_FIELDS),
        ProfileSection('audio_stream', 'Audio Stream', _AUDIO_STREAM_FIELDS),
        ProfileSection('format', 'Format', _FORMAT_FIELDS),
    )

    # Fields with constrained value sets get editable combo boxes.
    DROPDOWN_OPTIONS = {
        "codec_type": ["video", "audio"],
        "pix_fmt": [
            "yuv422p10le", "yuv422p", "yuv420p", "yuv420p10le",
            "yuv444p", "yuv444p10le", "uyvy422", "v210",
            "rgb24", "bgr24", "gbrp", "gbrp10le",
            "yuyv422", "gray", "nv12"
        ],
        "color_space": [
            "bt709", "bt470bg", "smpte170m", "smpte240m",
            "bt2020nc", "bt2020c", "fcc", "ycgco",
            "chroma-derived-nc", "ictcp", "rgb", "unknown"
        ],
        "color_transfer": [
            "bt709", "smpte170m", "gamma22", "gamma28",
            "smpte240m", "linear", "bt2020-10", "bt2020-12",
            "smpte2084", "arib-std-b67", "iec61966-2-1",
            "iec61966-2-4", "unknown"
        ],
        "color_primaries": [
            "bt709", "smpte170m", "bt470bg", "bt470m",
            "smpte240m", "film", "bt2020",
            "smpte431", "smpte432", "jedec-p22", "unknown"
        ],
        "field_order": [
            "progressive", "tt", "bb", "tb", "bt", "unknown"
        ],
        "sample_fmt": [
            "s16", "s32", "s16p", "s32p", "fltp", "flt",
            "dblp", "dbl", "s64", "s64p", "u8", "u8p"
        ],
        "channel_layout": [
            "mono", "stereo", "2.1", "3.0", "4.0",
            "5.1", "5.1(side)", "7.1", "7.1(wide)"
        ],
        "bits_per_raw_sample": [
            "8", "10", "12", "16", "24", "32"
        ],
        "sample_rate": [
            "48000", "96000", "44100", "88200",
            "176400", "192000", "32000", "22050"
        ],
        "channels": [
            "1", "2", "4", "6", "8"
        ],
    }

    CODEC_NAME_OPTIONS = {
        "video_stream": [
            "ffv1", "v210", "prores", "rawvideo", "mpeg2video",
            "dvvideo", "h264", "hevc", "jpeg2000",
            "huffyuv", "mjpeg"
        ],
        "audio_stream": [
            "pcm_s16le", "pcm_s24le", "pcm_s32le",
            "pcm_s16be", "pcm_s24be",
            "flac", "aac", "ac3", "mp3", "opus", "vorbis"
        ],
    }

    def dropdown_options_for(self, field_name, section_key=None):
        """codec_name means different things in the video and audio sections."""
        options = self.DROPDOWN_OPTIONS.get(field_name)
        if options is None and field_name == "codec_name" and section_key:
            options = self.CODEC_NAME_OPTIONS.get(section_key)
        return options

    # -- import / compare ---------------------------------------------------

    IMPORT_LABEL = "FFprobe"
    IMPORT_CAPTION = "Select FFprobe Output File"
    COMPARE_CAPTION = "Select FFprobe Output File to Compare"
    IMPORT_FILTER = ("FFprobe Output Files (*.txt *.json);;Text Files (*.txt);;"
                     "JSON Files (*.json);;All Files (*.*)")
    IMPORT_HINT = ("Please check the file contains valid FFprobe JSON output\n"
                   "(e.g., from: ffprobe -print_format json).")
    SECTION_LABELS = {
        'video_stream': 'VIDEO STREAM',
        'audio_stream': 'AUDIO STREAM',
        'format': 'FORMAT',
    }

    def import_profile_from_path(self, file_path):
        return ffprobe_import.import_ffprobe_file_to_profile(file_path)

    def validate_path_against_profile(self, file_path, profile):
        return ffprobe_import.validate_file_against_profile(file_path, profile)

    # -- profile assembly ---------------------------------------------------

    def sections_from_profile(self, profile_data):
        """Split an FfprobeProfile, or the plain dicts spex_config stores."""
        if hasattr(profile_data, '__dataclass_fields__'):
            return {
                'video_stream': asdict(profile_data.video_stream),
                'audio_stream': asdict(profile_data.audio_stream),
                'format': asdict(profile_data.format),
            }

        if not isinstance(profile_data, dict):
            return {}

        sections = {}
        for key in ('video_stream', 'audio_stream', 'format'):
            value = profile_data.get(key, {})
            sections[key] = asdict(value) if hasattr(value, '__dataclass_fields__') else value
        return sections

    def build_profile(self, values):
        """Assemble an FfprobeProfile, warning and returning None if invalid."""
        video_data = dict(values['video_stream'])
        audio_data = dict(values['audio_stream'])
        format_data = dict(values['format'])

        for section_label, data, required in (
            ("Video Stream", video_data, ("codec_name",)),
            ("Format", format_data, ("format_name",)),
        ):
            for field_name in required:
                value = data.get(field_name)
                if not value or (isinstance(value, list) and not value):
                    QMessageBox.warning(
                        self, "Validation Error",
                        f"{section_label} > {field_name} is required."
                    )
                    return None

        # These audio fields are typed as lists even for a single codec.
        for list_field in ('codec_name', 'codec_long_name'):
            audio_value = audio_data.get(list_field, "")
            if isinstance(audio_value, str) and audio_value:
                audio_data[list_field] = [audio_value]
            elif not audio_value:
                audio_data[list_field] = []

        # The form offers only a subset of each dataclass; fill the rest.
        for field_name in FFmpegVideoStream.__dataclass_fields__:
            video_data.setdefault(field_name, "")
        for field_name in FFmpegAudioStream.__dataclass_fields__:
            audio_data.setdefault(
                field_name, [] if field_name in ('codec_name', 'codec_long_name') else ""
            )
        for field_name in FFmpegFormat.__dataclass_fields__:
            format_data.setdefault(
                field_name, dict(_FORMAT_TAGS_DEFAULT) if field_name == 'tags' else ""
            )

        try:
            return FfprobeProfile(
                video_stream=FFmpegVideoStream(**video_data),
                audio_stream=FFmpegAudioStream(**audio_data),
                format=FFmpegFormat(**format_data),
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create profile:\n{str(e)}")
            return None

    def preview_line(self, profile_name, values):
        codec = values['video_stream'].get("codec_name") or "N/A"
        width = values['video_stream'].get("width") or "N/A"
        height = values['video_stream'].get("height") or "N/A"
        container = values['format'].get("format_name") or "N/A"
        return f"{profile_name}: {container} / {codec} {width}x{height}"

    # -- kept for callers that used the domain-named getter -----------------

    def get_ffprobe_profile(self):
        return self.build_profile_from_form()
