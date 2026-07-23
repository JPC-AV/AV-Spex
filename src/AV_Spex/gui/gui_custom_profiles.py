from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QScrollArea, QPushButton, QComboBox, QCheckBox, QGroupBox,
    QMessageBox, QDialog, QTextEdit, QGridLayout, QListWidget,
    QFileDialog, QRadioButton, QButtonGroup
)
from PyQt6.QtCore import Qt

import json
import os

from AV_Spex.processing.processing_mgmt import setup_mediaconch_policy
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_io import ConfigIO
from AV_Spex.utils import config_edit
from AV_Spex.utils.config_setup import (
    ChecksProfile, OutputsConfig, FixityConfig, ToolsConfig,
    BasicToolConfig, QCToolsConfig, MediaConchConfig, QCTParseToolConfig,
    FrameAnalysisConfig
)
from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.utils.log_setup import logger

config_mgr = ConfigManager()

class CustomProfileDialog(QDialog, ThemeableMixin):
    def __init__(self, parent=None, edit_profile=None):
        super().__init__(parent)
        self.profile = None
        self.edit_mode = edit_profile is not None
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
        
        # Report (now using checkbox for boolean)
        outputs_layout.addWidget(QLabel("Report:"), 1, 0)
        self.report_check = QCheckBox()
        outputs_layout.addWidget(self.report_check, 1, 1)
        
        outputs_group.setLayout(outputs_layout)
        self.config_layout.addWidget(outputs_group)
    
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
        
        # Basic tools (exiftool, ffprobe, mediainfo, mediatrace)
        basic_tools = ["exiftool", "ffprobe", "mediainfo", "mediatrace"]
        self.basic_tool_checks = {}
        
        for tool in basic_tools:
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
        self.bars_ref_group.addButton(self.bars_ref_detected_radio)
        self.bars_ref_group.addButton(self.bars_ref_smpte_radio)
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

        # Thumb Export
        self.thumb_export_check = QCheckBox("Thumbnail Export")
        self.thumb_export_check.setStyleSheet("font-weight: bold;")
        thumb_export_desc = QLabel("Export thumbnails of failed frames for review")
        thumb_export_desc.setIndent(20)

        # Perform Audio Analysis
        self.audio_analysis_check = QCheckBox("Perform Audio Analysis")
        self.audio_analysis_check.setStyleSheet("font-weight: bold;")
        audio_analysis_desc = QLabel("Detect audio clipping, channel imbalance, audible timecode, and audio dropout")
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
        qct_parse_layout.addWidget(self.thumb_export_check)
        qct_parse_layout.addWidget(thumb_export_desc)
        qct_parse_layout.addWidget(self.audio_analysis_check)
        qct_parse_layout.addWidget(audio_analysis_desc)
        
        qct_parse_group.setLayout(qct_parse_layout)
        parent_layout.addWidget(qct_parse_group)
    
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
        threshold_desc = QLabel("0 = pure black, 255 = pure white")
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
        edge_desc = QLabel("Pixels to examine from each edge")
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
        frames_desc = QLabel("Number of frames to sample across the video")
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
        padding_desc = QLabel("Extra margin around detected borders")
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
        self.max_border_retries_input = QLineEdit("5")
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
        
        # Duration Limit
        duration_row = QHBoxLayout()
        duration_label = QLabel("Duration Limit (s):")
        duration_label.setStyleSheet("font-weight: bold;")
        self.brng_duration_input = QLineEdit("300")
        self.brng_duration_input.setMaximumWidth(60)
        duration_row.addWidget(duration_label)
        duration_row.addWidget(self.brng_duration_input)
        duration_row.addStretch()
        brng_layout.addLayout(duration_row)
        duration_desc = QLabel("Maximum duration to analyze for BRNG violations")
        duration_desc.setIndent(20)
        brng_layout.addWidget(duration_desc)
        
        # Skip Color Bars
        self.brng_skip_colorbars_check = QCheckBox("Skip Color Bars")
        self.brng_skip_colorbars_check.setStyleSheet("font-weight: bold;")
        skip_bars_desc = QLabel("Exclude color bar sections from BRNG analysis")
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
        
        # Duration
        duration_row = QHBoxLayout()
        duration_label = QLabel("Duration (s):")
        duration_label.setStyleSheet("font-weight: bold;")
        self.signalstats_duration_input = QLineEdit("60")
        self.signalstats_duration_input.setMaximumWidth(60)
        duration_row.addWidget(duration_label)
        duration_row.addWidget(self.signalstats_duration_input)
        duration_row.addStretch()
        signalstats_layout.addLayout(duration_row)
        duration_desc = QLabel("How long to run signalstats analysis")
        duration_desc.setIndent(20)
        signalstats_layout.addWidget(duration_desc)
        
        # Analysis Periods
        periods_row = QHBoxLayout()
        periods_label = QLabel("Analysis Periods:")
        periods_label.setStyleSheet("font-weight: bold;")
        self.signalstats_periods_input = QLineEdit("3")
        self.signalstats_periods_input.setMaximumWidth(60)
        periods_row.addWidget(periods_label)
        periods_row.addWidget(self.signalstats_periods_input)
        periods_row.addStretch()
        signalstats_layout.addLayout(periods_row)
        periods_desc = QLabel("Number of analysis periods to spread across video")
        periods_desc.setIndent(20)
        signalstats_layout.addWidget(periods_desc)
        
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
            
            # Load validate filename
            self.validate_filename_check.setChecked(current_config.validate_filename)
            
            # Load outputs (now booleans)
            self.access_file_check.setChecked(current_config.outputs.access_file)
            self.report_check.setChecked(current_config.outputs.report)
            
            # Load QCTools extension into the combo box
            qctools_ext = getattr(current_config.outputs, 'qctools_ext', 'qctools.xml.gz')
            ext_index = self.qctools_ext_combo.findText(qctools_ext)
            if ext_index >= 0:
                self.qctools_ext_combo.setCurrentIndex(ext_index)
            
            # Load frame analysis settings
            if hasattr(current_config.outputs, 'frame_analysis'):
                fa = current_config.outputs.frame_analysis
                
                # Bitplane check
                self.enable_bitplane_check_check.setChecked(bool(getattr(fa, 'enable_bitplane_check', True)))

                # Border detection
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
                self.max_border_retries_input.setText(str(getattr(fa, 'max_border_retries', 3)))
                
                # BRNG analysis
                self.enable_brng_analysis_check.setChecked(bool(fa.enable_brng_analysis))
                self.brng_duration_input.setText(str(fa.brng_duration_limit))
                self.brng_skip_colorbars_check.setChecked(bool(fa.brng_skip_color_bars))
                
                # Signalstats
                self.enable_signalstats_check.setChecked(bool(fa.enable_signalstats))
                self.signalstats_duration_input.setText(str(fa.signalstats_duration))
                self.signalstats_periods_input.setText(str(getattr(fa, 'signalstats_periods', 3)))
                    
            # Load fixity (now booleans)
            self.fixity_checks['check_fixity'].setChecked(current_config.fixity.check_fixity)
            self.fixity_checks['validate_stream_fixity'].setChecked(current_config.fixity.validate_stream_fixity)
            self.fixity_checks['embed_stream_fixity'].setChecked(current_config.fixity.embed_stream_fixity)
            self.fixity_checks['output_fixity'].setChecked(current_config.fixity.output_fixity)
            self.fixity_checks['overwrite_stream_fixity'].setChecked(current_config.fixity.overwrite_stream_fixity)
            
            # Load checksum algorithms
            algorithm = getattr(current_config.fixity, 'checksum_algorithm', 'md5')
            index = self.checksum_algorithm_combo.findText(algorithm)
            if index >= 0:
                self.checksum_algorithm_combo.setCurrentIndex(index)
            
            stream_algorithm = getattr(current_config.fixity, 'stream_hash_algorithm', 'md5')
            stream_index = self.stream_hash_algorithm_combo.findText(stream_algorithm)
            if stream_index >= 0:
                self.stream_hash_algorithm_combo.setCurrentIndex(stream_index)
            
            # Load basic tools (now booleans)
            for tool_name in self.basic_tool_checks:
                tool_config = getattr(current_config.tools, tool_name)
                self.basic_tool_checks[tool_name]['check_tool'].setChecked(tool_config.check_tool)
                self.basic_tool_checks[tool_name]['run_tool'].setChecked(tool_config.run_tool)
            
            # Load MediaConch
            if current_config.tools.mediaconch.mediaconch_policy:
                # Check if the policy exists in the dropdown, if so select it
                index = self.mediaconch_policy_combo.findText(current_config.tools.mediaconch.mediaconch_policy)
                if index >= 0:
                    self.mediaconch_policy_combo.setCurrentIndex(index)
                else:
                    # If policy doesn't exist in dropdown, add it and select it
                    self.mediaconch_policy_combo.addItem(current_config.tools.mediaconch.mediaconch_policy)
                    self.mediaconch_policy_combo.setCurrentText(current_config.tools.mediaconch.mediaconch_policy)
            
            self.mediaconch_run_check.setChecked(current_config.tools.mediaconch.run_mediaconch)
            
            # Load QCTools (now boolean)
            self.qctools_run_check.setChecked(current_config.tools.qctools.run_tool)
            
            # Load QCT Parse (now boolean for run_tool)
            qct_config = current_config.tools.qct_parse
            self.qct_parse_run_check.setChecked(qct_config.run_tool)
            self.bars_detection_check.setChecked(qct_config.barsDetection)
            self.evaluate_bars_check.setChecked(qct_config.evaluateBars)
            self.thumb_export_check.setChecked(qct_config.thumbExport)
            bars_ref = getattr(qct_config, 'evaluateBarsReference', 'detected')
            self.bars_ref_smpte_radio.setChecked(bars_ref == 'smpte')
            self.bars_ref_detected_radio.setChecked(bars_ref != 'smpte')
            self.audio_analysis_check.setChecked(getattr(qct_config, 'audio_analysis', False))

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load current config: {str(e)}")
    
    def load_existing_profile(self, profile):
        """Load an existing profile into the dialog."""
        # Load profile info
        self.name_input.setText(profile.name)
        self.description_input.setPlainText(profile.description)
        
        # Load validate filename
        self.validate_filename_check.setChecked(profile.validate_filename)
        
        # Load outputs (now booleans)
        self.access_file_check.setChecked(profile.outputs.access_file)
        self.report_check.setChecked(profile.outputs.report)
        
        # Load QCTools extension into the combo box
        qctools_ext = getattr(profile.outputs, 'qctools_ext', 'qctools.xml.gz')
        ext_index = self.qctools_ext_combo.findText(qctools_ext)
        if ext_index >= 0:
            self.qctools_ext_combo.setCurrentIndex(ext_index)

        # Load frame analysis if it exists
        if hasattr(profile.outputs, 'frame_analysis'):
            fa = profile.outputs.frame_analysis
            
            # Bitplane check
            self.enable_bitplane_check_check.setChecked(bool(getattr(fa, 'enable_bitplane_check', True)))

            # Border detection
            self.enable_border_detection_check.setChecked(bool(getattr(fa, 'enable_border_detection', False)))
            mode_index = self.border_detection_combo.findData(getattr(fa, 'border_detection_mode', 'simple'))
            if mode_index >= 0:
                self.border_detection_combo.setCurrentIndex(mode_index)
            self.simple_border_pixels_input.setText(str(getattr(fa, 'simple_border_pixels', 25)))
            self.soph_threshold_input.setText(str(getattr(fa, 'sophisticated_threshold', 10)))
            self.soph_edge_width_input.setText(str(getattr(fa, 'sophisticated_edge_sample_width', 100)))
            self.soph_sample_frames_input.setText(str(getattr(fa, 'sophisticated_sample_frames', 30)))
            self.soph_padding_input.setText(str(getattr(fa, 'sophisticated_padding', 5)))
            self.auto_retry_borders_check.setChecked(bool(getattr(fa, 'auto_retry_borders', False)))
            self.max_border_retries_input.setText(str(getattr(fa, 'max_border_retries', 3)))
            
            # BRNG analysis
            self.enable_brng_analysis_check.setChecked(bool(getattr(fa, 'enable_brng_analysis', False)))
            self.brng_duration_input.setText(str(getattr(fa, 'brng_duration_limit', 300)))
            self.brng_skip_colorbars_check.setChecked(bool(getattr(fa, 'brng_skip_color_bars', False)))
            
            # Signalstats
            self.enable_signalstats_check.setChecked(bool(getattr(fa, 'enable_signalstats', False)))
            self.signalstats_duration_input.setText(str(getattr(fa, 'signalstats_duration', 60)))
            self.signalstats_periods_input.setText(str(getattr(fa, 'signalstats_periods', 3)))
        
        # Load fixity (now booleans)
        self.fixity_checks['check_fixity'].setChecked(profile.fixity.check_fixity)
        self.fixity_checks['validate_stream_fixity'].setChecked(profile.fixity.validate_stream_fixity)
        self.fixity_checks['embed_stream_fixity'].setChecked(profile.fixity.embed_stream_fixity)
        self.fixity_checks['output_fixity'].setChecked(profile.fixity.output_fixity)
        self.fixity_checks['overwrite_stream_fixity'].setChecked(profile.fixity.overwrite_stream_fixity)
        
        # Load checksum algorithms
        algorithm = getattr(profile.fixity, 'checksum_algorithm', 'md5')
        index = self.checksum_algorithm_combo.findText(algorithm)
        if index >= 0:
            self.checksum_algorithm_combo.setCurrentIndex(index)
        
        stream_algorithm = getattr(profile.fixity, 'stream_hash_algorithm', 'md5')
        stream_index = self.stream_hash_algorithm_combo.findText(stream_algorithm)
        if stream_index >= 0:
            self.stream_hash_algorithm_combo.setCurrentIndex(stream_index)
        
        # Load basic tools (now booleans)
        for tool_name in self.basic_tool_checks:
            tool_config = getattr(profile.tools, tool_name)
            self.basic_tool_checks[tool_name]['check_tool'].setChecked(tool_config.check_tool)
            self.basic_tool_checks[tool_name]['run_tool'].setChecked(tool_config.run_tool)
        
        # Load MediaConch
        if profile.tools.mediaconch.mediaconch_policy:
            # Check if the policy exists in the dropdown, if so select it
            index = self.mediaconch_policy_combo.findText(profile.tools.mediaconch.mediaconch_policy)
            if index >= 0:
                self.mediaconch_policy_combo.setCurrentIndex(index)
            else:
                # If policy doesn't exist in dropdown, add it and select it
                self.mediaconch_policy_combo.addItem(profile.tools.mediaconch.mediaconch_policy)
                self.mediaconch_policy_combo.setCurrentText(profile.tools.mediaconch.mediaconch_policy)
        
        self.mediaconch_run_check.setChecked(profile.tools.mediaconch.run_mediaconch)
        
        # Load QCTools (now boolean)
        self.qctools_run_check.setChecked(profile.tools.qctools.run_tool)
        
        # Load QCT Parse (now boolean for run_tool)
        qct_config = profile.tools.qct_parse
        self.qct_parse_run_check.setChecked(qct_config.run_tool)
        self.bars_detection_check.setChecked(qct_config.barsDetection)
        self.evaluate_bars_check.setChecked(qct_config.evaluateBars)
        self.thumb_export_check.setChecked(qct_config.thumbExport)
        bars_ref = getattr(qct_config, 'evaluateBarsReference', 'detected')
        self.bars_ref_smpte_radio.setChecked(bars_ref == 'smpte')
        self.bars_ref_detected_radio.setChecked(bars_ref != 'smpte')
        self.audio_analysis_check.setChecked(getattr(qct_config, 'audio_analysis', False))

    def get_profile_from_form(self):
        """Create a ChecksProfile from the form data."""
        # Validate required fields
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Validation Error", "Profile name is required.")
            return None
        
        # Create frame analysis config with all parameters
        frame_analysis = FrameAnalysisConfig(
            enable_bitplane_check=self.enable_bitplane_check_check.isChecked(),
            enable_border_detection=self.enable_border_detection_check.isChecked(),
            enable_brng_analysis=self.enable_brng_analysis_check.isChecked(),
            enable_signalstats=self.enable_signalstats_check.isChecked(),
            border_detection_mode=self.border_detection_combo.currentData() or "simple",
            simple_border_pixels=int(self.simple_border_pixels_input.text() or 25),
            sophisticated_threshold=int(self.soph_threshold_input.text() or 10),
            sophisticated_edge_sample_width=int(self.soph_edge_width_input.text() or 100),
            sophisticated_sample_frames=int(self.soph_sample_frames_input.text() or 30),
            sophisticated_padding=int(self.soph_padding_input.text() or 5),
            auto_retry_borders=self.auto_retry_borders_check.isChecked(),
            max_border_retries=int(self.max_border_retries_input.text() or 3),
            brng_duration_limit=int(self.brng_duration_input.text() or 300),
            brng_skip_color_bars=self.brng_skip_colorbars_check.isChecked(),
            signalstats_duration=int(self.signalstats_duration_input.text() or 60),
            signalstats_periods=int(self.signalstats_periods_input.text() or 3)
        )
        
        # Create outputs config (now with booleans)
        outputs = OutputsConfig(
            access_file=self.access_file_check.isChecked(),
            report=self.report_check.isChecked(),
            qctools_ext=self.qctools_ext_combo.currentText(),
            frame_analysis=frame_analysis
        )
        
        # Create fixity config (now with booleans)
        fixity = FixityConfig(
            check_fixity=self.fixity_checks['check_fixity'].isChecked(),
            validate_stream_fixity=self.fixity_checks['validate_stream_fixity'].isChecked(),
            embed_stream_fixity=self.fixity_checks['embed_stream_fixity'].isChecked(),
            output_fixity=self.fixity_checks['output_fixity'].isChecked(),
            overwrite_stream_fixity=self.fixity_checks['overwrite_stream_fixity'].isChecked(),
            checksum_algorithm=self.checksum_algorithm_combo.currentText(),
            stream_hash_algorithm=self.stream_hash_algorithm_combo.currentText()
        )
        
        # Create tools config (now with booleans)
        tools = ToolsConfig(
            exiftool=BasicToolConfig(
                check_tool=self.basic_tool_checks['exiftool']['check_tool'].isChecked(),
                run_tool=self.basic_tool_checks['exiftool']['run_tool'].isChecked()
            ),
            ffprobe=BasicToolConfig(
                check_tool=self.basic_tool_checks['ffprobe']['check_tool'].isChecked(),
                run_tool=self.basic_tool_checks['ffprobe']['run_tool'].isChecked()
            ),
            mediaconch=MediaConchConfig(
                mediaconch_policy=self.mediaconch_policy_combo.currentText(),
                run_mediaconch=self.mediaconch_run_check.isChecked()
            ),
            mediainfo=BasicToolConfig(
                check_tool=self.basic_tool_checks['mediainfo']['check_tool'].isChecked(),
                run_tool=self.basic_tool_checks['mediainfo']['run_tool'].isChecked()
            ),
            mediatrace=BasicToolConfig(
                check_tool=self.basic_tool_checks['mediatrace']['check_tool'].isChecked(),
                run_tool=self.basic_tool_checks['mediatrace']['run_tool'].isChecked()
            ),
            qctools=QCToolsConfig(
                run_tool=self.qctools_run_check.isChecked()
            ),
            qct_parse=QCTParseToolConfig(
                run_tool=self.qct_parse_run_check.isChecked(),
                barsDetection=self.bars_detection_check.isChecked(),
                evaluateBars=self.evaluate_bars_check.isChecked(),
                thumbExport=self.thumb_export_check.isChecked(),
                evaluateBarsReference=('smpte' if self.bars_ref_smpte_radio.isChecked() else 'detected'),
                audio_analysis=self.audio_analysis_check.isChecked()
            )
        )
        
        # Create and return the profile
        return ChecksProfile(
            name=name,
            description=self.description_input.toPlainText().strip(),
            validate_filename=self.validate_filename_check.isChecked(),
            outputs=outputs,
            fixity=fixity,
            tools=tools
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