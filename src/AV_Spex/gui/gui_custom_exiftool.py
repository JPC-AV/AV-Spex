from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox,
    QScrollArea, QGridLayout,
)
from PyQt6.QtCore import Qt

from AV_Spex.utils.config_setup import ExiftoolProfile
from AV_Spex.gui import gui_custom_profile_common as profile_common
from AV_Spex.gui.gui_custom_profile_common import ProfileSection
from AV_Spex.utils import exiftool_import


# Each tuple: (field_name, label_text, default_values, tooltip)

_EXIFTOOL_FIELDS = (
    # File Information
    ("FileType", "File Type", ["MKV"], "File format (e.g., MKV, MOV, MP4)"),
    ("FileTypeExtension", "File Extension", ["mkv"], "File extension without dot"),
    ("MIMEType", "MIME Type", ["video/x-matroska"], "MIME type of the file"),

    # Video Properties
    ("VideoFrameRate", "Frame Rate", ["29.97"], "Video frame rate in fps"),
    ("ImageWidth", "Width", ["720"], "Video width in pixels"),
    ("ImageHeight", "Height", ["486"], "Video height in pixels"),
    ("VideoScanType", "Scan Type", ["Interlaced"], "Progressive or Interlaced"),

    # Display Properties
    ("DisplayWidth", "Display Width", ["400"], "Display width"),
    ("DisplayHeight", "Display Height", ["297"], "Display height"),
    ("DisplayUnit", "Display Unit", ["Display Aspect Ratio"], "Unit for display dimensions"),

    # Audio Properties
    ("AudioChannels", "Audio Channels", ["2"], "Number of audio channels"),
    ("AudioSampleRate", "Sample Rate", ["48000"], "Audio sample rate in Hz"),
    ("AudioBitsPerSample", "Bits per Sample", ["24"], "Audio bit depth"),

    # Codec IDs
    ("CodecID", "Codec IDs", ["A_FLAC", "A_PCM/INT/LIT"], "List of accepted audio codec IDs"),
)


