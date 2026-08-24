from dataclasses import asdict

from PyQt6.QtWidgets import QMessageBox

from AV_Spex.utils.config_setup import (
    MediainfoProfile, MediainfoGeneralValues,
    MediainfoVideoValues, MediainfoAudioValues
)
from AV_Spex.gui import gui_custom_profile_common as profile_common
from AV_Spex.gui.gui_custom_profile_common import ProfileSection
from AV_Spex.utils import mediainfo_import


# Each tuple: (field_name, label_text, default_values, tooltip)

_GENERAL_FIELDS = (
    ("FileExtension", "File Extension", ["mkv"], "File extension (e.g., mkv, mov, avi)"),
    ("Format", "Format", ["Matroska"], "Container format (e.g., Matroska, MPEG-4, AVI)"),
    ("OverallBitRate_Mode", "Bitrate Mode", ["VBR"], "Overall bitrate mode (VBR or CBR)"),
)

_VIDEO_FIELDS = (
    ("Format", "Format", ["FFV1"], "Video codec (e.g., FFV1, v210, ProRes)"),
    ("Format_Settings_GOP", "GOP Settings", ["N=1"], "GOP structure"),
    ("CodecID", "Codec ID", ["V_MS/VFW/FOURCC / FFV1"], "Video codec identifier"),
    ("Width", "Width", ["720"], "Video width in pixels"),
    ("Height", "Height", ["486"], "Video height in pixels"),
    ("PixelAspectRatio", "Pixel Aspect Ratio", ["0.900"], "PAR value"),
    ("DisplayAspectRatio", "Display Aspect Ratio", ["1.333"], "DAR value"),
    ("FrameRate_Mode_String", "Frame Rate Mode", ["Constant"], "Constant or Variable"),
    ("FrameRate", "Frame Rate", ["29.970"], "Frame rate in fps"),
    ("Standard", "Standard", ["NTSC"], "Video standard (NTSC, PAL, etc.)"),
    ("ColorSpace", "Color Space", ["YUV"], "Color space"),
    ("ChromaSubsampling", "Chroma Subsampling", ["4:2:2"], "Chroma subsampling"),
    ("BitDepth", "Bit Depth", ["10"], "Video bit depth"),
    ("ScanType", "Scan Type", ["Interlaced"], "Interlaced or Progressive"),
    ("ScanOrder", "Scan Order", ["Bottom Field First"], "Field order for interlaced"),
    ("Compression_Mode", "Compression", ["Lossless"], "Lossless or Lossy"),
    ("colour_primaries", "Color Primaries", ["BT.601 NTSC"], "Color primaries"),
    ("colour_primaries_Source", "Color Primaries Source", ["Stream"], "Source of color primaries"),
    ("transfer_characteristics", "Transfer Characteristics", ["BT.709"], "Transfer function"),
    ("transfer_characteristics_Source", "Transfer Char. Source", ["Stream"], "Source of transfer char."),
    ("matrix_coefficients", "Matrix Coefficients", ["BT.601"], "Matrix coefficients"),
    ("MaxSlicesCount", "Max Slices Count", ["24"], "Max number of FFV1 slices"),
    ("ErrorDetectionType", "Error Detection", ["Per slice"], "Error detection type"),
)

_AUDIO_FIELDS = (
    ("Format", "Format", ["FLAC", "PCM"], "Audio codec(s) — add multiple for alternatives"),
    ("Channels", "Channels", ["2"], "Number of audio channels"),
    ("SamplingRate", "Sample Rate", ["48000"], "Audio sample rate in Hz"),
    ("BitDepth", "Bit Depth", ["24"], "Audio bit depth"),
    ("Compression_Mode", "Compression", ["Lossless"], "Lossless or Lossy"),
)


