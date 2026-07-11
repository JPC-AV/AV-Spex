from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout
)
from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QPalette

import os
import sys

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.utils.config_setup import SpexConfig, ChecksConfig
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.log_setup import logger

from AV_Spex.processing.worker_thread import ProcessingWorker
from AV_Spex.processing.avspex_processor import AVSpexProcessor
from AV_Spex.gui.gui_signals import ProcessingSignals

from AV_Spex import __version__
version_string = __version__

from AV_Spex.gui.gui_main_window.gui_main_window_ui import MainWindowUI
from AV_Spex.gui.gui_main_window.gui_main_window_signals import MainWindowSignals
from AV_Spex.gui.gui_main_window.gui_main_window_processing import MainWindowProcessing
from AV_Spex.gui.gui_main_window.gui_main_window_theme import MainWindowTheme

from AV_Spex.gui.gui_import_tab import ImportTab
from AV_Spex.gui.gui_checks_tab.gui_checks_tab import ChecksTab
from AV_Spex.gui.gui_spex_tab import SpexTab
from AV_Spex.gui.gui_complex_tab import ComplexTab

# Get configuration manager
config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)


class MainWindow(QMainWindow, ThemeableMixin):
    """Main application window with tabs for configuration and settings."""
    
    def __init__(self):
        super().__init__()
        self.signals = ProcessingSignals()
        self.worker = None
        self.processing_window = None

        # Initialize collections for theme-aware components
        self.import_tab_group_boxes = [] 
        self.spex_tab_group_boxes = []
        self.checks_tab_group_boxes = []
        self.complex_tab_group_boxes = [] 
        
        # Initialize settings
        self.settings = QSettings('NMAAHC', 'AVSpex')
        self.selected_directories = []
        self.check_spex_clicked = False
        self.source_directories = []

        # Initialize MainWindow
        self.ui = MainWindowUI(self)
        self.signals_handler = MainWindowSignals(self)
        self.processing = MainWindowProcessing(self)
        self.theme = MainWindowTheme(self)

        #Initialize Tabs
        self.checks_tab = ChecksTab(self)
        self.spex_tab = SpexTab(self)
        self.import_tab = ImportTab(self)
        self.complex_tab = ComplexTab(self)

        # Connect all signals
        self.signals_handler.setup_signal_connections()
        
        # Setup UI
        self.ui.setup_ui()
        
        # Setup theme handling
        self.setup_theme_handling()


    def on_theme_changed(self, palette):
        """Override the ThemeableMixin method to properly handle theme changes"""

        # Call the parent implementation first
        super().on_theme_changed(palette)
        
        # Now delegate to our theme helper
        if hasattr(self, 'theme'):
            self.theme.on_theme_changed(palette)
            
    
    def closeEvent(self, event):
        """Handle application shutdown and clean up resources."""
        # Clean up any child windows first
        for child_name in ['config_widget', 'processing_window', 'new_window', 'complex_widget', 'help_window']:
            child = getattr(self, child_name, None)
            if child and isinstance(child, QWidget):
                # If it has theme handling, clean it up
                if hasattr(child, 'cleanup_theme_handling'):
                    child.cleanup_theme_handling()
                child.close()
        
        # Clean up tab theme connections
        for tab_name in ['import_tab', 'checks_tab', 'spex_tab', 'complex_tab']:
            tab = getattr(self, tab_name, None)
            if tab and hasattr(tab, 'cleanup_theme_handling'):
                tab.cleanup_theme_handling()
        
        # Stop worker if running
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait()
        
        # Clean up main window theme connections last
        self.cleanup_theme_handling()
        
        # Call parent implementation
        super().closeEvent(event)
    