"""Structured read-only viewer for a spex domain's expected values.

Replaces the old console-styled text dump: each section renders as a styled
group box with a form of field/value rows. Values are selectable for
copying. Content arrives as plain strings — the per-domain adapters in
gui_spex_window.py handle flattening, so this dialog never touches config.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout, QLabel,
    QPushButton, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin


class SpexViewDialog(QDialog, ThemeableMixin):
    """Read-only, per-section view of one domain's expected values.

    Args:
        title: window title, e.g. "MediaInfo Values"
        sections: {section name: {field: value string}}
    """

    def __init__(self, title, sections, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 480)
        self.section_group_boxes = []

        layout = QVBoxLayout(self)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        content = QWidget()
        content_layout = QVBoxLayout(content)

        theme_manager = ThemeManager.instance()
        for section_name, fields in sections.items():
            group_box = QGroupBox(section_name)
            form = QFormLayout(group_box)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
            for field_name, value in fields.items():
                field_label = QLabel(str(field_name))
                field_label.setStyleSheet("font-weight: bold;")
                value_label = QLabel(str(value))
                value_label.setWordWrap(True)
                value_label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse)
                form.addRow(field_label, value_label)
            theme_manager.style_groupbox(group_box)
            self.section_group_boxes.append(group_box)
            content_layout.addWidget(group_box)
        content_layout.addStretch()

        scroll_area.setWidget(content)
        layout.addWidget(scroll_area)

        button_row = QHBoxLayout()
        button_row.addStretch()
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        theme_manager.style_button(close_button)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self.setup_theme_handling()

    def on_theme_changed(self, palette):
        self.setPalette(palette)
        theme_manager = ThemeManager.instance()
        for group_box in self.section_group_boxes:
            theme_manager.style_groupbox(group_box)
        theme_manager.style_buttons(self)
