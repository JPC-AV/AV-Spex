from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QLabel, QScrollArea, QMenuBar, QTabWidget, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QPixmap, QKeySequence, QAction

import os
import sys
import platform

from AV_Spex.gui.gui_theme_manager import ThemeManager
from AV_Spex.gui.gui_help_window import HelpWindow
from AV_Spex.utils import config_edit

class MainWindowUI:
    """UI components and setup for the main window"""
    
    def __init__(self, main_window):
        self.main_window = main_window
    
    def setup_ui(self):
        """Set up the main UI components"""
        self.main_window.setMinimumSize(750, 800)

        ## self.main_window.windowFlags() retrieves the current window flags
        ## Qt.WindowType.WindowMaximizeButtonHint enables the maximize button in the window's title bar.
        self.main_window.setWindowFlags(self.main_window.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        
        self.main_window.setWindowTitle("AV Spex")
        
        # Set up menu bar
        self.menu_bar = QMenuBar(self.main_window)
        self.main_window.setMenuBar(self.menu_bar)
        
        # Platform-specific menu setup
        if platform.system() == 'Darwin':
            # On macOS, the application menu is handled differently
            # File menu (this becomes the first menu after the Apple menu on macOS)
            self.file_menu = self.menu_bar.addMenu("File")
            self.import_action = self.file_menu.addAction("Import Directory")
            self.import_action.triggered.connect(self.main_window.import_tab.import_directories)
            
            # Add Quit action to the File menu on macOS
            self.file_menu.addSeparator()
            self.quit_action = self.file_menu.addAction("Quit")
            self.quit_action.setShortcut(QKeySequence.StandardKey.Quit)
            self.quit_action.triggered.connect(self.main_window.signals_handler.on_quit_clicked)
            
            # About action in its own menu
            self.help_menu = self.menu_bar.addMenu("Help")
            self.about_action = self.help_menu.addAction("About AV Spex")
            self.about_action.triggered.connect(self.main_window.import_tab.dialog_handlers.show_about_dialog)

            # Help action — ApplicationSpecificRole moves it into the
            # application menu (top left), alongside the About action
            self.help_action = self.help_menu.addAction("AV Spex Help")
            self.help_action.setMenuRole(QAction.MenuRole.ApplicationSpecificRole)
            self.help_action.triggered.connect(self.show_help_window)
        else:
            # For Windows/Linux (if we expand support)
            # App menu 
            self.app_menu = self.menu_bar.addMenu("AV Spex")
            self.about_action = self.app_menu.addAction("About AV Spex")
            self.about_action.triggered.connect(self.main_window.import_tab.dialog_handlers.show_about_dialog)

            self.help_action = self.app_menu.addAction("AV Spex Help")
            self.help_action.triggered.connect(self.show_help_window)

            # Add a separator
            self.app_menu.addSeparator()
            
            # Add Quit action to the app menu
            self.quit_action = self.app_menu.addAction("Quit")
            self.quit_action.triggered.connect(self.main_window.signals_handler.on_quit_clicked)
            
            # File menu (comes after the app menu)
            self.file_menu = self.menu_bar.addMenu("File")
            self.import_action = self.file_menu.addAction("Import Directory")
            self.import_action.triggered.connect(self.main_window.import_tab.import_directories)

        self.setup_main_layout()

        self.logo_setup()

        self.setup_tabs()

        self.setup_help_button()

        self.setup_profiles_menu()
    
    def setup_main_layout(self):
        """Set up the main window layout structure"""
        # Create and set central widget
        self.main_window.central_widget = QWidget()
        self.main_window.setCentralWidget(self.main_window.central_widget)

        # Set minimum size for the main window
        self.main_window.setMinimumSize(850, 800)  # Width: 850px, Height: 800px

        # Create main vertical layout
        self.main_window.main_layout = QVBoxLayout(self.main_window.central_widget)

        # Set layout margins and spacing
        self.main_window.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_window.main_layout.setSpacing(10)
    
    def logo_setup(self):
        """Set up the logo display"""
        self.main_window.theme._load_logo()
    
    def setup_tabs(self):
        """Set up tab styling"""
        theme_manager = ThemeManager.instance()
        
        # Create new tabs
        self.main_window.tabs = QTabWidget()
        self.main_window.tabs.setStyleSheet(theme_manager.get_tab_style())

        self.main_window.main_layout.addWidget(self.main_window.tabs)

        # Set up individual tabs
        self.main_window.import_tab.setup_import_tab()
        self.main_window.checks_tab.setup_checks_tab()
        self.main_window.spex_tab.setup_spex_tab()
        self.main_window.complex_tab.setup_complex_tab()

    def setup_help_button(self):
        """Add the round '?' help button to the bottom right corner"""
        help_button_layout = QHBoxLayout()
        help_button_layout.addStretch()

        self.main_window.help_button = QPushButton("?")
        self.main_window.help_button.setFixedSize(28, 28)
        self.main_window.help_button.setToolTip("Open AV Spex Help")
        self.main_window.help_button.clicked.connect(self.show_help_window)
        help_button_layout.addWidget(self.main_window.help_button)

        self.main_window.main_layout.addLayout(help_button_layout)

    def show_help_window(self):
        """Show the help window, creating it on first use."""
        if getattr(self.main_window, 'help_window', None) is None:
            self.main_window.help_window = HelpWindow(self.main_window)
        self.main_window.help_window.show()
        self.main_window.help_window.raise_()
        self.main_window.help_window.activateWindow()

    def setup_profiles_menu(self):
        """Set up the Profiles menu for custom profile management"""
        # Profiles menu
        self.profiles_menu = self.menu_bar.addMenu("Profiles")
        
        # Built-in profiles submenu
        self.builtin_profiles_menu = self.profiles_menu.addMenu("Built-in Profiles")
        
        self.step1_action = self.builtin_profiles_menu.addAction("Step 1 Profile")
        self.step1_action.triggered.connect(lambda: self.apply_builtin_profile('step1'))
        
        self.step2_action = self.builtin_profiles_menu.addAction("Step 2 Profile")
        self.step2_action.triggered.connect(lambda: self.apply_builtin_profile('step2'))
        
        self.all_off_action = self.builtin_profiles_menu.addAction("All Off Profile")
        self.all_off_action.triggered.connect(lambda: self.apply_builtin_profile('allOff'))

        self.vendor_action = self.builtin_profiles_menu.addAction("Vendor Profile")
        self.vendor_action.triggered.connect(lambda: self.apply_builtin_profile('vendor'))
        
        # Separator
        self.profiles_menu.addSeparator()
        
        # Custom profiles management
        self.manage_profiles_action = self.profiles_menu.addAction("Manage Custom Profiles...")
        self.manage_profiles_action.triggered.connect(self.open_profile_manager)
        
        self.create_profile_action = self.profiles_menu.addAction("Save Current as Profile...")
        self.create_profile_action.triggered.connect(self.create_profile_from_current)
        
        # Dynamic custom profiles submenu
        self.custom_profiles_menu = self.profiles_menu.addMenu("Custom Profiles")
        self.profiles_menu.aboutToShow.connect(self.update_custom_profiles_menu)

    def apply_builtin_profile(self, profile_name):
        """Apply a built-in profile."""
        try:
            if profile_name == 'step1':
                config_edit.apply_profile(config_edit.profile_step1)
            elif profile_name == 'step2':
                config_edit.apply_profile(config_edit.profile_step2)
            elif profile_name == 'allOff':
                config_edit.apply_profile(config_edit.profile_allOff)
            elif profile_name == 'vendor':
                config_edit.apply_profile(config_edit.profile_vendor)
            
            # Update the checks tab dropdown
            if hasattr(self.main_window, 'checks_tab'):
                self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
            
            # Refresh config display
            if hasattr(self.main_window, 'config_widget'):
                self.main_window.config_widget.load_config_values()
            
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.information(self.main_window, "Success", f"Applied {profile_name} profile")
            
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self.main_window, "Error", f"Failed to apply profile: {str(e)}")

    def open_profile_manager(self):
        """Open the profile management dialog."""
        from AV_Spex.gui.gui_custom_profiles import ProfileSelectionDialog
        dialog = ProfileSelectionDialog(self.main_window)
        if dialog.exec():
            # Update the checks tab dropdown
            if hasattr(self.main_window, 'checks_tab'):
                self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
            
            # Refresh config display
            if hasattr(self.main_window, 'config_widget'):
                self.main_window.config_widget.load_config_values()

    def create_profile_from_current(self):
        """Create a new profile from the current configuration."""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox
        
        # Get available custom profiles to suggest a name
        custom_profiles = config_edit.get_available_custom_profiles()
        suggested_name = f"Custom Profile {len(custom_profiles) + 1}"
        
        name, ok = QInputDialog.getText(
            self.main_window, 
            'Create Profile', 
            'Enter a name for the new profile:',
            text=suggested_name
        )
        
        if ok and name:
            try:
                # Create profile from current config
                new_profile = config_edit.create_profile_from_current_config(
                    name.strip(), 
                    "Created from current configuration"
                )
                
                # Save the profile
                config_edit.save_custom_profile(new_profile)
                
                # Update the checks tab dropdown
                if hasattr(self.main_window, 'checks_tab'):
                    self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                    # Set the dropdown to the new profile
                    dropdown = self.main_window.checks_tab.main_window.checks_profile_dropdown
                    dropdown.setCurrentText(f"[Custom] {name.strip()}")
                
                QMessageBox.information(
                    self.main_window, 
                    "Success", 
                    f"Created profile '{name}' from current configuration"
                )
                
            except Exception as e:
                QMessageBox.critical(self.main_window, "Error", f"Failed to create profile: {str(e)}")

    def update_custom_profiles_menu(self):
        """Dynamically update the custom profiles menu."""
        # Clear existing actions
        self.custom_profiles_menu.clear()
        
        # Get available custom profiles
        custom_profiles = config_edit.get_available_custom_profiles()
        
        if not custom_profiles:
            no_profiles_action = self.custom_profiles_menu.addAction("No custom profiles")
            no_profiles_action.setEnabled(False)
        else:
            for profile_name in custom_profiles:
                action = self.custom_profiles_menu.addAction(profile_name)
                action.triggered.connect(
                    lambda checked, name=profile_name: self.apply_custom_profile(name)
                )

    def apply_custom_profile(self, profile_name):
        """Apply a custom profile by name."""
        try:
            from PyQt6.QtWidgets import QMessageBox
            
            config_edit.apply_custom_profile(profile_name)
            
            # Update the checks tab dropdown
            if hasattr(self.main_window, 'checks_tab'):
                self.main_window.checks_tab.profile_handlers.refresh_profile_dropdown()
                # Set the dropdown to the applied profile
                dropdown = self.main_window.checks_tab.main_window.checks_profile_dropdown
                dropdown.setCurrentText(f"[Custom] {profile_name}")
            
            # Refresh config display
            if hasattr(self.main_window, 'config_widget'):
                self.main_window.config_widget.load_config_values()
                
            QMessageBox.information(self.main_window, "Success", f"Applied custom profile '{profile_name}'")
            
        except Exception as e:
            from PyQt6.QtWidgets import QMessageBox
            QMessageBox.critical(self.main_window, "Error", f"Failed to apply profile: {str(e)}")