class CustomMediainfoDialog(profile_common.SectionedProfileDialog):
    """Create or edit a custom MediaInfo profile.

    Three sections — General, Video, Audio — matching MediainfoProfile.
    """

    TOOL_LABEL = "MediaInfo"
    NEW_DESCRIPTION = (
        "Define expected MediaInfo values for file validation. "
        "Fields are organized by General, Video, and Audio sections."
    )
    NAME_PLACEHOLDER = "e.g., Custom MKV FFV1 Profile"
    MIN_SIZE = (750, 850)

    SECTIONS = (
        ProfileSection('general', 'General', _GENERAL_FIELDS),
        ProfileSection('video', 'Video', _VIDEO_FIELDS),
        ProfileSection('audio', 'Audio', _AUDIO_FIELDS),
    )

    # Fields with constrained value sets get editable combo boxes.
    # Values sourced from MediaInfoLib (Mpegv_* tables, Fill() calls).
    # Combo boxes remain editable so users can type custom values if needed.
    DROPDOWN_OPTIONS = {
        "ScanType": ["Interlaced", "Progressive", "MBAFF"],
        "ScanOrder": ["TFF", "BFF", "Top Field First", "Bottom Field First"],
        "Compression_Mode": ["Lossless", "Lossy"],
        "ChromaSubsampling": ["4:2:2", "4:2:0", "4:4:4", "4:1:1", "4:4:4:4"],
        "Standard": ["NTSC", "PAL"],
        "FrameRate_Mode_String": ["Constant", "Variable"],
        "OverallBitRate_Mode": ["VBR", "CBR"],
        # Video BitDepth and Audio BitDepth share the same field name,
        # so we use a single entry covering both common sets.
        "BitDepth": ["8", "10", "12", "16", "24", "32"],
    }

    # -- import / compare ---------------------------------------------------

    IMPORT_LABEL = "MediaInfo"
    IMPORT_CAPTION = "Select MediaInfo JSON File"
    COMPARE_CAPTION = "Select MediaInfo JSON File to Compare"
    IMPORT_FILTER = "MediaInfo Files (*.json);;All Files (*.*)"
    IMPORT_HINT = "Please check the file is a valid MediaInfo JSON output."
    SECTION_LABELS = {
        'general': 'GENERAL',
        'video': 'VIDEO',
        'audio': 'AUDIO',
    }

    def import_profile_from_path(self, file_path):
        return mediainfo_import.import_mediainfo_file_to_profile(file_path)

    def validate_path_against_profile(self, file_path, profile):
        return mediainfo_import.validate_file_against_profile(file_path, profile)

    # -- profile assembly ---------------------------------------------------

    def sections_from_profile(self, profile_data):
        """Split a MediainfoProfile — or the dicts spex_config stores — into sections.

        spex_config.mediainfo_values keys its sections 'expected_general' and
        friends, while a profile uses the bare names.
        """
        if hasattr(profile_data, '__dataclass_fields__'):
            return {
                'general': asdict(profile_data.general),
                'video': asdict(profile_data.video),
                'audio': asdict(profile_data.audio),
            }

        if not isinstance(profile_data, dict):
            return {}

        prefix = 'expected_' if 'expected_general' in profile_data else ''
        sections = {}
        for key in ('general', 'video', 'audio'):
            value = profile_data.get(f'{prefix}{key}', {})
            sections[key] = asdict(value) if hasattr(value, '__dataclass_fields__') else value
        return sections

    def build_profile(self, values):
        """Assemble a MediainfoProfile, warning and returning None if invalid."""
        general_data = values['general']
        video_data = values['video']
        audio_data = dict(values['audio'])

        for section_label, data, required in (
            ("General", general_data, ("FileExtension", "Format")),
            ("Video", video_data, ("Format",)),
        ):
            for field_name in required:
                value = data.get(field_name)
                if not value or (isinstance(value, list) and not value):
                    QMessageBox.warning(
                        self, "Validation Error",
                        f"{section_label} > {field_name} is required."
                    )
                    return None

        # MediainfoAudioValues.Format is typed as a list even for a single codec.
        audio_format = audio_data.get("Format", "")
        if isinstance(audio_format, str) and audio_format:
            audio_data["Format"] = [audio_format]
        elif not audio_format:
            audio_data["Format"] = []

        try:
            return MediainfoProfile(
                general=MediainfoGeneralValues(**general_data),
                video=MediainfoVideoValues(**video_data),
                audio=MediainfoAudioValues(**audio_data),
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create profile:\n{str(e)}")
            return None

    def preview_line(self, profile_name, values):
        container = values['general'].get("Format") or "N/A"
        codec = values['video'].get("Format") or "N/A"
        width = values['video'].get("Width") or "N/A"
        height = values['video'].get("Height") or "N/A"
        return f"{profile_name}: {container} / {codec} {width}x{height}"

    # -- kept for callers that used the domain-named getter -----------------

    def get_mediainfo_profile(self):
        return self.build_profile_from_form()
