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
    MediainfoProfile, MediainfoGeneralValues,
    MediainfoVideoValues, MediainfoAudioValues
)
from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.gui import gui_custom_profile_common as profile_common
from AV_Spex.utils import mediainfo_import


class CustomMediainfoDialog(QDialog, ThemeableMixin,
                            profile_common.SectionedProfileImportMixin):
    """
    Dialog for creating and editing custom MediaInfo profiles.
    
    Mirrors CustomExiftoolDialog but uses a QTabWidget with General/Video/Audio
    tabs to organize the three-section structure. Each section has its own set
    of field inputs with +/- buttons for multi-value fields.
    """
    
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
    
    @classmethod
    def _get_arrow_svg_path(cls):
        return profile_common.dropdown_arrow_svg_path()
    
    def __init__(self, parent=None, edit_mode=False, profile_name=None):
        super().__init__(parent)
        self.profile = None
        self.edit_mode = edit_mode
        self.original_profile_name = profile_name
        
        if edit_mode:
            self.setWindowTitle(f"Edit MediaInfo Profile: {profile_name}")
        else:
            self.setWindowTitle("Custom MediaInfo Profile")
        
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
            description = QLabel(f"Edit the MediaInfo profile: {profile_name}")
        else:
            description = QLabel(
                "Define expected MediaInfo values for file validation. "
                "Fields are organized by General, Video, and Audio sections."
            )
        description.setWordWrap(True)
        layout.addWidget(description)
        
        # Profile name input
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Profile Name:"))
        self.profile_name_input = QLineEdit()
        self.profile_name_input.setPlaceholderText("e.g., Custom MKV FFV1 Profile")
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
        
        # Tabbed section for General / Video / Audio
        self.section_tabs = QTabWidget()
        
        # Per-section field storage (mirrors field_inputs / field_containers in exiftool)
        self.general_inputs = {}
        self.general_containers = {}
        self.video_inputs = {}
        self.video_containers = {}
        self.audio_inputs = {}
        self.audio_containers = {}
        
        # General tab
        general_widget = self._create_section_tab(
            'general', self.general_inputs, self.general_containers,
            self._get_general_fields()
        )
        self.section_tabs.addTab(general_widget, "General")
        
        # Video tab
        video_widget = self._create_section_tab(
            'video', self.video_inputs, self.video_containers,
            self._get_video_fields()
        )
        self.section_tabs.addTab(video_widget, "Video")
        
        # Audio tab
        audio_widget = self._create_section_tab(
            'audio', self.audio_inputs, self.audio_containers,
            self._get_audio_fields()
        )
        self.section_tabs.addTab(audio_widget, "Audio")
        
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
    def _get_general_fields():
        return [
            ("FileExtension", "File Extension", ["mkv"], "File extension (e.g., mkv, mov, avi)"),
            ("Format", "Format", ["Matroska"], "Container format (e.g., Matroska, MPEG-4, AVI)"),
            ("OverallBitRate_Mode", "Bitrate Mode", ["VBR"], "Overall bitrate mode (VBR or CBR)"),
        ]
    
    @staticmethod
    def _get_video_fields():
        return [
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
        ]
    
    @staticmethod
    def _get_audio_fields():
        return [
            ("Format", "Format", ["FLAC", "PCM"], "Audio codec(s) — add multiple for alternatives"),
            ("Channels", "Channels", ["2"], "Number of audio channels"),
            ("SamplingRate", "Sample Rate", ["48000"], "Audio sample rate in Hz"),
            ("BitDepth", "Bit Depth", ["24"], "Audio bit depth"),
            ("Compression_Mode", "Compression", ["Lossless"], "Lossless or Lossy"),
        ]
    
    # ── Tab creation ───────────────────────────────────────────────────
    
    def _get_field_colors(self):
        return profile_common.field_colors()

    def _field_lineedit_style(self, colors=None):
        return profile_common.field_lineedit_style(colors)

    def _field_combobox_style(self, colors=None):
        return profile_common.field_combobox_style(colors)

    def _create_input_widget(self, field_name, value=""):
        """
        Create the appropriate input widget for a field.
        
        Fields listed in DROPDOWN_OPTIONS get an editable QComboBox
        pre-populated with known values.  All other fields get a plain
        QLineEdit.  Both widget types expose a compatible text()/setText()
        interface (QComboBox via currentText/setCurrentText and its
        lineEdit()).
        
        Returns:
            QComboBox or QLineEdit
        """
        colors = self._get_field_colors()
        
        if field_name in self.DROPDOWN_OPTIONS:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")  # blank first item so empty is easy
            combo.addItems(self.DROPDOWN_OPTIONS[field_name])
            combo.setCurrentText(str(value))
            combo.lineEdit().setPlaceholderText(f"Select or enter {field_name}...")
            combo.currentTextChanged.connect(self.update_preview)
            # Style the QComboBox frame AND its internal QLineEdit to
            # the same explicit Base color.  This overrides macOS native
            # rendering for both layers so there is no visible seam
            # between the combo frame and the edit area.  The complete
            # ::drop-down block ensures Qt draws the dropdown arrow in
            # stylesheet mode.
            combo.setStyleSheet(self._field_combobox_style(colors))
            combo.lineEdit().setStyleSheet("background: transparent;")
            combo.setProperty("field_input", True)
            return combo
        else:
            line_edit = QLineEdit()
            line_edit.setText(str(value))
            line_edit.setPlaceholderText(f"Enter {field_name} value...")
            line_edit.textChanged.connect(self.update_preview)
            # Apply the same explicit Base background so macOS native
            # rendering can't cause a mismatch with the combo boxes.
            line_edit.setStyleSheet(self._field_lineedit_style(colors))
            line_edit.setProperty("field_input", True)
            return line_edit

    def _create_section_tab(self, section_name, field_inputs, field_containers, fields):
        """
        Create a scrollable tab widget for one MediaInfo section.
        
        Uses the same +/- multi-value pattern as CustomExiftoolDialog.
        Fields in DROPDOWN_OPTIONS use editable combo boxes; all others
        use plain line edits.
        
        Uses QGridLayout columns directly so that labels, input widgets,
        and buttons each occupy their own column.  This prevents combo
        boxes from overlapping labels.
        
        Args:
            section_name: 'general', 'video', or 'audio'
            field_inputs: dict to store lists of input widgets per field
            field_containers: dict to store QVBoxLayout per field
            fields: list of (field_name, label, defaults, tooltip) tuples
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
            label.setMinimumWidth(150)
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
            widget = self._create_input_widget(field_name, first_value)
            inputs_layout.addWidget(widget)
            field_inputs[field_name].append(widget)
            
            # For fields with multiple defaults (e.g., Audio Format), add them
            for extra_value in default_values[1:]:
                extra_widget = self._create_input_widget(field_name, extra_value)
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
                lambda checked, fn=field_name, fi=field_inputs, fc=field_containers: 
                    self.add_textbox_row(fn, fi, fc)
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
    
    def add_textbox_row(self, field_name, field_inputs, field_containers, value=""):
        """Add a new input widget row for a field (combo box or line edit)"""
        container_layout = field_containers[field_name]
        
        widget = self._create_input_widget(field_name, value)
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

    def build_profile_from_form(self):
        return self.get_mediainfo_profile()
    
    # ── Load / collect data ────────────────────────────────────────────
    
    def load_profile_data(self, profile_data):
        """
        Load profile data into all three sections' form fields.
        
        Handles both MediainfoProfile dataclass instances and plain dicts
        (from spex_config.mediainfo_values which stores raw dicts).
        """
        from dataclasses import asdict
        
        # Determine how to get section data depending on input type
        if hasattr(profile_data, '__dataclass_fields__'):
            # It's a MediainfoProfile dataclass
            general_dict = asdict(profile_data.general) if hasattr(profile_data, 'general') else {}
            video_dict = asdict(profile_data.video) if hasattr(profile_data, 'video') else {}
            audio_dict = asdict(profile_data.audio) if hasattr(profile_data, 'audio') else {}
        elif isinstance(profile_data, dict):
            # Could be {'general': {...}, 'video': {...}, 'audio': {...}} format
            # OR {'expected_general': {...}, ...} format from spex_config
            if 'expected_general' in profile_data:
                general_dict = profile_data.get('expected_general', {})
                video_dict = profile_data.get('expected_video', {})
                audio_dict = profile_data.get('expected_audio', {})
            else:
                general_dict = profile_data.get('general', {})
                video_dict = profile_data.get('video', {})
                audio_dict = profile_data.get('audio', {})
            # Convert any nested dataclasses to dicts
            if hasattr(general_dict, '__dataclass_fields__'):
                general_dict = asdict(general_dict)
            if hasattr(video_dict, '__dataclass_fields__'):
                video_dict = asdict(video_dict)
            if hasattr(audio_dict, '__dataclass_fields__'):
                audio_dict = asdict(audio_dict)
        else:
            general_dict = {}
            video_dict = {}
            audio_dict = {}
        
        # Load into each section's fields
        self._load_section_data(self.general_inputs, self.general_containers, general_dict)
        self._load_section_data(self.video_inputs, self.video_containers, video_dict)
        self._load_section_data(self.audio_inputs, self.audio_containers, audio_dict)
        
        if hasattr(self, 'preview_text'):
            self.update_preview()
    
    def _load_section_data(self, field_inputs, field_containers, data_dict):
        """
        Load data into one section's field inputs.
        
        Clears existing inputs and creates new ones based on values.
        Handles both single values and lists.
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
                    widget = self._create_input_widget(field_name, str(val))
                    container_layout.addWidget(widget)
                    field_inputs[field_name].append(widget)
            else:
                # Field not in data, add one empty widget
                widget = self._create_input_widget(field_name, "")
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
        format_val = "N/A"
        if "Format" in self.video_inputs and self.video_inputs["Format"]:
            text = self._get_widget_text(self.video_inputs["Format"][0])
            if text:
                format_val = text
        
        width_val = "N/A"
        if "Width" in self.video_inputs and self.video_inputs["Width"]:
            text = self._get_widget_text(self.video_inputs["Width"][0])
            if text:
                width_val = text
        
        height_val = "N/A"
        if "Height" in self.video_inputs and self.video_inputs["Height"]:
            text = self._get_widget_text(self.video_inputs["Height"][0])
            if text:
                height_val = text
        
        container_val = "N/A"
        if "Format" in self.general_inputs and self.general_inputs["Format"]:
            text = self._get_widget_text(self.general_inputs["Format"][0])
            if text:
                container_val = text
        
        preview = f"{profile_name}: {container_val} / {format_val} {width_val}x{height_val}"
        self.preview_text.setText(preview)
    
    # ── Profile construction ───────────────────────────────────────────
    
    def get_mediainfo_profile(self):
        """
        Get the MediaInfo profile as a MediainfoProfile dataclass.
        
        Returns None and shows a warning if validation fails.
        """
        if not self.profile_name_input.text():
            QMessageBox.warning(self, "Validation Error", "Profile name is required.")
            return None
        
        general_data = self._collect_section_values(self.general_inputs)
        video_data = self._collect_section_values(self.video_inputs)
        audio_data = self._collect_section_values(self.audio_inputs)
        
        # Validate required General fields
        required_general = ["FileExtension", "Format"]
        for field_name in required_general:
            value = general_data.get(field_name)
            if not value or (isinstance(value, list) and not value):
                QMessageBox.warning(
                    self, "Validation Error",
                    f"General > {field_name} is required."
                )
                return None
        
        # Validate required Video fields
        required_video = ["Format"]
        for field_name in required_video:
            value = video_data.get(field_name)
            if not value or (isinstance(value, list) and not value):
                QMessageBox.warning(
                    self, "Validation Error",
                    f"Video > {field_name} is required."
                )
                return None
        
        # Ensure Audio.Format is always a list (matches MediainfoAudioValues type hint)
        audio_format = audio_data.get("Format", "")
        if isinstance(audio_format, str) and audio_format:
            audio_data["Format"] = [audio_format]
        elif not audio_format:
            audio_data["Format"] = []
        
        try:
            general = MediainfoGeneralValues(**general_data)
            video = MediainfoVideoValues(**video_data)
            audio = MediainfoAudioValues(**audio_data)
            return MediainfoProfile(general=general, video=video, audio=audio)
        except Exception as e:
            QMessageBox.critical(
                self, "Error",
                f"Failed to create profile:\n{str(e)}"
            )
            return None
    
    # ── Save handling ──────────────────────────────────────────────────
    
    def on_save_clicked(self):
        """Handle save button click"""
        profile = self.get_mediainfo_profile()
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
        # Refresh explicit styling on all field input widgets so they
        # track palette colors after a theme switch.
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