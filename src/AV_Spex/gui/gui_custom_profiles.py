from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QScrollArea, QPushButton, QComboBox, QCheckBox, QGroupBox,
    QMessageBox, QDialog, QTextEdit, QGridLayout, QListWidget,
    QFileDialog, QRadioButton, QButtonGroup
)
from PyQt6.QtCore import Qt

import copy
import json
import os
from dataclasses import replace

from AV_Spex.processing.processing_mgmt import setup_mediaconch_policy
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_io import ConfigIO
from AV_Spex.utils import config_edit
from AV_Spex.utils.config_setup import (
    ChecksProfile, OutputsConfig, FixityConfig, ToolsConfig,
    BasicToolConfig, QCToolsConfig, MediaConchConfig, QCTParseToolConfig,
    FrameAnalysisConfig, ClamsDetectionConfig, SUPPORTED_VIDEO_EXTENSIONS
)

# Tools shown with a Run Tool / Check Tool pair
BASIC_TOOLS = ["exiftool", "ffprobe", "mediainfo", "mediatrace", "mkvalidator"]
from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.utils.log_setup import logger

config_mgr = ConfigManager()

class CustomProfileDialog(QDialog, ThemeableMixin):
    def __init__(self, parent=None, edit_profile=None):
        super().__init__(parent)
        self.profile = None
        self.edit_mode = edit_profile is not None
        # Settings with no control in any GUI (duplicate_min_run_length, the
        # CLAMS numeric tuning) are carried over from whatever was loaded —
        # the edited profile or the current config — so saving doesn't reset
        # them. The form's own fields are layered on top in
        # get_profile_from_form().
        self._base_frame_analysis = FrameAnalysisConfig()
        self._base_clams = ClamsDetectionConfig()
        self.setWindowTitle("Custom Profile Editor" if self.edit_mode else "Create Custom Profile")
        self.setModal(True)

        # Add theme handling
        self.setup_theme_handling()

        # Set minimum size for the dialog
        self.setMinimumSize(700, 800)
        
        # Initialize layout
        layout = QVBoxLayout()
        layout.setSpacing(10)
        
        # Profile name and description
        self.setup_profile_info_section(layout)
        
        # Scrollable area for configuration sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self.config_layout = QVBoxLayout(scroll_widget)
        self.config_layout.setSpacing(10)
        scroll.setWidget(scroll_widget)
        
        # Configuration sections
        self.setup_outputs_section()
        self.setup_fixity_section()
        self.setup_tools_section()
        
        # Set scroll area height
        scroll.setMinimumHeight(500)
        layout.addWidget(scroll)
        
        # Dialog buttons
        self.setup_dialog_buttons(layout)
        
        self.setLayout(layout)

        # Apply initial theme styling
        self._apply_initial_theme_styling()
        
        # Style buttons at the end, after all UI is set up
        theme_manager = ThemeManager.instance()
        theme_manager.style_buttons(self)
        
        # Load existing profile if in edit mode
        if edit_profile:
            self.load_existing_profile(edit_profile)
    
    def _apply_initial_theme_styling(self):
        """Apply initial theme styling using ThemeManager."""
        theme_manager = ThemeManager.instance()
        
        # Style all group boxes
        for group_box in self.findChildren(QGroupBox):
            theme_manager.style_groupbox(group_box)

    
    def setup_profile_info_section(self, layout):
        """Setup the profile name and description section."""
        info_group = QGroupBox("Profile Information")
        info_layout = QVBoxLayout()
        
        # Profile name
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Profile Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Enter profile name...")
        name_layout.addWidget(self.name_input)
        info_layout.addLayout(name_layout)
        
        # Profile description
        desc_layout = QVBoxLayout()
        desc_layout.addWidget(QLabel("Description (optional):"))
        self.description_input = QTextEdit()
        self.description_input.setMaximumHeight(60)
        self.description_input.setPlaceholderText("Enter profile description...")
        desc_layout.addWidget(self.description_input)
        info_layout.addLayout(desc_layout)
        
        # Validate filename
        validate_fn_layout = QHBoxLayout()
        validate_fn_layout.addWidget(QLabel("Validate Filename:"))
        self.validate_filename_check = QCheckBox()
        self.validate_filename_check.setChecked(True)  # Default matches ChecksProfile default
        validate_fn_layout.addWidget(self.validate_filename_check)
        validate_fn_layout.addStretch()
        info_layout.addLayout(validate_fn_layout)

        # Input video file extension
        ext_layout = QHBoxLayout()
        ext_layout.addWidget(QLabel("Video File Extension:"))
        self.video_extension_combo = QComboBox()
        self.video_extension_combo.addItems(list(SUPPORTED_VIDEO_EXTENSIONS))
        self.video_extension_combo.setMinimumWidth(120)
        ext_layout.addWidget(self.video_extension_combo)
        ext_layout.addStretch()
        info_layout.addLayout(ext_layout)
        ext_desc = QLabel(
            "Container extension of the input video file. Non-MKV containers can't carry "
            "embedded stream fixity, custom Matroska tags (mediatrace), Matroska validation (mkvalidator), "
            "or signal flow; those are turned off when the profile is applied.")
        ext_desc.setIndent(20)
        ext_desc.setWordWrap(True)
        info_layout.addWidget(ext_desc)

        info_group.setLayout(info_layout)
        layout.addWidget(info_group)
    
    def setup_outputs_section(self):
        """Setup the outputs configuration section."""
        outputs_group = QGroupBox("Output Settings")
        outputs_layout = QGridLayout()
        
        # Access file (now using checkbox for boolean)
        outputs_layout.addWidget(QLabel("Access File:"), 0, 0)
        self.access_file_check = QCheckBox()
        outputs_layout.addWidget(self.access_file_check, 0, 1)

        # Access file sub-options (mirror the Checks tab)
        access_options = QVBoxLayout()
        access_options.setContentsMargins(20, 0, 0, 0)
        self.access_trim_bars_check = self._add_sub_option(
            access_options, "Trim color bars from start",
            "If color bars are detected at the head of the tape, skip them in the access file "
            "(requires Detect Color Bars or CLAMS Bars + Tone Detection)")
        self.access_crop_to_480_check = self._add_sub_option(
            access_options, "Crop NTSC to 720x480",
            "Trim NTSC sources to 720x480; if unchecked, keep the native 720x486 height")
        self.access_crop_borders_check = self._add_sub_option(
            access_options, "Crop detected borders",
            "If sophisticated border detection finds an active picture area, crop to it "
            "(requires Border Detection in Sophisticated mode and Crop NTSC to 720x480)")
        self.access_exclude_audio_check = self._add_sub_option(
            access_options, "Exclude flagged audio channel",
            "If audio analysis flags a channel as silent or carrying audible timecode, "
            "output dual-mono from the good channel (requires Audio Analysis)")
        outputs_layout.addLayout(access_options, 1, 0, 1, 2)

        # Report (now using checkbox for boolean)
        outputs_layout.addWidget(QLabel("Report:"), 2, 0)
        self.report_check = QCheckBox()
        outputs_layout.addWidget(self.report_check, 2, 1)

        outputs_layout.addWidget(QLabel("Save Console Log as PDF:"), 3, 0)
        self.save_console_pdf_check = QCheckBox()
        outputs_layout.addWidget(self.save_console_pdf_check, 3, 1)

        # Same dependencies as the Checks tab: sub-options need Access File,
        # and cropping borders needs the 480 crop
        self.access_file_check.toggled.connect(self._update_access_option_states)
        self.access_crop_to_480_check.toggled.connect(self._update_access_option_states)
        self._update_access_option_states()

        outputs_group.setLayout(outputs_layout)
        self.config_layout.addWidget(outputs_group)

    def _add_sub_option(self, layout, text, description):
        """Add a bold checkbox with a wrapped description under it; return the checkbox."""
        checkbox = QCheckBox(text)
        checkbox.setStyleSheet("font-weight: bold;")
        desc = QLabel(description)
        desc.setIndent(20)
        desc.setWordWrap(True)
        layout.addWidget(checkbox)
        layout.addWidget(desc)
        return checkbox

    def _update_access_option_states(self):
        access_on = self.access_file_check.isChecked()
        for checkbox in (self.access_trim_bars_check, self.access_crop_to_480_check,
                         self.access_exclude_audio_check):
            checkbox.setEnabled(access_on)
        self.access_crop_borders_check.setEnabled(
            access_on and self.access_crop_to_480_check.isChecked())
    
    def setup_fixity_section(self):
        """Setup the fixity configuration section."""
        fixity_group = QGroupBox("Fixity Settings")
        fixity_layout = QVBoxLayout()
        
        self.fixity_checks = {}
        
        # --- File Fixity ---
        file_fixity_label = QLabel("File Fixity")
        file_fixity_label.setStyleSheet("font-weight: bold;")
        fixity_layout.addWidget(file_fixity_label)
        
        file_grid = QGridLayout()
        
        file_fixity_options = [
            ("output_fixity", "Output Fixity:", 0),
            ("check_fixity", "Validate Fixity:", 1),
        ]
        for setting, label, row in file_fixity_options:
            file_grid.addWidget(QLabel(label), row, 0)
            checkbox = QCheckBox()
            self.fixity_checks[setting] = checkbox
            file_grid.addWidget(checkbox, row, 1)
        
        file_grid.addWidget(QLabel("Checksum Algorithm:"), 2, 0)
        self.checksum_algorithm_combo = QComboBox()
        self.checksum_algorithm_combo.addItems(["md5", "sha256"])
        file_grid.addWidget(self.checksum_algorithm_combo, 2, 1)
        
        fixity_layout.addLayout(file_grid)
        
        # Spacer between sections
        fixity_layout.addSpacing(10)
        
        # --- Stream Fixity ---
        stream_fixity_label = QLabel("Stream Fixity")
        stream_fixity_label.setStyleSheet("font-weight: bold;")
        fixity_layout.addWidget(stream_fixity_label)
        
        stream_grid = QGridLayout()
        
        stream_fixity_options = [
            ("embed_stream_fixity", "Embed Stream Fixity:", 0),
            ("overwrite_stream_fixity", "Overwrite Stream Fixity:", 1),
            ("validate_stream_fixity", "Validate Stream Fixity:", 2),
        ]
        for setting, label, row in stream_fixity_options:
            stream_grid.addWidget(QLabel(label), row, 0)
            checkbox = QCheckBox()
            self.fixity_checks[setting] = checkbox
            stream_grid.addWidget(checkbox, row, 1)
        
        stream_grid.addWidget(QLabel("Stream Hash Algorithm:"), 3, 0)
        self.stream_hash_algorithm_combo = QComboBox()
        self.stream_hash_algorithm_combo.addItems(["md5", "sha256"])
        stream_grid.addWidget(self.stream_hash_algorithm_combo, 3, 1)
        
        fixity_layout.addLayout(stream_grid)
        
        fixity_group.setLayout(fixity_layout)
        self.config_layout.addWidget(fixity_group)
    
    def setup_tools_section(self):
        """Setup the tools configuration section."""
        tools_group = QGroupBox("Tools Settings")
        tools_layout = QVBoxLayout()
        
        # Basic tools (exiftool, ffprobe, mediainfo, mediatrace, mkvalidator)
        self.basic_tool_checks = {}

        for tool in BASIC_TOOLS:
            tool_group = QGroupBox(tool.title())
            tool_layout = QGridLayout()
            
            # Run tool (now using checkbox for boolean)
            tool_layout.addWidget(QLabel("Run Tool:"), 0, 0)
            run_checkbox = QCheckBox()
            tool_layout.addWidget(run_checkbox, 0, 1)

            # Check tool (now using checkbox for boolean)
            tool_layout.addWidget(QLabel("Check Tool:"), 1, 0)
            check_checkbox = QCheckBox()
            tool_layout.addWidget(check_checkbox, 1, 1)
            
            self.basic_tool_checks[tool] = {
                'check_tool': check_checkbox,
                'run_tool': run_checkbox
            }
            
            tool_group.setLayout(tool_layout)
            tools_layout.addWidget(tool_group)
        
        # MediaConch
        self.setup_mediaconch_section(tools_layout)
        
        # QCTools
        self.setup_qctools_section(tools_layout)
        
        # QCT Parse
        self.setup_qct_parse_section(tools_layout)

        # CLAMS bars + tone detection
        self.setup_clams_section(tools_layout)

        tools_group.setLayout(tools_layout)
        self.config_layout.addWidget(tools_group)
        
        # Frame Analysis (own top-level section)
        self.setup_frame_analysis_section()
    
    def setup_mediaconch_section(self, parent_layout):
        """Setup MediaConch specific settings."""
        mediaconch_group = QGroupBox("MediaConch")
        mediaconch_layout = QGridLayout()
        
        # Policy dropdown (remains as combo box for string value)
        mediaconch_layout.addWidget(QLabel("Policy:"), 0, 0)
        self.mediaconch_policy_combo = QComboBox()
        
        # Load available policies from config manager
        from AV_Spex.utils.config_manager import ConfigManager
        config_mgr = ConfigManager()
        available_policies = config_mgr.get_available_policies()
        self.mediaconch_policy_combo.addItems(available_policies)
        
        mediaconch_layout.addWidget(self.mediaconch_policy_combo, 0, 1)
        
        # Import button
        self.import_policy_btn = QPushButton("Import New MediaConch Policy")
        self.import_policy_btn.clicked.connect(self.open_policy_file_dialog)
        mediaconch_layout.addWidget(self.import_policy_btn, 1, 0, 1, 2)  # Span both columns
        
        # Run MediaConch (now using checkbox for boolean)
        mediaconch_layout.addWidget(QLabel("Run MediaConch:"), 2, 0)
        self.mediaconch_run_check = QCheckBox()
        mediaconch_layout.addWidget(self.mediaconch_run_check, 2, 1)
        
        mediaconch_group.setLayout(mediaconch_layout)
        parent_layout.addWidget(mediaconch_group)

    def open_policy_file_dialog(self):
        """Open file dialog for selecting MediaConch policy file"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox
        from AV_Spex.processing.processing_mgmt import setup_mediaconch_policy
        
        file_dialog = QFileDialog()
        file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_dialog.setNameFilter("XML files (*.xml)")
        
        if file_dialog.exec():
            selected_files = file_dialog.selectedFiles()
            if selected_files:
                policy_path = selected_files[0]
                # Call setup_mediaconch_policy with selected file
                new_policy_name = setup_mediaconch_policy(policy_path)
                if new_policy_name:
                    # Refresh the policy dropdown to show the new policy
                    self.refresh_policy_dropdown()
                    # Set the dropdown to the newly imported policy
                    self.mediaconch_policy_combo.setCurrentText(new_policy_name)
                    QMessageBox.information(
                        self,
                        "Success",
                        f"Successfully imported MediaConch policy: {new_policy_name}"
                    )
                else:
                    # Show error message if policy setup failed
                    QMessageBox.critical(
                        self,
                        "Error",
                        "Failed to import MediaConch policy file. Check logs for details."
                    )
    
    def refresh_policy_dropdown(self):
        """Refresh the MediaConch policy dropdown with current available policies"""
        from AV_Spex.utils.config_manager import ConfigManager
        
        # Store current selection
        current_policy = self.mediaconch_policy_combo.currentText()
        
        # Clear and repopulate
        self.mediaconch_policy_combo.clear()
        
        # Get updated list of available policies
        config_mgr = ConfigManager()
        available_policies = config_mgr.get_available_policies()
        self.mediaconch_policy_combo.addItems(available_policies)
        
        # Restore selection if it still exists
        index = self.mediaconch_policy_combo.findText(current_policy)
        if index >= 0:
            self.mediaconch_policy_combo.setCurrentIndex(index)
    
    def setup_qctools_section(self, parent_layout):
        """Setup QCTools specific settings, aligned with ComplexWindow layout."""
        qctools_group = QGroupBox("QCTools")
        qctools_layout = QVBoxLayout()
        
        # Run Tool checkbox
        self.qctools_run_check = QCheckBox("Run Tool")
        self.qctools_run_check.setStyleSheet("font-weight: bold;")
        run_qctools_desc = QLabel("Run QCTools on input video file")
        run_qctools_desc.setIndent(20)
        
        # File Extension dropdown
        qctools_ext_label = QLabel("QCTools File Extension")
        qctools_ext_label.setStyleSheet("font-weight: bold;")
        qctools_ext_desc = QLabel("Set the extension for QCTools output files")
        qctools_ext_desc.setIndent(20)
        self.qctools_ext_combo = QComboBox()
        self.qctools_ext_combo.addItems(["qctools.xml.gz", "qctools.mkv"])
        self.qctools_ext_combo.setMinimumWidth(160)
        
        qctools_ext_row = QHBoxLayout()
        qctools_ext_row.addWidget(qctools_ext_label)
        qctools_ext_row.addWidget(self.qctools_ext_combo)
        qctools_ext_row.addStretch()
        
        # Add all widgets
        qctools_layout.addWidget(self.qctools_run_check)
        qctools_layout.addWidget(run_qctools_desc)
        qctools_layout.addSpacing(10)
        qctools_layout.addLayout(qctools_ext_row)
        qctools_layout.addWidget(qctools_ext_desc)
        
        qctools_group.setLayout(qctools_layout)
        parent_layout.addWidget(qctools_group)
    
    def setup_qct_parse_section(self, parent_layout):
        """Setup QCT Parse specific settings, aligned with ComplexWindow layout."""
        qct_parse_group = QGroupBox("qct-parse")
        qct_parse_layout = QVBoxLayout()
        
        # Run Tool
        self.qct_parse_run_check = QCheckBox("Run Tool")
        self.qct_parse_run_check.setStyleSheet("font-weight: bold;")
        run_qctparse_desc = QLabel("Run qct-parse tool on input video file")
        run_qctparse_desc.setIndent(20)
        
        # Bars Detection
        self.bars_detection_check = QCheckBox("Detect Color Bars")
        self.bars_detection_check.setStyleSheet("font-weight: bold;")
        bars_detection_desc = QLabel("Detect color bars in the video content")
        bars_detection_desc.setIndent(20)
        
        # Evaluate Bars
        self.evaluate_bars_check = QCheckBox("Evaluate Color Bars")
        self.evaluate_bars_check.setStyleSheet("font-weight: bold;")
        evaluate_bars_desc = QLabel("Compare video content against reference color bar values for validation")
        evaluate_bars_desc.setIndent(20)

        # Evaluate Bars reference: what the evaluation grades content against.
        bars_ref_label = QLabel("Compare against:")
        bars_ref_label.setIndent(40)
        self.bars_ref_group = QButtonGroup(self)
        self.bars_ref_detected_radio = QRadioButton("Bars detected in this video")
        self.bars_ref_smpte_radio = QRadioButton("Standard SMPTE values")
        self.bars_ref_both_radio = QRadioButton("Both")
        self.bars_ref_group.addButton(self.bars_ref_detected_radio)
        self.bars_ref_group.addButton(self.bars_ref_smpte_radio)
        self.bars_ref_group.addButton(self.bars_ref_both_radio)
        self.bars_ref_detected_radio.setChecked(True)
        bars_ref_detected_row = QHBoxLayout()
        bars_ref_detected_row.addSpacing(40)
        bars_ref_detected_row.addWidget(self.bars_ref_detected_radio)
        bars_ref_detected_row.addStretch()
        bars_ref_detected_desc = QLabel(
            "Uses levels measured from this file's own color bars. If no bars "
            "are found, standard SMPTE values are used as a fallback.")
        bars_ref_detected_desc.setIndent(60)
        bars_ref_detected_desc.setWordWrap(True)
        bars_ref_smpte_row = QHBoxLayout()
        bars_ref_smpte_row.addSpacing(40)
        bars_ref_smpte_row.addWidget(self.bars_ref_smpte_radio)
        bars_ref_smpte_row.addStretch()
        bars_ref_smpte_desc = QLabel(
            "Always uses standard SMPTE color bar values, ignoring any bars "
            "in the video.")
        bars_ref_smpte_desc.setIndent(60)
        bars_ref_smpte_desc.setWordWrap(True)
        bars_ref_both_row = QHBoxLayout()
        bars_ref_both_row.addSpacing(40)
        bars_ref_both_row.addWidget(self.bars_ref_both_radio)
        bars_ref_both_row.addStretch()
        bars_ref_both_desc = QLabel(
            "Runs the evaluation against both references; the report lets "
            "you toggle between the two sets of results.")
        bars_ref_both_desc.setIndent(60)
        bars_ref_both_desc.setWordWrap(True)

        # Thumb Export
        self.thumb_export_check = QCheckBox("Thumbnail Export")
        self.thumb_export_check.setStyleSheet("font-weight: bold;")
        thumb_export_desc = QLabel("Export thumbnails of failed frames for review")
        thumb_export_desc.setIndent(20)

        # Perform Audio Analysis
        self.audio_analysis_check = QCheckBox("Perform Audio Analysis")
        self.audio_analysis_check.setStyleSheet("font-weight: bold;")
        audio_analysis_desc = QLabel("Detect audio clipping, channel imbalance, identical channels, audible timecode, and audio dropout")
        audio_analysis_desc.setIndent(20)

        # Add all widgets
        qct_parse_layout.addWidget(self.qct_parse_run_check)
        qct_parse_layout.addWidget(run_qctparse_desc)
        qct_parse_layout.addWidget(self.bars_detection_check)
        qct_parse_layout.addWidget(bars_detection_desc)
        qct_parse_layout.addWidget(self.evaluate_bars_check)
        qct_parse_layout.addWidget(evaluate_bars_desc)
        qct_parse_layout.addWidget(bars_ref_label)
        qct_parse_layout.addLayout(bars_ref_detected_row)
        qct_parse_layout.addWidget(bars_ref_detected_desc)
        qct_parse_layout.addLayout(bars_ref_smpte_row)
        qct_parse_layout.addWidget(bars_ref_smpte_desc)
        qct_parse_layout.addLayout(bars_ref_both_row)
        qct_parse_layout.addWidget(bars_ref_both_desc)
        qct_parse_layout.addWidget(self.thumb_export_check)
        qct_parse_layout.addWidget(thumb_export_desc)
        qct_parse_layout.addWidget(self.audio_analysis_check)
        qct_parse_layout.addWidget(audio_analysis_desc)

        self.tone_leak_check = self._add_sub_option(
            qct_parse_layout, "Tone Leak Detection",
            "Detect a 1 kHz reference tone leaking from the transfer chain, heard as a faint "
            "high-pitched whine or squeak in quiet passages")
        self.clamped_levels_check = self._add_sub_option(
            qct_parse_layout, "Detect Clamped Levels",
            "Detect broadcast-range level clamping from the analog-to-digital converter")
        self.chroma_phase_check = self._add_sub_option(
            qct_parse_layout, "Detect Chroma Phase Errors",
            "Detect tape tracking artifacts where chroma collapses toward cyan or magenta")

        qct_parse_group.setLayout(qct_parse_layout)
        parent_layout.addWidget(qct_parse_group)

    def setup_clams_section(self, parent_layout):
        """CLAMS bars + tone detection. Only the on/off toggle is exposed; its
        numeric tuning is JSON-only and carried over from the loaded settings."""
        clams_group = QGroupBox("CLAMS Detection")
        clams_layout = QVBoxLayout()
        self.clams_run_check = self._add_sub_option(
            clams_layout, "CLAMS Bars + Tone Detection",
            "Run the CLAMS SSIM-based SMPTE bars detector and the cross-correlation tone "
            "detector. Where they disagree with qct-parse about head bars, SSIM decides.")
        clams_group.setLayout(clams_layout)
        parent_layout.addWidget(clams_group)
    
    def setup_frame_analysis_section(self):
        """Setup the frame analysis configuration section with sub-groups
        matching the ComplexWindow layout."""
        frame_group = QGroupBox("Frame Analysis Settings")
        frame_layout = QVBoxLayout()
        
        # --- Bitplane Check Settings ---
        self.setup_bitplane_check_profile_section(frame_layout)

        # --- Border Detection Settings ---
        self.setup_border_detection_profile_section(frame_layout)

        # --- BRNG Analysis Settings ---
        self.setup_brng_profile_section(frame_layout)

        # --- Signalstats Settings ---
        self.setup_signalstats_profile_section(frame_layout)

        # --- Standalone detectors ---
        detectors_group = QGroupBox("Other Frame Analysis Checks")
        detectors_layout = QVBoxLayout()
        self.duplicate_frame_check = self._add_sub_option(
            detectors_layout, "Duplicate Frame Detection",
            "Detect runs of repeated frames likely caused by TBC or framesync errors, using "
            "QCTools YDIF/UDIF/VDIF to find candidate freezes and OpenCV to verify them")
        self.dropped_sample_check = self._add_sub_option(
            detectors_layout, "Dropped Sample Detection",
            "Detect potential audio sample drops from TBC/framesync or ADC devices. Generates "
            "a spectrogram to identify audible pops and compares audio/video durations.")
        detectors_group.setLayout(detectors_layout)
        frame_layout.addWidget(detectors_group)

        frame_group.setLayout(frame_layout)
        self.config_layout.addWidget(frame_group)
    
    def setup_bitplane_check_profile_section(self, parent_layout):
        """Setup the bitplane check sub-section."""
        bitplane_group = QGroupBox("Bitplane Check")
        bitplane_layout = QVBoxLayout()

        self.enable_bitplane_check_check = QCheckBox("Enable Bitplane Check")
        self.enable_bitplane_check_check.setStyleSheet("font-weight: bold;")
        bitplane_desc = QLabel(
            "Verify that the 9th and 10th bits of 10-bit video contain data. "
            "Some TBC/framesync devices truncate these bits."
        )
        bitplane_desc.setWordWrap(True)
        bitplane_desc.setIndent(20)

        bitplane_layout.addWidget(self.enable_bitplane_check_check)
        bitplane_layout.addWidget(bitplane_desc)

        bitplane_group.setLayout(bitplane_layout)
        parent_layout.addWidget(bitplane_group)

    def setup_border_detection_profile_section(self, parent_layout):
        """Setup the border detection sub-section."""
        border_group = QGroupBox("Border Detection Settings")
        border_layout = QVBoxLayout()

        # Enable Border Detection
        self.enable_border_detection_check = QCheckBox("Enable Border Detection")
        self.enable_border_detection_check.setStyleSheet("font-weight: bold;")
        border_det_desc = QLabel("Detect and crop blanking borders from the video")
        border_det_desc.setIndent(20)
        
        border_layout.addWidget(self.enable_border_detection_check)
        border_layout.addWidget(border_det_desc)
        border_layout.addSpacing(10)
        
        # Border Detection Mode
        border_mode_row = QHBoxLayout()
        border_mode_label = QLabel("Detection Mode:")
        border_mode_label.setStyleSheet("font-weight: bold;")
        self.border_detection_combo = QComboBox()
        self.border_detection_combo.addItem("Simple", "simple")
        self.border_detection_combo.addItem("Sophisticated", "sophisticated")
        border_mode_row.addWidget(border_mode_label)
        border_mode_row.addWidget(self.border_detection_combo)
        border_mode_row.addStretch()
        border_layout.addLayout(border_mode_row)
        border_layout.addSpacing(10)
        
        # Simple Border Parameters
        simple_border_row = QHBoxLayout()
        simple_border_label = QLabel("Border Pixels:")
        simple_border_label.setStyleSheet("font-weight: bold;")
        self.simple_border_pixels_input = QLineEdit("25")
        self.simple_border_pixels_input.setMaximumWidth(60)
        simple_border_row.addWidget(simple_border_label)
        simple_border_row.addWidget(self.simple_border_pixels_input)
        simple_border_row.addStretch()
        border_layout.addLayout(simple_border_row)
        simple_desc = QLabel("Fixed number of pixels to crop from each edge")
        simple_desc.setIndent(20)
        border_layout.addWidget(simple_desc)
        border_layout.addSpacing(5)
        
        # Sophisticated Border Parameters
        soph_header = QLabel("Sophisticated Mode Parameters")
        soph_header.setStyleSheet("font-weight: bold;")
        border_layout.addWidget(soph_header)
        
        # Brightness Threshold
        threshold_row = QHBoxLayout()
        threshold_label = QLabel("Brightness Threshold:")
        self.soph_threshold_input = QLineEdit("10")
        self.soph_threshold_input.setMaximumWidth(60)
        threshold_row.addWidget(threshold_label)
        threshold_row.addWidget(self.soph_threshold_input)
        threshold_row.addStretch()
        border_layout.addLayout(threshold_row)
        threshold_desc = QLabel("Brightness an edge row or column must exceed to count as picture (0 = pure black, 255 = pure white)")
        threshold_desc.setIndent(20)
        border_layout.addWidget(threshold_desc)
        
        # Edge Sample Width
        edge_row = QHBoxLayout()
        edge_label = QLabel("Edge Sample Width:")
        self.soph_edge_width_input = QLineEdit("100")
        self.soph_edge_width_input.setMaximumWidth(60)
        edge_row.addWidget(edge_label)
        edge_row.addWidget(self.soph_edge_width_input)
        edge_row.addStretch()
        border_layout.addLayout(edge_row)
        edge_desc = QLabel("Pixels to search in from the left and right edges")
        edge_desc.setIndent(20)
        border_layout.addWidget(edge_desc)
        
        # Sample Frames
        frames_row = QHBoxLayout()
        frames_label = QLabel("Sample Frames:")
        self.soph_sample_frames_input = QLineEdit("30")
        self.soph_sample_frames_input.setMaximumWidth(60)
        frames_row.addWidget(frames_label)
        frames_row.addWidget(self.soph_sample_frames_input)
        frames_row.addStretch()
        border_layout.addLayout(frames_row)
        frames_desc = QLabel("Number of well-exposed frames to measure borders on (minimum 5)")
        frames_desc.setIndent(20)
        border_layout.addWidget(frames_desc)
        
        # Padding
        padding_row = QHBoxLayout()
        padding_label = QLabel("Padding:")
        self.soph_padding_input = QLineEdit("5")
        self.soph_padding_input.setMaximumWidth(60)
        padding_row.addWidget(padding_label)
        padding_row.addWidget(self.soph_padding_input)
        padding_row.addStretch()
        border_layout.addLayout(padding_row)
        padding_desc = QLabel("Extra pixels trimmed from each side of the detected picture area")
        padding_desc.setIndent(20)
        border_layout.addWidget(padding_desc)
        border_layout.addSpacing(5)
        
        # Auto Retry
        self.auto_retry_borders_check = QCheckBox(
            "Auto-retry border detection if BRNG detects edge artifacts"
        )
        self.auto_retry_borders_check.setStyleSheet("font-weight: bold;")
        auto_retry_desc = QLabel("Automatically adjusts borders if edge artifacts are found")
        auto_retry_desc.setIndent(20)
        border_layout.addWidget(self.auto_retry_borders_check)
        border_layout.addWidget(auto_retry_desc)
        
        # Max Retries
        max_retries_row = QHBoxLayout()
        max_retries_label = QLabel("Max Retries:")
        max_retries_label.setStyleSheet("font-weight: bold;")
        self.max_border_retries_input = QLineEdit("3")
        self.max_border_retries_input.setMaximumWidth(60)
        max_retries_row.addWidget(max_retries_label)
        max_retries_row.addWidget(self.max_border_retries_input)
        max_retries_row.addStretch()
        border_layout.addLayout(max_retries_row)
        max_retries_desc = QLabel("Maximum number of border adjustment attempts")
        max_retries_desc.setIndent(20)
        border_layout.addWidget(max_retries_desc)
        
        border_group.setLayout(border_layout)
        parent_layout.addWidget(border_group)
    
    def setup_brng_profile_section(self, parent_layout):
        """Setup the BRNG analysis sub-section."""
        brng_group = QGroupBox("BRNG Analysis Settings")
        brng_layout = QVBoxLayout()
        
        # Enable BRNG Analysis
        self.enable_brng_analysis_check = QCheckBox("Enable BRNG Analysis")
        self.enable_brng_analysis_check.setStyleSheet("font-weight: bold;")
        brng_desc = QLabel("Analyze broadcast range violations in the active area")
        brng_desc.setIndent(20)
        
        brng_layout.addWidget(self.enable_brng_analysis_check)
        brng_layout.addWidget(brng_desc)
        brng_layout.addSpacing(10)
        
        # Skip Color Bars
        self.brng_skip_colorbars_check = QCheckBox("Skip Color Bars")
        self.brng_skip_colorbars_check.setStyleSheet("font-weight: bold;")
        skip_bars_desc = QLabel("Exclude detected color bars from BRNG, signalstats and analysis-period placement (bars are always excluded from duplicate frame detection)")
        skip_bars_desc.setWordWrap(True)
        skip_bars_desc.setIndent(20)
        brng_layout.addWidget(self.brng_skip_colorbars_check)
        brng_layout.addWidget(skip_bars_desc)
        
        brng_group.setLayout(brng_layout)
        parent_layout.addWidget(brng_group)
    
    def setup_signalstats_profile_section(self, parent_layout):
        """Setup the signalstats sub-section."""
        signalstats_group = QGroupBox("Signalstats Settings")
        signalstats_layout = QVBoxLayout()
        
        # Enable Signalstats
        self.enable_signalstats_check = QCheckBox("Enable Signalstats Analysis")
        self.enable_signalstats_check.setStyleSheet("font-weight: bold;")
        signalstats_desc = QLabel("Enhanced FFprobe signalstats")
        signalstats_desc.setIndent(20)
        
        signalstats_layout.addWidget(self.enable_signalstats_check)
        signalstats_layout.addWidget(signalstats_desc)
        signalstats_layout.addSpacing(10)
        
        # Analysis periods (shared by signalstats and BRNG analysis)
        periods_row = QHBoxLayout()
        periods_label = QLabel("Number of Periods:")
        periods_label.setStyleSheet("font-weight: bold;")
        self.analysis_period_count_input = QLineEdit("3")
        self.analysis_period_count_input.setMaximumWidth(60)
        periods_row.addWidget(periods_label)
        periods_row.addWidget(self.analysis_period_count_input)
        periods_row.addStretch()
        signalstats_layout.addLayout(periods_row)
        periods_desc = QLabel("How many time windows to sample (shared by Signalstats and BRNG analysis)")
        periods_desc.setIndent(20)
        signalstats_layout.addWidget(periods_desc)

        duration_row = QHBoxLayout()
        duration_label = QLabel("Period Duration (s):")
        duration_label.setStyleSheet("font-weight: bold;")
        self.analysis_period_duration_input = QLineEdit("60")
        self.analysis_period_duration_input.setMaximumWidth(60)
        duration_row.addWidget(duration_label)
        duration_row.addWidget(self.analysis_period_duration_input)
        duration_row.addStretch()
        signalstats_layout.addLayout(duration_row)
        duration_desc = QLabel("Length of each analysis period")
        duration_desc.setIndent(20)
        signalstats_layout.addWidget(duration_desc)
        
        signalstats_group.setLayout(signalstats_layout)
        parent_layout.addWidget(signalstats_group)
    
    def setup_dialog_buttons(self, layout):
        """Setup dialog action buttons."""
        button_layout = QHBoxLayout()
        
        # Load from current config button
        load_current_button = QPushButton("Load from Current Config")
        load_current_button.clicked.connect(self.load_from_current_config)
        button_layout.addWidget(load_current_button)
        
        button_layout.addStretch()  # Add stretch to push save/cancel to the right
        
        # Save and Cancel buttons
        save_button = QPushButton("Save Profile")
        save_button.clicked.connect(self.on_save_clicked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        
        layout.addLayout(button_layout)
    
    def load_from_current_config(self):
        """Load settings from the current checks configuration."""
        try:
            current_config = config_edit.config_mgr.get_config('checks', config_edit.ChecksConfig)
            self._load_settings(current_config)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load current config: {str(e)}")

    def load_existing_profile(self, profile):
        """Load an existing profile into the dialog."""
        self.name_input.setText(profile.name)
        self.description_input.setPlainText(profile.description)
        self._load_settings(profile)

    def _load_settings(self, source):
        """Fill every control from a ChecksConfig or ChecksProfile.

        Both share validate_filename, video_file_extension, outputs, fixity and
        tools, so the current config and a saved profile load the same way.
        """
        def set_combo_text(combo, text):
            index = combo.findText(text)
            if index >= 0:
                combo.setCurrentIndex(index)

        # Top-level
        self.validate_filename_check.setChecked(bool(source.validate_filename))
        set_combo_text(self.video_extension_combo,
                       getattr(source, 'video_file_extension', 'mkv') or 'mkv')

        # Outputs
        outputs = source.outputs
        self.access_file_check.setChecked(bool(outputs.access_file))
        self.access_trim_bars_check.setChecked(bool(getattr(outputs, 'access_file_trim_color_bars', True)))
        self.access_crop_to_480_check.setChecked(bool(getattr(outputs, 'access_file_crop_to_480', True)))
        self.access_crop_borders_check.setChecked(bool(getattr(outputs, 'access_file_crop_borders', True)))
        self.access_exclude_audio_check.setChecked(bool(getattr(outputs, 'access_file_exclude_flagged_audio', False)))
        self.report_check.setChecked(bool(outputs.report))
        self.save_console_pdf_check.setChecked(bool(getattr(outputs, 'save_console_pdf', False)))
        set_combo_text(self.qctools_ext_combo, getattr(outputs, 'qctools_ext', 'qctools.xml.gz'))
        self._update_access_option_states()

        # Frame analysis
        fa = getattr(outputs, 'frame_analysis', None) or FrameAnalysisConfig()
        self._base_frame_analysis = copy.deepcopy(fa)

        self.enable_bitplane_check_check.setChecked(bool(fa.enable_bitplane_check))

        self.enable_border_detection_check.setChecked(bool(fa.enable_border_detection))
        mode_index = self.border_detection_combo.findData(fa.border_detection_mode)
        if mode_index >= 0:
            self.border_detection_combo.setCurrentIndex(mode_index)
        self.simple_border_pixels_input.setText(str(fa.simple_border_pixels))
        self.soph_threshold_input.setText(str(fa.sophisticated_threshold))
        self.soph_edge_width_input.setText(str(fa.sophisticated_edge_sample_width))
        self.soph_sample_frames_input.setText(str(fa.sophisticated_sample_frames))
        self.soph_padding_input.setText(str(fa.sophisticated_padding))
        self.auto_retry_borders_check.setChecked(bool(fa.auto_retry_borders))
        self.max_border_retries_input.setText(str(fa.max_border_retries))

        self.enable_brng_analysis_check.setChecked(bool(fa.enable_brng_analysis))
        self.brng_skip_colorbars_check.setChecked(bool(fa.brng_skip_color_bars))

        self.enable_signalstats_check.setChecked(bool(fa.enable_signalstats))
        self.analysis_period_count_input.setText(str(fa.analysis_period_count))
        self.analysis_period_duration_input.setText(str(fa.analysis_period_duration))

        self.duplicate_frame_check.setChecked(bool(fa.enable_duplicate_frame_detection))
        self.dropped_sample_check.setChecked(bool(fa.enable_dropped_sample_detection))

        # Fixity
        fixity = source.fixity
        for key, checkbox in self.fixity_checks.items():
            checkbox.setChecked(bool(getattr(fixity, key)))
        set_combo_text(self.checksum_algorithm_combo, getattr(fixity, 'checksum_algorithm', 'md5'))
        set_combo_text(self.stream_hash_algorithm_combo, getattr(fixity, 'stream_hash_algorithm', 'md5'))

        # Basic tools
        tools = source.tools
        for tool_name, checks in self.basic_tool_checks.items():
            tool_config = getattr(tools, tool_name, None)
            checks['check_tool'].setChecked(bool(tool_config and tool_config.check_tool))
            checks['run_tool'].setChecked(bool(tool_config and tool_config.run_tool))

        # MediaConch: select the policy, adding it to the list if it isn't there
        policy = tools.mediaconch.mediaconch_policy
        if policy:
            if self.mediaconch_policy_combo.findText(policy) < 0:
                self.mediaconch_policy_combo.addItem(policy)
            self.mediaconch_policy_combo.setCurrentText(policy)
        self.mediaconch_run_check.setChecked(bool(tools.mediaconch.run_mediaconch))

        # QCTools
        self.qctools_run_check.setChecked(bool(tools.qctools.run_tool))

        # qct-parse
        qct = tools.qct_parse
        self.qct_parse_run_check.setChecked(bool(qct.run_tool))
        self.bars_detection_check.setChecked(bool(qct.barsDetection))
        self.evaluate_bars_check.setChecked(bool(qct.evaluateBars))
        self.thumb_export_check.setChecked(bool(qct.thumbExport))
        bars_ref = getattr(qct, 'evaluateBarsReference', 'detected')
        self.bars_ref_smpte_radio.setChecked(bars_ref == 'smpte')
        self.bars_ref_both_radio.setChecked(bars_ref == 'both')
        self.bars_ref_detected_radio.setChecked(bars_ref not in ('smpte', 'both'))
        self.audio_analysis_check.setChecked(bool(getattr(qct, 'audio_analysis', False)))
        self.tone_leak_check.setChecked(bool(getattr(qct, 'detect_tone_leak', False)))
        self.clamped_levels_check.setChecked(bool(getattr(qct, 'detect_clamped_levels', False)))
        self.chroma_phase_check.setChecked(bool(getattr(qct, 'detect_chroma_phase_errors', False)))

        # CLAMS
        clams = getattr(tools, 'clams_detection', None) or ClamsDetectionConfig()
        self._base_clams = copy.deepcopy(clams)
        self.clams_run_check.setChecked(bool(clams.run_tool))

    def get_profile_from_form(self):
        """Create a ChecksProfile from the form data."""
        # Validate required fields
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Profile name is required.")
            return None

        # Numeric fields: an empty box means the default; anything else must be a whole number
        numeric_fields = [
            ("Border Pixels", self.simple_border_pixels_input, 25),
            ("Brightness Threshold", self.soph_threshold_input, 10),
            ("Edge Sample Width", self.soph_edge_width_input, 100),
            ("Sample Frames", self.soph_sample_frames_input, 30),
            ("Padding", self.soph_padding_input, 5),
            ("Max Retries", self.max_border_retries_input, 3),
            ("Number of Periods", self.analysis_period_count_input, 3),
            ("Period Duration", self.analysis_period_duration_input, 60),
        ]
        numbers = {}
        for label, line_edit, default in numeric_fields:
            text = line_edit.text().strip()
            try:
                numbers[label] = int(text) if text else default
            except ValueError:
                QMessageBox.warning(self, "Validation Error",
                                    f"{label} must be a whole number (got \"{text}\").")
                return None

        # Frame analysis: form fields layered over the loaded settings, so
        # values with no control here (duplicate_min_run_length) are kept
        frame_analysis = replace(
            copy.deepcopy(self._base_frame_analysis),
            enable_bitplane_check=self.enable_bitplane_check_check.isChecked(),
            enable_border_detection=self.enable_border_detection_check.isChecked(),
            enable_brng_analysis=self.enable_brng_analysis_check.isChecked(),
            enable_signalstats=self.enable_signalstats_check.isChecked(),
            enable_duplicate_frame_detection=self.duplicate_frame_check.isChecked(),
            enable_dropped_sample_detection=self.dropped_sample_check.isChecked(),
            border_detection_mode=self.border_detection_combo.currentData() or "simple",
            simple_border_pixels=numbers["Border Pixels"],
            sophisticated_threshold=numbers["Brightness Threshold"],
            sophisticated_edge_sample_width=numbers["Edge Sample Width"],
            sophisticated_sample_frames=numbers["Sample Frames"],
            sophisticated_padding=numbers["Padding"],
            auto_retry_borders=self.auto_retry_borders_check.isChecked(),
            max_border_retries=numbers["Max Retries"],
            brng_skip_color_bars=self.brng_skip_colorbars_check.isChecked(),
            analysis_period_count=numbers["Number of Periods"],
            analysis_period_duration=numbers["Period Duration"],
        )

        outputs = OutputsConfig(
            access_file=self.access_file_check.isChecked(),
            report=self.report_check.isChecked(),
            qctools_ext=self.qctools_ext_combo.currentText(),
            frame_analysis=frame_analysis,
            access_file_trim_color_bars=self.access_trim_bars_check.isChecked(),
            access_file_crop_borders=self.access_crop_borders_check.isChecked(),
            access_file_crop_to_480=self.access_crop_to_480_check.isChecked(),
            access_file_exclude_flagged_audio=self.access_exclude_audio_check.isChecked(),
            save_console_pdf=self.save_console_pdf_check.isChecked(),
        )

        fixity = FixityConfig(
            check_fixity=self.fixity_checks['check_fixity'].isChecked(),
            validate_stream_fixity=self.fixity_checks['validate_stream_fixity'].isChecked(),
            embed_stream_fixity=self.fixity_checks['embed_stream_fixity'].isChecked(),
            output_fixity=self.fixity_checks['output_fixity'].isChecked(),
            overwrite_stream_fixity=self.fixity_checks['overwrite_stream_fixity'].isChecked(),
            checksum_algorithm=self.checksum_algorithm_combo.currentText(),
            stream_hash_algorithm=self.stream_hash_algorithm_combo.currentText()
        )

        def basic_tool(tool_name):
            return BasicToolConfig(
                check_tool=self.basic_tool_checks[tool_name]['check_tool'].isChecked(),
                run_tool=self.basic_tool_checks[tool_name]['run_tool'].isChecked()
            )

        tools = ToolsConfig(
            exiftool=basic_tool('exiftool'),
            ffprobe=basic_tool('ffprobe'),
            mediaconch=MediaConchConfig(
                mediaconch_policy=self.mediaconch_policy_combo.currentText(),
                run_mediaconch=self.mediaconch_run_check.isChecked()
            ),
            mediainfo=basic_tool('mediainfo'),
            mediatrace=basic_tool('mediatrace'),
            qctools=QCToolsConfig(
                run_tool=self.qctools_run_check.isChecked()
            ),
            qct_parse=QCTParseToolConfig(
                run_tool=self.qct_parse_run_check.isChecked(),
                barsDetection=self.bars_detection_check.isChecked(),
                evaluateBars=self.evaluate_bars_check.isChecked(),
                thumbExport=self.thumb_export_check.isChecked(),
                evaluateBarsReference=('smpte' if self.bars_ref_smpte_radio.isChecked()
                                       else 'both' if self.bars_ref_both_radio.isChecked()
                                       else 'detected'),
                audio_analysis=self.audio_analysis_check.isChecked(),
                detect_clamped_levels=self.clamped_levels_check.isChecked(),
                detect_chroma_phase_errors=self.chroma_phase_check.isChecked(),
                detect_tone_leak=self.tone_leak_check.isChecked(),
            ),
            mkvalidator=basic_tool('mkvalidator'),
            # Numeric CLAMS tuning is JSON-only; keep the loaded values
            clams_detection=replace(
                copy.deepcopy(self._base_clams),
                run_tool=self.clams_run_check.isChecked()
            ),
        )

        return ChecksProfile(
            name=name,
            description=self.description_input.toPlainText().strip(),
            validate_filename=self.validate_filename_check.isChecked(),
            outputs=outputs,
            fixity=fixity,
            tools=tools,
            video_file_extension=self.video_extension_combo.currentText(),
        )

    def on_save_clicked(self):
        """Handle save button click."""
        profile = self.get_profile_from_form()
        if profile:
            try:
                config_edit.save_custom_profile(profile)
                self.profile = profile
                self.accept()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to save profile: {str(e)}")
    
    def get_profile(self):
        """Return the created/edited profile."""
        return self.profile
    
    def on_theme_changed(self, palette):
        """Apply theme changes to this dialog."""
        # Apply the palette directly
        self.setPalette(palette)
        
        # Get the theme manager
        theme_manager = ThemeManager.instance()
        
        # Update all group boxes
        for group_box in self.findChildren(QGroupBox):
            theme_manager.style_groupbox(group_box)
        
        # Update all buttons
        theme_manager.style_buttons(self)
        
        # Force repaint
        self.update()
    
    def closeEvent(self, event):
        """Clean up theme connections before closing."""
        self.cleanup_theme_handling()
        super().closeEvent(event)


class ProfileSelectionDialog(QDialog, ThemeableMixin):
    """Dialog for selecting and managing custom profiles."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_profile = None
        self.setWindowTitle("Manage Profiles")
        self.setModal(True)
        self.setup_theme_handling()
        self.setMinimumSize(400, 500)
        
        layout = QVBoxLayout()
        
        # Profile list
        layout.addWidget(QLabel("Available Profiles:"))
        self.profile_list = QListWidget()
        self.profile_list.itemDoubleClicked.connect(self.on_apply_profile)
        layout.addWidget(self.profile_list)
        
        # Buttons
        button_layout = QHBoxLayout()
        
        # Left side buttons
        left_buttons = QHBoxLayout()
        create_button = QPushButton("Create New")
        create_button.clicked.connect(self.create_new_profile)
        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(self.edit_profile)
        delete_button = QPushButton("Delete")
        delete_button.clicked.connect(self.delete_profile)
        
        left_buttons.addWidget(create_button)
        left_buttons.addWidget(edit_button)
        left_buttons.addWidget(delete_button)
        
        # Right side buttons
        right_buttons = QHBoxLayout()
        apply_button = QPushButton("Apply Profile")
        apply_button.clicked.connect(self.on_apply_profile)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        
        right_buttons.addWidget(apply_button)
        right_buttons.addWidget(close_button)
        
        button_layout.addLayout(left_buttons)
        button_layout.addStretch()
        button_layout.addLayout(right_buttons)
        
        layout.addLayout(button_layout)
        
        # Import/Export button row
        io_layout = QHBoxLayout()
        
        export_button = QPushButton("Export Profile")
        export_button.setToolTip("Export the selected profile to a JSON file for sharing")
        export_button.clicked.connect(self.export_profile)
        
        import_button = QPushButton("Import Profile")
        import_button.setToolTip("Import a profile from a JSON file")
        import_button.clicked.connect(self.import_profile)
        
        io_layout.addWidget(export_button)
        io_layout.addWidget(import_button)
        io_layout.addStretch()
        
        layout.addLayout(io_layout)
        self.setLayout(layout)
        
        # Apply initial theme styling
        self._apply_initial_theme_styling()
        
        self.refresh_profile_list()
    
    def _apply_initial_theme_styling(self):
        """Apply initial theme styling using ThemeManager."""
        theme_manager = ThemeManager.instance()
        
        # Style all group boxes
        for group_box in self.findChildren(QGroupBox):
            theme_manager.style_groupbox(group_box)
        
        # Style all buttons (including the new import button)
        theme_manager.style_buttons(self)
    
    def refresh_profile_list(self):
        """Refresh the list of available profiles."""
        self.profile_list.clear()
        
        # Add built-in profiles
        builtin_profiles = ["Step 1 Profile", "Step 2 Profile", "All Off Profile", "Vendor Profile"]
        for profile_name in builtin_profiles:
            self.profile_list.addItem(f"[Built-in] {profile_name}")
        
        # Add custom profiles
        custom_profiles = config_edit.get_available_custom_profiles()
        for profile_name in custom_profiles:
            self.profile_list.addItem(f"[Custom] {profile_name}")
    
    def create_new_profile(self):
        """Create a new custom profile."""
        dialog = CustomProfileDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.refresh_profile_list()
    
    def edit_profile(self):
        """Edit the selected custom profile."""
        current_item = self.profile_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "No Selection", "Please select a profile to edit.")
            return
        
        item_text = current_item.text()
        if not item_text.startswith("[Custom]"):
            QMessageBox.warning(self, "Cannot Edit", "Built-in profiles cannot be edited.")
            return
        
        profile_name = item_text.replace("[Custom] ", "")
        profile = config_edit.get_custom_profile(profile_name)
        
        if profile:
            dialog = CustomProfileDialog(self, edit_profile=profile)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.refresh_profile_list()
    
    def delete_profile(self):
        """Delete the selected custom profile."""
        current_item = self.profile_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "No Selection", "Please select a profile to delete.")
            return
        
        item_text = current_item.text()
        if not item_text.startswith("[Custom]"):
            QMessageBox.warning(self, "Cannot Delete", "Built-in profiles cannot be deleted.")
            return
        
        profile_name = item_text.replace("[Custom] ", "")
        
        reply = QMessageBox.question(
            self, "Confirm Delete", 
            f"Are you sure you want to delete the profile '{profile_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            if config_edit.delete_custom_profile(profile_name):
                self.refresh_profile_list()
                QMessageBox.information(self, "Success", f"Profile '{profile_name}' deleted.")
    
    def export_profile(self):
        """Export the selected profile (built-in or custom) to a JSON file."""
        current_item = self.profile_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "No Selection", "Please select a profile to export.")
            return
        
        item_text = current_item.text()
        
        # Map from list display names to export data
        builtin_map = {
            "Step 1 Profile": config_edit.profile_step1,
            "Step 2 Profile": config_edit.profile_step2,
            "All Off Profile": config_edit.profile_allOff,
            "Vendor Profile": config_edit.profile_vendor,
        }
        
        if item_text.startswith("[Built-in]"):
            profile_name = item_text.replace("[Built-in] ", "")
            profile_dict = builtin_map.get(profile_name)
            if not profile_dict:
                QMessageBox.warning(self, "Export Error", f"Unknown built-in profile: {profile_name}")
                return
            
            # Wrap the built-in dict in the standard export format
            export_data = {
                'profiles_checks': {
                    'custom_profiles': {
                        profile_name: profile_dict
                    }
                }
            }
        elif item_text.startswith("[Custom]"):
            profile_name = item_text.replace("[Custom] ", "")
            
            # Use ConfigIO to build the export dict for custom profiles
            config_io = ConfigIO(config_mgr)
            export_data = config_io.export_single_profile('profiles_checks', profile_name)
            
            if not export_data:
                QMessageBox.warning(
                    self, "Export Failed",
                    f"Profile '{profile_name}' could not be found for export."
                )
                return
        else:
            return
        
        # Open save dialog
        safe_name = profile_name.replace(' ', '_').replace('/', '_')
        suggested_filename = f"av_spex_profile_{safe_name}.json"
        
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Export Profile",
            suggested_filename,
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not filepath:
            return  # User cancelled
        
        try:
            os.makedirs(
                os.path.dirname(filepath) if os.path.dirname(filepath) else '.', 
                exist_ok=True
            )
            with open(filepath, 'w') as f:
                json.dump(export_data, f, indent=2)
            
            logger.info(f"Exported profile '{profile_name}' to: {filepath}")
            QMessageBox.information(
                self, "Export Successful",
                f"Profile '{profile_name}' exported to:\n{filepath}"
            )
        except Exception as e:
            logger.error(f"Error exporting profile: {e}")
            QMessageBox.critical(
                self, "Export Error",
                f"Failed to export profile:\n{str(e)}"
            )
    
    def import_profile(self):
        """Import profile(s) from a JSON file."""
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Import Profile",
            "",
            "JSON Files (*.json);;All Files (*)"
        )
        
        if not filepath:
            return  # User cancelled
        
        try:
            config_io = ConfigIO(config_mgr)
            import_results = config_io.import_configs(filepath)
            
            # Refresh the profile list to show newly imported profiles
            self.refresh_profile_list()
            
            # Also refresh the main checks tab dropdown if accessible
            main_window = self.parent()
            if main_window:
                if hasattr(main_window, 'checks_tab') and main_window.checks_tab:
                    main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                if hasattr(main_window, 'config_widget') and main_window.config_widget:
                    main_window.config_widget.load_config_values()
            
            # Show result to user
            renamed = import_results.get('renamed_profiles', [])
            errors = import_results.get('errors', [])
            
            if errors:
                error_text = "\n".join(f"• {e}" for e in errors)
                if renamed:
                    # Partial success: some profiles imported, some errors
                    QMessageBox.warning(
                        self, "Import Partially Successful",
                        f"Some items were imported, but errors occurred:\n\n{error_text}"
                    )
                else:
                    QMessageBox.warning(
                        self, "Import Errors",
                        f"The file was read, but errors occurred during import:\n\n{error_text}"
                    )
            elif renamed:
                self._show_rename_notification(filepath, renamed)
            else:
                QMessageBox.information(
                    self, "Import Successful",
                    f"Profile(s) imported successfully from:\n{os.path.basename(filepath)}"
                )
        
        except json.JSONDecodeError:
            QMessageBox.critical(
                self, "Import Error",
                "The selected file is not valid JSON.\n"
                "Please select a valid AV Spex profile or config export file."
            )
        except Exception as e:
            logger.error(f"Error importing profile: {e}")
            QMessageBox.critical(
                self, "Import Error",
                f"Failed to import profile:\n{str(e)}"
            )
    
    def _show_rename_notification(self, file_path, renamed_profiles):
        """
        Show a notification dialog listing profiles that were renamed
        during import due to name collisions.
        
        Args:
            file_path: Path to the imported file (for the success message)
            renamed_profiles: List of (original_name, new_name) tuples
        """
        rename_lines = []
        for original, renamed in renamed_profiles:
            rename_lines.append(f'  "{original}"  →  "{renamed}"')
        
        rename_text = "\n".join(rename_lines)
        
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Information)
        msg.setWindowTitle("Import Successful — Profiles Renamed")
        msg.setText(
            f"Profile(s) imported from {os.path.basename(file_path)}.\n\n"
            "Some imported profiles were renamed to avoid "
            "conflicts with existing profiles:"
        )
        msg.setInformativeText(rename_text)
        msg.setDetailedText(
            "When an imported profile has the same name as an existing profile, "
            "AV Spex adds an '(imported)' suffix to the imported profile to "
            "preserve both versions.\n\n"
            "You can rename imported profiles by selecting them and clicking Edit."
        )
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()
    
    def on_apply_profile(self):
        """Apply the selected profile."""
        current_item = self.profile_list.currentItem()
        if not current_item:
            QMessageBox.warning(self, "No Selection", "Please select a profile to apply.")
            return
        
        item_text = current_item.text()
        
        try:
            if item_text.startswith("[Built-in]"):
                profile_name = item_text.replace("[Built-in] ", "")
                # Apply built-in profile
                if profile_name == "Step 1 Profile":
                    config_edit.apply_profile(config_edit.profile_step1)
                elif profile_name == "Step 2 Profile":
                    config_edit.apply_profile(config_edit.profile_step2)
                elif profile_name == "All Off Profile":
                    config_edit.apply_profile(config_edit.profile_allOff)
                elif profile_name == "Vendor Profile":
                    config_edit.apply_profile(config_edit.profile_vendor)
            else:
                profile_name = item_text.replace("[Custom] ", "")
                # Apply custom profile
                config_edit.apply_custom_profile(profile_name)
            
            # Update the GUI to reflect the new configuration
            main_window = self.parent()
            if main_window:
                # Refresh the config widget to show the new settings (handles most dropdowns)
                if hasattr(main_window, 'config_widget') and main_window.config_widget:
                    main_window.config_widget.load_config_values()
                
                # Handle the main profile dropdown separately
                if hasattr(main_window, 'checks_tab') and main_window.checks_tab:
                    main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                    
                    # Set the dropdown to show the applied profile
                    dropdown = main_window.checks_profile_dropdown
                    if item_text.startswith("[Built-in]"):
                        # For built-in profiles, use the simplified name
                        if profile_name == "Step 1 Profile":
                            dropdown.setCurrentText("Step 1")
                        elif profile_name == "Step 2 Profile":
                            dropdown.setCurrentText("Step 2")
                        elif profile_name == "All Off Profile":
                            dropdown.setCurrentText("All Off")
                        elif profile_name == "Vendor Profile":
                            dropdown.setCurrentText("Vendor")
                    else:
                        # For custom profiles, use the [Custom] prefix format
                        dropdown.setCurrentText(f"[Custom] {profile_name}")
            
            QMessageBox.information(self, "Success", f"Applied profile: {profile_name}")
            self.selected_profile = profile_name
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to apply profile: {str(e)}")
    
    def on_theme_changed(self, palette):
        """Apply theme changes to this dialog."""
        # Apply the palette directly
        self.setPalette(palette)
        
        # Get the theme manager
        theme_manager = ThemeManager.instance()

        # Update all group boxes
        for group_box in self.findChildren(QGroupBox):
            theme_manager.style_groupbox(group_box)
        
        # Update all buttons
        theme_manager.style_buttons(self)
        
        # Force repaint
        self.update()
    
    def closeEvent(self, event):
        """Clean up theme connections before closing."""
        self.cleanup_theme_handling()
        super().closeEvent(event)