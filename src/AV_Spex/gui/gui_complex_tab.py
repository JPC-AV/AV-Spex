from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QScrollArea
)

from AV_Spex.gui.gui_theme_manager import ThemeableMixin
from AV_Spex.gui.gui_complex_window import ComplexWindow


class ComplexTab(ThemeableMixin):
    """Complex tab for advanced configuration options"""

    def __init__(self, main_window):
        self.main_window = main_window

        # Initialize theme handling
        self.setup_theme_handling()

    def on_theme_changed(self, palette):
        """Handle theme changes for this tab"""
        # The complex widget handles its own group boxes
        if hasattr(self.main_window, 'complex_widget') and self.main_window.complex_widget:
            if hasattr(self.main_window.complex_widget, 'on_theme_changed'):
                self.main_window.complex_widget.on_theme_changed(palette)

    def setup_complex_tab(self):
        """Set up the Complex tab with theme-aware styling"""
        # Kept for compatibility with MainWindow theme handling
        self.main_window.complex_tab_group_boxes = []

        # Create the tab
        complex_tab = QWidget()
        complex_layout = QVBoxLayout(complex_tab)
        self.main_window.tabs.addTab(complex_tab, "Complex")

        # Single scroll area holding the ComplexWindow directly
        main_scroll_area = QScrollArea(self.main_window)
        main_scroll_area.setWidgetResizable(True)
        main_scroll_area.setMinimumWidth(450)
        self.main_window.complex_widget = ComplexWindow()
        main_scroll_area.setWidget(self.main_window.complex_widget)

        complex_layout.addWidget(main_scroll_area)
