#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import os
import sys
import colorlog
from datetime import datetime
from colorlog import ColoredFormatter
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from AV_Spex.gui.gui_processing_window_console import MessageType

class QtLogHandler(logging.Handler, QObject):
    """
    A custom logging handler that emits Qt signals for log messages.
    Bridges Python's logging system with Qt's signal/slot mechanism.
    """
    
    # Signal to emit log messages: (message, level)
    log_message = pyqtSignal(str, object)
    
    def __init__(self, level=logging.NOTSET):
        # Initialize both parent classes
        logging.Handler.__init__(self, level)
        QObject.__init__(self)
        
        # Create a formatter - use the same format as the console
        self.setFormatter(logging.Formatter('%(message)s'))
        
    def emit(self, record):
        """Process a log record and emit it as a Qt signal"""
        # Format the record according to our formatter
        msg = self.format(record)
        
        # Check for custom msg_type passed via extra parameter
        # This allows callers to override the default level-based coloring
        msg_type = getattr(record, 'msg_type', None)
        
        # Fall back to mapping logging levels to MessageType if no custom type
        if msg_type is None:
            msg_type = MessageType.NORMAL
            if record.levelno >= logging.CRITICAL:
                msg_type = MessageType.ERROR  # Critical as error but could be a distinct type
            elif record.levelno >= logging.ERROR:
                msg_type = MessageType.ERROR
            elif record.levelno >= logging.WARNING:
                msg_type = MessageType.WARNING
            elif record.levelno >= logging.INFO:
                msg_type = MessageType.INFO
            elif record.levelno == logging.DEBUG:
                msg_type = MessageType.COMMAND  # Use command style for debug messages
            
        # Emit the signal with message and type
        self.log_message.emit(msg, msg_type)


def get_log_directory():
    """Determine the appropriate log directory based on how the app is running"""
    if getattr(sys, 'frozen', False):
        # If running as packaged app
        log_dir = os.path.join(str(Path.home()), 'Library', 'Logs', 'AVSpex')
    else:
        # If running from source
        script_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
        log_dir = os.path.join(root_dir, 'logs')
    
    # Add date subdirectory
    log_dir = os.path.join(log_dir, datetime.now().strftime('%Y-%m-%d'))
    os.makedirs(log_dir, exist_ok=True)
    return log_dir

# Much of this script is taken from the AMIA open source project loglog. More information here: https://github.com/amiaopensource/loglog

def setup_logger(): 
    # Suppress matplotlib verbose output before any matplotlib imports
    # This prevents matplotlib configuration messages from appearing
    logging.getLogger('matplotlib').setLevel(logging.WARNING)
    logging.getLogger('matplotlib.font_manager').setLevel(logging.WARNING)
    logging.getLogger('matplotlib.colorbar').setLevel(logging.WARNING)
    logging.getLogger('PIL').setLevel(logging.WARNING)  # Also suppress PIL/Pillow messages
    
    # Assigns getLogger function from imported module, creates logger 
    logger = logging.getLogger()
    # Sets 'lowest' log level
    logger.setLevel(logging.DEBUG)

    # Establishes path to 'logs' directory
    log_dir = get_log_directory()
    log_name = f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}_AVSpex"
    log_path = os.path.join(log_dir, f"{log_name}.log")

    # Define log formats which will be used in 'formatter' for the 2 log handlers
    LOG_FORMAT = '%(asctime)s - %(levelname)s: %(message)s'
    STDOUT_FORMAT = '%(message)s' 

    ## This project uses 2 log handlers, one for the log file 'file_handler', and one for the terminal output 'console_handler' 
    # define file handler and set formatter
    file_handler = logging.FileHandler(log_path)
    formatter    = logging.Formatter(LOG_FORMAT)
    file_handler.setFormatter(formatter)
    # set log level
    file_handler.setLevel(logging.DEBUG)
    # add file handler to logger
    logger.addHandler(file_handler)

    # define console_handler and set format for terminal output
    console_handler = colorlog.StreamHandler()
    console_formatter = colorlog.ColoredFormatter(
        '%(log_color)s' + STDOUT_FORMAT,
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        }
    )
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.DEBUG)
    # add console handler to logger
    logger.addHandler(console_handler)

    return logger

# Initialize logger once on module import
logger = setup_logger()


# Known-benign ffmpeg stderr notices: messages a decoder prints for bitstream
# features it doesn't fully model, even though the operation still succeeds.
# Each entry maps a substring to match → a plain-language explanation. Add new
# entries here so every ffmpeg call site reports them the same way.
_BENIGN_FFMPEG_NOTICES = (
    (
        "variable ACT flag",
        "the DNxHD/VC-3 decoder does not model this file's variable Adaptive "
        "Coding/Truncation (ACT) flag. This is a decoder limitation, not a fault "
        "in the file — frames still decode and the output is produced. Common in "
        "DNxHD-in-MXF (e.g. exports from professional NLEs).",
    ),
)


