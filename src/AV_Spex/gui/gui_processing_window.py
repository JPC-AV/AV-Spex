from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QAbstractItemView, QTextEdit,
    QProgressBar, QSplitter, QMessageBox, QFileDialog
)
from PyQt6.QtCore import Qt, QEvent, QSize, QSettings, QMarginsF
from PyQt6.QtGui import (
    QPalette, QFont, QPdfWriter, QPageSize, QPageLayout, QTextOption, QTextCursor
)

import os
import re
from datetime import datetime
from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.gui.gui_processing_window_console import ConsoleTextEdit, MessageType
from AV_Spex.gui.gui_theme_manager import ThemeManager

from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_setup import ChecksConfig
from AV_Spex.utils.log_setup import connect_logger_to_ui

config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)

# Qt's text layout silently stops laying out a QTextDocument once the laid-out
# height reaches ~2^23 device units, and QTextDocument.print() reports no error
# when it does — the PDF simply ends mid-console. The ceiling is measured in
# *device* units, so it is a page count only for a given writer resolution:
# at QPdfWriter's 1200 dpi default it works out to ~712 Letter pages (~18,500
# console lines), which a long multi-tape run exceeds. Printing at 300 dpi
# quadruples the headroom to ~2,850 pages; PDF text is vector, so nothing is
# lost visually. _console_pdf_looks_truncated() catches the remaining case.
CONSOLE_PDF_RESOLUTION = 300
_PDF_LAYOUT_HEIGHT_LIMIT = 2 ** 23
_PDF_PAGE_OBJECT_RE = re.compile(rb'/Type\s*/Page[^s]')


def _console_pdf_looks_truncated(file_path, page_height):
    """Report whether a written console PDF hit Qt's layout-height ceiling.

    A truncated print always stops within one page of _PDF_LAYOUT_HEIGHT_LIMIT
    device units, so counting the page objects Qt wrote and multiplying by the
    writer's page height tells us whether the document ran into the wall. A
    document that genuinely ends within one page of the ceiling would be
    flagged too — a false "may be incomplete" on a ~2,800-page export is a fair
    trade for never silently losing console output again.

    The check fails open: if the file can't be read, or the page objects can't
    be found (a future Qt could compress them), it reports False rather than
    warning about a PDF it could not inspect.

    Args:
        file_path (str): The PDF that was just written
        page_height (int): QPdfWriter.height() — the page height in device units

    Returns:
        bool: True only when the output is positively at the ceiling
    """
    if page_height <= 0:
        return False
    try:
        with open(file_path, 'rb') as pdf_file:
            page_count = len(_PDF_PAGE_OBJECT_RE.findall(pdf_file.read()))
    except OSError:
        return False
    if page_count == 0:
        return False
    return page_count * page_height >= _PDF_LAYOUT_HEIGHT_LIMIT - page_height


