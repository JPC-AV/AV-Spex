"""Card widget for the Spex tab.

Each spex domain (filename, MediaInfo, Exiftool, FFprobe, signal flow,
qct-parse) is shown as one ProfileCard: a profile dropdown, an inline state
line, a one-line summary of the current expected values, and View / Edit /
New / Delete buttons. The card is purely presentational — it emits signals
and exposes setters; all config reads/writes live in SpexWindow.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

from PyQt6.QtWidgets import (
    QGroupBox, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QSizePolicy
)
from PyQt6.QtCore import pyqtSignal

from AV_Spex.gui.gui_theme_manager import ThemeManager

PLACEHOLDER = "Select a profile..."


@dataclass
class DomainSpec:
    """Everything SpexWindow needs to drive one card generically."""
    key: str                        # active_profiles key, e.g. 'exiftool'
    title: str                      # card title, e.g. "Exiftool"
    description: str                # inline description under the title
    summary_fn: Callable            # spex values -> one-line summary string
    spex_values_fn: Callable        # SpexConfig -> that domain's values object
    view_fn: Callable               # SpexConfig -> {section: {key: value}} for the View dialog
    has_profiles: bool = True       # False for the read-only qct-parse card
    readonly_note: str = ""         # state line shown when has_profiles is False
    apply_fn: Optional[Callable] = None    # (profile, profile_name=...) applies to spex
    save_fn: Optional[Callable] = None     # (name, profile_data) -> bool
    delete_fn: Optional[Callable] = None   # (name) -> bool
    dialog_factory: Optional[Callable] = None  # (parent, edit_mode, profile_name) -> QDialog


class ProfileCard(QGroupBox):
    """One spex domain as a card: dropdown + state + summary + buttons."""

    profileSelected = pyqtSignal(str)
    viewRequested = pyqtSignal()
    editRequested = pyqtSignal()
    newRequested = pyqtSignal()
    deleteRequested = pyqtSignal()

    def __init__(self, spec: DomainSpec, parent=None):
        super().__init__(spec.title, parent)
        self.spec = spec
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        description = QLabel(spec.description)
        description.setWordWrap(True)
        description.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(description)

        # Profile dropdown row
        self.profile_dropdown = None
        if spec.has_profiles:
            dropdown_row = QHBoxLayout()
            self.profile_dropdown = QComboBox()
            self.profile_dropdown.setMinimumWidth(220)
            self.profile_dropdown.currentTextChanged.connect(self._on_dropdown_changed)
            dropdown_row.addWidget(self.profile_dropdown, stretch=1)
            layout.addLayout(dropdown_row)

        # State line (inline explanation, never a tooltip)
        self.state_label = QLabel(spec.readonly_note if not spec.has_profiles else "")
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("color: gray; font-style: italic; font-size: 11px;")
        layout.addWidget(self.state_label)

        # Summary line
        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.summary_label)

        # Button row
        button_row = QHBoxLayout()
        self.view_button = QPushButton("View")
        self.view_button.clicked.connect(self.viewRequested.emit)
        button_row.addWidget(self.view_button)

        self.edit_button = None
        self.new_button = None
        self.delete_button = None
        if spec.has_profiles:
            self.edit_button = QPushButton("Edit...")
            self.edit_button.clicked.connect(self.editRequested.emit)
            button_row.addWidget(self.edit_button)

            self.new_button = QPushButton("New...")
            self.new_button.clicked.connect(self.newRequested.emit)
            button_row.addWidget(self.new_button)

            self.delete_button = QPushButton("Delete")
            self.delete_button.clicked.connect(self.deleteRequested.emit)
            button_row.addWidget(self.delete_button)

        button_row.addStretch()
        layout.addLayout(button_row)

    # --- signal plumbing ----------------------------------------------------

    def _on_dropdown_changed(self, text):
        if text and text != PLACEHOLDER:
            self.profileSelected.emit(text)

    # --- setters used by SpexWindow ----------------------------------------

    def set_profiles(self, names, active_name=None):
        """Rebuild the dropdown; select active_name or the placeholder."""
        if self.profile_dropdown is None:
            return
        self.profile_dropdown.blockSignals(True)
        self.profile_dropdown.clear()
        if active_name is None or active_name not in names:
            self.profile_dropdown.addItem(PLACEHOLDER)
        for name in names:
            self.profile_dropdown.addItem(name)
        if active_name is not None and active_name in names:
            self.profile_dropdown.setCurrentText(active_name)
        self.profile_dropdown.blockSignals(False)

    def selected_profile(self):
        """The selected profile name, or None if on the placeholder."""
        if self.profile_dropdown is None:
            return None
        text = self.profile_dropdown.currentText()
        return text if text and text != PLACEHOLDER else None

    def set_state_text(self, text):
        self.state_label.setText(text)

    def set_summary(self, text):
        self.summary_label.setText(text)

    def set_protected(self, protected):
        """Swap Edit/Duplicate labeling and gate Delete for built-ins."""
        if self.edit_button is not None:
            self.edit_button.setText("Duplicate..." if protected else "Edit...")
        if self.delete_button is not None:
            self.delete_button.setEnabled(not protected and self.selected_profile() is not None)

    def set_card_enabled(self, enabled):
        """Enable/disable the interactive controls (used for the signal-flow
        card when the configured input extension is not MKV)."""
        if self.profile_dropdown is not None:
            self.profile_dropdown.setEnabled(enabled)
        for button in (self.edit_button, self.new_button, self.delete_button):
            if button is not None:
                button.setEnabled(enabled)

    def restyle(self):
        """Re-apply theme-managed styling (called from on_theme_changed)."""
        theme_manager = ThemeManager.instance()
        theme_manager.style_buttons(self)
        if self.profile_dropdown is not None:
            theme_manager.style_combobox(self.profile_dropdown)
