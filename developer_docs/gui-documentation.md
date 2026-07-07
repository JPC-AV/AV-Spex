# AV Spex GUI Developer Documentation

> **See also:** [`../CLAUDE.md`](../CLAUDE.md) for the project map · [`config-manager-documentation.md`](config-manager-documentation.md) for the config system the GUI drives · [`cli-documentation.md`](cli-documentation.md) for the processing pipeline the worker thread runs.

## Overview

This document describes the code that defines the GUI mode of the AV Spex application.

The GUI is created using the PyQt6, a set of Python bindings for Qt6 library.   

PyQt6's import structure is organized into several modules, the main ones are:

 - `PyQt6.QtWidgets`
    Contains all UI components (widgets) for building interfaces:
    ```python
    from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QLabel
    ```
- `PyQt6.QtCore`
    Contains core non-GUI functionality:
    ```python
    from PyQt6.QtCore import Qt, QSize, QTimer, QRect, pyqtSignal
    ```
- `PyQt6.QtGui`
    Contains GUI-related classes that aren't widgets:
    ```python
    from PyQt6.QtGui import QIcon, QFont, QColor, QPainter, QPixmap
    ```

The AV Spex GUI code is organized into a modular structure with subdirectories:

### Main GUI Directory Structure:
```
gui/
├── __init__.py
├── gui_checks_tab/
│   ├── __init__.py
│   ├── gui_checks_tab.py
│   └── gui_checks_window.py
├── gui_complex_tab.py
├── gui_complex_window.py
├── gui_custom_exiftool.py
├── gui_custom_ffprobe.py
├── gui_custom_filename.py
├── gui_custom_mediainfo.py
├── gui_custom_profiles.py
├── gui_custom_signalflow.py
├── gui_import_tab.py
├── gui_main.py
├── gui_main_window/
│   ├── __init__.py
│   ├── gui_main_window_processing.py
│   ├── gui_main_window_signals.py
│   ├── gui_main_window_theme.py
│   └── gui_main_window_ui.py
├── gui_processing_window.py
├── gui_processing_window_console.py
├── gui_signals.py
├── gui_spex_tab.py
└── gui_theme_manager.py
```

### Key Components:

**Core GUI Modules:**
- `gui_main.py` - Main GUI entry point and launcher
- `gui_signals.py` - Signal definitions for communication throughout gui
- `gui_theme_manager.py` - Theme management and styling

**Main Window Module (`gui_main_window/`):**
- `gui_main_window_ui.py` - UI layout and component setup
- `gui_main_window_processing.py` - Processing workflow management
- `gui_main_window_signals.py` - Signal handling and connections for processing workflow
- `gui_main_window_theme.py` - Window specific styling and connection to theme manager

**Checks Tab Module (`gui_checks_tab/`):**
- `gui_checks_tab.py` - Checks tab "profiles" section with "ProfileHandlers" class for managing profile changes
- `gui_checks_window.py` - Checks tab "output" section

**Complex Tab Module:**
- `gui_complex_tab.py` - "Complex" tab container; hosts the `ComplexWindow` in a scroll area
- `gui_complex_window.py` - Configuration window for QCTools, qct-parse, and all frame analysis settings (border detection, BRNG analysis, signalstats)

**Tab Modules:**
- `gui_import_tab.py` - Import tab with directory selection and config import/export
- `gui_spex_tab.py` - Spex tab which uses a similar ProfileHandlers class for managing file name and signal flow profiles

**Custom Profile Dialogs:**
- `gui_custom_exiftool.py` - Dialog for creating/editing custom ExifTool expected-value profiles
- `gui_custom_ffprobe.py` - Dialog for creating/editing custom FFprobe expected-value profiles
- `gui_custom_mediainfo.py` - Dialog for creating/editing custom MediaInfo expected-value profiles
- `gui_custom_filename.py` - Dialog for creating/editing custom filename profiles
- `gui_custom_signalflow.py` - Dialog for creating/editing signal flow profiles
- `gui_custom_profiles.py` - Profile manager dialog (list, load, delete saved profiles)

**Other GUI Components:**
- `gui_processing_window.py` - Processing status and progress display
- `gui_processing_window_console.py` - Console text output, prints the logging messages to the processing window

## Entry Point: 
The GUI is launched from `gui_main.py`, which contains the main `MainWindow` class and serves as the primary entry point for the application. The `MainWindow` class coordinates all the modular components:

```python
from AV_Spex.gui.gui_main_window.gui_main_window_ui import MainWindowUI
from AV_Spex.gui.gui_main_window.gui_main_window_signals import MainWindowSignals
from AV_Spex.gui.gui_main_window.gui_main_window_processing import MainWindowProcessing
from AV_Spex.gui.gui_main_window.gui_main_window_theme import MainWindowTheme

from AV_Spex.gui.gui_import_tab import ImportTab
from AV_Spex.gui.gui_checks_tab.gui_checks_tab import ChecksTab
from AV_Spex.gui.gui_spex_tab import SpexTab

class MainWindow(QMainWindow, ThemeableMixin):
    """Main application window with tabs for configuration and settings."""
    
    def __init__(self):
        super().__init__()
        self.signals = ProcessingSignals()
        self.worker = None
        self.processing_window = None

        # Initialize collections for theme-aware components
        self.import_tab_group_boxes = [] 
        self.spex_tab_group_boxes = []
        self.checks_tab_group_boxes = []
        
        # Initialize settings
        self.settings = QSettings('NMAAHC', 'AVSpex')
        self.selected_directories = []
        self.check_spex_clicked = False
        self.source_directories = []

        # Initialize MainWindow helper classes
        self.ui = MainWindowUI(self)
        self.signals_handler = MainWindowSignals(self)
        self.processing = MainWindowProcessing(self)
        self.theme = MainWindowTheme(self)

        # Initialize Tabs
        self.checks_tab = ChecksTab(self)
        self.spex_tab = SpexTab(self)
        self.import_tab = ImportTab(self)

        # Connect all signals
        self.signals_handler.setup_signal_connections()
        
        # Setup UI
        self.ui.setup_ui()
        
        # Setup theme handling
        self.setup_theme_handling()
```

## Main Window

The Main Window functionality is distributed across multiple modules in the `gui_main_window/` directory, which serves as the central UI component for the AV Spex gui:

- **`gui_main_window_ui.py`**: Contains the `MainWindowUI` class with UI setup and layout management
- **`gui_main_window_processing.py`**: Contains the `MainWindowProcessing` class for processing workflows and worker thread management  
- **`gui_main_window_signals.py`**: Contains the `MainWindowSignals` class for signal connections and event handling
- **`gui_main_window_theme.py`**: Contains the `MainWindowTheme` class for theme-related styling and updates

The main `MainWindow` class in `gui_main.py` coordinates these modules by creating instances of each helper class and storing them as attributes:

```python
class MainWindow(QMainWindow, ThemeableMixin):
    def __init__(self):
        super().__init__()
        # ... initialization ...
        
        # Initialize MainWindow helper classes
        self.ui = MainWindowUI(self)
        self.signals_handler = MainWindowSignals(self)
        self.processing = MainWindowProcessing(self)
        self.theme = MainWindowTheme(self)
```

### Inter-Module Communication:

The modules reference each other through the main window instance. For example:

- **Processing module** calling signal handlers: `self.main_window.signals_handler.update_main_status_label()`
- **Signal handlers** calling processing methods: `self.main_window.processing.on_processing_started()`
- **Theme module** accessing UI elements: `self.main_window.tabs` for styling
- **UI module** calling theme setup: `self.main_window.theme._load_logo()`

This design allows each module to focus on its specific responsibilities while maintaining clear communication pathways through the central `MainWindow` instance.

### Core Methods:
- `setup_ui()`: Initializes the main UI components and layout (in `MainWindowUI`)
- `setup_tabs()`: Creates the tabbed interface for configuration (in `MainWindowUI`)
    -  `setup_ui()` establishes the window structure
    - Calls `setup_main_layout()` to create the core layout
    - Calls `logo_setup()` to load application branding
    - Calls `setup_tabs()` to create the tabbed interface, which then calls:
        - `setup_checks_tab()` to build the Checks configuration UI
        - `setup_spex_tab()` to build the Spex configuration UI
