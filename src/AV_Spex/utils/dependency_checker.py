#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import shutil
import subprocess
import platform
from dataclasses import dataclass
from typing import Callable, List, Dict, Optional, Tuple
from enum import Enum

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QPushButton, QTextEdit, QProgressBar, QGroupBox,
    QScrollArea, QWidget, QMessageBox
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt6.QtGui import QFont, QIcon

from AV_Spex.utils.log_setup import logger

class DependencyStatus(Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    VERSION_ISSUE = "version_issue"
    CHECKING = "checking"

@dataclass
class DependencyInfo:
    name: str
    command: str
    version_command: Optional[str] = None
    min_version: Optional[str] = None
    description: str = ""
    install_hint: str = ""
    status: DependencyStatus = DependencyStatus.CHECKING
    version_found: Optional[str] = None
    error_message: Optional[str] = None
    # For capabilities that aren't a command on PATH (a bundled library's build
    # options, say). Returns (ok, detail); when set it replaces the which() lookup.
    check_fn: Optional[Callable[[], Tuple[bool, str]]] = None


OPENCV_FFMPEG_HINT = (
    "Reinstall AV-Spex built against an OpenCV with FFmpeg. macOS x86_64 "
    "opencv-python wheels from 4.13.0.90 onward ship without it; pin "
    "opencv-python-headless<4.13 for Intel builds, or use the Apple Silicon build."
)


def opencv_ffmpeg_support() -> Tuple[bool, str]:
    """Report whether the bundled OpenCV can decode video at all.

    OpenCV without the FFmpeg backend falls back to AVFoundation on macOS, which
    has no Matroska demuxer and no FFV1 decoder — so every VideoCapture on our
    source files fails. That is silent at the C++ level, so it is surfaced here
    at startup rather than being discovered as an empty analysis afterwards.
    """
    try:
        import cv2
    except ImportError as e:
        return False, f"OpenCV is not importable: {e}"

    version = getattr(cv2, "__version__", "unknown")
    try:
        for line in cv2.getBuildInformation().splitlines():
            if 'FFMPEG' in line and ':' in line:
                enabled = line.split(':', 1)[1].strip().upper().startswith('YES')
                if enabled:
                    return True, f"OpenCV {version} (FFmpeg backend available)"
                return False, (
                    f"OpenCV {version} was built without FFmpeg, so it cannot "
                    f"open FFV1/Matroska files. Border detection, BRNG comparison "
                    f"and duplicate-frame verification will be skipped."
                )
    except Exception as e:
        return False, f"Could not read the OpenCV build configuration: {e}"

    return False, f"OpenCV {version} reports no FFMPEG entry in its build information"

class DependencyCheckWorker(QThread):
    """Worker thread for checking dependencies without blocking the GUI"""
    dependency_checked = pyqtSignal(str, DependencyInfo)  # dependency_name, info
    all_checks_complete = pyqtSignal(bool)  # success status
    
    def __init__(self, dependencies: List[DependencyInfo]):
        super().__init__()
        self.dependencies = dependencies
        self._cancelled = False
    
    def cancel(self):
        self._cancelled = True
    
    def run(self):
        """Check all dependencies in background thread"""
        all_good = True
        
        for dep in self.dependencies:
            if self._cancelled:
                return
                
            # Capability checks (bundled libraries) run their own probe
            if dep.check_fn is not None:
                ok, detail = dep.check_fn()
                dep.version_found = detail
                if ok:
                    dep.status = DependencyStatus.FOUND
                else:
                    # Present, but not usable for what we need — not "missing"
                    dep.status = DependencyStatus.VERSION_ISSUE
                    dep.error_message = detail
                    all_good = False
            # Check if command exists
            elif not shutil.which(dep.command):
                dep.status = DependencyStatus.NOT_FOUND
                dep.error_message = f"{dep.command} command not found in PATH"
                all_good = False
            else:
                # Check version if specified
                if dep.version_command and dep.min_version:
                    version_ok, version_str = self._check_version(dep)
                    dep.version_found = version_str
                    if not version_ok:
                        dep.status = DependencyStatus.VERSION_ISSUE
                        dep.error_message = f"Version {version_str} found, but {dep.min_version} or higher required"
                        all_good = False
                    else:
                        dep.status = DependencyStatus.FOUND
                else:
                    dep.status = DependencyStatus.FOUND
            
            self.dependency_checked.emit(dep.name, dep)
        
        self.all_checks_complete.emit(all_good)
    
    def _check_version(self, dep: DependencyInfo) -> Tuple[bool, str]:
        """Check version of a dependency"""
        try:
            result = subprocess.run(
                dep.version_command.split(), 
                capture_output=True, 
                text=True, 
                timeout=10
            )
            
            if result.returncode == 0:
                version_str = result.stdout.strip()
                # Basic version comparison - you might want to make this more sophisticated
                if dep.min_version:
                    # This is a simple comparison - you might want to use packaging.version
                    return True, version_str  # Simplified for now
                return True, version_str
            else:
                return False, "Unknown"
        except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError):
            return False, "Unknown"

