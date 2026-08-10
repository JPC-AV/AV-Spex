from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel, QScrollArea, 
    QPushButton, QListWidget, QFileDialog, QProgressBar, QSizePolicy, 
    QStyle, QMessageBox, QComboBox, QDialog, QTextBrowser
)
from PyQt6.QtCore import Qt, QDir, QSize
from PyQt6.QtGui import QPixmap

import json
import os

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.gui.gui_processing_window import DirectoryListWidget
from AV_Spex.utils.config_io import ConfigIO
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.config_setup import ChecksConfig, SpexConfig, FilenameConfig, SignalflowConfig
from AV_Spex.utils.log_setup import logger

from AV_Spex import __version__
version_string = __version__

config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)

class ImportTab(ThemeableMixin):
    """Import tab with nested handler classes for more hierarchical organization"""
    
    class ConfigHandlers:
        """Configuration import/export/reset handlers"""
        
        def __init__(self, parent_tab):
            self.parent_tab = parent_tab
            self.main_window = parent_tab.main_window
            self.spex_tab = self.main_window.spex_tab
        
        def export_selected_config(self):
            selected_option = self.main_window.export_config_dropdown.currentText()
            # Skip export if the placeholder option is selected
            if selected_option == "Export Config Type...":
                return
            elif selected_option == "Export Checks Config":
                self.export_config_dialog('checks')
            elif selected_option == "Export Spex Config":
                self.export_config_dialog('spex')
            elif selected_option == "Export All File name Profiles":
                self.export_config_dialog('filename')
            elif selected_option == "Export All Signal flow Profiles":
                self.export_config_dialog('signalflow')
            elif selected_option == "Export Spex and Checks Config":
                self.export_config_dialog(['checks','spex'])
            elif selected_option == "Export All Custom Checks Profiles":
                self.export_config_dialog('profiles_checks')
            elif selected_option == "Export All Exiftool Profiles":
                self.export_config_dialog('exiftool')
            elif selected_option == "Export All MediaInfo Profiles":
                self.export_config_dialog('mediainfo')
            elif selected_option == "Export All FFprobe Profiles":
                self.export_config_dialog('ffprobe')
            elif selected_option == "Export All Config":
                self.export_config_dialog(['checks', 'spex', 'filename', 
                               'signalflow', 'profiles_checks', 'exiftool',
                               'mediainfo', 'ffprobe'])

        def import_config(self):
            """Import configuration from a file."""
            file_dialog = QFileDialog(self.main_window, "Import Configuration")
            file_dialog.setNameFilter("Config Files (*.json);;All Files (*)")
            file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
            
            if file_dialog.exec():
                file_path = file_dialog.selectedFiles()[0]
                try:
                    # Use the ConfigIO class to import config
                    config_io = ConfigIO(config_mgr)
                    import_results = config_io.import_configs(file_path)
                    
                    # Reload UI components to reflect new settings
                    self.main_window.config_widget.load_config_values()

                    # Ensure recent config ref
                    checks_config = config_mgr.get_config('checks', ChecksConfig)

                    # Checks Tab dropdowns 
                    # Refresh the checks profile dropdown (includes custom profiles)
                    if hasattr(self.main_window, 'checks_tab') and hasattr(self.main_window.checks_tab, 'profile_handlers'):
                        self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                    elif hasattr(self.main_window, 'checks_profile_dropdown'):
                        # Fallback: manually set the built-in profile selection
                        self.main_window.checks_profile_dropdown.blockSignals(True)
                        
                        # Set based on exiftool.run_tool value (using boolean now)
                        if checks_config.tools.exiftool.run_tool and not checks_config.tools.exiftool.check_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Vendor")
                        elif checks_config.tools.exiftool.run_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Step 1")
                        elif not checks_config.tools.exiftool.run_tool and checks_config.tools.exiftool.check_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Step 2")
                        else:
                            # Fallback to "All Off" if both are False
                            self.main_window.checks_profile_dropdown.setCurrentText("All Off")
                        
                        self.main_window.checks_profile_dropdown.blockSignals(False)

                    # Spex Tab — one call re-syncs every card (dropdowns,
                    # active-profile state lines, and summaries)
                    if hasattr(self.spex_tab, 'refresh'):
                        self.spex_tab.refresh()

                    # Show result to user — with rename notification if applicable
                    renamed = import_results.get('renamed_profiles', [])
                    errors = import_results.get('errors', [])
                    
                    if errors:
                        error_text = "\n".join(f"• {e}" for e in errors)
                        if renamed:
                            # Partial success: some profiles imported, some errors
                            QMessageBox.warning(
                                self.main_window, "Import Partially Successful",
                                f"Some items were imported from {file_path}, "
                                f"but errors occurred:\n\n{error_text}"
                            )
                        else:
                            QMessageBox.warning(
                                self.main_window, "Import Errors",
                                f"The file was read, but errors occurred during import:"
                                f"\n\n{error_text}"
                            )
                    elif renamed:
                        self._show_rename_notification(file_path, renamed)
                    else:
                        QMessageBox.information(
                            self.main_window, "Success", 
                            f"Configuration imported successfully from {file_path}"
                        )

                except json.JSONDecodeError:
                    QMessageBox.critical(
                        self.main_window, "Error",
                        "The selected file is not valid JSON.\n"
                        "Please select a valid AV Spex config export file."
                    )
                except Exception as e:
                    logger.error(f"Error importing config: {str(e)}")
                    QMessageBox.critical(self.main_window, "Error", f"Error importing configuration: {str(e)}")

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
            
            msg = QMessageBox(self.main_window)
            msg.setIcon(QMessageBox.Icon.Information)
            msg.setWindowTitle("Import Successful — Profiles Renamed")
            msg.setText(
                f"Configuration imported from {os.path.basename(file_path)}.\n\n"
                "Some imported profiles were renamed to avoid "
                "conflicts with existing profiles:"
            )
            msg.setInformativeText(rename_text)
            msg.setDetailedText(
                "When an imported profile has the same name as an existing profile, "
                "AV Spex adds an '(imported)' suffix to the imported profile to "
                "preserve both versions.\n\n"
                "You can rename imported profiles through Manage Profiles "
                "in the Checks tab."
            )
            msg.setStandardButtons(QMessageBox.StandardButton.Ok)
            msg.exec()

        def export_config_dialog(self, config_type):
            """Export configuration to a file."""
            file_dialog = QFileDialog(self.main_window, "Export Configuration")
            file_dialog.setNameFilter("JSON Files (*.json);;All Files (*)")
            file_dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            file_dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            file_dialog.setDefaultSuffix("json")
            
            if file_dialog.exec():
                file_path = file_dialog.selectedFiles()[0]
                try:
                    # Use the ConfigIO class to export config
                    config_io = ConfigIO(config_mgr)
                    # Needs to deliver config_type as a list
                    config_io.save_config_files(file_path, config_type)
                    
                    QMessageBox.information(self.main_window, "Success", f"Configuration exported successfully to {file_path}")
                except Exception as e:
                    logger.error(f"Error exporting config: {str(e)}")
                    QMessageBox.critical(self.main_window, "Error", f"Error exporting configuration: {str(e)}")

        def reset_config(self):
            """Reset configuration to default values."""
            # Ask for confirmation
            result = QMessageBox.question(
                self.main_window,
                "Confirm Reset",
                "Are you sure you want to reset all configuration to default values? This cannot be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            
            if result == QMessageBox.StandardButton.Yes:
                try:
                    config_mgr.reset_config('checks', ChecksConfig)
                    config_mgr.reset_config('spex', SpexConfig)
                    config_mgr.reset_config('filename', FilenameConfig)
                    config_mgr.reset_config('signalflow', SignalflowConfig)
            
                    config_mgr.save_config('checks', is_last_used=True)
                    config_mgr.save_config('spex', is_last_used=True)

                    # Reload UI components to reflect new settings
                    self.main_window.config_widget.load_config_values()

                    # Get fresh copies of configs after reset
                    checks_config = config_mgr.get_config('checks', ChecksConfig)

                    # Checks Tab dropdowns
                    # Refresh the checks profile dropdown (includes custom profiles)
                    if hasattr(self.main_window, 'checks_tab') and hasattr(self.main_window.checks_tab, 'profile_handlers'):
                        self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                    elif hasattr(self.main_window, 'checks_profile_dropdown'):
                        # Fallback: manually set the built-in profile selection
                        self.main_window.checks_profile_dropdown.blockSignals(True)
                        
                        # Set based on exiftool.run_tool value (using boolean now)
                        if checks_config.tools.exiftool.run_tool and not checks_config.tools.exiftool.check_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Vendor")
                        elif checks_config.tools.exiftool.run_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Step 1")
                        elif not checks_config.tools.exiftool.run_tool and checks_config.tools.exiftool.check_tool:
                            self.main_window.checks_profile_dropdown.setCurrentText("Step 2")
                        else:
                            # Fallback to "All Off" if both are False
                            self.main_window.checks_profile_dropdown.setCurrentText("All Off")
                        
                        self.main_window.checks_profile_dropdown.blockSignals(False)

                    # Spex Tab — one call re-syncs every card (dropdowns,
                    # active-profile state lines, and summaries)
                    if hasattr(self.spex_tab, 'refresh'):
                        self.spex_tab.refresh()

                    QMessageBox.information(self.main_window, "Success", "Configuration has been reset to default values")
                except Exception as e:
                    logger.error(f"Error resetting config: {str(e)}")
                    QMessageBox.critical(self.main_window, "Error", f"Error resetting configuration: {str(e)}")
    
    class DialogHandlers:
        """Dialog implementations for the import tab"""
        
        def __init__(self, parent_tab):
            self.parent_tab = parent_tab
            self.main_window = parent_tab.main_window
        
        def show_config_info(self):
            """Define the information dialog method"""
            # Create a custom dialog
            dialog = QDialog(self.main_window)
            dialog.setWindowTitle("Configuration Management Help")
            dialog.setMinimumWidth(500)
            
            # Create layout
            layout = QVBoxLayout(dialog)
            
            # Title
            title = QLabel("<h2>Configuration Management</h2>")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title)
            
            # Content
            content = QTextBrowser()
            content.setOpenExternalLinks(True)
            content.setHtml("""
            <p>This section allows you to save, load, or reset AV Spex configuration settings.</p>
            
            <p><b>Import Config</b><br>
            • Loads previously saved configuration settings from a JSON file<br>
            • Import file can be Checks Config, Spex Config, or All Config<br>
            • Compatible with files created using the Export feature<br>
            • Imported custom profiles are added alongside your existing profiles<br>
            • If an imported profile has the same name as an existing one, it will be 
              renamed with an "(imported)" suffix</p>
            
            <p><b>Export Config</b><br>
            • Saves your current configuration settings to a JSON file<br>
            • Options:<br>
            - <i>Checks Config</i>: Exports only which tools run and how (fixity settings, 
                which tools run/check, etc.)<br>
            - <i>Spex Config</i>: Exports only the expected values for file validation
                (codecs, formats, naming conventions, etc.)<br>
            - <i>Custom Checks Profiles</i>: Exports your custom checks profiles for sharing 
                with other users<br>
            - <i>Exiftool Profiles</i>: Exports your custom exiftool profiles<br>
            - <i>MediaInfo Profiles</i>: Exports your custom MediaInfo profiles<br>
            - <i>FFprobe Profiles</i>: Exports your custom FFprobe profiles<br>
            - <i>Spex and Checks Config</i>: Exports both Checks and Spex settings<br>
            - <i>All Config</i>: Exports everything including custom profiles</p>
            
            <p><b>Reset to Default</b><br>
            • Restores all settings to the application's built-in defaults<br>
            • Use this if settings have been changed and you want to start fresh<br>
            • Note: This action cannot be undone</p>
            """)
            
            layout.addWidget(content)
        
            # Close button
            close_button = QPushButton("Close")
            close_button.clicked.connect(dialog.accept)
            layout.addWidget(close_button)
            
            # Show dialog
            dialog.exec()
        
        def show_about_dialog(self):
            """Show the About dialog with version information and logo."""
            # Create a dialog
            about_dialog = QDialog(self.main_window)
            about_dialog.setWindowTitle("About AV Spex")
            about_dialog.setMinimumWidth(400)
            
            # Create layout
            layout = QVBoxLayout(about_dialog)
            
            # Add logo
            logo_label = QLabel()
            logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            # Get the logo path
            logo_path = config_mgr.get_logo_path('av_spex_the_logo.png')
            
            # Use ThemeManager to load logo
            theme_manager = ThemeManager.instance()
            theme_manager.load_logo(logo_label, logo_path, width=300)
            
            layout.addWidget(logo_label)
            
            # Add version information
            version_label = QLabel(f"Version: {version_string}")
            version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            version_label.setStyleSheet("font-weight: bold; font-size: 14px; margin: 10px;")
            layout.addWidget(version_label)
            
            # Add additional information if needed
            info_label = QLabel("AV Spex - Audio/Video Specification Checker")
            info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(info_label)
            
            copyright_label = QLabel("GNU General Public License v3.0")
            copyright_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(copyright_label)
            
            # Show the dialog
            about_dialog.exec()
            
    def __init__(self, main_window):
        self.main_window = main_window
        
        # Initialize nested handler classes
        self.config_handlers = self.ConfigHandlers(self)
        self.dialog_handlers = self.DialogHandlers(self)
    
        # Initialize theme handling
        self.setup_theme_handling()

    def on_theme_changed(self, palette):
        """Handle theme changes for this tab"""
        theme_manager = ThemeManager.instance()
        
        # Update all group boxes
        for group_box in self.main_window.import_tab_group_boxes:
            if group_box is not None:
                theme_manager.style_groupbox(group_box)
        
        # Style combobox
        if hasattr(self.main_window, 'export_config_dropdown'):
            theme_manager.style_combobox(self.main_window.export_config_dropdown)
            
        # Update buttons in groups (this will override special styling)
        if hasattr(self, 'import_group'):
            theme_manager.style_buttons(self.import_group)
            
        if hasattr(self, 'config_import_group'):
            theme_manager.style_buttons(self.config_import_group)
        
        # Re-apply special button styling after theme changes
        if hasattr(self.main_window, 'check_spex_button'):
            theme_manager.style_button(self.main_window.check_spex_button, special_style="check_spex")
        
        if hasattr(self.main_window, 'open_processing_button'):
            theme_manager.style_button(self.main_window.open_processing_button, special_style="processing_window")
            
        if hasattr(self.main_window, 'cancel_processing_button'):
            theme_manager.style_button(self.main_window.cancel_processing_button, special_style="cancel_processing")
    
    def setup_import_tab(self):
        """Set up the Import tab for directory selection"""
        # Get the theme manager instance
        theme_manager = ThemeManager.instance()
        
        # Initialize the group boxes collection for this tab
        self.main_window.import_tab_group_boxes = []
        
        # Create the tab
        import_tab = QWidget()
        import_layout = QVBoxLayout(import_tab)
        self.main_window.tabs.addTab(import_tab, "Import")
        
        # Main scroll area
        main_scroll_area = QScrollArea(self.main_window)
        main_scroll_area.setWidgetResizable(True)
        main_widget = QWidget(self.main_window)
        main_scroll_area.setWidget(main_widget)
        
        # Vertical layout for the content
        vertical_layout = QVBoxLayout(main_widget)
        
        # Import directory section
        self.import_group = QGroupBox("Import Directories")
        theme_manager.style_groupbox(self.import_group, "top center")
        self.main_window.import_tab_group_boxes.append(self.import_group)
        
        import_layout_section = QVBoxLayout()
        
        # Import directory button
        import_directories_button = QPushButton("Import Directory...")
        import_directories_button.clicked.connect(self.import_directories)
        theme_manager.style_button(import_directories_button)
        
        # Directory section
        directory_label = QLabel("Selected Directories:")
        directory_label.setStyleSheet("font-weight: bold;")
        self.main_window.directory_list = DirectoryListWidget(self.main_window)
        self.main_window.directory_list.setStyleSheet("""
            QListWidget {
                border: 1px solid gray;
                border-radius: 3px;
            }
        """)
        
        # Delete button
        delete_button = QPushButton("Delete Selected")
        delete_button.clicked.connect(self.delete_selected_directory)
        theme_manager.style_button(delete_button)
        
        # Add widgets to layout
        import_layout_section.addWidget(import_directories_button)
        import_layout_section.addWidget(directory_label)
        import_layout_section.addWidget(self.main_window.directory_list)
        import_layout_section.addWidget(delete_button)
        
        self.import_group.setLayout(import_layout_section)
        vertical_layout.addWidget(self.import_group)
        
        # Config Import section
        self.config_import_group = QGroupBox("Config Import")
        theme_manager.style_groupbox(self.config_import_group, "top center")
        self.main_window.import_tab_group_boxes.append(self.config_import_group)
        
        config_import_layout = QVBoxLayout()

        # Create a horizontal layout for the header row
        header_layout = QHBoxLayout()

        # Create the config info button
        info_button = QPushButton()
        info_button.setIcon(self.main_window.style().standardIcon(QStyle.StandardPixmap.SP_MessageBoxInformation))
        info_button.setFixedSize(24, 24)
        info_button.setToolTip("Click for more info about config options")
        info_button.setFlat(True)  # Make it look like just an icon
        info_button.clicked.connect(self.dialog_handlers.show_config_info)
        header_layout.addWidget(info_button)

        # Description label
        config_desc_label = QLabel("Import, export, or reset Checks/Spex configuration:")
        config_desc_label.setStyleSheet("font-weight: bold;")
        header_layout.addWidget(config_desc_label)

        # Add a stretch to push the info button to the right
        header_layout.addStretch(1)

        # Add the header layout to the main vertical layout
        config_import_layout.addLayout(header_layout)

        # Add some spacing
        config_import_layout.addSpacing(10)
        
        # Create buttons layout
        buttons_layout = QHBoxLayout()
        
        # Import Config button
        import_config_button = QPushButton("Import Config")
        import_config_button.clicked.connect(self.config_handlers.import_config)
        theme_manager.style_button(import_config_button)
        buttons_layout.addWidget(import_config_button)
        
        # Export Config layout
        export_button_layout = QHBoxLayout()

        # Create the dropdown for export options
        self.main_window.export_config_dropdown = QComboBox()

        # Add the default placeholder option first
        self.main_window.export_config_dropdown.addItem("Export Config Type...")  
        self.main_window.export_config_dropdown.addItem("Export All File name Profiles")
        self.main_window.export_config_dropdown.addItem("Export All Signal flow Profiles")
        self.main_window.export_config_dropdown.addItem("Export Checks Config")
        self.main_window.export_config_dropdown.addItem("Export Spex Config")
        self.main_window.export_config_dropdown.addItem("Export All Custom Checks Profiles")
        self.main_window.export_config_dropdown.addItem("Export All Exiftool Profiles")
        self.main_window.export_config_dropdown.addItem("Export All MediaInfo Profiles")
        self.main_window.export_config_dropdown.addItem("Export All FFprobe Profiles")
        self.main_window.export_config_dropdown.addItem("Export Spex and Checks Config")
        self.main_window.export_config_dropdown.addItem("Export All Config")

        # Connect the combobox signal to your function
        self.main_window.export_config_dropdown.currentIndexChanged.connect(self.config_handlers.export_selected_config)

        theme_manager.style_combobox(self.main_window.export_config_dropdown)
        
        # Add widgets to layout
        export_button_layout.addWidget(self.main_window.export_config_dropdown)
        buttons_layout.addLayout(export_button_layout)

        # Set the first item as the current item (the placeholder)
        self.main_window.export_config_dropdown.setCurrentIndex(0)
        
        # Reset to Default Config button
        reset_config_button = QPushButton("Reset to Default")
        reset_config_button.clicked.connect(self.config_handlers.reset_config)
        theme_manager.style_button(reset_config_button)
        buttons_layout.addWidget(reset_config_button)
        
        config_import_layout.addLayout(buttons_layout)
        
        self.config_import_group.setLayout(config_import_layout)
        vertical_layout.addWidget(self.config_import_group)
        
        # Add scroll area to main layout
        import_layout.addWidget(main_scroll_area)
        
        # Bottom section with processing controls
        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 10, 0, 10)  # Add some vertical padding
        
        # Open Processing Window button
        self.main_window.open_processing_button = QPushButton("Show Processing Window")
        theme_manager.style_button(self.main_window.open_processing_button, special_style="processing_window")
        self.main_window.open_processing_button.clicked.connect(self.main_window.signals_handler.on_open_processing_clicked)
        # Initially disable the button since no processing is running
        self.main_window.open_processing_button.setEnabled(False)
        bottom_row.addWidget(self.main_window.open_processing_button)
        
        # Cancel button
        self.main_window.cancel_processing_button = QPushButton("Cancel Processing")
        theme_manager.style_button(self.main_window.cancel_processing_button, special_style="cancel_processing")
        self.main_window.cancel_processing_button.clicked.connect(self.main_window.processing.cancel_processing)
        self.main_window.cancel_processing_button.setEnabled(False)
        bottom_row.addWidget(self.main_window.cancel_processing_button)
        
        # create layout for current processing
        self.now_processing_layout = QVBoxLayout()
        
        # Add a status label that shows current file being processed
        self.main_window.main_status_label = QLabel("Not processing")
        self.main_window.main_status_label.setWordWrap(True)
        self.main_window.main_status_label.setMaximumWidth(300)  # Limit width to prevent stretching
        self.main_window.main_status_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)  # Minimize height
        self.main_window.main_status_label.setVisible(False)  # initially hidden 
        self.now_processing_layout.addWidget(self.main_window.main_status_label)
        
        # Add a small indeterminate progress bar
        self.main_window.processing_indicator = QProgressBar(self.main_window)
        self.main_window.processing_indicator.setMaximumWidth(100)  # Make it small
        self.main_window.processing_indicator.setMaximumHeight(10)  # Make it shorter
        self.main_window.processing_indicator.setRange(0, 0)
        self.main_window.processing_indicator.setTextVisible(False)  # No percentage text
        theme_manager.style_progress_bar(self.main_window.processing_indicator)
        self.main_window.processing_indicator.setVisible(False)  # Initially hidden
        self.now_processing_layout.addWidget(self.main_window.processing_indicator)
        
        # Add the processing button layout to the bottom row
        # Use a stretch factor of 0 to keep it from expanding
        bottom_row.addLayout(self.now_processing_layout, 0)
        
       # Add a stretch to push the buttons to the right
        bottom_row.addStretch(1)
        
        # Dry Run button - analyze what would happen without processing
        self.main_window.dry_run_button = QPushButton("Dry Run")
        self.main_window.dry_run_button.setToolTip(
            "Analyze what would happen during processing without actually running any tools"
        )
        theme_manager.style_button(self.main_window.dry_run_button)
        self.main_window.dry_run_button.clicked.connect(self.on_dry_run_clicked)
        bottom_row.addWidget(self.main_window.dry_run_button, 0)
        
        # Check Spex button
        self.main_window.check_spex_button = QPushButton("Check Spex!")
        theme_manager.style_button(self.main_window.check_spex_button, special_style="check_spex")
        self.main_window.check_spex_button.clicked.connect(self.on_check_spex_clicked)
        bottom_row.addWidget(self.main_window.check_spex_button, 0)
        
        import_layout.addLayout(bottom_row)
    
    def import_directories(self):
        """Import directories for processing."""
        # Get the last directory from settings
        last_directory = self.main_window.settings.value('last_directory', '')
        
        # Use native file dialog
        file_dialog = QFileDialog(self.main_window, "Select Directories")
        file_dialog.setFileMode(QFileDialog.FileMode.Directory)
        file_dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        
        # Set the starting directory to the parent of the last used directory
        if last_directory:
            dir_info = QDir(last_directory)
            if dir_info.cdUp():  # Move up to parent directory
                parent_dir = dir_info.absolutePath()
                file_dialog.setDirectory(parent_dir)
        
        # Try to enable multiple directory selection with the native dialog
        file_dialog.setOption(QFileDialog.Option.ReadOnly, False)

        if file_dialog.exec():
            directories = file_dialog.selectedFiles()  # Get selected directories
            
            # Save the last used directory
            if directories:
                self.main_window.settings.setValue('last_directory', directories[0])
                self.main_window.settings.sync()  # Ensure settings are saved
            
            for directory in directories:
                if directory not in self.main_window.selected_directories:
                    self.main_window.selected_directories.append(directory)
                    self.main_window.directory_list.addItem(directory)
    
    def update_selected_directories(self):
        """Update source_directories from the QListWidget."""
        self.main_window.source_directories = [self.main_window.directory_list.item(i).text() for i in range(self.main_window.directory_list.count())]

    def get_source_directories(self):
        """Return the selected directories if Check Spex was clicked."""
        return self.main_window.selected_directories if self.main_window.check_spex_clicked else None
    
    def delete_selected_directory(self):
        """Delete the selected directory from the list widget and the selected_directories list."""
        # Get the selected items
        selected_items = self.main_window.directory_list.selectedItems()
        
        if not selected_items:
            return  # No item selected, do nothing
        
        # Remove each selected item from both the QListWidget and selected_directories list
        for item in selected_items:
            # Remove from the selected_directories list
            directory = item.text()
            if directory in self.main_window.selected_directories:
                self.main_window.selected_directories.remove(directory)
            
            # Remove from the QListWidget
            self.main_window.directory_list.takeItem(self.main_window.directory_list.row(item))

    def on_check_spex_clicked(self):
        """Handle the Check Spex button click."""
        self.update_selected_directories()
        self.main_window.check_spex_clicked = True  # Mark that the button was clicked
        config_mgr.save_config('checks', is_last_used=True)
        config_mgr.save_config('spex', is_last_used=True)
        # Make sure the processing window is visible before starting the process
        if hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            # If it exists but might be hidden, show it
            self.main_window.processing_window.show()
            self.main_window.processing_window.raise_()
            self.main_window.processing_window.activateWindow()
        
        # Call worker thread
        self.main_window.processing.call_process_directories()

    def on_dry_run_clicked(self):
        """Handle the Dry Run button click."""
        self.update_selected_directories()
        
        # Check if any directories are selected
        if not self.main_window.source_directories:
            QMessageBox.warning(
                self.main_window,
                "No Directories Selected",
                "Please select at least one directory to analyze."
            )
            return
        
        # Save current config state (so dry run uses current settings)
        config_mgr.save_config('checks', is_last_used=True)
        config_mgr.save_config('spex', is_last_used=True)
        
        # Make sure the processing window is visible
        if hasattr(self.main_window, 'processing_window') and self.main_window.processing_window:
            self.main_window.processing_window.show()
            self.main_window.processing_window.raise_()
            self.main_window.processing_window.activateWindow()
        
        # Call dry run
        self.main_window.processing.call_dry_run()