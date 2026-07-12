from PyQt6.QtWidgets import QLabel, QHBoxLayout, QMainWindow
from PyQt6.QtCore import Qt

import os

from AV_Spex.gui.gui_theme_manager import ThemeManager
from AV_Spex.utils.config_manager import ConfigManager

config_mgr = ConfigManager()

class MainWindowTheme:
    """Theme handling helper methods for the main window"""
    
    def __init__(self, main_window):
        self.main_window = main_window
        # Keep track of the logo label to avoid duplicates
        self.logo_widget = None
    
    def on_theme_changed(self, palette):
        """Handle theme changes across the application."""
        # Get the theme manager
        theme_manager = ThemeManager.instance()
        
        # Apply palette to main window
        self.main_window.setPalette(palette)
        
        # Update the tabs
        if hasattr(self.main_window, 'tabs'):
            theme_manager.style_tabs(self.main_window.tabs)

        # Style comboboxes
        if hasattr(self.main_window, 'export_config_dropdown'):
            theme_manager.style_combobox(self.main_window.export_config_dropdown)
        
        # Style the special buttons
        self._style_special_buttons()
        
        # Only refresh logo if we're in the main window, not a dialog
        if isinstance(self.main_window, QMainWindow) and hasattr(self.main_window, 'main_layout'):
            self._refresh_logo()
        
        # Force repaint
        self.main_window.update()
    
    def _refresh_logo(self):
        """Refresh the logo when theme changes"""
        # First check if we have a main layout
        if not hasattr(self.main_window, 'main_layout'):
            return
            
        # First remove any existing logo
        self._remove_existing_logo()
        
        # Now load the new theme-appropriate logo
        self._load_logo()
    
    def _remove_existing_logo(self):
        """Find and remove any existing logo"""
        # First try to remove our tracked logo widget if it exists
        if self.logo_widget is not None:
            # Remove tracked widget
            if self.logo_widget.parent():
                self.logo_widget.setParent(None)
                self.logo_widget.deleteLater()
            self.logo_widget = None
            
        # Scan through main layout items to find any other logo layouts
        for i in range(self.main_window.main_layout.count()):
            item = self.main_window.main_layout.itemAt(i)
            if item and item.layout():
                layout = item.layout()
                # Look for any QLabel with a pixmap in the layout
                for j in range(layout.count()):
                    inner_item = layout.itemAt(j)
                    if inner_item and inner_item.widget() and isinstance(inner_item.widget(), QLabel) and inner_item.widget().pixmap() is not None:
                        # Found a logo widget, remove the entire layout
                        self._remove_layout_item(self.main_window.main_layout, i)
                        return  # Stop after removing one

    def _load_logo(self):
        """Load and display the logo based on current theme"""
        # Get ThemeManager instance
        theme_manager = ThemeManager.instance()
        
        # Define light and dark logo paths
        light_logo_path = config_mgr.get_logo_path('Branding_avspex_noJPC_030725.png')
        dark_logo_path = config_mgr.get_logo_path('Branding_avspex_noJPC_inverted_032325.png')
        
        # Get appropriate logo for current theme
        logo_path = theme_manager.get_theme_appropriate_logo(light_logo_path, dark_logo_path)
        
        # Verify logo path exists
        if not os.path.exists(logo_path):
            print(f"Logo file not found: {logo_path}")
            return
            
        # Create and add image layout
        image_layout = QHBoxLayout()
        
        # Create a new label with explicit parent
        self.logo_widget = QLabel(self.main_window)
        self.logo_widget.setMinimumHeight(100)
        
        # Use the ThemeManager to load the logo
        success = theme_manager.load_logo(self.logo_widget, logo_path, width=self.main_window.width())
        if not success:
            print(f"Failed to load logo: {logo_path}")
            return
            
        self.logo_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
        image_layout.addWidget(self.logo_widget)
        
        # Insert at the top of the main layout
        self.main_window.main_layout.insertLayout(0, image_layout)
    
    
    def _style_special_buttons(self):
        """Apply special styling to buttons that need custom styling"""
        theme_manager = ThemeManager.instance()
        
        # Style the 'Check Spex' button
        if hasattr(self.main_window, 'check_spex_button'):
            theme_manager.style_button(self.main_window.check_spex_button, special_style="check_spex")
        
        # Style the 'Show Processing Window' button
        if hasattr(self.main_window, 'open_processing_button'):
            theme_manager.style_button(self.main_window.open_processing_button, special_style="processing_window")
            
        # Style the 'Cancel Processing' button
        if hasattr(self.main_window, 'cancel_processing_button'):
            theme_manager.style_button(self.main_window.cancel_processing_button, special_style="cancel_processing")

        # Style the progress indicator
        if hasattr(self.main_window, 'processing_indicator'):
            theme_manager.style_progress_bar(self.main_window.processing_indicator)

        # Style the round '?' help button
        if hasattr(self.main_window, 'help_button'):
            self._style_help_button()

    def _style_help_button(self):
        """Style the '?' help button as a round, palette-aware button"""
        button = self.main_window.help_button
        palette = self.main_window.palette()
        button_color = palette.color(palette.ColorRole.Button).name()
        button_text_color = palette.color(palette.ColorRole.ButtonText).name()
        highlight_color = palette.color(palette.ColorRole.Highlight).name()
        highlight_text_color = palette.color(palette.ColorRole.HighlightedText).name()

        button.setStyleSheet(f"""
            QPushButton {{
                font-weight: bold;
                font-size: 14px;
                border: 1px solid gray;
                border-radius: 14px;
                background-color: {button_color};
                color: {button_text_color};
            }}
            QPushButton:hover {{
                background-color: {highlight_color};
                color: {highlight_text_color};
            }}
        """)


    def _remove_layout_item(self, layout, index):
        """Helper method to remove an item from a layout"""
        if index >= 0 and index < layout.count():
            item = layout.takeAt(index)
            if item:
                # If the item has a layout, we need to clear it first
                if item.layout():
                    while item.layout().count():
                        child = item.layout().takeAt(0)
                        if child.widget():
                            child.widget().deleteLater()
                # If the item has a widget, delete it
                if item.widget():
                    item.widget().deleteLater()
                # Delete the item itself
                del item
    
