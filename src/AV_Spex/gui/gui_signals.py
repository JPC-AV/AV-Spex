from PyQt6.QtCore import QObject, pyqtSignal

class ProcessingSignals(QObject):
    started = pyqtSignal(str)  # Signal for when processing starts
    completed = pyqtSignal(str)  # Signal for when processing completes
    error = pyqtSignal(str)  # Signal for when an error occurs
    cancelled = pyqtSignal() # For cancel button
    
    status_update = pyqtSignal(str)  # Signal for general status updates
    progress = pyqtSignal(int, int)  # Signal for numerical progress (current, total)

    file_started = pyqtSignal(str, int, int)  # Signal for when processing of a specific file starts
    tool_started = pyqtSignal(str)  # Signal for when a specific tool starts
    tool_completed = pyqtSignal(str)  # Signal for when a tool completes
    step_completed = pyqtSignal(str)  # Signal when a specific step is completed
    step_failed = pyqtSignal(str)     # Signal when a specific step fails
    step_reset = pyqtSignal(str)      # Signal to reset a step back to pending (e.g. before re-running)
    
    fixity_progress = pyqtSignal(str)  # For detailed fixity status
    mediaconch_progress = pyqtSignal(str)  # For detailed mediaconch status
    metadata_progress = pyqtSignal(str)  # For detailed metadata status
    output_progress = pyqtSignal(str)  # For detailed output creation status

    stream_hash_progress = pyqtSignal(int)  # Signal for stream hash progress percentage
    md5_progress = pyqtSignal(int)          # Signal for MD5 calculation progress percentage
    access_file_progress = pyqtSignal(int)  # Signal for access file creation progress percentage
    qctools_progress = pyqtSignal(int)       # Signal for qctools xml creation progress percentage
    qctparse_progress = pyqtSignal(int)      # Signal for qct-parse analysis progress percentage
    clams_detection_progress = pyqtSignal(int)  # Signal for CLAMS detection (bars + tone) progress percentage
    tone_leak_progress = pyqtSignal(int)        # Signal for tone leak detection progress percentage
    mkvalidator_progress = pyqtSignal(int)     # Signal for mkvalidator validation progress percentage (time-estimated)
    frame_analysis_progress = pyqtSignal(int)  # Signal for frame analysis progress percentage
    report_progress = pyqtSignal(int)          # Signal for HTML report generation progress percentage

    clear_status = pyqtSignal()  # Signal to clear status message