def report_ffmpeg_stderr(stderr_text, operation, *, failure=False):
    """Log ffmpeg stderr output consistently across the app.

    Every ffmpeg call site should route its captured stderr through here so the
    same message reads the same way regardless of which step produced it.

    Known-benign decoder notices (see ``_BENIGN_FFMPEG_NOTICES``, e.g. the DNxHD
    "variable ACT flag" message emitted for many MXF files) are reported once at
    WARNING with a plain-language explanation — never as an ERROR — because they
    don't indicate a failed operation. Any remaining, unrecognized stderr lines
    are logged together at ERROR when ``failure`` is True (the ffmpeg run failed)
    or WARNING otherwise.

    Args:
        stderr_text (str | bytes | None): Raw stderr captured from ffmpeg.
        operation (str): Short description of the step, e.g. "access copy".
        failure (bool): Whether the ffmpeg run actually failed. Controls the
            level used for unrecognized output (ERROR vs WARNING).
    """
    if not stderr_text:
        return
    if isinstance(stderr_text, bytes):
        stderr_text = stderr_text.decode("utf-8", errors="replace")

    lines = [ln.strip() for ln in stderr_text.splitlines() if ln.strip()]
    if not lines:
        return

    benign_seen = []   # preserve order, de-duplicate repeats
    other_lines = []
    for line in lines:
        match = next(((needle, exp) for needle, exp in _BENIGN_FFMPEG_NOTICES if needle in line), None)
        if match:
            if match not in benign_seen:
                benign_seen.append(match)
        else:
            other_lines.append(line)

    for _needle, explanation in benign_seen:
        logger.warning(f"ffmpeg [{operation}]: known-benign decoder notice — {explanation}\n")

    if other_lines:
        joined = "; ".join(other_lines)
        if failure:
            logger.error(f"ffmpeg [{operation}] failed: {joined}\n")
        else:
            logger.warning(f"ffmpeg [{operation}] stderr: {joined}\n")


# Store reference to the current per-file handler so we can remove it later
_current_file_handler = None

def start_file_log(output_directory, video_id, log_level=logging.DEBUG):
    """
    Start capturing logs to a per-file log in the output directory.
    
    This creates a new file handler that captures logs only for this specific
    file's processing. The handler is added to the existing logger, so logs
    still go to the main log file and console as well.
    
    Args:
        output_directory (str): The qc_metadata directory for this file
        video_id (str): The video identifier (used in log filename)
        log_level (int): Minimum log level to capture (default: DEBUG)
    
    Returns:
        str: Path to the created log file
    """
    global _current_file_handler
    
    # Remove any existing per-file handler first
    stop_file_log()
    
    # Create the log file path
    log_filename = f"{video_id}_avspex_processing.log"
    log_path = os.path.join(output_directory, log_filename)

    # If a log from a previous run exists, write a separator directly to the
    # file (not through the logger, so it doesn't echo to console/main log)
    if os.path.isfile(log_path) and os.path.getsize(log_path) > 0:
        with open(log_path, 'a', encoding='utf-8') as existing_log:
            existing_log.write('\n\n')
            existing_log.write('=' * 80 + '\n')
            existing_log.write(f"{'NEW PROCESSING RUN':^80}\n")
            existing_log.write('=' * 80 + '\n\n')

    # Create file handler for this specific file (append mode preserves prior runs)
    file_handler = logging.FileHandler(log_path, mode='a', encoding='utf-8')
    
    # Use a clean format for per-file logs
    log_format = '%(asctime)s - %(levelname)s: %(message)s'
    file_handler.setFormatter(logging.Formatter(log_format))
    file_handler.setLevel(log_level)
    
    # Add a marker attribute so we can identify this handler later
    file_handler._is_per_file_handler = True
    
    # Add to the logger
    logger.addHandler(file_handler)
    _current_file_handler = file_handler
    
    # Log the start of processing
    logger.info(f"=== Processing log for: {video_id} ===")
    logger.info(f"Processing started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    return log_path


def stop_file_log():
    """
    Stop capturing logs to the per-file log and close the handler.
    
    This removes the per-file handler from the logger, ensuring subsequent
    logs don't go to the old file.
    """
    global _current_file_handler
    
    if _current_file_handler is not None:
        # Log completion before removing
        logger.info(f"Processing completed at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=== End of processing log ===")
        
        # Remove from logger and close
        logger.removeHandler(_current_file_handler)
        _current_file_handler.close()
        _current_file_handler = None
    
    # Also check for any orphaned per-file handlers (safety cleanup)
    for handler in logger.handlers[:]:  # Use slice copy to avoid modification during iteration
        if getattr(handler, '_is_per_file_handler', False):
            logger.removeHandler(handler)
            handler.close()


def disconnect_qt_log_handler():
    """Remove and close the QtLogHandler before Qt tears down its C++ objects."""
    for handler in logger.handlers[:]:
        if isinstance(handler, QtLogHandler):
            try:
                handler.log_message.disconnect()
            except (TypeError, RuntimeError):
                pass
            logger.removeHandler(handler)
            handler.close()


def connect_logger_to_ui(ui_component):
    """
    Connect the existing logger to a UI component without recreating the logger.
    Only adds a QtLogHandler to the existing logger.
    
    Args:
        ui_component: The UI component with update_status method
    
    Returns:
        The logger instance with the added Qt handler
    """
    if ui_component is not None and hasattr(ui_component, 'update_status'):
        # Check if a Qt handler is already connected to prevent duplicates
        for handler in logger.handlers:
            if isinstance(handler, QtLogHandler):
                # If there's already a Qt handler, disconnect old signals and connect new one
                handler.log_message.disconnect()
                handler.log_message.connect(ui_component.update_status)
                return logger
                
        # If no Qt handler exists, create and add a new one
        qt_handler = QtLogHandler()
        qt_handler.log_message.connect(ui_component.update_status)
        # Set log level - can adjust this to control what appears in the UI
        qt_handler.setLevel(logging.DEBUG)  
        # Add Qt handler to logger
        logger.addHandler(qt_handler)
    

# Example logs (only execute if this file is run directly, not imported)
if __name__ == "__main__":
    logger.debug('A debug message')
    logger.info('An info message')
    logger.warning('Something is not right.')
    logger.error('A Major error has happened.')
    logger.critical('Fatal error. Cannot continue')