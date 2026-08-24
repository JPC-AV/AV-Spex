"""
Shared pieces of the custom expected-value profile dialogs.

The MediaInfo and FFprobe dialogs are the same dialog with different field
definitions: identical field styling, and an import/compare flow that differs
only in which import module it calls and what it labels things. Both live here
so there is one copy.

Two parts:

- **Field styling** — module-level functions producing the QLineEdit and
  QComboBox stylesheets, plus the cached dropdown-arrow SVG. Explicit
  stylesheets are required (see CLAUDE.md): a styled widget loses Qt's native
  rendering, and on macOS the palette cannot be trusted to distinguish states.
- **`SectionedProfileImportMixin`** — the import-from-file and compare-with-file
  flow for a profile made of named sections. A dialog supplies three small
  hooks and a few labels.

The ExifTool dialog is not a client of the mixin: its profile is flat, so its
validation payload has no ``sections`` key. It shares nothing here yet.
"""

import os
import tempfile
from dataclasses import dataclass
from typing import Tuple

from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTabWidget,
    QTextEdit, QVBoxLayout, QWidget,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPalette

from AV_Spex.gui.gui_theme_manager import ThemeableMixin


# ---------------------------------------------------------------------------
# Field styling
# ---------------------------------------------------------------------------

# One cached SVG per stroke color. Styling a QComboBox replaces Qt's native
# arrow, so the replacement has to carry its own color — a fixed one disappears
# against the opposite theme's background.
_ARROW_SVG_PATHS = {}


def dropdown_arrow_svg_path(stroke=None):
    """Path to a chevron SVG used for QComboBox arrows.

    The stroke defaults to the palette's text color so the arrow stays legible
    in both themes. Each color is written once and cached for the process.
    """
    stroke = stroke or field_colors()['text']

    cached = _ARROW_SVG_PATHS.get(stroke)
    if cached and os.path.exists(cached):
        return cached

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="12" height="12" viewBox="0 0 12 12">'
        f'<path d="M2.5 4 L6 7.5 L9.5 4" stroke="{stroke}" '
        'stroke-width="1.75" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )
    f = tempfile.NamedTemporaryFile(
        suffix='.svg', delete=False, mode='w', prefix='avspex_arrow_'
    )
    f.write(svg)
    f.close()
    _ARROW_SVG_PATHS[stroke] = f.name
    return f.name


def field_colors():
    """Palette colors used by both field stylesheets, looked up in one place."""
    palette = QApplication.palette()
    return {
        'bg':        palette.color(QPalette.ColorRole.Base).name(),
        'text':      palette.color(QPalette.ColorRole.Text).name(),
        'border':    palette.color(QPalette.ColorRole.Mid).name(),
        'highlight': palette.color(QPalette.ColorRole.Highlight).name(),
        'hi_text':   palette.color(QPalette.ColorRole.HighlightedText).name(),
    }


def field_lineedit_style(colors=None):
    """Stylesheet for standalone QLineEdit field inputs."""
    c = colors or field_colors()
    return (
        f"QLineEdit {{"
        f" background-color: {c['bg']};"
        f" color: {c['text']};"
        f" border: 1px solid {c['border']};"
        f" border-radius: 3px;"
        f" padding: 2px 4px;"
        f"}}"
    )


def field_combobox_style(colors=None):
    """Stylesheet for editable QComboBox field inputs.

    Applies the same Base background as standalone QLineEdits, and includes
    complete ``::drop-down`` / ``::down-arrow`` rules so Qt still draws an
    arrow once the widget is in stylesheet mode.
    """
    c = colors or field_colors()
    arrow_path = dropdown_arrow_svg_path(c['text'])
    return (
        f"QComboBox {{"
        f" background-color: {c['bg']};"
        f" color: {c['text']};"
        f" border: 1px solid {c['border']};"
        f" border-radius: 3px;"
        f" padding: 2px 4px;"
        f"}}"
        f"QComboBox:hover {{"
        f" border: 1px solid {c['highlight']};"
        f"}}"
        f"QComboBox::drop-down {{"
        f" subcontrol-origin: padding;"
        f" subcontrol-position: right;"
        f" width: 18px;"
        f" border-left: 1px solid {c['border']};"
        f" border-top-right-radius: 3px;"
        f" border-bottom-right-radius: 3px;"
        f"}}"
        f"QComboBox::down-arrow {{"
        f" image: url({arrow_path});"
        f" width: 12px;"
        f" height: 12px;"
        f"}}"
        f"QComboBox QAbstractItemView {{"
        f" background-color: {c['bg']};"
        f" color: {c['text']};"
        f" selection-background-color: {c['highlight']};"
        f" selection-color: {c['hi_text']};"
        f"}}"
    )


