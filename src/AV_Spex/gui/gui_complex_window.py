from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QCheckBox, QLineEdit,
    QLabel, QComboBox, QStyle, QStyleOptionButton, QRadioButton, QButtonGroup
)
from PyQt6.QtCore import Qt

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.utils.config_manager import ConfigManager

config_mgr = ConfigManager()

class ComplexWindow(QWidget, ThemeableMixin):
    """Configuration window for complex analysis settings.

    Sections are grouped by what is being checked (report generation,
    color bars & tone, video signal, audio) rather than by which tool
    implements the check. Each option carries a short description label
    beneath it, matching the other tabs.

    qct-parse's run_tool is implicit here: turning on any qct-parse-backed
    check enables it, turning off all of them disables it (mirrors the
    CLI's --enable-* auto-enable guardrails). The Checks tab still exposes
    run_tool directly; both write the full flag set so the config stays
    coherent.
    """

    INDENT = 24
    DESC_INDENT = 20

    def __init__(self):
        super().__init__()
        self.is_loading = False

        # Initialize themed_group_boxes before setup_theme_handling
        self.themed_group_boxes = {}

        # Setup theme handling
        self.setup_theme_handling()

        # Setup UI and load config
        self.setup_ui()
        self.load_config_values()

    def setup_ui(self):
        """Create fixed layout structure"""
        main_layout = QVBoxLayout(self)

        self.setup_report_generation_section(main_layout)
        self.setup_colorbars_section(main_layout)
        self.setup_video_signal_section(main_layout)
        self.setup_audio_section(main_layout)
        main_layout.addStretch()
        self.connect_signals()

    # --- small layout helpers -------------------------------------------

    def _make_checkbox(self, text):
        cb = QCheckBox(text)
        cb.setStyleSheet("font-weight: bold;")
        return cb

    def _desc_label(self, text, extra_indent=0):
        """Return a wrapped description label indented under its option.

        Indented sub-options get the small gray style used for dependent
        options on the Checks tab; top-level options keep the default size.
        """
        label = QLabel(text)
        label.setIndent(self.DESC_INDENT + extra_indent)
        label.setWordWrap(True)
        if extra_indent:
            label.setStyleSheet("color: gray; font-size: 10px;")
            label.setProperty("small_desc", True)
        return label

    def _add_option(self, layout, option, desc, spacing=None):
        """Add an option (checkbox or row layout) with its description
        directly beneath it, tighter than the spacing between options.

        Small gray sub-option descriptions hug their option (0px); the
        full-size descriptions get a slightly larger gap (3px). Pass
        `spacing` to override (e.g. rows whose height is padded out by a
        taller widget like a combo box need less)."""
        pair = QVBoxLayout()
        pair.setSpacing(0)
        pair.setContentsMargins(0, 0, 0, 0)
        if isinstance(option, QHBoxLayout):
            pair.addLayout(option)
        else:
            pair.addWidget(option)
        if spacing is None:
            spacing = 0 if desc.property("small_desc") else 3
        if spacing:
            # An explicit spacer item rather than setSpacing so the gap can
            # go negative — combo boxes carry empty pixels at the bottom of
            # their widget rect that layout spacing alone can't compensate
            pair.addSpacing(spacing)
        pair.addWidget(desc)
        layout.addLayout(pair)

    def _indent_row(self, widget, indent=None):
        """Return an HBox with the widget indented under its parent option.

        `indent` defaults to one level (self.INDENT); pass a larger value for
        controls nested a further level down (e.g. self.INDENT * 2)."""
        row = QHBoxLayout()
        row.setSpacing(6)  # explicit: nested rows inherit _add_option's 0 spacing
        row.addSpacing(self.INDENT if indent is None else indent)
        row.addWidget(widget)
        row.addStretch()
        return row

    def _checkbox_text_offset(self):
        """Width of a checkbox indicator + its label spacing, so text-only
        rows can line up with the text of indented checkboxes. The style
        must be queried with a checkbox option/widget — without that
        context it reports smaller metrics."""
        cb = QCheckBox()
        opt = QStyleOptionButton()
        opt.initFrom(cb)
        style = cb.style()
        return (style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, opt, cb)
                + style.pixelMetric(QStyle.PixelMetric.PM_CheckBoxLabelSpacing, opt, cb))

    def _param_row(self, label_text, line_edit):
        """Return an indented label + input row for a numeric parameter.
        The label is aligned with the text of indented checkboxes."""
        row = QHBoxLayout()
        row.setSpacing(6)  # explicit: nested rows inherit _add_option's 0 spacing
        row.addSpacing(self.INDENT + self._checkbox_text_offset())
        label = QLabel(label_text)
        label.setStyleSheet("font-weight: bold;")
        line_edit.setMaximumWidth(60)
        row.addWidget(label)
        row.addWidget(line_edit)
        row.addStretch()
        return row

    # --- QCTools Report section -----------------------------------------

    def setup_report_generation_section(self, main_layout):
        """QCTools report generation (the sidecar most other checks read)."""
        theme_manager = ThemeManager.instance()

        self.qctools_group = QGroupBox("QCTools Report")
        theme_manager.style_groupbox(self.qctools_group, "top center")
        self.themed_group_boxes['qctools'] = self.qctools_group

        layout = QVBoxLayout()

        self.run_qctools_cb = self._make_checkbox("Run QCTools")
        self._add_option(layout, self.run_qctools_cb, self._desc_label(
            "Run QCTools on the input video file to generate the per-frame "
            "report that the color bars, video signal, and audio checks read. "
            "An existing report is reused instead of re-running."))

        ext_row = QHBoxLayout()
        ext_row.setSpacing(6)
        ext_label = QLabel("File Extension:")
        ext_label.setStyleSheet("font-weight: bold;")
        self.qctools_ext_combo = QComboBox()
        self.qctools_ext_combo.addItems(["qctools.xml.gz", "qctools.mkv"])
        self.qctools_ext_combo.setMinimumWidth(160)
        ext_row.addWidget(ext_label)
        ext_row.addWidget(self.qctools_ext_combo)
        ext_row.addStretch()
        layout.addSpacing(6)
        # spacing=-3: see the Border Detection row — combo boxes carry empty
        # pixels below their visible control
        self._add_option(layout, ext_row, self._desc_label(
            "Set the extension for QCTools output files"),
            spacing=-3)

        self.qctools_group.setLayout(layout)
        main_layout.addWidget(self.qctools_group)

    # --- Color Bars & Tone section ----------------------------------------

    def setup_colorbars_section(self, main_layout):
        """Color bars detection/evaluation (qct-parse) and CLAMS bars + tone."""
        theme_manager = ThemeManager.instance()

        self.colorbars_group = QGroupBox("Color Bars && Tone")
        theme_manager.style_groupbox(self.colorbars_group, "top center")
        self.themed_group_boxes['colorbars'] = self.colorbars_group

        layout = QVBoxLayout()

        self.bars_detection_cb = self._make_checkbox("Detect Color Bars")
        self._add_option(layout, self.bars_detection_cb, self._desc_label(
            "Detect color bars in the video content (via qct-parse). The "
            "detected section is used to skip bars in BRNG analysis and trim "
            "them from the access file."))

        self.evaluate_bars_cb = self._make_checkbox("Evaluate Color Bars")
        self._add_option(layout, self._indent_row(self.evaluate_bars_cb), self._desc_label(
            "Compare video content against reference color bar values for validation",
            extra_indent=self.INDENT))

        # Reference selection: what the evaluation grades content against.
        # Nested a further level under Evaluate Color Bars.
        self.bars_ref_label = QLabel("Compare against:")
        self.bars_ref_label.setIndent(self.DESC_INDENT)
        layout.addLayout(self._indent_row(self.bars_ref_label, self.INDENT * 2))

        self.bars_ref_group = QButtonGroup(self)
        self.bars_ref_detected_radio = QRadioButton("Bars detected in this video")
        self.bars_ref_smpte_radio = QRadioButton("Standard SMPTE values")
        self.bars_ref_group.addButton(self.bars_ref_detected_radio)
        self.bars_ref_group.addButton(self.bars_ref_smpte_radio)
        self.bars_ref_detected_radio.setChecked(True)

        self._add_option(layout, self._indent_row(self.bars_ref_detected_radio, self.INDENT * 2),
            self._desc_label(
                "Uses levels measured from this file's own color bars. If no "
                "bars are found, standard SMPTE values are used as a fallback.",
                extra_indent=self.INDENT * 2))
        self._add_option(layout, self._indent_row(self.bars_ref_smpte_radio, self.INDENT * 2),
            self._desc_label(
                "Always uses standard SMPTE color bar values, ignoring any "
                "bars in the video.",
                extra_indent=self.INDENT * 2))

        self.thumb_export_cb = self._make_checkbox("Export Thumbnails")
        self._add_option(layout, self._indent_row(self.thumb_export_cb), self._desc_label(
            "Export thumbnails of failed frames for review",
            extra_indent=self.INDENT))

        self.run_clams_detection_cb = self._make_checkbox("CLAMS Bars + Tone Detection")
        self._add_option(layout, self.run_clams_detection_cb, self._desc_label(
            "Run the CLAMS SSIM-based SMPTE bars detector and the "
            "cross-correlation tone detector together. Detected regions "
            "guide qct-parse's bars scans, and the head-bars end time used "
            "for BRNG-skip and access-file trim is settled by consensus "
            "between the two detectors: where they disagree, SSIM decides, "
            "and a head claim CLAMS contradicts is dropped rather than "
            "trimmed on."))

        self.colorbars_group.setLayout(layout)
        main_layout.addWidget(self.colorbars_group)

    # --- Video Signal Checks section --------------------------------------

    def setup_video_signal_section(self, main_layout):
        """Video signal checks: qct-parse detections and frame analysis."""
        theme_manager = ThemeManager.instance()

        self.video_signal_group = QGroupBox("Video Signal Checks")
        theme_manager.style_groupbox(self.video_signal_group, "top center")
        self.themed_group_boxes['video_signal'] = self.video_signal_group

        layout = QVBoxLayout()

        self.detect_clamped_levels_cb = self._make_checkbox("Detect Clamped Levels")
        self._add_option(layout, self.detect_clamped_levels_cb, self._desc_label(
            "Detect broadcast-range level clamping from the analog-to-digital converter"))

        self.detect_chroma_phase_errors_cb = self._make_checkbox("Detect Chroma Phase Errors")
        self._add_option(layout, self.detect_chroma_phase_errors_cb, self._desc_label(
            "Detect tape tracking artifacts where chroma collapses toward cyan or magenta"))

        self.enable_duplicate_frame_cb = self._make_checkbox("Duplicate Frame Detection")
        self._add_option(layout, self.enable_duplicate_frame_cb, self._desc_label(
            "Detect runs of repeated frames likely caused by TBC or framesync "
            "errors, using QCTools YDIF/UDIF/VDIF to find candidate freezes "
            "and OpenCV to verify them"))

        self.enable_bitplane_check_cb = self._make_checkbox("Bitplane Check")
        self._add_option(layout, self.enable_bitplane_check_cb, self._desc_label(
            "Verify that the 9th and 10th bits of 10-bit video contain data. "
            "Some TBC/framesync devices truncate these bits, producing "
            "effectively 8-bit video."))

        # Border detection: checkbox + mode on one row, params below
        border_row = QHBoxLayout()
        border_row.setSpacing(6)
        self.enable_border_detection_cb = self._make_checkbox("Border Detection")
        border_mode_label = QLabel("Mode:")
        border_mode_label.setStyleSheet("font-weight: bold;")
        self.border_mode_combo = QComboBox()
        self.border_mode_combo.addItem("Simple", "simple")
        self.border_mode_combo.addItem("Sophisticated", "sophisticated")
        border_row.addWidget(self.enable_border_detection_cb)
        border_row.addSpacing(12)
        border_row.addWidget(border_mode_label)
        border_row.addWidget(self.border_mode_combo)
        border_row.addStretch()
        # The combo box pads the row's height, so pull this description up
        # to look even with the plain checkbox options
        self._add_option(layout, border_row, self._desc_label(
            "Detect and crop blanking borders from the video. Simple mode "
            "crops a fixed number of pixels; Sophisticated mode detects "
            "borders via edge analysis. Required for Signalstats."),
            spacing=-3)

        # Simple border parameters
        self.simple_params_widget = QWidget()
        simple_params_layout = QVBoxLayout(self.simple_params_widget)
        simple_params_layout.setContentsMargins(0, 0, 0, 0)
        self.simple_border_pixels_input = QLineEdit("25")
        self._add_option(simple_params_layout, self._param_row(
            "Border Pixels:", self.simple_border_pixels_input),
            self._desc_label(
                "Fixed number of pixels to crop from each edge",
                extra_indent=self.INDENT))
        layout.addWidget(self.simple_params_widget)

        # Sophisticated border parameters
        self.sophisticated_params_widget = QWidget()
        soph_layout = QVBoxLayout(self.sophisticated_params_widget)
        soph_layout.setContentsMargins(0, 0, 0, 0)

        self.soph_threshold_input = QLineEdit("10")
        self._add_option(soph_layout, self._param_row(
            "Brightness Threshold:", self.soph_threshold_input),
            self._desc_label(
                "0 = pure black, 255 = pure white", extra_indent=self.INDENT))

        self.soph_edge_width_input = QLineEdit("100")
        self._add_option(soph_layout, self._param_row(
            "Edge Sample Width:", self.soph_edge_width_input),
            self._desc_label(
                "Pixels to examine from each edge", extra_indent=self.INDENT))

        self.soph_sample_frames_input = QLineEdit("30")
        self._add_option(soph_layout, self._param_row(
            "Sample Frames:", self.soph_sample_frames_input),
            self._desc_label(
                "Number of frames to sample across the video",
                extra_indent=self.INDENT))

        self.soph_padding_input = QLineEdit("5")
        self._add_option(soph_layout, self._param_row(
            "Padding:", self.soph_padding_input),
            self._desc_label(
                "Extra margin around detected borders",
                extra_indent=self.INDENT))

        self.auto_retry_borders_cb = self._make_checkbox(
            "Auto-retry if BRNG finds edge artifacts")
        self._add_option(soph_layout, self._indent_row(self.auto_retry_borders_cb),
            self._desc_label(
                "Automatically adjusts borders if edge artifacts are found",
                extra_indent=self.INDENT))

        self.max_border_retries_input = QLineEdit("5")
        self._add_option(soph_layout, self._param_row(
            "Max Retries:", self.max_border_retries_input),
            self._desc_label(
                "Maximum number of border adjustment attempts",
                extra_indent=self.INDENT))

        layout.addWidget(self.sophisticated_params_widget)
        self.sophisticated_params_widget.setVisible(False)

        # Signalstats
        self.enable_signalstats_cb = self._make_checkbox("Signalstats Analysis")
        self._add_option(layout, self.enable_signalstats_cb, self._desc_label(
            "Enhanced FFprobe signalstats over the detected active picture "
            "area (requires Border Detection)"))

        # BRNG analysis
        self.enable_brng_analysis_cb = self._make_checkbox("BRNG Analysis")
        self._add_option(layout, self.enable_brng_analysis_cb, self._desc_label(
            "Analyze broadcast range violations in the active picture area"))

        self.brng_duration_input = QLineEdit("300")
        self._add_option(layout, self._param_row(
            "Duration Limit (s):", self.brng_duration_input),
            self._desc_label(
                "Maximum duration to analyze for BRNG violations",
                extra_indent=self.INDENT))

        self.brng_skip_colorbars_cb = self._make_checkbox("Skip Color Bars")
        self._add_option(layout, self._indent_row(self.brng_skip_colorbars_cb),
            self._desc_label(
                "Exclude color bar sections from BRNG analysis",
                extra_indent=self.INDENT))

        # Shared analysis periods (signalstats + BRNG)
        periods_row = QHBoxLayout()
        periods_row.setSpacing(6)
        periods_label = QLabel("Analysis Periods:")
        periods_label.setStyleSheet("font-weight: bold;")
        periods_count_label = QLabel("Count:")
        self.analysis_period_count_input = QLineEdit("3")
        self.analysis_period_count_input.setMaximumWidth(60)
        periods_duration_label = QLabel("Duration (s):")
        self.analysis_period_duration_input = QLineEdit("60")
        self.analysis_period_duration_input.setMaximumWidth(60)
        periods_row.addWidget(periods_label)
        periods_row.addWidget(periods_count_label)
        periods_row.addWidget(self.analysis_period_count_input)
        periods_row.addSpacing(8)
        periods_row.addWidget(periods_duration_label)
        periods_row.addWidget(self.analysis_period_duration_input)
        periods_row.addStretch()
        layout.addSpacing(6)
        self._add_option(layout, periods_row, self._desc_label(
            "Number and length of the time windows sampled across the video "
            "for Signalstats and BRNG analysis"))

        self.video_signal_group.setLayout(layout)
        main_layout.addWidget(self.video_signal_group)

        # Connect enable/disable logic
        self.enable_border_detection_cb.stateChanged.connect(self.update_border_detection_visibility)
        self.border_mode_combo.currentIndexChanged.connect(self.update_border_detection_visibility)

    # --- Audio Checks section ---------------------------------------------

    def setup_audio_section(self, main_layout):
        """Audio checks: qct-parse audio analysis and dropped sample detection."""
        theme_manager = ThemeManager.instance()

        self.audio_group = QGroupBox("Audio Checks")
        theme_manager.style_groupbox(self.audio_group, "top center")
        self.themed_group_boxes['audio'] = self.audio_group

        layout = QVBoxLayout()

        self.audio_analysis_cb = self._make_checkbox("Audio Analysis")
        self._add_option(layout, self.audio_analysis_cb, self._desc_label(
            "Detect audio clipping, channel imbalance, identical (dual mono) "
            "channels, audible timecode (LTC), and audio dropout"))

        self.detect_tone_leak_cb = self._make_checkbox("Tone Leak Detection")
        self._add_option(layout, self.detect_tone_leak_cb, self._desc_label(
            "Detect a 1 kHz reference tone leaking from the transfer chain, "
            "heard as a faint high-pitched whine or squeak in quiet passages"))

        self.enable_dropped_sample_cb = self._make_checkbox("Dropped Sample Detection")
        self._add_option(layout, self.enable_dropped_sample_cb, self._desc_label(
            "Detect potential audio sample drops from TBC/framesync or ADC "
            "devices. Generates a spectrogram to identify audible pops and "
            "compares audio/video durations."))

        self.audio_group.setLayout(layout)
        main_layout.addWidget(self.audio_group)

    # --- visibility / dependency logic --------------------------------------

    def update_border_detection_visibility(self):
        """Update visibility of border detection components"""
        enabled = self.enable_border_detection_cb.isChecked()
        is_sophisticated = self.border_mode_combo.currentData() == "sophisticated"

        # Show/hide mode-specific parameters
        self.border_mode_combo.setEnabled(enabled)
        self.simple_params_widget.setVisible(enabled and not is_sophisticated)
        self.sophisticated_params_widget.setVisible(enabled and is_sophisticated)

        # Update signalstats availability
        self.update_signalstats_dependency()

    def update_signalstats_dependency(self):
        """Update signalstats checkbox availability based on border detection"""
        border_enabled = self.enable_border_detection_cb.isChecked()

        if not border_enabled:
            self.enable_signalstats_cb.setChecked(False)
            self.enable_signalstats_cb.setEnabled(False)
        else:
            self.enable_signalstats_cb.setEnabled(True)

    def _apply_bars_dependencies(self):
        """Evaluate Bars and Export Thumbnails depend on Detect Color Bars;
        the bars-reference radios depend on Evaluate Color Bars."""
        bars_on = self.bars_detection_cb.isChecked()
        for cb in (self.evaluate_bars_cb, self.thumb_export_cb):
            if not bars_on:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
            cb.setEnabled(bars_on)

        evaluate_on = bars_on and self.evaluate_bars_cb.isChecked()
        for w in (self.bars_ref_label, self.bars_ref_detected_radio,
                  self.bars_ref_smpte_radio):
            w.setEnabled(evaluate_on)

    def on_theme_changed(self, palette):
        """Handle theme changes for ComplexWindow"""
        # Apply the palette directly
        self.setPalette(palette)

        # Get the theme manager
        theme_manager = ThemeManager.instance()

        # Update all tracked group boxes with their specific title positions
        for key, group_box in self.themed_group_boxes.items():
            # Preserve the title position if set
            position = group_box.property("title_position") or "top left"
            theme_manager.style_groupbox(group_box, position)

        # Style all buttons
        theme_manager.style_buttons(self)

        # Force repaint
        self.update()

    # --- signal wiring -------------------------------------------------------

    def connect_signals(self):
        """Connect all widget signals to their handlers"""
        # QCTools report section
        self.run_qctools_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['tools', 'qctools', 'run_tool'])
        )
        self.qctools_ext_combo.currentTextChanged.connect(self.on_qctools_ext_changed)

        # qct-parse-backed checks: any change rewrites the full flag set and
        # derives run_tool implicitly
        for cb in (self.bars_detection_cb, self.evaluate_bars_cb,
                   self.thumb_export_cb, self.audio_analysis_cb,
                   self.detect_clamped_levels_cb, self.detect_chroma_phase_errors_cb,
                   self.detect_tone_leak_cb):
            cb.stateChanged.connect(self.on_qct_parse_flag_changed)

        # Bars-reference radios: the detected radio's toggled fires exactly
        # once per user change (exclusive pair), so one connection is enough.
        self.bars_ref_detected_radio.toggled.connect(self.on_qct_parse_flag_changed)

        # CLAMS detection — single toggle runs both bars and tone detectors.
        # Numeric tuning is JSON-only.
        self.run_clams_detection_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['tools', 'clams_detection', 'run_tool'])
        )

        # Frame analysis enable checkboxes
        self.enable_bitplane_check_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_bitplane_check'])
        )
        self.enable_border_detection_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_border_detection'])
        )
        self.enable_brng_analysis_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_brng_analysis'])
        )
        self.enable_signalstats_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_signalstats'])
        )
        self.enable_dropped_sample_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_dropped_sample_detection'])
        )
        self.enable_duplicate_frame_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'enable_duplicate_frame_detection'])
        )

        # Border mode combo
        self.border_mode_combo.currentIndexChanged.connect(self.on_frame_analysis_mode_changed)

        # Simple parameters
        self.simple_border_pixels_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('simple_border_pixels', text)
        )

        # Sophisticated parameters
        self.soph_threshold_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('sophisticated_threshold', text)
        )
        self.soph_edge_width_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('sophisticated_edge_sample_width', text)
        )
        self.soph_sample_frames_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('sophisticated_sample_frames', text)
        )
        self.soph_padding_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('sophisticated_padding', text)
        )
        self.auto_retry_borders_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'auto_retry_borders'])
        )
        self.max_border_retries_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('max_border_retries', text)
        )

        # BRNG parameters
        self.brng_duration_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('brng_duration_limit', text)
        )
        self.brng_skip_colorbars_cb.stateChanged.connect(
            lambda state: self.on_boolean_changed(state, ['outputs', 'frame_analysis', 'brng_skip_color_bars'])
        )

        # Analysis period parameters
        self.analysis_period_duration_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('analysis_period_duration', text)
        )
        self.analysis_period_count_input.textChanged.connect(
            lambda text: self.on_frame_analysis_param_changed('analysis_period_count', text)
        )

    # --- config loading -------------------------------------------------------

    def load_config_values(self):
        """Load current config values into UI elements"""
        # Set loading flag to True
        self.is_loading = True

        checks_config = config_mgr.get_config('checks', ChecksConfig)

        # QCTools
        qctools = checks_config.tools.qctools
        self.run_qctools_cb.setChecked(bool(qctools.run_tool))
        self.qctools_ext_combo.blockSignals(True)
        qctools_ext = getattr(checks_config.outputs, 'qctools_ext', 'qctools.xml.gz')
        ext_index = self.qctools_ext_combo.findText(qctools_ext)
        if ext_index >= 0:
            self.qctools_ext_combo.setCurrentIndex(ext_index)
        else:
            self.qctools_ext_combo.setCurrentText('qctools.xml.gz')
        self.qctools_ext_combo.blockSignals(False)

        # qct-parse: run_tool is implicit, so each check displays what will
        # actually run (flag AND run_tool)
        qct = checks_config.tools.qct_parse
        run_tool = bool(qct.run_tool)
        self.bars_detection_cb.setChecked(run_tool and qct.barsDetection)
        self.evaluate_bars_cb.setChecked(run_tool and qct.evaluateBars)
        self.thumb_export_cb.setChecked(run_tool and qct.thumbExport)
        bars_ref = getattr(qct, 'evaluateBarsReference', 'detected')
        self.bars_ref_smpte_radio.setChecked(bars_ref == 'smpte')
        self.bars_ref_detected_radio.setChecked(bars_ref != 'smpte')
        self.audio_analysis_cb.setChecked(run_tool and getattr(qct, 'audio_analysis', False))
        self.detect_clamped_levels_cb.setChecked(run_tool and getattr(qct, 'detect_clamped_levels', False))
        self.detect_chroma_phase_errors_cb.setChecked(run_tool and getattr(qct, 'detect_chroma_phase_errors', False))
        self.detect_tone_leak_cb.setChecked(run_tool and getattr(qct, 'detect_tone_leak', False))
        self._apply_bars_dependencies()

        # CLAMS detection — single toggle runs both bars and tone detectors.
        clams = getattr(checks_config.tools, 'clams_detection', None)
        self.run_clams_detection_cb.setChecked(bool(getattr(clams, 'run_tool', False)))

        # Frame Analysis
        if hasattr(checks_config.outputs, 'frame_analysis'):
            frame_config = checks_config.outputs.frame_analysis

            # Load sub-step enable states
            self.enable_bitplane_check_cb.setChecked(bool(frame_config.enable_bitplane_check))
            self.enable_border_detection_cb.setChecked(bool(frame_config.enable_border_detection))
            self.enable_brng_analysis_cb.setChecked(bool(frame_config.enable_brng_analysis))
            self.enable_signalstats_cb.setChecked(bool(frame_config.enable_signalstats))
            self.enable_dropped_sample_cb.setChecked(bool(frame_config.enable_dropped_sample_detection))
            self.enable_duplicate_frame_cb.setChecked(bool(getattr(frame_config, 'enable_duplicate_frame_detection', True)))

            # Set border detection mode
            mode_index = self.border_mode_combo.findData(frame_config.border_detection_mode)
            if mode_index >= 0:
                self.border_mode_combo.setCurrentIndex(mode_index)

            # Load parameter values
            self.simple_border_pixels_input.setText(str(frame_config.simple_border_pixels))
            self.soph_threshold_input.setText(str(frame_config.sophisticated_threshold))
            self.soph_edge_width_input.setText(str(frame_config.sophisticated_edge_sample_width))
            self.soph_sample_frames_input.setText(str(frame_config.sophisticated_sample_frames))
            self.soph_padding_input.setText(str(frame_config.sophisticated_padding))
            self.auto_retry_borders_cb.setChecked(bool(frame_config.auto_retry_borders))
            self.brng_duration_input.setText(str(frame_config.brng_duration_limit))
            self.brng_skip_colorbars_cb.setChecked(bool(frame_config.brng_skip_color_bars))
            self.max_border_retries_input.setText(str(getattr(frame_config, 'max_border_retries', 3)))
            self.analysis_period_duration_input.setText(str(frame_config.analysis_period_duration))
            self.analysis_period_count_input.setText(str(getattr(frame_config, 'analysis_period_count', 3)))

            # Update visibility based on loaded state
            self.update_border_detection_visibility()

        # Set loading flag back to False after everything is loaded
        self.is_loading = False

    # --- config write handlers -------------------------------------------------

    def on_boolean_changed(self, state, path):
        """Handle changes in boolean checkboxes"""
        # Skip updates while loading
        if self.is_loading:
            return

        new_value = Qt.CheckState(state) == Qt.CheckState.Checked

        if path[0] == "tools" and path[1] == "qctools":
            updates = {'tools': {'qctools': {path[2]: new_value}}}
            config_mgr.update_config('checks', updates)
        elif path[0] == "tools" and path[1] == "clams_detection":
            updates = {'tools': {'clams_detection': {path[2]: new_value}}}
            config_mgr.update_config('checks', updates)
        elif path[0] == "outputs" and path[1] == "frame_analysis":
            updates = {'outputs': {'frame_analysis': {path[2]: new_value}}}
            config_mgr.update_config('checks', updates)

    def on_qct_parse_flag_changed(self, _state=None):
        """Handle changes to any qct-parse-backed check.

        Applies the bars-detection dependency, then rewrites the full
        qct_parse flag set with run_tool derived from the checkbox states so
        the saved config always matches what the GUI displays.
        """
        if self.is_loading:
            return

        self._apply_bars_dependencies()

        run_tool = any([
            self.bars_detection_cb.isChecked(),
            self.evaluate_bars_cb.isChecked(),
            self.audio_analysis_cb.isChecked(),
            self.detect_clamped_levels_cb.isChecked(),
            self.detect_chroma_phase_errors_cb.isChecked(),
            self.detect_tone_leak_cb.isChecked(),
        ])
        updates = {'tools': {'qct_parse': {
            'run_tool': run_tool,
            'barsDetection': self.bars_detection_cb.isChecked(),
            'evaluateBars': self.evaluate_bars_cb.isChecked(),
            'thumbExport': self.thumb_export_cb.isChecked(),
            'evaluateBarsReference': self._bars_reference_value(),
            'audio_analysis': self.audio_analysis_cb.isChecked(),
            'detect_clamped_levels': self.detect_clamped_levels_cb.isChecked(),
            'detect_chroma_phase_errors': self.detect_chroma_phase_errors_cb.isChecked(),
            'detect_tone_leak': self.detect_tone_leak_cb.isChecked(),
        }}}
        config_mgr.update_config('checks', updates)

    def _bars_reference_value(self):
        """Current Evaluate Color Bars reference: 'smpte' or 'detected'."""
        return 'smpte' if self.bars_ref_smpte_radio.isChecked() else 'detected'

    def on_frame_analysis_mode_changed(self, index):
        """Handle border detection mode changes"""
        if self.is_loading:
            return

        mode = self.border_mode_combo.itemData(index)

        # Update visibility
        self.update_border_detection_visibility()

        # Update config
        updates = {'outputs': {'frame_analysis': {'border_detection_mode': mode}}}
        config_mgr.update_config('checks', updates)

    def on_frame_analysis_param_changed(self, param_name, value):
        """Handle frame analysis parameter changes"""
        if self.is_loading:
            return

        # Convert to appropriate type
        if param_name in ['simple_border_pixels', 'sophisticated_threshold', 'sophisticated_edge_sample_width',
                        'sophisticated_sample_frames', 'sophisticated_padding', 'sophisticated_viz_time',
                        'sophisticated_search_window', 'brng_duration_limit',
                        'analysis_period_duration', 'analysis_period_count', 'max_border_retries']:
            try:
                # Handle empty string case
                value = int(value) if value.strip() else 0
            except ValueError:
                return  # Don't update config if conversion fails

        updates = {'outputs': {'frame_analysis': {param_name: value}}}
        config_mgr.update_config('checks', updates)

    def on_qctools_ext_changed(self, ext):
        """Handle changes in QCTools file extension dropdown"""
        if self.is_loading:
            return

        updates = {'outputs': {'qctools_ext': ext}}
        config_mgr.update_config('checks', updates)
