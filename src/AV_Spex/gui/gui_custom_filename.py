from dataclasses import asdict

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel,
    QScrollArea, QPushButton, QComboBox,
    QMessageBox, QDialog
)

from AV_Spex.utils.config_setup import FilenameSection, FilenameProfile

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin

class CustomFilenameDialog(QDialog, ThemeableMixin):
    def __init__(self, parent=None, edit_mode=False, profile_name=None):
        super().__init__(parent)
        self.pattern = None
        self.edit_mode = edit_mode
        self.original_profile_name = profile_name
        self.setWindowTitle("Edit Filename Profile" if edit_mode else "Custom Filename Pattern")
        self.setModal(True)

        # Add theme handling
        self.setup_theme_handling()

        # Set minimum size for the dialog
        self.setMinimumSize(500, 600)  # Width: 500px, Height: 600px

        # Initialize layout
        layout = QVBoxLayout()
        layout.setSpacing(10)  # Reduce overall vertical spacing

        # Add description
        description = QLabel("Define your filename pattern using 1-8 sections separated by underscores.")
        description.setWordWrap(True)
        layout.addWidget(description)

        # Profile name input
        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Profile Name:"))
        self.profile_name_input = QLineEdit()
        self.profile_name_input.setPlaceholderText("e.g., My Filename Profile")
        if edit_mode and profile_name:
            self.profile_name_input.setText(profile_name)
            self.profile_name_input.setEnabled(False)  # Don't allow renaming in edit mode
        name_layout.addWidget(self.profile_name_input)
        layout.addLayout(name_layout)
        
        # Scrollable area for sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        self.sections_layout = QVBoxLayout(scroll_widget)
        self.sections_layout.setSpacing(5)  # Reduce spacing between sections
        self.sections_layout.setContentsMargins(5, 5, 5, 5)  # Reduce margins
        scroll.setWidget(scroll_widget)

        # Set a reasonable fixed height for the scroll area
        scroll.setMinimumHeight(300)  # Ensure scroll area is tall enough
        
        # Initial section
        self.sections = []
        self.add_section()
        
        # Buttons for managing sections
        section_buttons_layout = QHBoxLayout()
        add_button = QPushButton("Add Section")
        add_button.clicked.connect(self.add_section)
        remove_button = QPushButton("Remove Last Section")
        remove_button.clicked.connect(self.remove_section)
        section_buttons_layout.addWidget(add_button)
        section_buttons_layout.addWidget(remove_button)
        
        # File Extension input
        extension_layout = QHBoxLayout()
        extension_layout.addWidget(QLabel("File Extension:"))
        self.extension_input = QLineEdit()
        self.extension_input.setText("mkv")
        extension_layout.addWidget(self.extension_input)
        
        # Preview section
        preview_layout = QHBoxLayout()
        preview_layout.addWidget(QLabel("Preview:"))
        self.preview_label = QLabel()
        preview_layout.addWidget(self.preview_label)
        
        # Dialog buttons
        button_layout = QHBoxLayout()
        save_button = QPushButton("Save Pattern")
        save_button.clicked.connect(self.on_save_clicked)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        button_layout.addWidget(save_button)
        button_layout.addWidget(cancel_button)
        
        # Add all layouts to main layout
        layout.addWidget(scroll)
        layout.addLayout(section_buttons_layout)
        layout.addLayout(extension_layout)
        layout.addLayout(preview_layout)
        layout.addLayout(button_layout)
        
        self.setLayout(layout)
        self.update_preview()
        
    def add_section(self):
        """Add a new filename section widget"""
        if len(self.sections) >= 8:
            QMessageBox.warning(self, "Warning", "Maximum 8 sections allowed")
            return
            
        section_widget = QWidget()
        section_layout = QHBoxLayout(section_widget)
        section_layout.setContentsMargins(0, 0, 0, 0)  # Remove margins around section
        section_layout.setSpacing(5)  # Reduce spacing between elements
        
        # Section number label
        section_num = len(self.sections) + 1
        section_label = QLabel(f"Section {section_num}:")
        section_layout.addWidget(section_label)
        
        # Section type combo box
        type_combo = QComboBox()
        type_combo.addItems(["Literal", "Wildcard", "Regex"])
        section_layout.addWidget(type_combo)
        
        # Value input
        value_input = QLineEdit()
        section_layout.addWidget(value_input)
        
        # Help button with tooltip
        help_button = QPushButton("?")
        help_button.setFixedSize(20, 20)
        help_text = {
            0: "Literal: Exact text match (e.g., 'JPC')",
            1: "Wildcard: Use # for digits, @ for letters, * for either\n" +
               "Examples:\n" +
               "#### = exactly 4 digits\n" +
               "@@ = exactly 2 letters\n" +
               "*** = 3 characters (letters or numbers)",
            2: "Regex: Regular expression pattern (e.g., '\\d{3}')"
        }
        help_button.clicked.connect(lambda: QMessageBox.information(self, "Help", help_text[type_combo.currentIndex()]))
        section_layout.addWidget(help_button)
        
        # Store section controls
        section = {
            'widget': section_widget,
            'type_combo': type_combo,
            'value_input': value_input
        }
        self.sections.append(section)
        
        # Connect signals for preview updates
        type_combo.currentIndexChanged.connect(self.update_preview)
        value_input.textChanged.connect(self.update_preview)
        
        self.sections_layout.addWidget(section_widget)
        self.update_preview()
        
    def remove_section(self):
        """Remove the last filename section"""
        if self.sections:
            section = self.sections.pop()
            section['widget'].deleteLater()
            self.update_preview()
        if len(self.sections) < 1:
            self.add_section()  # Ensure at least one section exists
            
    def update_preview(self):
        """Update the filename preview"""
        parts = []
        for section in self.sections:
            value = section['value_input'].text()
            if value:
                parts.append(value)
                
        if parts:
            preview = "_".join(parts) + "." + self.extension_input.text()
            self.preview_label.setText(preview)
            
    def get_filename_pattern(self):
        """Get the filename pattern as a FilenameProfile dataclass"""
        if not self.sections:
            QMessageBox.warning(self, "Validation Error", "At least one section is required.")
            return None
            
        if not all(section['value_input'].text() for section in self.sections):
            QMessageBox.warning(self, "Validation Error", "All sections must have a value.")
            return None
            
        if not self.extension_input.text():
            QMessageBox.warning(self, "Validation Error", "File extension is required.")
            return None
            
        fn_sections = {}
        for i, section in enumerate(self.sections, 1):
            section_type = section['type_combo'].currentText().lower()
            value = section['value_input'].text()
            
            # Create a FilenameSection instance for each section
            fn_sections[f"section{i}"] = FilenameSection(
                value=value,
                section_type=section_type
            )
            
        # Create and return a FilenameProfile instance
        return FilenameProfile(
            fn_sections=fn_sections,
            FileExtension=self.extension_input.text()
        )

    def on_save_clicked(self):
        """Validate and accept. The caller saves and applies the profile —
        this dialog no longer writes config itself."""
        pattern = self.get_filename_pattern()
        if not pattern:
            return
        if not self.edit_mode and not self.profile_name_input.text().strip():
            # Suggest a name from the first section, matching the old
            # auto-generated "Custom (<section1>)" naming.
            first_section = pattern.fn_sections.get('section1') if pattern.fn_sections else None
            first_value = first_section.value if first_section else ''
            self.profile_name_input.setText(f"Custom ({first_value})")
        self.pattern = pattern
        self.accept()  # This will trigger QDialog.accepted

    def get_profile(self):
        """Return {'name', 'data', 'is_edit'} for the caller to save/apply."""
        if self.pattern is None:
            return None
        name = self.original_profile_name if self.edit_mode else self.profile_name_input.text().strip()
        return {
            'name': name,
            'data': self.pattern,
            'is_edit': self.edit_mode,
        }

    def get_pattern(self):
        """Return the stored pattern"""
        return self.pattern

    def _clear_sections(self):
        """Remove every section row without the keep-one-row backfill that
        remove_section() applies. Rows are detached from the layout
        synchronously — deleteLater() alone leaves the old widgets visible
        until the event loop runs, which showed up as a duplicate empty
        "Section 1" row when loading a profile into a fresh dialog."""
        while self.sections:
            section = self.sections.pop()
            widget = section['widget']
            self.sections_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()

    def load_profile_data(self, profile):
        """Load an existing filename profile (dataclass or dict) into the dialog."""
        if profile is None:
            return
        if hasattr(profile, '__dataclass_fields__'):
            profile = asdict(profile)
        fn_sections = profile.get('fn_sections') or {}

        self._clear_sections()

        for section_key in sorted(fn_sections.keys()):
            section_data = fn_sections[section_key]
            if hasattr(section_data, '__dataclass_fields__'):
                section_data = asdict(section_data)
            self.add_section()
            section = self.sections[-1]

            # Set section type
            type_index = {
                'literal': 0,
                'wildcard': 1,
                'regex': 2
            }.get(str(section_data.get('section_type', 'literal')).lower(), 0)
            section['type_combo'].setCurrentIndex(type_index)

            # Set value
            section['value_input'].setText(str(section_data.get('value', '')))

        if not self.sections:
            self.add_section()  # Ensure at least one section exists

        # Load extension
        if profile.get('FileExtension'):
            self.extension_input.setText(profile['FileExtension'])

        self.update_preview()

    # Backward-compatible alias for the pre-redesign method name
    def load_existing_pattern(self, pattern):
        self.load_profile_data(pattern)

    def on_theme_changed(self, palette):
        # Apply the theme changes to this dialog only
        self.setPalette(palette)
        

    def closeEvent(self, event):
        # Clean up theme connections before closing
        self.cleanup_theme_handling()
        super().closeEvent(event)