# ---------------------------------------------------------------------------
# Import / compare flow
# ---------------------------------------------------------------------------

class SectionedProfileImportMixin:
    """Import-from-file and compare-with-file for a sectioned profile dialog.

    A dialog opts in by setting the class attributes below and implementing the
    three hooks. Everything else — the file pickers, the result dialog, the
    per-section breakdown, the "Import These Values" path — is shared.

    Class attributes:
        IMPORT_LABEL:    tool name used in messages, e.g. "MediaInfo"
        IMPORT_CAPTION:  file-picker caption for importing
        COMPARE_CAPTION: file-picker caption for comparing
        IMPORT_FILTER:   file-picker filter string
        IMPORT_HINT:     extra guidance shown when an import yields nothing
        SECTION_LABELS:  {section_key: DISPLAY NAME}, in display order
    """

    IMPORT_LABEL = ""
    IMPORT_CAPTION = "Select File"
    COMPARE_CAPTION = "Select File to Compare"
    IMPORT_FILTER = "All Files (*.*)"
    IMPORT_HINT = ""
    SECTION_LABELS = {}

    # -- hooks the dialog must provide --------------------------------------

    def import_profile_from_path(self, file_path):
        """Parse the tool's output file into a profile dataclass, or None."""
        raise NotImplementedError

    def validate_path_against_profile(self, file_path, profile):
        """Compare a tool output file against a profile; return the result dict."""
        raise NotImplementedError

    def build_profile_from_form(self):
        """Return the profile the form currently describes, or None if invalid."""
        raise NotImplementedError

    # -- shared behavior ----------------------------------------------------

    def import_from_file(self):
        """Load a tool output file into the form."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.IMPORT_CAPTION, "", self.IMPORT_FILTER
        )
        if not file_path:
            return

        try:
            profile = self.import_profile_from_path(file_path)

            if profile:
                self.load_profile_data(profile)

                base_name = os.path.splitext(os.path.basename(file_path))[0]
                if not self.edit_mode:
                    self.profile_name_input.setText(f"Imported from {base_name}")

                QMessageBox.information(
                    self, "Import Successful",
                    f"Successfully imported {self.IMPORT_LABEL} data from:\n{file_path}"
                )
            else:
                QMessageBox.warning(
                    self, "Import Failed",
                    f"Could not import {self.IMPORT_LABEL} data from:\n{file_path}\n\n"
                    f"{self.IMPORT_HINT}"
                )
        except Exception as e:
            QMessageBox.critical(
                self, "Import Error",
                f"Error importing file:\n{str(e)}"
            )

    def compare_with_file(self):
        """Compare the form's current profile against a tool output file."""
        profile = self.build_profile_from_form()
        if not profile:
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, self.COMPARE_CAPTION, "", self.IMPORT_FILTER
        )
        if not file_path:
            return

        try:
            validation = self.validate_path_against_profile(file_path, profile)
            self.show_comparison_results(file_path, validation)
        except Exception as e:
            QMessageBox.critical(
                self, "Comparison Error",
                f"Error comparing file:\n{str(e)}"
            )

    def _comparison_detail_lines(self, validation):
        """Render the per-section match/mismatch/missing breakdown."""
        details = []

        if 'error' in validation:
            return [f"Error: {validation['error']}"]
        if 'sections' not in validation:
            return details

        for section_key, section_label in self.SECTION_LABELS.items():
            section = validation['sections'].get(section_key, {})
            matches = section.get('matches', {})
            mismatches = section.get('mismatches', {})
            missing = section.get('missing', {})

            if matches or mismatches or missing:
                details.append(f"═══ {section_label} ═══")
                details.append("")

            if matches:
                details.append("  ✅ MATCHING FIELDS:")
                for field, values in matches.items():
                    details.append(f"    {field}: {values['actual']}")
                details.append("")

            if mismatches:
                details.append("  ❌ MISMATCHED FIELDS:")
                for field, values in mismatches.items():
                    details.append(f"    {field}:")
                    details.append(f"      Expected: {values['expected']}")
                    details.append(f"      Actual: {values['actual']}")
                details.append("")

            if missing:
                details.append("  ⚠️ MISSING FIELDS:")
                for field, values in missing.items():
                    details.append(f"    {field}: Expected {values['expected']}")
                details.append("")

        return details

    @staticmethod
    def _validation_has_differences(validation):
        """True if any section reported a mismatch or a missing field."""
        return any(
            section.get('mismatches') or section.get('missing')
            for section in validation.get('sections', {}).values()
        )

    def show_comparison_results(self, file_path, validation):
        """Show comparison results in a dialog with a per-section breakdown."""
        result_dialog = QDialog(self)
        result_dialog.setWindowTitle("Comparison Results")
        result_dialog.setModal(True)
        result_dialog.setMinimumSize(650, 550)

        layout = QVBoxLayout()

        summary_label = QLabel(
            f"<b>File:</b> {os.path.basename(file_path)}<br>"
            f"<b>Status:</b> {'✅ VALID' if validation.get('valid') else '❌ INVALID'}<br>"
            f"<b>Matching Fields:</b> "
            f"{validation.get('matching_fields', 0)}/{validation.get('total_fields', 0)}"
        )
        summary_label.setWordWrap(True)
        layout.addWidget(summary_label)

        details_text = QTextEdit()
        details_text.setReadOnly(True)
        details_text.setPlainText("\n".join(self._comparison_detail_lines(validation)))
        layout.addWidget(details_text)

        if self._validation_has_differences(validation):
            import_btn = QPushButton("Import These Values")
            import_btn.clicked.connect(
                lambda: self.import_from_validation(file_path, result_dialog)
            )
            layout.addWidget(import_btn)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(result_dialog.accept)
        layout.addWidget(close_btn)

        result_dialog.setLayout(layout)
        result_dialog.exec()

    def import_from_validation(self, file_path, dialog):
        """Load the compared file's values into the form."""
        try:
            profile = self.import_profile_from_path(file_path)
            if profile:
                self.load_profile_data(profile)
                dialog.accept()
                QMessageBox.information(self, "Import Successful", "Values imported from file")
        except Exception as e:
            QMessageBox.critical(self, "Import Error", f"Error importing: {str(e)}")


