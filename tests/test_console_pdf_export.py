"""Tests for the console "Save as PDF" export truncation guard.

Qt's text layout silently stops laying out a QTextDocument once it reaches
~2^23 device units of height, and QTextDocument.print() reports success anyway.
A 41-hour, 100-tape run (~37,700 console lines) hit that ceiling at QPdfWriter's
1200 dpi default and lost more than half its output with no warning at all.

Two things guard against a repeat, and both are pinned here: the export runs at
a resolution that leaves real headroom, and a PDF that did land on the ceiling
is detected so the user is told instead of being congratulated.

The detector is tested against fabricated PDF bytes rather than real prints —
what it actually consumes is a page-object count and a page height, and
building those directly keeps the test fast and free of font metrics.
"""

import pytest

from AV_Spex.gui.gui_processing_window import (
    CONSOLE_PDF_RESOLUTION,
    _PDF_LAYOUT_HEIGHT_LIMIT,
    _console_pdf_looks_truncated,
)


# QPdfWriter.height() for US Letter with 15 mm margins, at the resolutions the
# export has used. Measured from a real writer, not derived.
PAGE_HEIGHT_300_DPI = 2946
PAGE_HEIGHT_1200_DPI = 11782


def write_fake_pdf(path, page_count):
    """Write a file carrying page_count page objects, as Qt's PDF output does."""
    body = b"%PDF-1.4\n" + b"".join(
        b"<< /Type /Page /Parent 1 0 R >>\n" for _ in range(page_count)
    )
    path.write_bytes(body + b"/Type /Pages\n%%EOF\n")
    return str(path)


def test_export_resolution_leaves_headroom_for_a_long_run():
    """1200 dpi caps the export at ~712 pages; the chosen resolution must not."""
    max_pages_at_export_resolution = _PDF_LAYOUT_HEIGHT_LIMIT // PAGE_HEIGHT_300_DPI

    assert CONSOLE_PDF_RESOLUTION == 300
    # The 100-tape run needed ~2,150 pages of console; 1200 dpi allowed 713.
    assert max_pages_at_export_resolution > 2800


def test_output_short_of_the_ceiling_is_complete(tmp_path):
    path = write_fake_pdf(tmp_path / "short.pdf", 1569)

    assert _console_pdf_looks_truncated(path, PAGE_HEIGHT_300_DPI) is False


def test_output_sitting_on_the_ceiling_is_flagged(tmp_path):
    """A print that stops exactly at the layout limit is the truncation signature."""
    at_ceiling = _PDF_LAYOUT_HEIGHT_LIMIT // PAGE_HEIGHT_300_DPI
    path = write_fake_pdf(tmp_path / "ceiling.pdf", at_ceiling)

    assert _console_pdf_looks_truncated(path, PAGE_HEIGHT_300_DPI) is True


def test_the_original_failure_is_still_recognized(tmp_path):
    """The user's PDF: 713 pages at 1200 dpi, reported as a success."""
    path = write_fake_pdf(tmp_path / "regression.pdf", 713)

    assert _console_pdf_looks_truncated(path, PAGE_HEIGHT_1200_DPI) is True
    # ...while a genuinely complete 1200 dpi export of the same shape is not.
    short = write_fake_pdf(tmp_path / "ok.pdf", 385)
    assert _console_pdf_looks_truncated(short, PAGE_HEIGHT_1200_DPI) is False


@pytest.mark.parametrize("page_height", [0, -1])
def test_nonsense_page_height_does_not_warn(tmp_path, page_height):
    path = write_fake_pdf(tmp_path / "any.pdf", 100)

    assert _console_pdf_looks_truncated(path, page_height) is False


def test_unreadable_output_fails_open(tmp_path):
    """Never warn about a PDF that could not be inspected."""
    assert _console_pdf_looks_truncated(str(tmp_path / "missing.pdf"), PAGE_HEIGHT_300_DPI) is False


def test_unrecognized_pdf_structure_fails_open(tmp_path):
    """A future Qt could compress the page objects; that must not become a warning."""
    path = tmp_path / "opaque.pdf"
    path.write_bytes(b"%PDF-1.4\n<compressed object stream>\n%%EOF\n")

    assert _console_pdf_looks_truncated(str(path), PAGE_HEIGHT_300_DPI) is False