class DependencyCheckDialog(QDialog):
    """Dialog for displaying dependency check results"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dependency Check")
        self.setModal(True)
        self.setMinimumSize(600, 400)
        
        self.dependencies = self._get_required_dependencies()
        self.dependency_widgets = {}
        
        self.setup_ui()
        self.start_dependency_check()
    
    def _get_required_dependencies(self) -> List[DependencyInfo]:
        """Define required dependencies with detailed information"""
        return [
            DependencyInfo(
                name="FFmpeg",
                command="ffmpeg",
                version_command="ffmpeg -version",
                description="Required for video/audio processing",
                install_hint="Install via: brew install ffmpeg (macOS) or visit https://ffmpeg.org/"
            ),
            DependencyInfo(
                name="MediaInfo",
                command="mediainfo",
                version_command="mediainfo --version",
                description="Required for media metadata extraction",
                install_hint="Install via: brew install mediainfo (macOS) or visit https://mediaarea.net/"
            ),
            DependencyInfo(
                name="ExifTool",
                command="exiftool",
                version_command="exiftool -ver",
                description="Required for metadata extraction",
                install_hint="Install via: brew install exiftool (macOS) or visit https://exiftool.org/"
            ),
            DependencyInfo(
                name="MediaConch",
                command="mediaconch",
                version_command="mediaconch --version",
                description="Required for media conformance checking",
                install_hint="Install via: brew install mediaconch (macOS) or visit https://mediaarea.net/MediaConch"
            ),
            DependencyInfo(
                name="QCTools",
                command="qcli",
                version_command="qcli --version",
                description="Required for quality control analysis and video QC metrics",
                install_hint="Install via: brew install qcli (macOS) or visit https://mediaarea.net/QCTools"
            ),
            DependencyInfo(
                name="MKVToolNix",
                command="mkvmerge",
                version_command="mkvmerge --version",
                description="Required for MKV container operations",
                install_hint="Install via: brew install mkvtoolnix (macOS) or visit https://mkvtoolnix.download/"
            ),
            DependencyInfo(
                name="mkvalidator",
                command="mkvalidator",
                version_command="mkvalidator --version",
                description="Required for Matroska file structure validation",
                install_hint="Install via: brew install mkvalidator (macOS) or visit https://www.matroska.org/downloads/mkvalidator.html"
            ),
            DependencyInfo(
                name="OpenCV video decoding",
                command="",
                description="Bundled with AV-Spex; required for border detection, "
                            "BRNG comparison and duplicate-frame verification",
                install_hint=OPENCV_FFMPEG_HINT,
                check_fn=opencv_ffmpeg_support
            )
        ]

    def setup_ui(self):
        """Set up the dialog UI"""
        layout = QVBoxLayout(self)
        
        # Title
        title = QLabel("Checking Required Dependencies...")
        title.setFont(QFont("Arial", 14, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, len(self.dependencies))
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        
        # Scroll area for dependency status
        scroll_area = QScrollArea()
        scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(scroll_widget)
        
        # Create widgets for each dependency
        for dep in self.dependencies:
            dep_widget = self._create_dependency_widget(dep)
            self.dependency_widgets[dep.name] = dep_widget
            self.scroll_layout.addWidget(dep_widget)
        
        scroll_area.setWidget(scroll_widget)
        scroll_area.setWidgetResizable(True)
        layout.addWidget(scroll_area)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        self.retry_button = QPushButton("Retry Check")
        self.retry_button.clicked.connect(self.start_dependency_check)
        self.retry_button.setEnabled(False)
        button_layout.addWidget(self.retry_button)
        
        button_layout.addStretch()
        
        self.continue_button = QPushButton("Continue Anyway")
        self.continue_button.clicked.connect(self.accept)
        self.continue_button.setEnabled(False)
        button_layout.addWidget(self.continue_button)
        
        self.close_button = QPushButton("Close Application")
        self.close_button.clicked.connect(self.reject)
        button_layout.addWidget(self.close_button)
        
        layout.addLayout(button_layout)
    
    def _create_dependency_widget(self, dep: DependencyInfo) -> QGroupBox:
        """Create a widget for displaying dependency status"""
        group_box = QGroupBox(dep.name)
        layout = QVBoxLayout(group_box)
        
        # Description
        desc_label = QLabel(dep.description)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        # Status label
        status_label = QLabel("Checking...")
        status_label.setObjectName("status_label")
        layout.addWidget(status_label)
        
        # Version label (initially hidden)
        version_label = QLabel("")
        version_label.setObjectName("version_label")
        version_label.hide()
        layout.addWidget(version_label)
        
        # Install hint (initially hidden)
        hint_label = QLabel(dep.install_hint)
        hint_label.setObjectName("hint_label")
        hint_label.setWordWrap(True)
        hint_label.setStyleSheet("color: #666; font-style: italic;")
        hint_label.hide()
        layout.addWidget(hint_label)
        
        return group_box
    
    def start_dependency_check(self):
        """Start the dependency checking process"""
        self.progress_bar.setValue(0)
        self.retry_button.setEnabled(False)
        self.continue_button.setEnabled(False)
        
        # Reset all dependency statuses
        for dep in self.dependencies:
            dep.status = DependencyStatus.CHECKING
            self._update_dependency_widget(dep.name, dep)
        
        # Start worker thread
        self.worker = DependencyCheckWorker(self.dependencies)
        self.worker.dependency_checked.connect(self._on_dependency_checked)
        self.worker.all_checks_complete.connect(self._on_all_checks_complete)
        self.worker.start()
    
    def _on_dependency_checked(self, dep_name: str, dep_info: DependencyInfo):
        """Handle individual dependency check completion"""
        self._update_dependency_widget(dep_name, dep_info)
        current_value = self.progress_bar.value()
        self.progress_bar.setValue(current_value + 1)
    
    def _on_all_checks_complete(self, all_good: bool):
        """Handle completion of all dependency checks"""
        self.retry_button.setEnabled(True)
        self.continue_button.setEnabled(True)
        
        if all_good:
            self.continue_button.setText("Continue")
            self.continue_button.setStyleSheet("background-color: #4CAF50; color: white;")
            # Auto-close after 2 seconds if all dependencies are found
            QTimer.singleShot(2000, self.accept)
        else:
            self.continue_button.setText("Continue Anyway")
            self.continue_button.setStyleSheet("background-color: #FF9800; color: white;")
    
    def _update_dependency_widget(self, dep_name: str, dep_info: DependencyInfo):
        """Update the display for a specific dependency"""
        widget = self.dependency_widgets.get(dep_name)
        if not widget:
            return
        
        status_label = widget.findChild(QLabel, "status_label")
        version_label = widget.findChild(QLabel, "version_label")
        hint_label = widget.findChild(QLabel, "hint_label")
        
        if dep_info.status == DependencyStatus.FOUND:
            status_label.setText("✅ Found")
            status_label.setStyleSheet("color: green; font-weight: bold;")
            if dep_info.version_found:
                version_label.setText(f"Version: {dep_info.version_found}")
                version_label.show()
            hint_label.hide()
            
        elif dep_info.status == DependencyStatus.NOT_FOUND:
            status_label.setText("❌ Not Found")
            status_label.setStyleSheet("color: red; font-weight: bold;")
            hint_label.show()
            version_label.hide()
            
        elif dep_info.status == DependencyStatus.VERSION_ISSUE:
            status_label.setText("⚠️ Version Issue")
            status_label.setStyleSheet("color: orange; font-weight: bold;")
            if dep_info.version_found:
                version_label.setText(f"Found: {dep_info.version_found}")
                version_label.show()
            hint_label.show()
            
        else:  # CHECKING
            status_label.setText("🔄 Checking...")
            status_label.setStyleSheet("color: blue;")
            version_label.hide()
            hint_label.hide()

class DependencyManager:
    """Manager class for handling dependency checks"""
    
    @staticmethod
    def check_dependencies_gui(parent=None) -> bool:
        """Show dependency check dialog and return whether to continue"""
        dialog = DependencyCheckDialog(parent)
        result = dialog.exec()
        return result == QDialog.DialogCode.Accepted
    
    @staticmethod
    def check_dependencies_cli() -> bool:
        """Check dependencies for CLI mode"""
        dependencies = DependencyManager._get_cli_dependencies()
        
        print("Checking dependencies...")
        all_good = True
        
        for dep in dependencies:
            if dep.check_fn is not None:
                ok, detail = dep.check_fn()
                if ok:
                    print(f"✅ {dep.name}: {detail}")
                else:
                    print(f"⚠️  {dep.name}: {detail}")
                    print(f"   {dep.install_hint}")
                    all_good = False
            elif shutil.which(dep.command):
                print(f"✅ {dep.name}: Found")
            else:
                print(f"❌ {dep.name}: Not found")
                print(f"   {dep.install_hint}")
                all_good = False

        return all_good
    
    @staticmethod
    def _get_cli_dependencies() -> List[DependencyInfo]:
        """Get dependency list for CLI checking"""
        return [
            DependencyInfo(
                name="FFmpeg",
                command="ffmpeg",
                install_hint="Install via: brew install ffmpeg (macOS)"
            ),
            DependencyInfo(
                name="MediaInfo", 
                command="mediainfo",
                install_hint="Install via: brew install mediainfo (macOS)"
            ),
            DependencyInfo(
                name="ExifTool",
                command="exiftool", 
                install_hint="Install via: brew install exiftool (macOS)"
            ),
            DependencyInfo(
                name="MediaConch",
                command="mediaconch",
                install_hint="Install via: brew install mediaconch (macOS)"
            ),
            DependencyInfo(
                name="QCTools",
                command="qcli",
                version_command="qcli --version",
                description="Required for quality control analysis and video QC metrics",
                install_hint="Install via: brew install qcli (macOS) or visit https://mediaarea.net/QCTools"
            ),
            DependencyInfo(
                name="MKVToolNix",
                command="mkvmerge",
                install_hint="Install via: brew install mkvtoolnix (macOS) or visit https://mkvtoolnix.download/"
            ),
            DependencyInfo(
                name="mkvalidator",
                command="mkvalidator",
                install_hint="Install via: brew install mkvalidator (macOS) or visit https://www.matroska.org/downloads/mkvalidator.html"
            ),
            DependencyInfo(
                name="OpenCV video decoding",
                command="",
                description="Bundled with AV-Spex; required for border detection, "
                            "BRNG comparison and duplicate-frame verification",
                install_hint=OPENCV_FFMPEG_HINT,
                check_fn=opencv_ffmpeg_support
            )
        ]


def check_py_version():
    """Check Python version requirement"""
    if sys.version_info[:2] < (3, 10):
        print("This project requires Python 3.10 or higher.")
        sys.exit(1)

def check_external_dependency(command):
    """Original function - kept for backward compatibility"""
    return shutil.which(command) is not None

def cli_deps_check():
    """Check all prerequisites before processing"""
    
    check_py_version()
    
    required_commands = ['ffmpeg', 'mediainfo', 'exiftool', 'mediaconch', 'qcli', 'mkvmerge', 'mkvalidator']
    for command in required_commands:
        if not check_external_dependency(command):
            logger.critical(f"Error: {command} not found. Please install it.")
            return False

    # Not fatal — everything except the frame-reading analyses still works — but
    # it has to be said up front, or the affected checks silently report nothing.
    ffmpeg_ok, detail = opencv_ffmpeg_support()
    if not ffmpeg_ok:
        logger.warning(f"Warning: {detail}")
        logger.warning(f"  {OPENCV_FFMPEG_HINT}")

    return True