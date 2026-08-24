from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel, 
    QScrollArea, QPushButton, QComboBox, 
    QMessageBox, QDialog, QGridLayout, QListWidget,
    QFileDialog, QInputDialog, QTextEdit, QTabWidget
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette

import os
import tempfile

from AV_Spex.utils import config_edit
from AV_Spex.utils.config_setup import (
    FfprobeProfile, FFmpegVideoStream,
    FFmpegAudioStream, FFmpegFormat
)
from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.gui import gui_custom_profile_common as profile_common
from AV_Spex.utils import ffprobe_import


class CustomFfprobeDialog(QDialog, ThemeableMixin,
                          profile_common.SectionedProfileImportMixin):
    """
    Dialog for creating and editing custom FFprobe profiles.
    
    Mirrors CustomMediainfoDialog but uses a QTabWidget with
    Video Stream / Audio Stream / Format tabs to organize the
    three-section structure. Each section has its own set of field
    inputs with +/- buttons for multi-value fields.
    """
    
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
    
    # Codec name suggestions per section — keyed by (section, field_name).
    # These are kept separate from DROPDOWN_OPTIONS because the same
    # field name "codec_name" appears in both video and audio tabs with
    # different value sets.
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
    
    @classmethod
    def _get_arrow_svg_path(cls):
        return profile_common.dropdown_arrow_svg_path()
    
    def __init__(self, parent=None, edit_mode=False, profile_name=None):
        super().__init__(parent)
        self.profile = None
        self.edit_mode = edit_mode
        self.original_profile_name = profile_name
        
        if edit_mode:
            self.setWindowTitle(f"Edit FFprobe Profile: {profile_name}")
        else:
            self.setWindowTitle("Custom FFprobe Profile")
        
        self.setModal(True)
        
        # Add theme handling
        self.setup_theme_handling()
        
        # Set minimum size for the dialog
        self.setMinimumSize(750, 850)
        
        # Initialize layout
        layout = QVBoxLayout()
        layout.setSpacing(10)
        
        # Add description
        if edit_mode:
            description = QLabel(f"Edit the FFprobe profile: {profile_name}")
        else:
            description = QLabel(
                "Define expected FFprobe values for file validation. "
                "Fields are organized by Video Stream, Audio Stream, and Format sections."
            )
        description.setWordWrap(True)
        layout.addWidget(description)
        
        # Profile name input
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Profile Name:"))
        self.profile_name_input = QLineEdit()
        self.profile_name_input.setPlaceholderText("e.g., Custom MKV FFV1 FFprobe Profile")
        if edit_mode:
            self.profile_name_input.setText(profile_name)
            self.profile_name_input.setEnabled(False)
        name_layout.addWidget(self.profile_name_input)
        layout.addLayout(name_layout)
        
        # Import section
        import_layout = QHBoxLayout()
        import_button = QPushButton("Import from File...")
        import_button.clicked.connect(self.import_from_file)
        compare_button = QPushButton("Compare with File...")
        compare_button.clicked.connect(self.compare_with_file)
        import_layout.addWidget(import_button)
        import_layout.addWidget(compare_button)
        layout.addLayout(import_layout)
        
        # Tabbed section for Video Stream / Audio Stream / Format
        self.section_tabs = QTabWidget()
        
        # Per-section field storage
        self.video_inputs = {}
        self.video_containers = {}
        self.audio_inputs = {}
        self.audio_containers = {}
        self.format_inputs = {}
        self.format_containers = {}
        
        # Video Stream tab
        video_widget = self._create_section_tab(
            'video_stream', self.video_inputs, self.video_containers,
            self._get_video_stream_fields()
        )
        self.section_tabs.addTab(video_widget, "Video Stream")
        
        # Audio Stream tab
        audio_widget = self._create_section_tab(
            'audio_stream', self.audio_inputs, self.audio_containers,
            self._get_audio_stream_fields()
        )
        self.section_tabs.addTab(audio_widget, "Audio Stream")
        
        # Format tab
        format_widget = self._create_section_tab(
            'format', self.format_inputs, self.format_containers,
            self._get_format_fields()
        )
        self.section_tabs.addTab(format_widget, "Format")
        
        layout.addWidget(self.section_tabs)
        
        # Preview section
        preview_layout = QVBoxLayout()
        preview_layout.addWidget(QLabel("Profile Preview:"))
        self.preview_text = QLineEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)
        
        # Dialog buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save Profile")
        save_button.clicked.connect(self.on_save_clicked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        
        # Add all to main layout
        layout.addLayout(preview_layout)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        self.update_preview()
    
    # ── Field definitions ──────────────────────────────────────────────
    # Each tuple: (field_name, label_text, default_values, tooltip)
    
    @staticmethod
    def _get_video_stream_fields():
        return [
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
        ]
    
    @staticmethod
    def _get_audio_stream_fields():
        return [
            ("codec_name", "Codec Name", ["flac", "pcm_s24le"], "Audio codec(s) — add multiple for alternatives"),
            ("codec_long_name", "Codec Long Name", ["FLAC (Free Lossless Audio Codec)", "PCM signed 24-bit little-endian"], "Full codec name(s)"),
            ("codec_type", "Codec Type", ["audio"], "Stream type"),
            ("codec_tag", "Codec Tag", ["0x0000"], "Hex codec tag value"),
            ("sample_fmt", "Sample Format", ["s32"], "Audio sample format"),
            ("sample_rate", "Sample Rate", ["48000"], "Audio sample rate in Hz"),
            ("channels", "Channels", ["2"], "Number of audio channels"),
            ("channel_layout", "Channel Layout", ["stereo"], "Channel layout"),
            ("bits_per_raw_sample", "Bits Per Raw Sample", ["24"], "Audio bit depth"),
        ]
    
    @staticmethod
    def _get_format_fields():
        return [
            ("format_name", "Format Name", ["matroska webm"], "Container format short name"),
            ("format_long_name", "Format Long Name", ["Matroska / WebM"], "Container format full name"),
        ]
    
    # ── Tab creation ───────────────────────────────────────────────────
    
    def _get_field_colors(self):
        return profile_common.field_colors()

    def _field_lineedit_style(self, colors=None):
        return profile_common.field_lineedit_style(colors)

    def _field_combobox_style(self, colors=None):
        return profile_common.field_combobox_style(colors)

    def _create_input_widget(self, field_name, value="", section_name=None):
        """
        Create the appropriate input widget for a field.
        
        Fields listed in DROPDOWN_OPTIONS get an editable QComboBox.
        codec_name fields get section-specific options from
        CODEC_NAME_OPTIONS.  All others get a plain QLineEdit.
        """
        colors = self._get_field_colors()
        
        # Determine dropdown options: global lookup first, then
        # section-specific codec_name suggestions.
        options = self.DROPDOWN_OPTIONS.get(field_name)
        if options is None and field_name == "codec_name" and section_name:
            options = self.CODEC_NAME_OPTIONS.get(section_name)
        
        if options is not None:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")  # blank first item
            combo.addItems(options)
            combo.setCurrentText(str(value))
            combo.lineEdit().setPlaceholderText(f"Select or enter {field_name}...")
            combo.currentTextChanged.connect(self.update_preview)
            combo.setStyleSheet(self._field_combobox_style(colors))
            combo.lineEdit().setStyleSheet("background: transparent;")
            combo.setProperty("field_input", True)
            return combo
        else:
            line_edit = QLineEdit()
            line_edit.setText(str(value))
            line_edit.setPlaceholderText(f"Enter {field_name} value...")
            line_edit.textChanged.connect(self.update_preview)
            line_edit.setStyleSheet(self._field_lineedit_style(colors))
            line_edit.setProperty("field_input", True)
            return line_edit

    def _create_section_tab(self, section_name, field_inputs, field_containers, fields):
        """
        Create a scrollable tab widget for one FFprobe section.
        
        Uses the same +/- multi-value pattern as CustomMediainfoDialog.
        """
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_widget.setAutoFillBackground(False)
        scroll_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        grid_layout = QGridLayout(scroll_widget)
        grid_layout.setSpacing(5)
        grid_layout.setContentsMargins(5, 5, 5, 5)
        # Column 0 = labels (fixed), column 1 = inputs (stretch)
        grid_layout.setColumnStretch(0, 0)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.setColumnStretch(2, 0)
        grid_layout.setColumnStretch(3, 0)
        scroll.setWidget(scroll_widget)
        
        row = 0
        for field_name, label_text, default_values, tooltip in fields:
            # Column 0: Label
            label = QLabel(f"{label_text}:")
            label.setToolTip(tooltip)
            label.setMinimumWidth(170)
            grid_layout.addWidget(label, row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            
            # Column 1: Vertical layout for multiple input widgets
            inputs_container = QWidget()
            inputs_container.setAutoFillBackground(False)
            inputs_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            inputs_layout = QVBoxLayout(inputs_container)
            inputs_layout.setContentsMargins(0, 0, 0, 0)
            inputs_layout.setSpacing(5)
            
            field_inputs[field_name] = []
            field_containers[field_name] = inputs_layout
            
            # Add first widget with default value
            first_value = default_values[0] if default_values else ""
            widget = self._create_input_widget(field_name, first_value, section_name)
            inputs_layout.addWidget(widget)
            field_inputs[field_name].append(widget)
            
            # For fields with multiple defaults (e.g., Audio codec_name), add them
            for extra_value in default_values[1:]:
                extra_widget = self._create_input_widget(field_name, extra_value, section_name)
                inputs_layout.addWidget(extra_widget)
                field_inputs[field_name].append(extra_widget)
            
            grid_layout.addWidget(inputs_container, row, 1)
            
            # Column 2: + button
            add_btn = QPushButton("+")
            add_btn.setMaximumWidth(30)
            add_btn.setMaximumHeight(25)
            add_btn.setToolTip(f"Add {label_text}")
            add_btn.setStyleSheet("QPushButton { background-color: transparent; }")
            add_btn.clicked.connect(
                lambda checked, fn=field_name, fi=field_inputs, fc=field_containers, sn=section_name: 
                    self.add_textbox_row(fn, fi, fc, "", sn)
            )
            grid_layout.addWidget(add_btn, row, 2, Qt.AlignmentFlag.AlignTop)
            
            # Column 3: - button
            remove_btn = QPushButton("-")
            remove_btn.setMaximumWidth(30)
            remove_btn.setMaximumHeight(25)
            remove_btn.setToolTip(f"Remove last {label_text}")
            remove_btn.setStyleSheet("QPushButton { background-color: transparent; }")
            remove_btn.clicked.connect(
                lambda checked, fn=field_name, fi=field_inputs: 
                    self.remove_textbox_row(fn, fi)
            )
            grid_layout.addWidget(remove_btn, row, 3, Qt.AlignmentFlag.AlignTop)
            
            row += 1
        
        return scroll
    
    # ── Row add/remove ─────────────────────────────────────────────────
    
    def add_textbox_row(self, field_name, field_inputs, field_containers, value="", section_name=None):
        """Add a new input widget row for a field (combo box or line edit)"""
        container_layout = field_containers[field_name]
        
        widget = self._create_input_widget(field_name, value, section_name)
        container_layout.addWidget(widget)
        field_inputs[field_name].append(widget)
        
        if hasattr(self, 'preview_text'):
            self.update_preview()

    def remove_textbox_row(self, field_name, field_inputs):
        """Remove the last input widget row for a field"""
        if len(field_inputs[field_name]) > 1:
            widget = field_inputs[field_name].pop()
            widget.deleteLater()
            if hasattr(self, 'preview_text'):
                self.update_preview()
    
    # ── Import / Compare ───────────────────────────────────────────────
    
    # -- SectionedProfileImportMixin configuration --------------------------

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

    def build_profile_from_form(self):
        return self.get_ffprobe_profile()
    
    # ── Load / collect data ────────────────────────────────────────────
    
    def load_profile_data(self, profile_data):
        """
        Load profile data into all three sections' form fields.
        
        Handles both FfprobeProfile dataclass instances and plain dicts
        (from spex_config.ffmpeg_values which stores raw dicts).
        """
        from dataclasses import asdict
        
        # Determine how to get section data depending on input type
        if hasattr(profile_data, '__dataclass_fields__'):
            # It's a FfprobeProfile dataclass
            video_dict = asdict(profile_data.video_stream) if hasattr(profile_data, 'video_stream') else {}
            audio_dict = asdict(profile_data.audio_stream) if hasattr(profile_data, 'audio_stream') else {}
            format_dict = asdict(profile_data.format) if hasattr(profile_data, 'format') else {}
        elif isinstance(profile_data, dict):
            # Could be {'video_stream': {...}, 'audio_stream': {...}, 'format': {...}}
            video_dict = profile_data.get('video_stream', {})
            audio_dict = profile_data.get('audio_stream', {})
            format_dict = profile_data.get('format', {})
            # Convert any nested dataclasses to dicts
            if hasattr(video_dict, '__dataclass_fields__'):
                video_dict = asdict(video_dict)
            if hasattr(audio_dict, '__dataclass_fields__'):
                audio_dict = asdict(audio_dict)
            if hasattr(format_dict, '__dataclass_fields__'):
                format_dict = asdict(format_dict)
        else:
            video_dict = {}
            audio_dict = {}
            format_dict = {}
        
        # Load into each section's fields
        self._load_section_data(self.video_inputs, self.video_containers, video_dict, 'video_stream')
        self._load_section_data(self.audio_inputs, self.audio_containers, audio_dict, 'audio_stream')
        self._load_section_data(self.format_inputs, self.format_containers, format_dict, 'format')
        
        if hasattr(self, 'preview_text'):
            self.update_preview()
    
    def _load_section_data(self, field_inputs, field_containers, data_dict, section_name=None):
        """
        Load data into one section's field inputs.
        
        Clears existing inputs and creates new ones based on values.
        """
        for field_name, widgets in field_inputs.items():
            container_layout = field_containers[field_name]
            
            # Remove all existing widgets
            while container_layout.count():
                item = container_layout.takeAt(0)
                if item.widget():
                    item.widget().setParent(None)
                    item.widget().deleteLater()
            
            field_inputs[field_name].clear()
            
            # Get value from data
            if field_name in data_dict:
                value = data_dict[field_name]
                
                if isinstance(value, list):
                    values = value
                elif value is not None and str(value) != '':
                    values = [value]
                else:
                    values = [""]
                
                for val in values:
                    widget = self._create_input_widget(field_name, str(val), section_name)
                    container_layout.addWidget(widget)
                    field_inputs[field_name].append(widget)
            else:
                # Field not in data, add one empty widget
                widget = self._create_input_widget(field_name, "", section_name)
                container_layout.addWidget(widget)
                field_inputs[field_name].append(widget)
    
    def _collect_section_values(self, field_inputs):
        """
        Collect values from one section's field inputs.
        
        Handles both QLineEdit (.text()) and QComboBox (.currentText()).
        Multi-value fields become lists; single-value fields become strings;
        empty fields become "".
        """
        section_data = {}
        
        for field_name, widgets in field_inputs.items():
            values = []
            for widget in widgets:
                if isinstance(widget, QComboBox):
                    text = widget.currentText().strip()
                else:
                    text = widget.text().strip()
                if text:
                    values.append(text)
            
            if len(values) > 1:
                section_data[field_name] = values
            elif len(values) == 1:
                section_data[field_name] = values[0]
            else:
                section_data[field_name] = ""
        
        return section_data
    
    # ── Preview ────────────────────────────────────────────────────────
    
    def _get_widget_text(self, widget):
        """Get text from either a QLineEdit or QComboBox."""
        if isinstance(widget, QComboBox):
            return widget.currentText()
        return widget.text()
    
    def update_preview(self):
        """Update the profile preview"""
        profile_name = self.profile_name_input.text() or "Unnamed Profile"
        
        # Get representative values for preview
        codec_val = "N/A"
        if "codec_name" in self.video_inputs and self.video_inputs["codec_name"]:
            text = self._get_widget_text(self.video_inputs["codec_name"][0])
            if text:
                codec_val = text
        
        width_val = "N/A"
        if "width" in self.video_inputs and self.video_inputs["width"]:
            text = self._get_widget_text(self.video_inputs["width"][0])
            if text:
                width_val = text
        
        height_val = "N/A"
        if "height" in self.video_inputs and self.video_inputs["height"]:
            text = self._get_widget_text(self.video_inputs["height"][0])
            if text:
                height_val = text
        
        format_val = "N/A"
        if "format_name" in self.format_inputs and self.format_inputs["format_name"]:
            text = self._get_widget_text(self.format_inputs["format_name"][0])
            if text:
                format_val = text
        
        preview = f"{profile_name}: {format_val} / {codec_val} {width_val}x{height_val}"
        self.preview_text.setText(preview)
    
    # ── Profile construction ───────────────────────────────────────────
    
    def get_ffprobe_profile(self):
        """
        Get the FFprobe profile as a FfprobeProfile dataclass.
        
        Returns None and shows a warning if validation fails.
        """
        if not self.profile_name_input.text():
            QMessageBox.warning(self, "Validation Error", "Profile name is required.")
            return None
        
        video_data = self._collect_section_values(self.video_inputs)
        audio_data = self._collect_section_values(self.audio_inputs)
        format_data = self._collect_section_values(self.format_inputs)
        
        # Validate required Video Stream fields
        required_video = ["codec_name"]
        for field_name in required_video:
            value = video_data.get(field_name)
            if not value or (isinstance(value, list) and not value):
                QMessageBox.warning(
                    self, "Validation Error",
                    f"Video Stream > {field_name} is required."
                )
                return None
        
        # Validate required Format fields
        required_format = ["format_name"]
        for field_name in required_format:
            value = format_data.get(field_name)
            if not value or (isinstance(value, list) and not value):
                QMessageBox.warning(
                    self, "Validation Error",
                    f"Format > {field_name} is required."
                )
                return None
        
        # Ensure Audio list fields are always lists
        for list_field in ('codec_name', 'codec_long_name'):
            audio_value = audio_data.get(list_field, "")
            if isinstance(audio_value, str) and audio_value:
                audio_data[list_field] = [audio_value]
            elif not audio_value:
                audio_data[list_field] = []
        
        # Add defaults for missing fields
        from AV_Spex.utils.config_setup import EncoderSettings
        
        # Fill in missing video fields
        for f in FFmpegVideoStream.__dataclass_fields__:
            if f not in video_data:
                video_data[f] = ""
        
        # Fill in missing audio fields
        for f in FFmpegAudioStream.__dataclass_fields__:
            if f not in audio_data:
                if f in ('codec_name', 'codec_long_name'):
                    audio_data[f] = []
                else:
                    audio_data[f] = ""
        
        # Fill in missing format fields and add tags
        for f in FFmpegFormat.__dataclass_fields__:
            if f not in format_data:
                if f == 'tags':
                    format_data['tags'] = {
                        'creation_time': None,
                        'ENCODER': None,
                        'TITLE': None,
                        'ENCODER_SETTINGS': None,
                        'DESCRIPTION': None,
                        'ORIGINAL MEDIA TYPE': None,
                        'ENCODED_BY': None
                    }
                else:
                    format_data[f] = ""
        
        # Ensure tags exists
        if 'tags' not in format_data:
            format_data['tags'] = {
                'creation_time': None,
                'ENCODER': None,
                'TITLE': None,
                'ENCODER_SETTINGS': None,
                'DESCRIPTION': None,
                'ORIGINAL MEDIA TYPE': None,
                'ENCODED_BY': None
            }
        
        try:
            video_stream = FFmpegVideoStream(**video_data)
            audio_stream = FFmpegAudioStream(**audio_data)
            ffmpeg_format = FFmpegFormat(**format_data)
            return FfprobeProfile(
                video_stream=video_stream,
                audio_stream=audio_stream,
                format=ffmpeg_format
            )
        except Exception as e:
            QMessageBox.critical(
                self, "Error",
                f"Failed to create profile:\n{str(e)}"
            )
            return None
    
    # ── Save handling ──────────────────────────────────────────────────
    
    def on_save_clicked(self):
        """Handle save button click"""
        profile = self.get_ffprobe_profile()
        if profile:
            try:
                profile_name = (
                    self.original_profile_name if self.edit_mode 
                    else self.profile_name_input.text()
                )
                self.profile = {
                    'name': profile_name,
                    'data': profile,
                    'is_edit': self.edit_mode
                }
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save profile: {str(e)}")
                
    def get_profile(self):
        """Return the stored profile"""
        return self.profile
        
    def load_existing_profile(self, profile_name, profile_data):
        """Load an existing profile into the dialog"""
        self.profile_name_input.setText(profile_name)
        self.load_profile_data(profile_data)
    
    # ── Theme handling ─────────────────────────────────────────────────
    
    def on_theme_changed(self, palette):
        """Apply theme changes to this dialog"""
        self.setPalette(palette)
        colors = self._get_field_colors()
        combo_style = self._field_combobox_style(colors)
        line_style = self._field_lineedit_style(colors)
        for combo in self.findChildren(QComboBox):
            if combo.property("field_input"):
                combo.setStyleSheet(combo_style)
                if combo.isEditable() and combo.lineEdit():
                    combo.lineEdit().setStyleSheet("background: transparent;")
        for line_edit in self.findChildren(QLineEdit):
            if line_edit.property("field_input"):
                line_edit.setStyleSheet(line_style)
        
    def closeEvent(self, event):
        """Clean up theme connections before closing"""
        self.cleanup_theme_handling()
        super().closeEvent(event)