class ProcessingWindow(QMainWindow, ThemeableMixin):
    """Window to display processing status and progress."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Processing Status")
        self.resize(700, 500)  # Set initial size
        self.setMinimumSize(500, 300)  # Set minimum size
        self.setWindowFlags(Qt.WindowType.Window)
        # Initialize settings for this window
        self.settings = QSettings('NMAAHC', 'AVSpex')

        # Console position where the current file's output starts, used when
        # saving a per-file console PDF (see save_file_console_pdf)
        self._file_console_start_pos = 0
        
        # Central widget and main_layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)  # Add some padding
        
        # Status label with larger font
        self.file_status_label = QLabel("No file processing yet...")
        file_font = self.file_status_label.font()
        file_font.setPointSize(10)
        self.file_status_label.setFont(file_font)
        self.file_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self.file_status_label)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMinimum(0)
        main_layout.addWidget(self.progress_bar)

        # Create a splitter for steps list and details text
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter, 1)  # stretch factor of 1

        # Steps list widget - shows steps that will be executed
        self.steps_list = QListWidget()
        self.steps_list.setMinimumHeight(150)
        self.steps_list.setAlternatingRowColors(True)
        self.steps_list.setMinimumWidth(150)  # Ensure minimum width
        splitter.addWidget(self.steps_list)

        # Create a container for the console and zoom controls
        console_container = QWidget()
        console_layout = QVBoxLayout(console_container)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(2)

        # Create zoom controls toolbar
        zoom_toolbar = QWidget()
        zoom_layout = QHBoxLayout(zoom_toolbar)
        zoom_layout.setContentsMargins(0, 0, 0, 0)
        zoom_layout.setSpacing(2)

        # Add zoom label
        zoom_label = QLabel("Text Size:")
        zoom_layout.addWidget(zoom_label)

        # Zoom out button
        self.zoom_out_button = QPushButton("-")
        self.zoom_out_button.setMaximumWidth(30)
        self.zoom_out_button.setToolTip("Decrease text size (Ctrl+-)")
        self.zoom_out_button.clicked.connect(self.zoom_out_console)
        zoom_layout.addWidget(self.zoom_out_button)

        # Zoom reset button
        self.zoom_reset_button = QPushButton("Reset")
        self.zoom_reset_button.setMaximumWidth(60)
        self.zoom_reset_button.setToolTip("Reset text size to default")
        self.zoom_reset_button.clicked.connect(self.reset_console_zoom)
        zoom_layout.addWidget(self.zoom_reset_button)

        # Zoom in button
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setMaximumWidth(30)
        self.zoom_in_button.setToolTip("Increase text size (Ctrl++)")
        self.zoom_in_button.clicked.connect(self.zoom_in_console)
        zoom_layout.addWidget(self.zoom_in_button)

        # Current size label
        self.font_size_label = QLabel("14pt")
        self.font_size_label.setMinimumWidth(40)
        zoom_layout.addWidget(self.font_size_label)

        # After the font_size_label, before the stretch
        zoom_layout.addWidget(self.font_size_label)

        # Add separator space
        zoom_layout.addSpacing(20)

        # Save as PDF button - exports the console contents, preserving styling
        self.save_pdf_button = QPushButton("Save as PDF")
        self.save_pdf_button.setToolTip(
            "Save the console output to a PDF file (a plain-text copy is "
            "saved alongside it), or to a text file"
        )
        self.save_pdf_button.clicked.connect(self.save_console_as_pdf)
        zoom_layout.addWidget(self.save_pdf_button)

        # Clear console button
        self.clear_console_button = QPushButton("Clear")
        self.clear_console_button.setMaximumWidth(60)
        self.clear_console_button.setToolTip("Clear all console output")
        self.clear_console_button.clicked.connect(self.clear_console_with_confirmation)
        zoom_layout.addWidget(self.clear_console_button)

        # Add stretch to push controls to the left
        zoom_layout.addStretch()

        # Add toolbar to console container
        console_layout.addWidget(zoom_toolbar)

        # Details text - use custom ConsoleTextEdit instead of QTextEdit
        self.details_text = ConsoleTextEdit()
        console_layout.addWidget(self.details_text)
        splitter.addWidget(console_container)

        # Set initial splitter sizes
        splitter.setSizes([200, 500])  # Allocate more space to the details text

        # Detailed status
        self.detailed_status = QLabel("")
        self.detailed_status.setWordWrap(True)
        main_layout.addWidget(self.detailed_status)

        # Detail progress bar
        self.setup_details_progress_bar(main_layout)

        # Add cancel button
        self.cancel_button = QPushButton("Cancel")
        main_layout.addWidget(self.cancel_button)

        # Load the configuration and populate steps
        checks_config = config_mgr.get_config('checks', ChecksConfig)
        self.populate_steps_list()
        
        # Setup theme handling (only once)
        self.setup_theme_handling()

        # Apply initial progress bar styles
        self.apply_progress_bar_style()
        
        # Connect theme changes to progress bar styling
        self.theme_manager = ThemeManager.instance()
        self.theme_manager.themeChanged.connect(self.apply_progress_bar_style)
        # After theme handling setup, style the zoom buttons
        self.style_zoom_buttons()
        # Load saved zoom preference if it exists
        self.load_zoom_preference()

        # Initial welcome message
        self.details_text.append_message("Processing window initialized", MessageType.INFO)
        self.details_text.append_message("Ready to process files", MessageType.SUCCESS)

        self.logger = connect_logger_to_ui(self)

    def clear_console_with_confirmation(self):
        """Clear console after user confirmation."""
        
        # Create confirmation dialog
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Clear Console")
        msg_box.setText("Are you sure you want to clear all console output?")
        msg_box.setInformativeText("This action cannot be undone.")
        msg_box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        msg_box.setDefaultButton(QMessageBox.StandardButton.No)
        
        # Style the message box buttons if theme manager is available
        theme_manager = ThemeManager.instance()
        if theme_manager:
            # Apply theme-aware styling to message box
            msg_box.setStyleSheet(self.get_message_box_style())
        
        # Show dialog and get response
        response = msg_box.exec()
        
        if response == QMessageBox.StandardButton.Yes:
            # Clear the console
            self.details_text.clear_console()
            
            # Add a message indicating console was cleared
            self.details_text.append_message("Console cleared", MessageType.INFO)
            
    def save_console_as_pdf(self):
        """Export the console output, as a styled PDF or as plain text.

        The console (`details_text`) is already a fully formatted
        QTextDocument, so we print that document straight to PDF. This keeps
        the exact colors, bold, monospace font, and spacing seen on screen.
        NORMAL text carries no explicit color, so it renders black on the
        white page; the colored message types keep their console colors.

        The PDF is the nice-looking artifact but it is the one that can lose
        content (see CONSOLE_PDF_RESOLUTION), so a plain-text copy is written
        next to it every time — no layout engine is involved in
        `toPlainText()`, so it cannot truncate. Choosing the Text Files filter
        exports only that copy.
        """
        # Nothing to export if the console is empty
        if not self.details_text.toPlainText().strip():
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Nothing to Save")
            msg_box.setText("The console is empty — there is nothing to export.")
            msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg_box.setStyleSheet(self.get_message_box_style())
            msg_box.exec()
            return

        # Default filename includes a timestamp so repeated exports don't collide
        default_name = f"AVSpex_console_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.pdf"
        default_path = os.path.join(os.path.expanduser("~"), default_name)

        file_path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save Console Output", default_path,
            "PDF Files (*.pdf);;Text Files (*.txt)"
        )
        if not file_path:
            return  # user cancelled

        # A typed extension wins; otherwise the chosen filter decides
        wants_text = (file_path.lower().endswith(".txt")
                      or (not file_path.lower().endswith(".pdf")
                          and "*.txt" in selected_filter))
        if wants_text:
            if not file_path.lower().endswith(".txt"):
                file_path += ".txt"
            try:
                self._write_console_text(file_path)
                self.update_status(f"Console output saved to {file_path}", MessageType.SUCCESS)
            except OSError as e:
                self.update_status(f"Failed to save text file: {str(e)}", MessageType.ERROR)
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("Save Failed")
                msg_box.setText("Could not save the console output to a text file.")
                msg_box.setInformativeText(str(e))
                msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
                msg_box.setStyleSheet(self.get_message_box_style())
                msg_box.exec()
            return

        if not file_path.lower().endswith(".pdf"):
            file_path += ".pdf"

        # Insurance against the PDF losing content: a copy that can't truncate.
        # A failure here is reported but never blocks the PDF export.
        text_path = os.path.splitext(file_path)[0] + ".txt"
        try:
            self._write_console_text(text_path)
        except OSError as e:
            text_path = None
            self.update_status(
                f"Could not save the plain-text copy of the console: {str(e)}",
                MessageType.WARNING
            )

        try:
            complete = self._write_console_pdf(file_path)
            if complete:
                saved_to = file_path
                if text_path:
                    saved_to = f"{file_path} (with a plain-text copy at {text_path})"
                self.update_status(f"Console output saved to {saved_to}", MessageType.SUCCESS)
            else:
                fallback = (f"The complete log is in {text_path}."
                            if text_path else
                            "Select all the console text and paste it into a text "
                            "file to keep the full log.")
                self.update_status(
                    f"Console output saved to {file_path}, but it is incomplete — "
                    f"the console is too long for a single PDF and the export was "
                    f"cut short. {fallback}",
                    MessageType.WARNING
                )
                msg_box = QMessageBox(self)
                msg_box.setIcon(QMessageBox.Icon.Warning)
                msg_box.setWindowTitle("Saved, but Incomplete")
                msg_box.setText("The PDF was written but is missing the end of the console.")
                msg_box.setInformativeText(
                    "This console is longer than a PDF can hold, so the export "
                    "stops partway through. " +
                    (f"Nothing is lost — the full console output was also saved as "
                     f"plain text at {text_path}."
                     if text_path else
                     "The on-screen console still has everything: select all of it "
                     "(Cmd+A, Cmd+C) and paste it into a text file to keep the "
                     "complete log.")
                )
                msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
                msg_box.setStyleSheet(self.get_message_box_style())
                msg_box.exec()
        except Exception as e:
            self.update_status(f"Failed to save PDF: {str(e)}", MessageType.ERROR)
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Save Failed")
            msg_box.setText("Could not save the console output to PDF.")
            msg_box.setInformativeText(str(e))
            msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg_box.setStyleSheet(self.get_message_box_style())
            msg_box.exec()

    def _write_console_text(self, file_path, start_position=0):
        """Write the console output (or a tail of it) to a plain-text file.

        The styling is lost, but so is every way the export could come up
        short: this is a straight dump of the same text the user would get by
        selecting the console and copying it.

        Args:
            file_path (str): Destination .txt path
            start_position (int): Character position to start from, matching
                _write_console_pdf()'s slicing of a single video's output.
        """
        text = self.details_text.toPlainText()
        if start_position > 0:
            text = text[start_position:]
        with open(file_path, 'w', encoding='utf-8') as text_file:
            text_file.write(text)

    def _write_console_pdf(self, file_path, start_position=0):
        """Print the console document (or a tail of it) to a PDF file.

        Args:
            file_path (str): Destination .pdf path
            start_position (int): Character position to start from — anything
                before it is dropped, which is how a single video's slice of
                the console is exported.

        Returns:
            bool: True if the whole document made it into the PDF, False if it
                hit Qt's layout ceiling and the output is incomplete.
        """
        writer = QPdfWriter(file_path)
        # Must be set before the page size: it decides how much document fits
        # under Qt's layout-height ceiling (see CONSOLE_PDF_RESOLUTION).
        writer.setResolution(CONSOLE_PDF_RESOLUTION)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.Letter))
        writer.setPageMargins(QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter)

        # Clone the live document so we can enable wrapping for the page
        # width without disturbing the on-screen console (which uses NoWrap).
        doc = self.details_text.document().clone(self)
        doc.setDefaultFont(self.details_text.font())
        text_option = QTextOption()
        text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        doc.setDefaultTextOption(text_option)

        if start_position > 0:
            # Trim everything logged before this video started
            cursor = QTextCursor(doc)
            cursor.setPosition(0)
            cursor.setPosition(min(start_position, doc.characterCount() - 1),
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

        doc.print(writer)

        # QPdfWriter reports nothing when it cannot open the destination — the
        # print is simply a no-op — so an unwritable path would otherwise be
        # announced as a successful save.
        if os.path.getsize(file_path) == 0:
            raise OSError(f"Nothing could be written to {file_path}")

        return not _console_pdf_looks_truncated(file_path, writer.height())

    def mark_file_console_start(self, source_directory=None, current=None, total=None):
        """Remember where in the console this video's output begins.

        Connected to the file_started signal; the position is used later by
        save_file_console_pdf() to export only this video's console output.
        """
        cursor = QTextCursor(self.details_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._file_console_start_pos = cursor.position()

    def save_file_console_pdf(self, video_id, destination_directory):
        """Save this video's console output to its qc_metadata directory.

        Runs when a file finishes processing, if "Save Console Log as PDF" is
        enabled on the Checks tab. Failures are reported to the console but
        never interrupt processing.
        """
        checks_config = config_mgr.get_config('checks', ChecksConfig)
        if not getattr(checks_config.outputs, 'save_console_pdf', False):
            return

        if not destination_directory or not os.path.isdir(destination_directory):
            self.update_status(
                f"Could not save console PDF: {destination_directory} is not a directory",
                MessageType.ERROR
            )
            return

        file_path = os.path.join(destination_directory, f"{video_id}_avspex_console.pdf")

        try:
            complete = self._write_console_pdf(file_path, self._file_console_start_pos)
            if complete:
                self.update_status(f"Console output saved to {file_path}", MessageType.INFO)
            else:
                self.update_status(
                    f"Console output saved to {file_path}, but this file's console "
                    "output was too long to fit in a PDF and the end is missing.",
                    MessageType.WARNING
                )
        except Exception as e:
            self.update_status(f"Failed to save console PDF: {str(e)}", MessageType.ERROR)

    def get_message_box_style(self):
        """Get theme-aware styling for message boxes."""
        palette = self.palette()
        bg_color = palette.color(palette.ColorRole.Window).name()
        text_color = palette.color(palette.ColorRole.WindowText).name()
        button_color = palette.color(palette.ColorRole.Button).name()
        button_text = palette.color(palette.ColorRole.ButtonText).name()
        
        return f"""
            QMessageBox {{
                background-color: {bg_color};
                color: {text_color};
            }}
            QMessageBox QPushButton {{
                min-width: 60px;
                padding: 5px 15px;
                background-color: {button_color};
                color: {button_text};
                border: 1px solid gray;
                border-radius: 3px;
            }}
            QMessageBox QPushButton:hover {{
                background-color: #4CAF50;
                color: white;
            }}
        """

    def sizeHint(self):
        """Override size hint to provide default window size"""
        return QSize(700, 500)

    def setup_details_progress_bar(self, layout):
        """Set up the modern overlay progress bar."""
        # Create progress bar
        self.detail_progress_bar = QProgressBar()
        self.detail_progress_bar.setTextVisible(False)  # Hide default text
        self.detail_progress_bar.setMinimum(0)
        self.detail_progress_bar.setMaximum(100)
        
        # Create overlay label
        self.overlay_container = QWidget(self.detail_progress_bar)
        overlay_layout = QHBoxLayout(self.overlay_container)
        overlay_layout.setContentsMargins(0, 0, 0, 0)
        
        self.overlay_label = QLabel("0%")
        self.overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addWidget(self.overlay_label)
        
        # Set overlay to cover the progress bar
        self.overlay_container.setGeometry(self.detail_progress_bar.rect())
        self.detail_progress_bar.installEventFilter(self)
        
        # Add to layout
        layout.addWidget(self.detail_progress_bar)

    def apply_progress_bar_style(self, palette=None):
        """Apply modern overlay style to progress bar using current palette."""
        if palette is None:
            palette = self.palette()
        
        # Get colors from palette
        base_color = palette.color(QPalette.ColorRole.Base).name()
        highlight_color = palette.color(QPalette.ColorRole.Highlight).name()
        text_color = palette.color(QPalette.ColorRole.HighlightedText).name()
        
        # Style the progress bar
        self.detail_progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: none;
                border-radius: 4px;
                background-color: {base_color};
                text-align: center;
                height: 22px;
            }}
            
            QProgressBar::chunk {{
                background-color: {highlight_color};
                border-radius: 4px;
            }}
        """)
        
        # Style the overlay text
        self.overlay_label.setStyleSheet(f"""
            color: {text_color};
            font-weight: bold;
        """)

    def eventFilter(self, obj, event):
        """Ensure overlay label stays positioned correctly."""
        if obj == self.detail_progress_bar and event.type() == QEvent.Type.Resize:
            self.overlay_container.setGeometry(self.detail_progress_bar.rect())
        return super().eventFilter(obj, event)


    def populate_steps_list(self):
        """Populate the steps list with enabled checks from config."""
        try:
            # Get checks config
            checks_config = config_mgr.get_config('checks', ChecksConfig)
            if not checks_config:
                self.update_status("Warning: Could not load checks configuration")
                return

            # Fixity Steps 
            if checks_config.fixity.validate_stream_fixity:
                self._add_step_item("Validate Stream Fixity")
            if checks_config.fixity.check_fixity:
                self._add_step_item("Validate Fixity")
            if checks_config.fixity.embed_stream_fixity:
                self._add_step_item("Embed Stream Fixity")
            if checks_config.fixity.output_fixity:
                self._add_step_item("Output Fixity")
            
            # MediaConch - now using boolean check
            if checks_config.tools.mediaconch.run_mediaconch:
                self._add_step_item("MediaConch Validation")
            
            # Metadata tools - note consistent naming 
            if checks_config.tools.exiftool.run_tool or checks_config.tools.exiftool.check_tool:
                self._add_step_item("Exiftool")
            if checks_config.tools.ffprobe.run_tool or checks_config.tools.ffprobe.check_tool:
                self._add_step_item("FFprobe")
            if checks_config.tools.mediainfo.run_tool or checks_config.tools.mediainfo.check_tool:
                self._add_step_item("Mediainfo")
            if checks_config.tools.mediatrace.run_tool or checks_config.tools.mediatrace.check_tool:
                self._add_step_item("Mediatrace")
            mkvalidator_cfg = getattr(checks_config.tools, 'mkvalidator', None)
            if mkvalidator_cfg and (mkvalidator_cfg.run_tool or mkvalidator_cfg.check_tool):
                self._add_step_item("mkvalidator")

            # Output tools
            if checks_config.tools.qctools.run_tool:
                self._add_step_item("QCTools")
            if checks_config.tools.qct_parse.run_tool:
                self._add_step_item("QCT Parse")
            # CLAMS detection (bars + tone) runs straight from the video file,
            # independent of QCTools / qct-parse. Step label matches the
            # step_completed payload emitted from process_qctools_output.
            clams_cfg = getattr(checks_config.tools, 'clams_detection', None)
            if clams_cfg and getattr(clams_cfg, 'run_tool', False):
                self._add_step_item("CLAMS Detection")
            
            # Frame Analysis
            if hasattr(checks_config.outputs, 'frame_analysis'):
                frame_config = checks_config.outputs.frame_analysis
                if frame_config.enable_bitplane_check:
                    self._add_step_item("Frame Analysis - Bitplane Check")
                if frame_config.enable_border_detection:
                    self._add_step_item("Frame Analysis - Border Detection")
                # Only add signalstats if enabled AND in sophisticated mode
                if (frame_config.enable_signalstats and 
                    frame_config.border_detection_mode == "sophisticated"):
                    self._add_step_item("Frame Analysis - Signalstats")
                if frame_config.enable_brng_analysis:
                    self._add_step_item("Frame Analysis - BRNG Analysis")
                if frame_config.enable_dropped_sample_detection:
                    self._add_step_item("Frame Analysis - Dropped Sample Detection")
                if getattr(frame_config, 'enable_duplicate_frame_detection', False):
                    self._add_step_item("Frame Analysis - Duplicate Frame Detection")
            
            # Output files
            if checks_config.outputs.access_file:
                self._add_step_item("Generate Access File")
            if checks_config.outputs.report:
                self._add_step_item("Generate Report")
            
            # Final steps
            self._add_step_item("All Processing")
            
        except Exception as e:
            self.update_status(f"Error loading steps: {str(e)}")
    
    def _add_step_item(self, step_name):
        """Add a step item to the list."""
        item = QListWidgetItem(f"⬜ {step_name}")
        self.steps_list.addItem(item)
    
    def mark_step_complete(self, step_name):
        """Mark a step as complete in the list."""
        # Find and update the item
        found = False
        for i in range(self.steps_list.count()):
            item = self.steps_list.item(i)
            item_text = item.text()[2:]  # Remove the checkbox prefix
            
            # Check for exact match first
            if item_text == step_name:
                item.setText(f"✅ {step_name}")
                item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
                found = True
                break
            # If no exact match, try case-insensitive matching
            elif item_text.lower() == step_name.lower():
                item.setText(f"✅ {item_text}")  # Keep original capitalization
                item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
                found = True
                break
        
        if not found:
            self.details_text.append(f"Warning: No matching step found for '{step_name}'")

    def mark_step_failed(self, step_name):
        """Mark a step as failed in the list."""
        found = False
        for i in range(self.steps_list.count()):
            item = self.steps_list.item(i)
            item_text = item.text()[2:]  # Remove the checkbox prefix

            # Check for exact match first
            if item_text == step_name:
                item.setText(f"❌ {step_name}")
                item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
                found = True
                break
            # If no exact match, try case-insensitive matching
            elif item_text.lower() == step_name.lower():
                item.setText(f"❌ {item_text}")  # Keep original capitalization
                item.setFont(QFont("Arial", weight=QFont.Weight.Bold))
                found = True
                break

        if not found:
            self.details_text.append(f"Warning: No matching step found for '{step_name}'")

    def mark_step_pending(self, step_name):
        """Reset a step back to pending (unchecked) state."""
        found = False
        for i in range(self.steps_list.count()):
            item = self.steps_list.item(i)
            item_text = item.text()[2:]  # Remove the checkbox/status prefix

            if item_text == step_name:
                item.setText(f"⬜ {step_name}")
                item.setFont(QFont("Arial", weight=QFont.Weight.Normal))
                found = True
                break
            elif item_text.lower() == step_name.lower():
                item.setText(f"⬜ {item_text}")
                item.setFont(QFont("Arial", weight=QFont.Weight.Normal))
                found = True
                break

        if not found:
            self.details_text.append(f"Warning: No matching step found for '{step_name}'")

    def reset_steps_list(self):
        """Reset the steps list when processing a new file, but preserve dependency check status."""
        for i in range(self.steps_list.count()):
            item = self.steps_list.item(i)
            item_text = item.text()
        
        # Clear the list widget
        self.steps_list.clear()
        
        # Repopulate with fresh steps
        self.populate_steps_list()


    def update_detailed_status(self, message):
        """Update the detailed status message."""
        self.detailed_status.setText(message)
        QApplication.processEvents()

    def update_detail_progress(self, percentage):
        """Update the detail progress bar with the current percentage."""
        # If this is the first update (percentage very small) or a reset signal (percentage = 0),
        # we're likely starting a new process step
        if percentage <= 1:
            # Reset the progress bar
            self.detail_progress_bar.setMaximum(100)
            self.detail_progress_bar.setValue(0)
        
        # Now update with the current progress
        self.detail_progress_bar.setValue(percentage)
        
        # Update percentage label
        self.overlay_label.setText(f"{percentage}%")

    def update_status(self, message, msg_type=None):
        """
        Update the main status message and append to details text.
        Detects message type based on content and formats accordingly.
        """
        if msg_type is None:
            # Determine message type based on content
            msg_type = MessageType.NORMAL
            lowercase_msg = message.lower()
            
            # ERROR detection
            if "error" in lowercase_msg or "failed" in lowercase_msg:
                msg_type = MessageType.ERROR
            
            # WARNING detection
            elif "warning" in lowercase_msg:
                msg_type = MessageType.WARNING
            
            # COMMAND detection
            elif lowercase_msg.startswith(("finding", "checking", "executing", "running")):
                msg_type = MessageType.COMMAND
            
            # SUCCESS detection
            elif any(success_term in lowercase_msg for success_term in [
                "success", "complete", "finished", "done", "identified successfully"
            ]):
                msg_type = MessageType.SUCCESS
            
            # INFO detection
            elif any(info_term in lowercase_msg for info_term in [
                "found", "version", "dependencies", "starting", "processing"
            ]):
                msg_type = MessageType.INFO
        
        # Append the message to the console with styling
        self.details_text.append_message(message, msg_type)

    def update_file_status(self, filename, current_index=None, total_files=None):
        """Update the file status label when processing a new file."""
        if current_index is not None and total_files is not None:
            self.file_status_label.setText(f"Processing ({current_index} / {total_files}): {os.path.basename(filename)}")
        else:
            self.file_status_label.setText(f"Processing: {filename}")
        
        # Update the progress bar
        self.progress_bar.setMaximum(total_files)  # Set maximum to total files
        self.progress_bar.setValue(current_index - 1)  # Set value to index - 1

        # Reset the detail progress bar for the new file
        self.reset_progress_bars()

        # Scroll to bottom
        scrollbar = self.details_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()  # Bring window to front
        self.activateWindow()  # Activate the window

    def closeEvent(self, event):
        # Check if this is a forced close from the application quit
        if QApplication.instance().closingDown():
            # Allow actual close when application is quitting
            if hasattr(self, 'theme_manager'):
                try:
                    self.theme_manager.themeChanged.disconnect(self.apply_progress_bar_style)
                except:
                    pass  # Already disconnected
            super().closeEvent(event)
            return
            
        # Prevent the default close behavior
        event.ignore()
        
        # Hide the window instead
        self.hide()
        
        # Notify the parent window that the processing window was hidden
        parent = self.parent()
        if parent and hasattr(parent, 'on_processing_window_hidden'):
            parent.on_processing_window_hidden()

    def on_theme_changed(self, palette):
        """Handle theme changes - update all styling."""
        # Apply palette to all components
        self.setPalette(palette)
        self.file_status_label.setPalette(palette)
        
        # Get theme manager
        theme_manager = ThemeManager.instance()
        
        # Style console text
        theme_manager.style_console_text(self.details_text)
        
        # Style the cancel button with special styling
        theme_manager.style_button(self.cancel_button)
        
        # Style the zoom buttons
        self.style_zoom_buttons()
        
        # Force repaint
        self.update()

    def reset_progress_bars(self):
        """Reset all progress bars when starting a new file."""
        # Reset the detail progress bar
        self.detail_progress_bar.setValue(0)
        self.detail_progress_bar.setMaximum(100)
        self.overlay_label.setText("0%")
        
        # Optionally reset the main progress bar's text
        self.progress_bar.setFormat("%p%")

    def zoom_in_console(self):
        """Increase console text size."""
        if self.details_text.zoom_in():
            self.update_font_size_label()
            self.update_zoom_button_states()
            self.save_zoom_preference()

    def zoom_out_console(self):
        """Decrease console text size."""
        if self.details_text.zoom_out():
            self.update_font_size_label()
            self.update_zoom_button_states()
            self.save_zoom_preference()

    def reset_console_zoom(self):
        """Reset console text size to default."""
        self.details_text.reset_zoom()
        self.update_font_size_label()
        self.update_zoom_button_states()
        self.save_zoom_preference()

    def update_font_size_label(self):
        """Update the font size label with current size."""
        size = self.details_text.get_current_font_size()
        self.font_size_label.setText(f"{size}pt")

    def update_zoom_button_states(self):
        """Enable/disable zoom buttons based on current size limits."""
        current_size = self.details_text.get_current_font_size()
        self.zoom_in_button.setEnabled(current_size < self.details_text._max_font_size)
        self.zoom_out_button.setEnabled(current_size > self.details_text._min_font_size)

    def save_zoom_preference(self):
        """Save the current zoom level to settings."""
        self.settings.setValue('console_font_size', self.details_text.get_current_font_size())

    def load_zoom_preference(self):
        """Load saved zoom level from settings."""
        saved_size = self.settings.value('console_font_size', 14, type=int)
        if saved_size != 14:  # Only apply if different from default
            self.details_text._current_font_size = saved_size
            self.details_text._apply_font_size_change()
            self.update_font_size_label()
            self.update_zoom_button_states()

    def style_zoom_buttons(self):
        """Apply theme-aware styling to zoom buttons and clear button."""
        theme_manager = ThemeManager.instance()
        
        # Style the zoom buttons with standard button styling
        theme_manager.style_button(self.zoom_in_button)
        theme_manager.style_button(self.zoom_out_button)
        theme_manager.style_button(self.zoom_reset_button)
        theme_manager.style_button(self.save_pdf_button)
        
        # Style clear button with a slightly different look
        palette = self.palette()
        button_color = palette.color(palette.ColorRole.Button).name()
        text_color = palette.color(palette.ColorRole.ButtonText).name()
        
        clear_button_style = f"""
            QPushButton {{
                font-weight: bold;
                padding: 8px;
                border: 1px solid gray;
                border-radius: 4px;
                background-color: {button_color};
                color: {text_color};
            }}
            QPushButton:hover {{
                background-color: #ff9999;
                color: #4d2b12;
            }}
        """
        self.clear_console_button.setStyleSheet(clear_button_style)

    def keyPressEvent(self, event):
        """Handle keyboard shortcuts for zooming."""
        if event.modifiers() == Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Plus or event.key() == Qt.Key.Key_Equal:
                self.zoom_in_console()
            elif event.key() == Qt.Key.Key_Minus:
                self.zoom_out_console()
            elif event.key() == Qt.Key.Key_0:
                self.reset_console_zoom()
        super().keyPressEvent(event)


class DirectoryListWidget(QListWidget):
    """Custom list widget with drag and drop support for directories."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        # Critical settings for drag and drop
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.main_window = parent

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            
            for url in urls:
                path = url.toLocalFile()
                
                if os.path.isdir(path):
                    # Check for duplicates before adding
                    if path not in [self.item(i).text() for i in range(self.count())]:
                        self.addItem(path)
                        
                        # Update selected_directories if main_window is available
                        if hasattr(self.main_window, 'selected_directories'):
                            if path not in self.main_window.selected_directories:
                                self.main_window.selected_directories.append(path)
            
            event.acceptProposedAction()