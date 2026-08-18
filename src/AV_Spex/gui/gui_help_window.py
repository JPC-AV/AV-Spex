from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTextBrowser
)
from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import (
    QTextCursor, QTextCharFormat, QTextBlockFormat, QShortcut, QKeySequence
)

import html
import os
import re

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.utils.config_manager import ConfigManager

config_mgr = ConfigManager()


class HelpWindow(QWidget, ThemeableMixin):
    """Standalone window that displays the AV Spex help document.

    The help content lives in the bundled config directory
    (config/help/avspex_help.md) so it ships inside the frozen app and can
    be edited independently of the README.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # Parented to the main window but flagged as a real window, so it
        # floats independently yet still closes with the application.
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("AV Spex Help")
        self.setMinimumSize(700, 650)

        layout = QVBoxLayout(self)

        self.content = QTextBrowser()
        self.content.setOpenExternalLinks(True)
        self.content.setMarkdown(self._load_help_text())
        self._insert_toc()
        layout.addWidget(self.content)

        button_layout = QHBoxLayout()
        self.decrease_text_button = QPushButton("−")
        self.decrease_text_button.setToolTip("Decrease text size (Cmd+-)")
        self.decrease_text_button.clicked.connect(lambda: self._adjust_text_size(-1))
        button_layout.addWidget(self.decrease_text_button)
        self.increase_text_button = QPushButton("+")
        self.increase_text_button.setToolTip("Increase text size (Cmd+=)")
        self.increase_text_button.clicked.connect(lambda: self._adjust_text_size(1))
        button_layout.addWidget(self.increase_text_button)
        button_layout.addStretch()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        button_layout.addWidget(self.close_button)
        layout.addLayout(button_layout)

        # Text zoom state, clamped so the document stays readable. Standard
        # zoom shortcuts work whenever the window has focus.
        self._zoom_level = 0
        QShortcut(QKeySequence.StandardKey.ZoomIn, self,
                  activated=lambda: self._adjust_text_size(1))
        QShortcut(QKeySequence.StandardKey.ZoomOut, self,
                  activated=lambda: self._adjust_text_size(-1))

        self.setup_theme_handling()

    def _load_help_text(self):
        """Read the bundled help document, with a fallback if it's missing."""
        help_path = config_mgr.find_file(os.path.join('help', 'avspex_help.md'))
        if help_path:
            try:
                with open(help_path, 'r', encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                pass
            else:
                # Resolve the document's relative image references (e.g. the
                # BRNG differential example) against the bundled help dir
                help_dir = os.path.dirname(help_path)
                self.content.setSearchPaths([help_dir])
                self.content.document().setBaseUrl(
                    QUrl.fromLocalFile(help_dir + os.sep))
                return text
        return ("# AV Spex Help\n\n"
                "The help document could not be found. Please see the README at "
                "https://github.com/JPC-AV/JPC_AV_videoQC for documentation.")

    def _insert_toc(self):
        """Build a linked table of contents from the rendered headings.

        Qt's markdown renderer does not generate anchors for headings, so
        this walks the rendered document, attaches an anchor name to each
        ##/### heading, and inserts a "Contents" list of anchor links after
        the document title. Derived from the document itself, so edits to
        the help markdown are picked up automatically.
        """
        doc = self.content.document()

        # Anchor every section heading and collect TOC entries
        entries = []
        used_slugs = set()
        block = doc.firstBlock()
        while block.isValid():
            level = block.blockFormat().headingLevel()
            if level in (2, 3) and block.text().strip():
                text = block.text()
                slug = self._slugify(text, used_slugs)
                cursor = QTextCursor(block)
                cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                                    QTextCursor.MoveMode.KeepAnchor)
                anchor_format = QTextCharFormat()
                anchor_format.setAnchor(True)
                anchor_format.setAnchorNames([slug])
                cursor.mergeCharFormat(anchor_format)
                entries.append((level, text, slug))
            block = block.next()

        if not entries:
            return

        # Build the Contents list, nesting ### entries under their ## section
        parts = ["<h2>Contents</h2>", "<ul>"]
        prev_level = 2
        for level, text, slug in entries:
            if level > prev_level:
                parts.append("<ul>")
            elif level < prev_level:
                parts.append("</ul>")
            parts.append(f'<li><a href="#{slug}">{html.escape(text)}</a></li>')
            prev_level = level
        if prev_level == 3:
            parts.append("</ul>")
        parts.append("</ul>")

        # Insert after the first block (the document title), with formats
        # reset so the list doesn't inherit the title's heading style
        cursor = QTextCursor(doc.firstBlock())
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        cursor.insertHtml("".join(parts))

        self.content.moveCursor(QTextCursor.MoveOperation.Start)

    @staticmethod
    def _slugify(text, used_slugs):
        """GitHub-style heading slug, deduplicated against used_slugs."""
        slug = re.sub(r'[^\w\s-]', '', text.lower()).strip()
        slug = re.sub(r'\s+', '-', slug) or 'section'
        base = slug
        counter = 1
        while slug in used_slugs:
            counter += 1
            slug = f"{base}-{counter}"
        used_slugs.add(slug)
        return slug

    ZOOM_MIN = -3
    ZOOM_MAX = 8

    def _adjust_text_size(self, delta):
        """Grow or shrink the document text by one zoom step, within limits.

        QTextEdit's zoomIn/zoomOut rescale every font size in the document
        (headings, body, code) but leave images untouched.
        """
        new_level = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self._zoom_level + delta))
        step = new_level - self._zoom_level
        if not step:
            return
        self._zoom_level = new_level
        if step > 0:
            self.content.zoomIn(step)
        else:
            self.content.zoomOut(-step)

    def on_theme_changed(self, palette):
        """Handle theme changes for this window"""
        self.setPalette(palette)
        theme_manager = ThemeManager.instance()
        theme_manager.style_button(self.close_button)
        theme_manager.style_button(self.decrease_text_button)
        theme_manager.style_button(self.increase_text_button)