class CustomExiftoolDialog(profile_common.SectionedProfileDialog):
    """Create or edit a custom ExifTool profile.

    The ExifTool profile is flat, so this is the single-section case: one
    untabbed field area rather than a tab per section.
    """

    TOOL_LABEL = "Exiftool"
    NEW_DESCRIPTION = (
        "Define expected Exiftool values for file validation. "
        "Each field can have multiple values."
    )
    NAME_PLACEHOLDER = "e.g., Custom HD Profile"
    MIN_SIZE = (700, 800)

    SECTIONS = (ProfileSection('root', None, _EXIFTOOL_FIELDS),)

    # ExifTool's inputs are left unstyled so macOS renders them natively; the
    # sectioned dialogs style theirs to keep combo boxes and line edits matched.
    STYLED_FIELDS = False

    def description_text(self, edit_mode, profile_name):
        if edit_mode:
            return f"Edit the exiftool profile: {profile_name}"
        return self.NEW_DESCRIPTION

    # -- layout -------------------------------------------------------------

    def _create_section_area(self, section):
        """One row per field: label, stacked inputs, then the +/- buttons.

        Overridden rather than inherited because this dialog lays its fields out
        as full-width rows instead of the four-column grid the tabbed dialogs
        use.
        """
        self.section_inputs[section.key] = {}
        self.section_containers[section.key] = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_widget.setAutoFillBackground(False)
        scroll_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        scroll_widget.setStyleSheet("QWidget { background-color: transparent; }")
        fields_layout = QGridLayout(scroll_widget)
        fields_layout.setSpacing(5)
        fields_layout.setContentsMargins(5, 5, 5, 5)
        scroll.setWidget(scroll_widget)
        scroll.setMinimumHeight(500)

        for row, (field_name, label_text, default_values, tooltip) in enumerate(section.fields):
            field_layout = QHBoxLayout()
            field_layout.setContentsMargins(0, 0, 0, 0)
            field_layout.setSpacing(5)

            label = QLabel(f"{label_text}:")
            label.setToolTip(tooltip)
            label.setMinimumWidth(120)
            field_layout.addWidget(label)

            inputs_layout = QVBoxLayout()
            inputs_layout.setContentsMargins(0, 0, 0, 0)
            inputs_layout.setSpacing(5)
            # Only the first default is pre-filled here; the extra CodecID
            # values are added as further rows below, matching the tabbed
            # dialogs' behavior.
            self._register_field(section.key, field_name, inputs_layout, default_values[:1])
            field_layout.addLayout(inputs_layout, 1)

            add_btn, remove_btn = self._row_buttons(section.key, field_name, label_text)
            field_layout.addWidget(add_btn)
            field_layout.addWidget(remove_btn)

            field_widget = QWidget()
            field_widget.setLayout(field_layout)
            field_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            field_widget.setAutoFillBackground(False)
            fields_layout.addWidget(field_widget, row, 0, 1, 2)

        return scroll

    # -- import / compare ---------------------------------------------------

    IMPORT_LABEL = "exiftool"
    IMPORT_CAPTION = "Select Exiftool Output File"
    COMPARE_CAPTION = "Select Exiftool Output File to Compare"
    IMPORT_FILTER = "Exiftool Files (*.json *.txt *.log);;All Files (*.*)"
    IMPORT_HINT = "Please check the file is a valid exiftool output."
    # A flat profile has one unnamed section, so the comparison view shows no
    # section headings.
    SECTION_LABELS = {'root': None}

    def import_profile_from_path(self, file_path):
        return exiftool_import.import_exiftool_file_to_profile(file_path)

    def validate_path_against_profile(self, file_path, profile):
        return exiftool_import.validate_file_against_profile(file_path, profile)

    def _comparison_detail_lines(self, validation):
        """Render a flat comparison result.

        exiftool_import returns matches/mismatches/missing at the top level
        rather than under 'sections', because the profile has no sections.
        """
        if 'error' in validation:
            return [f"Error: {validation['error']}"]

        details = []
        if validation.get('matches'):
            details.append("✅ MATCHING FIELDS:")
            for field, values in validation['matches'].items():
                details.append(f"  {field}: {values['actual']}")
            details.append("")
        if validation.get('mismatches'):
            details.append("❌ MISMATCHED FIELDS:")
            for field, values in validation['mismatches'].items():
                details.append(f"  {field}:")
                details.append(f"    Expected: {values['expected']}")
                details.append(f"    Actual: {values['actual']}")
            details.append("")
        if validation.get('missing'):
            details.append("⚠️ MISSING FIELDS:")
            for field, values in validation['missing'].items():
                details.append(f"  {field}: Expected {values['expected']}")
        return details

    @staticmethod
    def _validation_has_differences(validation):
        return bool(validation.get('mismatches') or validation.get('missing'))

    # -- profile assembly ---------------------------------------------------

    def sections_from_profile(self, profile_data):
        """A flat profile is one section; read its fields straight off."""
        if isinstance(profile_data, dict):
            return {'root': profile_data}
        return {
            'root': {
                field_name: getattr(profile_data, field_name)
                for field_name, *_ in _EXIFTOOL_FIELDS
                if hasattr(profile_data, field_name)
            }
        }

    def build_profile(self, values):
        """Assemble an ExiftoolProfile, warning and returning None if invalid."""
        profile_data = values['root']

        for field_name in ("FileType", "FileTypeExtension", "MIMEType"):
            value = profile_data.get(field_name)
            if not value or (isinstance(value, list) and not value):
                QMessageBox.warning(self, "Validation Error", f"{field_name} is required.")
                return None

        try:
            return ExiftoolProfile(**profile_data)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to create profile:\n{str(e)}")
            return None

    def preview_line(self, profile_name, values):
        root = values['root']
        file_type = root.get("FileType") or "N/A"
        width = root.get("ImageWidth") or "N/A"
        height = root.get("ImageHeight") or "N/A"
        return f"{profile_name}: {file_type} {width}x{height}"

    # -- kept for callers that used the domain-named getter -----------------

    def get_exiftool_profile(self):
        return self.build_profile_from_form()
