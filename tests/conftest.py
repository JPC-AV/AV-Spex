# tests/conftest.py
import pytest
import os
import logging
from pathlib import Path

@pytest.fixture
def sample_colorbars_csv(tmp_path):
    """Create a sample colorbars values CSV file"""
    csv_content = """QCTools Fields,SMPTE Colorbars,JPC_AV_05000 Colorbars
YMAX,940.0,1019
YMIN,28.0,4
UMIN,148.0,4
UMAX,876.0,1019
VMIN,124.0,6
VMAX,867.0,1016
SATMIN,0.0,1
SATMAX,405.0,704"""
    
    csv_path = tmp_path / "colorbars_values.csv"
    csv_path.write_text(csv_content)
    return str(csv_path)

@pytest.fixture
def sample_duration_csv(tmp_path):
    """Create a sample duration CSV file"""
    csv_content = """qct-parse color bars found:
00:00:03:1030,00:00:07:12:3320"""
    
    csv_path = tmp_path / "colorbars_duration.csv"
    csv_path.write_text(csv_content)
    return str(csv_path)

@pytest.fixture
def sample_thumbs_dict(tmp_path):
    """Create a sample thumbs dictionary with test image"""
    thumb_dir = tmp_path / "ThumbExports"
    thumb_dir.mkdir()
    test_image = thumb_dir / "JPC_AV_05000.color_bars_detection.bars_found.first_frame.00.00.03.1030.png"
    test_image.write_text("dummy image content")
    
    return {
        'First frame of color bars\n\nAt timecode: 00:00:03:1030': (
            str(test_image),
            'bars_found',
            '00:00:03:1030'
        )
    }

@pytest.fixture
def setup_logging():
    """Setup basic logging configuration for tests"""
    logging.basicConfig(level=logging.CRITICAL)

# ---------------------------------------------------------------------------
# Qt / GUI fixtures
#
# A session-scoped QApplication on the offscreen platform. This is enough for
# structural and logic tests on dialogs; it is NOT suitable for asserting on
# palette colors, because macOS reports a different palette offscreen than
# under cocoa (see CLAUDE.md on disabled-state styling).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def qapp():
    PyQt6 = pytest.importorskip("PyQt6")
    # Qt does not infer its plugin directory from the PyQt6 package, and an
    # unset QT_PLUGIN_PATH makes even the offscreen platform fail to load
    # (it aborts the interpreter rather than raising). Point it at the
    # plugins root that ships with the installed PyQt6.
    plugins = os.path.join(os.path.dirname(PyQt6.__file__), "Qt6", "plugins")
    if os.path.isdir(plugins):
        os.environ.setdefault("QT_PLUGIN_PATH", plugins)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    QApplication = pytest.importorskip("PyQt6.QtWidgets").QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def silent_dialogs(monkeypatch):
    """Stop QMessageBox popups from blocking, and record what was raised.

    Validation failures in these dialogs surface as modal warnings; without
    this fixture a failing-validation test would hang waiting for a click.
    """
    QtWidgets = pytest.importorskip("PyQt6.QtWidgets")
    seen = []

    def _record(kind):
        def _fn(parent, title, text, *a, **k):
            seen.append((kind, title, text))
            return QtWidgets.QMessageBox.StandardButton.Ok
        return _fn

    for kind in ("warning", "critical", "information", "question"):
        monkeypatch.setattr(QtWidgets.QMessageBox, kind, staticmethod(_record(kind)))
    return seen
