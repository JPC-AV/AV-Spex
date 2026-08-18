"""Thin shim for the Spex tab.

All real UI lives in SpexWindow (gui_spex_window.py) — a card grid, one
card per spex domain. This class only creates the tab page and scroll area
and delegates the public API, mirroring the ChecksTab → ChecksWindow and
ComplexTab → ComplexWindow structure.
"""

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea

from AV_Spex.gui.gui_theme_manager import ThemeableMixin
from AV_Spex.gui.gui_spex_window import SpexWindow


class SpexTab(ThemeableMixin):
    """Spex tab: expected-value profiles shown as cards."""

    def __init__(self, main_window):
        self.main_window = main_window
        self.spex_window = None

    def setup_spex_tab(self):
        """Create the Spex tab page and embed the card grid."""
        # Kept for compatibility with MainWindow's theme registry
        self.main_window.spex_tab_group_boxes = []

        spex_tab = QWidget()
        spex_layout = QVBoxLayout(spex_tab)
        self.main_window.tabs.addTab(spex_tab, "Spex")

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setStyleSheet("QScrollArea { border: none; }")
        scroll_area.setMinimumWidth(450)

        self.spex_window = SpexWindow(main_window=self.main_window)
        scroll_area.setWidget(self.spex_window)
        self.main_window.spex_tab_group_boxes = list(self.spex_window.cards.values())

        spex_layout.addWidget(scroll_area)

    # --- public API ---------------------------------------------------------

    def refresh(self):
        """Re-sync all cards with the configs on disk (single entry point,
        used by the Import tab after config import/reset)."""
        if self.spex_window is not None:
            self.spex_window.refresh()

    def set_signalflow_enabled(self, is_mkv: bool):
        """Gray the signal-flow card for non-MKV input. Called by the
        Checks tab when the configured video extension changes."""
        if self.spex_window is not None:
            self.spex_window.set_signalflow_enabled(is_mkv)

    def on_theme_changed(self, palette):
        # SpexWindow registers its own theme handling; nothing to do here.
        pass
