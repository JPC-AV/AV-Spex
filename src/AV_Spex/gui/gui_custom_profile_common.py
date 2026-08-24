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

from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QLabel, QMessageBox,
    QPushButton, QTextEdit, QVBoxLayout,
)
from PyQt6.QtGui import QPalette


# ---------------------------------------------------------------------------
# Field styling
# ---------------------------------------------------------------------------

_ARROW_SVG_PATH = None


def dropdown_arrow_svg_path():
    """Path to a chevron SVG used for QComboBox arrows.

    Written to a temp file on first call and cached for the process, so every
    dialog and every combo box reuses the one file.
    """
    global _ARROW_SVG_PATH
    if _ARROW_SVG_PATH and os.path.exists(_ARROW_SVG_PATH):
        return _ARROW_SVG_PATH

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'width="12" height="12" viewBox="0 0 12 12">'
        '<path d="M2.5 4 L6 7.5 L9.5 4" stroke="#ffffff" '
        'stroke-width="1.75" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )
    f = tempfile.NamedTemporaryFile(
        suffix='.svg', delete=False, mode='w', prefix='avspex_arrow_'
    )
    f.write(svg)
    f.close()
    _ARROW_SVG_PATH = f.name
    return _ARROW_SVG_PATH


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
    arrow_path = dropdown_arrow_svg_path()
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