- `call_process_directories()`: creates worker thread and connects worker-specific signals (in `MainWindowProcessing`)
    - Calls `initialize_processing_window()` if needed
        - `initialize_processing_window()` creates and configures the [processing window](#processing-window-and-console-text-box)
    - Signal connection: Connections between the GUI and Processor to report progress throughout the process (Described in detail in [AV Spex GUI Processing Signals Flow section](#AVSpexGUIProcessingSignalsFlow))

## Checks Window

The Checks Window functionality is organized within the `gui_checks_tab/` directory:

- `gui_checks_tab.py` - Checks tab "profiles" section with "ProfileHandlers" class for managing profile changes
- `gui_checks_window.py` - Checks tab "output" section

The `ChecksWindow` class provides an interface for displaying and editing the `ChecksConfig`. The window is added as a widget to the `MainWindow` in `setup_checks_tab()` function.

### Core Methods:
- `setup_ui()`: Initializes the Checks interface
  - Creates the main layout structure
  - Calls section-specific setup methods in sequence (validation section, outputs section, fixity section, tools section)
  - Connects all UI signals to their handlers
- `setup_validation_section(main_layout)` — top of the tab; exposes `checks_config.validate_filename`
- `setup_outputs_section(main_layout)` — access file (with sub-options for trim/crop), HTML report, qctools extension
- `setup_fixity_section(main_layout)` — fixity options plus checksum/stream-hash algorithm dropdowns
- `setup_tools_section(main_layout)` — `check_tool`/`run_tool` checkboxes for the standard metadata tools and MediaConch policy chooser

Each section utilizes the ThemeManager to maintain consistent styling, using Qt GroupBoxes and the ThemeManager's `style_groupbox()` function (more in the [Theme Manager section](#theme-manager-documentation)). Signal connections trigger updates through handler methods that work with the `ConfigManager` to persist changes to `ChecksConfig`.

As described in the [ConfigManager documentation](https://github.com/JPC-AV/JPC_AV_videoQC/blob/main/developer_docs/config-manager-documentation.md), the updates to the `ChecksConfig` from the `ChecksWindow` primarily make use of the `ConfigManager`'s `update_config()` function, such as in the `on_checkbox_changed` function:

```python
def on_checkbox_changed(self, state, path):
    """Handle changes in boolean checkboxes."""
    if self.is_loading:
        return  # Skip writes while populating widgets

    new_value = Qt.CheckState(state) == Qt.CheckState.Checked

    if path[0] == "tools" and len(path) > 2:
        tool_name, field = path[1], path[2]
        updates = {'tools': {tool_name: {field: new_value}}}
    elif len(path) == 3:  # nested e.g. ['outputs', 'frame_analysis', 'enable_brng_analysis']
        updates = {path[0]: {path[1]: {path[2]: new_value}}}
    else:
        updates = {path[0]: {path[1]: new_value}}

    config_mgr.update_config('checks', updates)
```

> **Note**: Boolean fields are now native Python `bool` (not `"yes"`/`"no"` strings). The `is_loading` guard prevents spurious config writes while widgets are being populated by `load_config_values()`.

## Complex Tab

The Complex tab exposes advanced configuration options for QCTools, qct-parse, and frame analysis. It is defined across two modules:

- `gui_complex_tab.py` - Contains the `ComplexTab` class, which creates the "Complex" tab widget and hosts a `ComplexWindow` inside a scroll area
- `gui_complex_window.py` - Contains the `ComplexWindow` class, which provides all the actual UI controls

The tab is initialized by `MainWindowUI.setup_tabs()` alongside the other tabs:

```python
self.main_window.complex_tab.setup_complex_tab()
```

### ComplexWindow Sections

`ComplexWindow.setup_ui()` builds the window in this order:

#### 1. QCTools

Controls the `checks_config.tools.qctools` settings:

- **Run Tool** checkbox — enables/disables QCTools execution
- **QCTools File Extension** dropdown — selects output format: `qctools.xml.gz` or `qctools.mkv` (stored in `checks_config.outputs.qctools_ext`)

#### 2. qct-parse

Controls the `checks_config.tools.qct_parse` settings. Dependency logic is enforced in the UI:

- **Run Tool** — when checked, automatically enables and checks all sub-options; when unchecked, greys them out
- **Detect Color Bars** (`barsDetection`) — when unchecked, automatically unchecks and disables Evaluate Color Bars and Thumbnail Export
- **Evaluate Color Bars** (`evaluateBars`) — compares detected bars against expected values
- **Thumbnail Export** (`thumbExport`) — exports thumbnails of failed frames; enabled when at least one of bars detection or evaluate bars is active
- **Perform Audio Analysis** (`audio_analysis`) — detects audio clipping, channel imbalance, audible timecode, and audio dropout
- **Detect Clamped Levels** (`detect_clamped_levels`) — detects broadcast-range level clamping from the analog-to-digital converter

#### 3. CLAMS Detection

Controls the `checks_config.tools.clams_detection` settings (single section, runs both detectors):

- **Run Tool** — runs the CLAMS SSIM-based SMPTE bars detector and the cross-correlation tone detector together. The bars detector runs in parallel with qct-parse for side-by-side comparison; the tone detector identifies spans of monotonic audio (e.g. SMPTE bars-and-tones).
- Numeric tuning of the bars/tone parameters is **JSON-only** — only the `Run Tool` toggle is exposed in the UI. qct-parse remains authoritative for downstream BRNG-skip and access-file trim.

#### 4. Frame Analysis sections

`setup_frame_analysis_sections()` renders these sub-sections in order, each with its own enable checkbox under `checks_config.outputs.frame_analysis`:

##### 4a. Bitplane Check

- **Enable Bitplane Check** (`enable_bitplane_check`) — verifies that the 9th and 10th bits of 10-bit video contain data (some TBC/framesync devices truncate them, producing effectively 8-bit video).

##### 4b. Frame Analysis Periods

Shared settings for signalstats and BRNG analysis:

- **Number of Periods** (`analysis_period_count`) — how many time windows to sample across the video
- **Period Duration (s)** (`analysis_period_duration`) — length of each analysis window in seconds

##### 4c. Border Detection

- **Enable Border Detection** (`enable_border_detection`)
- **Detection Mode** (`border_detection_mode`) dropdown — `simple` (fixed pixel crop) or `sophisticated` (edge detection)
  - *Simple mode*: **Border Pixels** field (`simple_border_pixels`, default 25)
  - *Sophisticated mode*: Detection Parameters sub-group (`sophisticated_threshold`, `sophisticated_edge_sample_width`, `sophisticated_sample_frames`, `sophisticated_padding`), plus **Auto-retry** checkbox (`auto_retry_borders`) and **Max Retries** field (`max_border_retries`)
- Signalstats is disabled in the UI if border detection is turned off.

##### 4d. Signalstats

- **Enable Signalstats Analysis** (`enable_signalstats`) — runs the FFmpeg `signalstats` filter. Requires border detection to be enabled (enforced by the UI).

##### 4e. BRNG Analysis

- **Enable BRNG Analysis** (`enable_brng_analysis`)
- **Duration Limit (s)** (`brng_duration_limit`) — maximum duration to analyze for out-of-range values
- **Skip Color Bars** (`brng_skip_color_bars`) — excludes color bar sections detected by qct-parse from BRNG analysis

##### 4f. Dropped Sample Detection

- **Enable Dropped Sample Detection** (`enable_dropped_sample_detection`)

##### 4g. Duplicate Frame Detection

- **Enable Duplicate Frame Detection** (`enable_duplicate_frame_detection`)
- **Min Run Length** (`duplicate_min_run_length`) — minimum consecutive duplicate-frame run to flag

### Config Update Pattern

`ComplexWindow` uses `on_boolean_changed()` for checkboxes and `on_frame_analysis_param_changed()` for numeric inputs. Both call `config_mgr.update_config('checks', updates)` to persist changes. The `is_loading` flag prevents spurious config writes during `load_config_values()`.

```python
def on_boolean_changed(self, state, path):
    if self.is_loading:
        return
    new_value = Qt.CheckState(state) == Qt.CheckState.Checked
    # path examples:
    #   ['tools', 'qctools', 'run_tool']
    #   ['outputs', 'frame_analysis', 'enable_border_detection']
    updates = {path[0]: {path[1]: {path[2]: new_value}}}
    config_mgr.update_config('checks', updates)
```

## Processing Window and Console Text Box

The processing window functionality is split between two modules that work together to provide visualization of processing operations:

- **`gui_processing_window.py`**: Contains the `ProcessingWindow` class for overall window management and status display
- **`gui_processing_window_console.py`**: Contains the `ConsoleTextEdit` class for specialized console text output and logging display

### ProcessingWindow Class

The `ProcessingWindow` class serves as a comprehensive status display interface with multiple components for tracking processing progress.

#### Core UI Components:

**File Status Display:**
- `file_status_label`: Shows current file being processed with progress count
- `progress_bar`: Main progress bar showing overall file processing progress
- Displays format: "Processing (current/total): filename"

**Steps Tracking System:**
- `steps_list`: QListWidget showing all enabled processing steps based on current `ChecksConfig`
- `populate_steps_list()`: Dynamically builds step list from configuration (fixity, tools, outputs)
- `mark_step_complete(step_name)`: Updates steps with checkmarks and bold formatting
- `reset_steps_list()`: Clears and repopulates steps for new file processing

**Console Output System:**
- `details_text`: Instance of custom `ConsoleTextEdit` class for styled console output
- `update_status(message, msg_type=None)`: Main method for adding messages with automatic type detection
- Intelligent message type detection based on content (error, warning, command, success, info)

**Detailed Progress Tracking:**
- `detailed_status`: QLabel for current operation status messages
- `detail_progress_bar`: Secondary progress bar with overlay percentage display
- `update_detail_progress(percentage)`: Updates detailed progress with percentage overlay
- Modern overlay styling with theme-aware colors

#### Advanced Features:

**Window Management:**
- Custom close behavior: hides window instead of closing when user clicks X
- Proper cleanup during application shutdown

**Theme Integration:**
- Comprehensive theme support through `ThemeableMixin`
- Dynamic progress bar styling based on current palette
- Console text styling integration
- Real-time theme updates for all components

**Logger Integration:**
- Direct integration with application logging system through `connect_logger_to_ui()`
- Automatic routing of log messages to console display
- Maintains separate logger instance for processing window

### ConsoleTextEdit Class

The `ConsoleTextEdit` class provides a console-like interface to output log messages to the processing window.

#### Message Type System:

**Supported Message Types:**
```python
class MessageType(Enum):
    NORMAL = auto()    # Regular output
    SUCCESS = auto()   # Success messages (green, bold)
    ERROR = auto()     # Error messages (red, bold)
    WARNING = auto()   # Warning messages (orange, bold)
    INFO = auto()      # Informational messages (blue, bold)
    COMMAND = auto()   # Command being executed (bold)
```

**Advanced Text Formatting:**
- `append_message(text, msg_type)`: Adds styled text with appropriate formatting
- `_get_format_for_type(msg_type)`: Creates and caches QTextCharFormat for each message type
- Monospace font support (Courier New) for console-like appearance
- Color-coded messages with distinct styling for each type

**Performance Optimization:**
- Format caching system to avoid recreating text formats
- `clear_formats()`: Clears format cache during theme changes
- Efficient cursor management and text insertion

**Special Features:**
- `add_processing_divider(text)`: Creates visual dividers between processing runs
- Horizontal scrolling support with no word wrapping
- Automatic scrolling to bottom for new messages

**Text Styling:**
- Base font: Courier New, monospace, 14pt
- Bold formatting for non-normal message types
- Preserved text formatting across theme changes

### Integration with Main Application:

**Signal Flow Integration:**
The processing window integrates with the main application through signal connections:

```python
# Status updates from processing signals
self.main_window.signals.status_update.connect(processing_window.update_status)
self.main_window.signals.file_started.connect(processing_window.update_file_status)
self.main_window.signals.step_completed.connect(processing_window.mark_step_complete)

# Progress tracking signals
self.main_window.signals.stream_hash_progress.connect(processing_window.update_detail_progress)
self.main_window.signals.md5_progress.connect(processing_window.update_detail_progress)
self.main_window.signals.access_file_progress.connect(processing_window.update_detail_progress)
```

**Configuration-Driven Display:**
The processing window dynamically adapts its step display based on the current `ChecksConfig`:
- Automatically includes only enabled fixity operations
- Shows only configured tools and their run/check status
- Updates step list when configuration changes

**Logger Integration Pattern:**
The ProcessingWindow's `update_status()` function integrates with the application logger system:

```python
# Logger connection established during initialization
self.logger = connect_logger_to_ui(self)

# Messages automatically routed from logging system to console display
# Supports all standard logging levels with appropriate formatting
```

This integrated system provides users with comprehensive, real-time feedback about processing operations while maintaining clean separation between window management, console display, and logging systems.

## AV Spex GUI Processing Signals Flow

The AV Spex GUI uses PyQt's signal-slot mechanism to handle processing events and update the user interface asynchronously. This document outlines the signal flow architecture between the main components of the application to show how processing events are communicated throughout the system.

### 1. Signal Definition (`ProcessingSignals` Class)

The `ProcessingSignals` class (in `gui_signals.py`) defines all the custom signals used throughout the application:

```python
class ProcessingSignals(QObject):
    started = pyqtSignal(str)               # Processing started
    completed = pyqtSignal(str)             # Processing completed
    error = pyqtSignal(str)                 # Error occurred
    cancelled = pyqtSignal()                # Processing cancelled

    status_update = pyqtSignal(str)         # General status updates
    progress = pyqtSignal(int, int)         # Numerical progress (current, total)

    file_started = pyqtSignal(str, int, int)  # File processing started (path, current_idx, total)
    tool_started = pyqtSignal(str)            # Tool processing started
    tool_completed = pyqtSignal(str)          # Tool processing completed
    step_completed = pyqtSignal(str)          # Processing step completed
    step_failed = pyqtSignal(str)             # Processing step failed
    step_reset = pyqtSignal(str)              # Reset a step back to pending (e.g. before re-running)

    fixity_progress = pyqtSignal(str)         # Fixity status updates
    mediaconch_progress = pyqtSignal(str)     # MediaConch status updates
    metadata_progress = pyqtSignal(str)       # Metadata status updates
    output_progress = pyqtSignal(str)         # Output creation status updates

    stream_hash_progress = pyqtSignal(int)       # Stream hash progress percentage
    md5_progress = pyqtSignal(int)               # MD5 calculation progress percentage
    access_file_progress = pyqtSignal(int)       # Access file creation progress percentage
    qctools_progress = pyqtSignal(int)           # QCTools XML creation progress percentage
    qctparse_progress = pyqtSignal(int)          # qct-parse analysis progress percentage
    clams_detection_progress = pyqtSignal(int)   # CLAMS bars + tone detector progress percentage
    tone_leak_progress = pyqtSignal(int)         # Tone leak detection progress percentage
    frame_analysis_progress = pyqtSignal(int)    # Frame analysis progress percentage
    report_progress = pyqtSignal(int)            # HTML report generation progress percentage

    clear_status = pyqtSignal()               # Clear status message
```

### 2. Main Window (`MainWindow` Class)

The `MainWindow` class (distributed across the `gui_main_window/` modules):
- Instantiates the `ProcessingSignals` object
- Connects signals to appropriate handler methods
- Creates and manages the `ProcessingWindow`
- Creates and manages the worker thread

### 3. Processing Window (`ProcessingWindow` Class)

The `ProcessingWindow` class:
- Displays status messages, progress bar, and step completion status
- Updates UI elements based on signals
- Contains the cancel button to terminate processing

### 4. Worker Thread (`ProcessingWorker` Class)

The `ProcessingWorker` class:
- Runs the actual processing in a separate thread
- Emits worker-specific signals such as `started_processing`, `finished`, etc.
- Forwards signals from the processor

### 5. Processor (`AVSpexProcessor` Class)

The `AVSpexProcessor` class:
- Performs the actual media file processing
- Emits signals about the processing status

## Signal Flow Sequence

```
┌─────────────┐    1. Creates    ┌─────────────┐
│  MainWindow │────────────────▶ │ ProcessingWindow │
└─────┬───────┘                 └───────┬─────┘
      │                                 │
      │ 2. Creates                      │
      ▼                                 │
┌─────────────┐    3. Runs     ┌─────────────┐
│ Worker Thread│────────────────▶ │ AVSpexProcessor │
└─────┬───────┘                 └───────┬─────┘
      │                                 │
      │ 4. Emits worker signals         │ 5. Emits processing signals
      │                                 │
      │                                 │
      ▼                                 ▼
┌─────────────────────────────────────────────┐
│               Signal Bus                     │
└─────────────────┬───────────────────────────┘
                  │
                  │ 6. Signals routed to handlers
                  │
                  ▼
┌─────────────────────────────────────────────┐
│       MainWindow signal handler methods      │
└─────────────────┬───────────────────────────┘
                  │
                  │ 7. Update UI
                  │
                  ▼
┌─────────────────────────────────────────────┐
│              ProcessingWindow                │
└─────────────────────────────────────────────┘
```

## Signal Connection Setup

In the `MainWindow` class (in `gui_main_window_signals.py`), the `setup_signal_connections` method connects signals to their respective handler methods:

```python
def setup_signal_connections(self):
     # Processing window signals
    self.signals.started.connect(self.on_processing_started)
    self.signals.completed.connect(self.on_processing_completed)
    self.signals.error.connect(self.on_error)
    self.signals.cancelled.connect(self.on_processing_cancelled)

    # Connect file_started signal to update main status label
    self.signals.file_started.connect(self.update_main_status_label)
    
    # Tool-specific signals
    self.signals.tool_started.connect(self.on_tool_started)
    self.signals.tool_completed.connect(self.on_tool_completed)
    self.signals.fixity_progress.connect(self.on_fixity_progress)
    self.signals.mediaconch_progress.connect(self.on_mediaconch_progress)
    self.signals.metadata_progress.connect(self.on_metadata_progress)
    self.signals.output_progress.connect(self.on_output_progress)
```

## Signal Handler Methods

The signal handler methods in `MainWindow` update the `ProcessingWindow` UI:

```python
def on_tool_started(self, tool_name):
    """Handle tool processing start"""
    if self.processing_window:
        self.processing_window.update_status(f"Starting {tool_name}")

def on_tool_completed(self, message):
    """Handle tool processing completion"""
    if self.processing_window:
        self.processing_window.update_status(message)
        # Let UI update
        QApplication.processEvents()
```

## Worker Thread Signal Connections

When initializing the worker thread, specific worker signals are connected:

```python
def call_process_directories(self):
    # ...
    self.worker = ProcessingWorker(self.source_directories, self.signals)
    
    # Connect worker-specific signals
    self.worker.started_processing.connect(self.on_processing_started)
    self.worker.finished.connect(self.on_worker_finished)
    self.worker.error.connect(self.on_error)
    self.worker.processing_time.connect(self.on_processing_time)
    
    # Start the worker thread
    self.worker.start()
```

## Signal Emission Points

### In the Worker Thread

```python
def run(self):
    try:
        # Signal that processing has started
        self.started_processing.emit()
        
        # Process directories...
        
        # Signal completion with timing information
        self.processing_time.emit(processing_time)
        self.finished.emit()
    
    except Exception as e:
        self.error.emit(f"Processing error: {str(e)}")
```

### In the Processor

```python
def process_single_directory(self, source_directory):
    if self.check_cancelled():
        return False

    # Process fixity...
    if fixity_enabled:
        if self.signals:
            self.signals.tool_started.emit("Fixity...")
        
        # Processing...
        
        if self.signals:
            self.signals.tool_completed.emit("Fixity processing complete")
```


# Theme Manager Documentation

## Overview

The Theme Manager system in our PyQt6 application provides a centralized mechanism for applying consistent theming across the application, especially when switching between light and dark modes. It allows all UI components to respond to system palette changes and maintain visual consistency throughout the application.

## Architecture

The theme management system consists of two core components:

1. **ThemeManager**: A singleton class responsible for detecting palette changes and providing styling methods
2. **ThemeableMixin**: A mixin class that can be added to any widget that needs to respond to theme changes

### Class Diagram

```
┌───────────────────┐     Theme Change     ┌──────────────────┐
│                   │       Events         │                  │
│   QApplication    │───────────────────▶ │   ThemeManager   │
│                   │                      │                  │
└───────────────────┘                      └──────────────────┘
                                                    │
                                                    │ Notifies
                                                    ▼
┌───────────────────┐     Implements     ┌──────────────────┐
│                   │                    │                  │
│    MainWindow     │──────────────────▶│  ThemeableMixin  │
│  ProcessingWindow │                    │                  │
│   ChecksTab       │                    │                  │
│   SpexTab         │                    │                  │
│   ImportTab       │                    │                  │
└───────────────────┘                    └──────────────────┘
```

## Key Components

### ThemeManager Class

The `ThemeManager` class is implemented as a singleton to ensure that only one instance exists in the application. It monitors the system palette changes and provides styling methods for different UI components.

#### Key Features:

- **Singleton Pattern**: Only one instance of ThemeManager exists at any time
- **System Palette Monitoring**: Connects to QApplication's paletteChanged signal
- **Theme Change Notifications**: Emits signals when theme changes are detected
- **Styling Utilities**: Provides methods for consistent styling of UI component types
- **Fallback Theme Detection**: Multiple fallback methods for detecting system theme, including macOS-specific detection

### ThemeableMixin Class

The `ThemeableMixin` class is a mixin that can be applied to any QWidget-derived class that needs to respond to theme changes.

#### Key Features:

- **Theme Change Handling**: Automatically responds to theme changes and propagates to child components
- **Simple Integration**: Can be added to any PyQt widget class
- **Customizable Response**: Can be overridden to customize theme behavior (for 'Checks Spex' and other buttons)
- **Proper Cleanup**: Includes methods to disconnect from theme signals
- **Child Component Propagation**: Automatically finds and updates child components with theme handlers

## Signal Flow

When the system palette changes, the following sequence occurs:

1. QApplication emits a `paletteChanged` signal with the new palette
2. ThemeManager receives this signal and emits its own `themeChanged` signal
3. All widgets that implement ThemeableMixin receive the `themeChanged` signal
4. Each widget's `on_theme_changed` method is called to apply the new styling
5. ThemeableMixin automatically propagates theme changes to child components

## Integration with UI Components

### MainWindow

The main application window integrates with the theme system through its dedicated theme helper:

1. Creates a `MainWindowTheme` helper class instance
2. Delegates theme changes to `self.theme.on_theme_changed(palette)`
3. The theme helper manages logo refreshing, special button styling, and component updates
4. Handles cleanup during window closure for all child components

### Tab Components (ImportTab, ChecksTab, SpexTab)

All tab components implement `ThemeableMixin` and follow a consistent pattern:

1. Inherit from `ThemeableMixin` and call `setup_theme_handling()` during initialization
2. Maintain collections of UI components for styling (e.g., `self.main_window.import_tab_group_boxes`)
3. Implement custom `on_theme_changed(palette)` methods that style their specific components
4. Clean up theme connections appropriately

## Styling Methods

The ThemeManager provides comprehensive styling methods for all UI component types:

### Component-Specific Styling

#### style_groupbox(group_box, title_position)
Applies consistent styling to QGroupBox widgets with support for title positioning:
- Sets border, background color, and text color based on current palette
- Supports title positioning ("top left", "top center", etc.)
- Preserves existing title position if none specified

#### style_button(button, special_style)
Applies styling to individual buttons with special button types:
- **Standard buttons**: Uses palette-based colors for consistency
- **Special styles**: 
  - `"check_spex"`: Green styling for the main Check Spex button
  - `"processing_window"`: White/green styling for processing controls
  - `"cancel_processing"`: Red styling for cancellation actions

#### style_combobox(combo_box)
Applies comprehensive styling to QComboBox widgets:
- Styles the main combobox appearance and hover states
- Customizes dropdown arrow and border styling
- Styles dropdown list items and selection highlighting

#### style_console_text(text_edit)
Enhanced text editor styling with robust theme detection:
- Applies theme-appropriate background and text colors
- Includes custom scrollbar styling
- Features fallback color schemes for packaged applications
- Handles selection highlighting and border styling

#### style_progress_bar(progress_bar)
Applies palette-based styling to progress bars for consistency with system theme.

### Bulk Styling Methods

#### style_buttons(parent_widget)
Finds and styles all QPushButton widgets within a parent widget using standard styling.

#### style_comboboxes(parent_widget) 
Finds and styles all QComboBox widgets within a parent widget.

#### style_tabs(tab_widget)
Applies comprehensive styling to QTabWidget components including tab bars and content areas.

### Logo and Branding

#### get_theme_appropriate_logo(light_logo_path, dark_logo_path)
Returns the correct logo path based on current system theme using robust detection methods.

#### load_logo(label, logo_path, width, height)
Loads and scales logo images into QLabel widgets with optional dimension constraints.

### Utility Methods

#### detect_system_theme()
Robust theme detection with multiple fallback methods:
1. **PyQt6 palette analysis**: Primary method using luminance calculation
2. **macOS system detection**: Uses NSUserDefaults or subprocess calls
3. **Environment variable override**: Supports `AVSPEX_THEME` environment variable
4. **Time-based fallback**: Uses current hour as creative fallback method

#### apply_theme_to_all(widget)
Convenience method that applies appropriate styling to all supported widget types under a parent widget.

## Usage Examples

### Adding Theme Support to a New Component

```python
from AV_Spex.gui.gui_theme_manager import ThemeableMixin

class MyNewTab(ThemeableMixin):
    def __init__(self, main_window):
        self.main_window = main_window
        
        # Initialize collections for theme-aware components
        self.my_tab_group_boxes = []
        
        # Set up theme handling
        self.setup_theme_handling()
    
    def on_theme_changed(self, palette):
        """Handle theme changes for this tab"""
        theme_manager = ThemeManager.instance()
        
        # Update all group boxes
        for group_box in self.my_tab_group_boxes:
            if group_box is not None:
                theme_manager.style_groupbox(group_box)
        
        # Style buttons within specific groups
        if hasattr(self, 'my_group'):
            theme_manager.style_buttons(self.my_group)
    
    def cleanup_theme_handling(self):
        """Clean up theme connections"""
        super().cleanup_theme_handling()
```

### Using Special Button Styling

```python
# Style special buttons with predefined styles
theme_manager = ThemeManager.instance()

# Main action button
theme_manager.style_button(self.check_spex_button, special_style="check_spex")

# Processing control buttons  
theme_manager.style_button(self.open_processing_button, special_style="processing_window")
theme_manager.style_button(self.cancel_button, special_style="cancel_processing")

# Standard button
theme_manager.style_button(self.regular_button)  # Uses default styling
```

### Logo Management with Theme Support

```python
# In a theme change handler
theme_manager = ThemeManager.instance()

# Define logos for different themes
light_logo = config_mgr.get_logo_path('logo_light.png') 
dark_logo = config_mgr.get_logo_path('logo_dark.png')

# Get appropriate logo for current theme
logo_path = theme_manager.get_theme_appropriate_logo(light_logo, dark_logo)

# Load and display the logo
success = theme_manager.load_logo(self.logo_label, logo_path, width=400)
```

# Dependency Manager

The Dependency Manager system provides a mechanism for checking external CLI tool dependencies required by the AV Spex application. It offers both GUI and CLI checking modes, with detailed feedback about missing dependencies and installation guidance for users.

## Architecture

The dependency management system consists of three core components:

1. **DependencyInfo**: A dataclass that encapsulates information about each required dependency
2. **DependencyCheckWorker**: A QThread worker that performs non-blocking dependency checks in the GUI
3. **DependencyManager**: A static class that orchestrates dependency checking for both GUI and CLI modes

### Class Diagram

```
┌───────────────────┐     Creates     ┌──────────────────┐
│                   │                 │                  │
│ DependencyManager │────────────────▶│ DependencyInfo   │
│                   │                 │                  │
└─────────┬─────────┘                 └──────────────────┘
          │
          │ Creates (GUI mode)
          ▼
┌───────────────────┐    Runs checks   ┌──────────────────┐
│ DependencyCheck   │─────────────────▶│ DependencyCheck  │
│     Dialog        │                  │     Worker       │
└───────────────────┘                  └──────────────────┘
```

## Key Components

### DependencyInfo Class

The `DependencyInfo` dataclass describes a single dependency:

```python
@dataclass
class DependencyInfo:
    name: str                           # Display name (e.g., "FFmpeg")
    command: str                        # CLI command (e.g., "ffmpeg")
    version_command: Optional[str]      # Version check command
    min_version: Optional[str]          # Minimum required version
    description: str                    # User-friendly description
    install_hint: str                   # Installation instructions
    status: DependencyStatus            # Current check status
    version_found: Optional[str]        # Detected version
    error_message: Optional[str]        # Error details if check failed
```

### DependencyCheckWorker Class

The worker thread performs dependency checks without blocking the GUI:

- **Non-blocking Checks**: Runs in separate thread to maintain UI responsiveness
- **Real-time Updates**: Emits signals as each dependency is checked
- **Version Validation**: Checks both existence and version requirements when specified
- **Cancellation Support**: Can be interrupted if user cancels the operation

## Signal Flow Sequence

```
┌─────────────┐    1. Creates    ┌─────────────┐
│ Application │────────────────▶ │ DependencyCheck │
│  Startup    │                 │    Dialog       │
└─────────────┘                 └───────┬─────────┘
                                        │
                                        │ 2. Creates
                                        ▼
                                ┌─────────────┐
                                │ DependencyCheck │
                                │    Worker       │
                                └───────┬─────────┘
                                        │
                                        │ 3. Emits check results
                                        │
                                        ▼
                                ┌─────────────────────┐
                                │    Signal Bus       │
                                └─────────┬───────────┘
                                          │
                                          │ 4. Updates UI
                                          │
                                          ▼
                                ┌─────────────────────┐
                                │  Dialog UI Updates  │
                                └─────────────────────┘
```

## Required Dependencies

The system currently checks for five external CLI tools:

1. **FFmpeg** (`ffmpeg`)
2. **MediaInfo** (`mediainfo`)
3. **ExifTool** (`exiftool`)
4. **MediaConch** (`mediaconch`)
5. **QCTools** (`qcli`)