# ---------------------------------------------------------------------------
# The dialog itself
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProfileSection:
    """One section of a profile, and the fields it offers.

    key:    the section's name on the profile ('general', 'video_stream'). For a
            flat profile there is a single section keyed 'root'.
    title:  tab label. None means this dialog has one untabbed section.
    fields: (field_name, label, default_values, tooltip) tuples, in display order.
    """
    key: str
    title: str
    fields: Tuple[tuple, ...]


class SectionedProfileDialog(QDialog, ThemeableMixin, SectionedProfileImportMixin):
    """Base dialog for creating and editing a custom expected-value profile.

    Subclasses declare their sections and field tables, and implement three
    hooks: how to assemble a profile from the collected values, how to split an
    existing profile back into sections, and what the preview line says.
    Everything else — layout, multi-value rows, load/collect, save, theming —
    lives here.

    A profile with one section renders as a plain scroll area; several sections
    render as tabs.
    """

    # -- per-domain configuration -------------------------------------------

    TOOL_LABEL = ""                 # appears in the window title
    NEW_DESCRIPTION = ""            # blurb shown when creating a new profile
    NAME_PLACEHOLDER = ""
    MIN_SIZE = (750, 850)
    SECTIONS: Tuple[ProfileSection, ...] = ()
    DROPDOWN_OPTIONS = {}           # {field_name: [known values]}; these get a combo box

    # ExifTool's fields are deliberately left unstyled so macOS renders them
    # natively; the sectioned dialogs style theirs to keep combo boxes and line
    # edits visually matched. See CLAUDE.md on disabled-state styling before
    # turning this on for a dialog whose fields can be disabled.
    STYLED_FIELDS = True

    # -- hooks a subclass must provide --------------------------------------

    def build_profile(self, values):
        """Assemble (and validate) the profile from {section_key: {field: value}}.

        Return None after warning the user if the values are not valid.
        """
        raise NotImplementedError

    def sections_from_profile(self, profile_data):
        """Split an existing profile into {section_key: {field: value}}."""
        raise NotImplementedError

    def preview_line(self, profile_name, values):
        """One-line summary shown under the fields."""
        return profile_name

    def description_text(self, edit_mode, profile_name):
        """Blurb at the top of the dialog."""
        if edit_mode:
            return f"Edit the {self.TOOL_LABEL} profile: {profile_name}"
        return self.NEW_DESCRIPTION

    # -- construction -------------------------------------------------------

    def __init__(self, parent=None, edit_mode=False, profile_name=None):
        super().__init__(parent)
        self.profile = None
        self.edit_mode = edit_mode
        self.original_profile_name = profile_name

        if edit_mode:
            self.setWindowTitle(f"Edit {self.TOOL_LABEL} Profile: {profile_name}")
        else:
            self.setWindowTitle(f"Custom {self.TOOL_LABEL} Profile")

        self.setModal(True)
        self.setup_theme_handling()
        self.setMinimumSize(*self.MIN_SIZE)

        layout = QVBoxLayout()
        layout.setSpacing(10)

        description = QLabel(self.description_text(edit_mode, profile_name))
        description.setWordWrap(True)
        layout.addWidget(description)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Profile Name:"))
        self.profile_name_input = QLineEdit()
        self.profile_name_input.setPlaceholderText(self.NAME_PLACEHOLDER)
        if edit_mode:
            self.profile_name_input.setText(profile_name)
            # Renaming here would orphan the profile being edited.
            self.profile_name_input.setEnabled(False)
        name_layout.addWidget(self.profile_name_input)
        layout.addLayout(name_layout)

        import_layout = QHBoxLayout()
        import_button = QPushButton("Import from File...")
        import_button.clicked.connect(self.import_from_file)
        compare_button = QPushButton("Compare with File...")
        compare_button.clicked.connect(self.compare_with_file)
        import_layout.addWidget(import_button)
        import_layout.addWidget(compare_button)
        layout.addLayout(import_layout)

        # Per-section widget storage, keyed the same way as the profile.
        self.section_inputs = {}
        self.section_containers = {}

        if len(self.SECTIONS) == 1:
            layout.addWidget(self._create_section_area(self.SECTIONS[0]))
        else:
            self.section_tabs = QTabWidget()
            for section in self.SECTIONS:
                self.section_tabs.addTab(self._create_section_area(section), section.title)
            layout.addWidget(self.section_tabs)

        preview_layout = QVBoxLayout()
        preview_layout.addWidget(QLabel("Profile Preview:"))
        self.preview_text = QLineEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)

        button_layout = QHBoxLayout()
        save_button = QPushButton("Save Profile")
        save_button.clicked.connect(self.on_save_clicked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)

        layout.addLayout(preview_layout)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.update_preview()

    # -- field styling ------------------------------------------------------

    def _get_field_colors(self):
        return field_colors()

    def _field_lineedit_style(self, colors=None):
        return field_lineedit_style(colors)

    def _field_combobox_style(self, colors=None):
        return field_combobox_style(colors)

    # -- widget construction ------------------------------------------------

    def dropdown_options_for(self, field_name, section_key=None):
        """Known values offered for a field, or None for a free-text field.

        Override when the same field name carries different suggestions in
        different sections — FFprobe's codec_name is the example.
        """
        return self.DROPDOWN_OPTIONS.get(field_name)

    def _create_input_widget(self, field_name, value="", section_key=None):
        """A combo box for fields with known values, otherwise a line edit.

        Both expose a compatible text interface, read through _get_widget_text.
        """
        options = self.dropdown_options_for(field_name, section_key)
        if options is not None:
            colors = self._get_field_colors()
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItem("")  # blank first item so empty is easy to pick
            combo.addItems(options)
            combo.setCurrentText(str(value))
            combo.lineEdit().setPlaceholderText(f"Select or enter {field_name}...")
            combo.currentTextChanged.connect(self.update_preview)
            # Style the frame and its internal line edit to the same Base color,
            # so macOS native rendering cannot leave a seam between the two
            # layers. The full ::drop-down block keeps Qt drawing the arrow once
            # the widget is in stylesheet mode.
            combo.setStyleSheet(self._field_combobox_style(colors))
            combo.lineEdit().setStyleSheet("background: transparent;")
            combo.setProperty("field_input", True)
            return combo

        line_edit = QLineEdit()
        line_edit.setText(str(value))
        line_edit.setPlaceholderText(f"Enter {field_name} value...")
        line_edit.textChanged.connect(self.update_preview)
        if self.STYLED_FIELDS:
            line_edit.setStyleSheet(self._field_lineedit_style())
            line_edit.setProperty("field_input", True)
        return line_edit

    def _register_field(self, section_key, field_name, inputs_layout, default_values):
        """Create a field's initial widget row(s) and record them."""
        self.section_inputs[section_key][field_name] = []
        self.section_containers[section_key][field_name] = inputs_layout

        values = list(default_values) or [""]
        for value in values:
            widget = self._create_input_widget(field_name, value, section_key)
            inputs_layout.addWidget(widget)
            self.section_inputs[section_key][field_name].append(widget)

    def _row_buttons(self, section_key, field_name, label_text):
        """The +/- buttons that grow and shrink a multi-value field."""
        add_btn = QPushButton("+")
        add_btn.setMaximumWidth(30)
        add_btn.setMaximumHeight(25)
        add_btn.setToolTip(f"Add {label_text}")
        add_btn.setStyleSheet("QPushButton { background-color: transparent; }")
        add_btn.clicked.connect(
            lambda checked, fn=field_name, sk=section_key: self.add_textbox_row(fn, sk)
        )

        remove_btn = QPushButton("-")
        remove_btn.setMaximumWidth(30)
        remove_btn.setMaximumHeight(25)
        remove_btn.setToolTip(f"Remove last {label_text}")
        remove_btn.setStyleSheet("QPushButton { background-color: transparent; }")
        remove_btn.clicked.connect(
            lambda checked, fn=field_name, sk=section_key: self.remove_textbox_row(fn, sk)
        )
        return add_btn, remove_btn

    def _create_section_area(self, section):
        """Scrollable field area for one section.

        Four grid columns — label, inputs, +, - — so combo boxes cannot overlap
        the labels.
        """
        self.section_inputs[section.key] = {}
        self.section_containers[section.key] = {}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_widget.setAutoFillBackground(False)
        scroll_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        grid_layout = QGridLayout(scroll_widget)
        grid_layout.setSpacing(5)
        grid_layout.setContentsMargins(5, 5, 5, 5)
        grid_layout.setColumnStretch(0, 0)
        grid_layout.setColumnStretch(1, 1)
        grid_layout.setColumnStretch(2, 0)
        grid_layout.setColumnStretch(3, 0)
        scroll.setWidget(scroll_widget)

        for row, (field_name, label_text, default_values, tooltip) in enumerate(section.fields):
            label = QLabel(f"{label_text}:")
            label.setToolTip(tooltip)
            label.setMinimumWidth(150)
            grid_layout.addWidget(
                label, row, 0,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )

            inputs_container = QWidget()
            inputs_container.setAutoFillBackground(False)
            inputs_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            inputs_layout = QVBoxLayout(inputs_container)
            inputs_layout.setContentsMargins(0, 0, 0, 0)
            inputs_layout.setSpacing(5)

            self._register_field(section.key, field_name, inputs_layout, default_values)
            grid_layout.addWidget(inputs_container, row, 1)

            add_btn, remove_btn = self._row_buttons(section.key, field_name, label_text)
            grid_layout.addWidget(add_btn, row, 2, Qt.AlignmentFlag.AlignTop)
            grid_layout.addWidget(remove_btn, row, 3, Qt.AlignmentFlag.AlignTop)

        return scroll

    # -- multi-value rows ---------------------------------------------------

    def add_textbox_row(self, field_name, section_key=None, value=""):
        """Add another input row to a field."""
        section_key = section_key or self.SECTIONS[0].key
        widget = self._create_input_widget(field_name, value, section_key)
        self.section_containers[section_key][field_name].addWidget(widget)
        self.section_inputs[section_key][field_name].append(widget)
        if hasattr(self, 'preview_text'):
            self.update_preview()

    def remove_textbox_row(self, field_name, section_key=None):
        """Remove a field's last input row, always leaving one behind."""
        section_key = section_key or self.SECTIONS[0].key
        widgets = self.section_inputs[section_key][field_name]
        if len(widgets) > 1:
            widgets.pop().deleteLater()
            if hasattr(self, 'preview_text'):
                self.update_preview()

    # -- reading and writing form state -------------------------------------

    @staticmethod
    def _get_widget_text(widget):
        """Text from either a QLineEdit or a QComboBox."""
        if isinstance(widget, QComboBox):
            return widget.currentText()
        return widget.text()

    def _collect_section_values(self, section_key):
        """One section's values: a list if multi-valued, a string, or ""."""
        section_data = {}
        for field_name, widgets in self.section_inputs[section_key].items():
            values = [t for t in (self._get_widget_text(w).strip() for w in widgets) if t]
            if len(values) > 1:
                section_data[field_name] = values
            elif len(values) == 1:
                section_data[field_name] = values[0]
            else:
                section_data[field_name] = ""
        return section_data

    def collect_values(self):
        """Every section's values, keyed by section."""
        return {s.key: self._collect_section_values(s.key) for s in self.SECTIONS}

    def _load_section_data(self, section_key, data_dict):
        """Replace one section's widgets with rows matching data_dict."""
        inputs = self.section_inputs[section_key]
        containers = self.section_containers[section_key]

        for field_name in inputs:
            container_layout = containers[field_name]
            while container_layout.count():
                item = container_layout.takeAt(0)
                if item.widget():
                    item.widget().setParent(None)
                    item.widget().deleteLater()
            inputs[field_name].clear()

            value = data_dict.get(field_name) if isinstance(data_dict, dict) else None
            if isinstance(value, list):
                values = value or [""]
            elif value is not None and str(value) != '':
                values = [value]
            else:
                values = [""]

            for val in values:
                widget = self._create_input_widget(field_name, str(val), section_key)
                container_layout.addWidget(widget)
                inputs[field_name].append(widget)

    def load_profile_data(self, profile_data):
        """Load a profile (dataclass or plain dict) into the form."""
        sections = self.sections_from_profile(profile_data)
        for section in self.SECTIONS:
            self._load_section_data(section.key, sections.get(section.key, {}))
        if hasattr(self, 'preview_text'):
            self.update_preview()

    def load_existing_profile(self, profile_name, profile_data):
        """Load a saved profile, name included."""
        self.profile_name_input.setText(profile_name)
        self.load_profile_data(profile_data)

    def update_preview(self):
        self.preview_text.setText(
            self.preview_line(self.profile_name_input.text() or "Unnamed Profile",
                              self.collect_values())
        )

    # -- profile assembly ---------------------------------------------------

    def build_profile_from_form(self):
        """The profile the form describes, or None if it does not validate."""
        if not self.profile_name_input.text():
            QMessageBox.warning(self, "Validation Error", "Profile name is required.")
            return None
        return self.build_profile(self.collect_values())

    def on_save_clicked(self):
        profile = self.build_profile_from_form()
        if not profile:
            return
        try:
            self.profile = {
                # In edit mode the name field is disabled, so the original name
                # is the authoritative one.
                'name': self.original_profile_name if self.edit_mode
                        else self.profile_name_input.text(),
                'data': profile,
                'is_edit': self.edit_mode,
            }
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to save profile: {str(e)}")

    def get_profile(self):
        """The saved profile dict, or None if the dialog was not saved."""
        return self.profile

    # -- theming ------------------------------------------------------------

    def on_theme_changed(self, palette):
        self.setPalette(palette)
        if not self.STYLED_FIELDS:
            return
        # Explicitly styled widgets do not follow the palette on their own, so
        # their stylesheets are rebuilt against the new colors.
        colors = self._get_field_colors()
        combo_style = self._field_combobox_style(colors)
        line_style = self._field_lineedit_style(colors)
        for combo in self.findChildren(QComboBox):
            if combo.property("field_input"):
                combo.setStyleSheet(combo_style)
                if combo.isEditable() and combo.lineEdit():
                    combo.lineEdit().setStyleSheet("background: transparent;")
        for line_edit in self.findChildren(QLineEdit):
            if line_edit.property("field_input"):
                line_edit.setStyleSheet(line_style)

    def closeEvent(self, event):
        self.cleanup_theme_handling()
