# ConfigManager Documentation

> **See also:** [`../CLAUDE.md`](../CLAUDE.md) for the project map · [`cli-documentation.md`](cli-documentation.md) for the processing flow · [`gui-documentation.md`](gui-documentation.md) for how the GUI reads/writes config.

## Overview

The ConfigManager is a singleton class. It handles loading, caching, updating, and persisting configuration data across all modules in the AV Spex app.

## Configuration Architecture

The AV Spex application uses seven primary configs:

| Config key | Dataclass | JSON file | Purpose |
|------------|-----------|-----------|---------|
| `checks` | `ChecksConfig` | `checks_config.json` | Which tools run, fixity options, output options |
| `spex` | `SpexConfig` | `spex_config.json` | Expected metadata values for validation |
| `filename` | `FilenameConfig` | `filename_config.json` | Filename profile patterns |
| `signalflow` | `SignalflowConfig` | `signalflow_config.json` | Equipment signal chain profiles |
| `ffprobe` | `FfprobeConfig` | `ffprobe_config.json` | Custom FFprobe expected-value profiles |
| `mediainfo` | `MediainfoConfig` | `mediainfo_config.json` | Custom MediaInfo expected-value profiles |
| `exiftool` | `ExiftoolConfig` | `exiftool_config.json` | Custom ExifTool expected-value profiles |

Each configuration type is backed by:
- **JSON configuration files**: Default configurations bundled with the application
- **Dataclass definitions**: Type-safe Python objects defined in `config_setup.py`
- **User overrides**: Last-used configurations stored in the user's config directory

> **Note on boolean fields:** Boolean settings (`run_tool`, `check_tool`, `check_fixity`, etc.) are stored as native Python `bool` values. Older config JSON files used `"yes"`/`"no"` strings; `ConfigManager._migrate_yes_no_to_bool()` converts these on load for backward compatibility. New code should always use `True`/`False`.

## Instantiation and Singleton Pattern

The ConfigManager uses the **singleton design pattern** to ensure that only one instance exists throughout the entire application. This means that no matter how many times you call `ConfigManager()`, you always get the same instance.

```python
class ConfigManager:
    _instance = None  # Stores the single instance
    _configs: Dict[str, Any] = {}  # Shared configuration cache
    
    def __new__(cls):
        # If no instance exists yet, create one
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            # One-time initialization happens here
        
        # Always return the same instance
        return cls._instance
```

**How the Singleton Works:**

1. **First call** to `ConfigManager()`: Creates a new instance and stores it in `_instance`
2. **Subsequent calls** to `ConfigManager()`: Returns the existing instance from `_instance`
3. **Shared state**: All instances share the same `_configs` cache dictionary

**Why This Matters:**
```python
# These are all the same instance
config_mgr1 = ConfigManager()
config_mgr2 = ConfigManager()
config_mgr3 = ConfigManager()

# Changes made through one affect all others
config_mgr1.update_config('checks', {'tools': {'mediainfo': {'run_tool': True}}})
config_mgr2.get_config('checks', ChecksConfig)  # Will see the update made through config_mgr1
```

### Directory Structure and Path Management

During instantiation, the ConfigManager establishes comprehensive path management for different resource types:

```python
# Bundle directory (application installation location)
if getattr(sys, 'frozen', False):
    cls._instance._bundle_dir = os.path.join(sys._MEIPASS, 'AV_Spex')
else:
    cls._instance._bundle_dir = os.path.dirname(os.path.dirname(__file__))

# User configuration directory
cls._instance._user_config_dir = appdirs.user_config_dir(
    appname="AVSpex", appauthor="NMAAHC"
)

# Resource directories
cls._instance._logo_files_dir = os.path.join(cls._instance._bundle_dir, 'logo_image_files')
cls._instance._bundled_policies_dir = os.path.join(cls._instance._bundle_dir, 'config', 'mediaconch_policies')
cls._instance._user_policies_dir = os.path.join(cls._instance._user_config_dir, 'mediaconch_policies')
```

The ConfigManager automatically creates necessary directories and validates that bundled resources exist.

## Configuration Dataclass Structure

### Core Configuration Types

#### ChecksConfig
Controls processing workflow and tool execution:

```python
@dataclass
class ChecksConfig:
    outputs: OutputsConfig           # Controls report, access file, and frame analysis
    fixity: FixityConfig             # Fixity checking and validation settings
    tools: ToolsConfig               # Individual tool configurations
    validate_filename: bool = True   # Whether to run filename validation
```

**OutputsConfig**: Controls output generation
```python
@dataclass
class OutputsConfig:
    access_file: bool                            # Create low-resolution access copies
    report: bool                                 # Generate HTML reports
    qctools_ext: str                             # File extension for QCTools output (e.g., "qctools.xml.gz")
    frame_analysis: FrameAnalysisConfig          # Border/BRNG/signalstats/etc. sub-steps
    access_file_trim_color_bars: bool = True     # Trim color bars from access copy head
    access_file_crop_borders: bool = True        # Crop detected borders in access copy
```

**FixityConfig**: Manages file integrity checking
```python
@dataclass
class FixityConfig:
    check_fixity: bool                  # Compare against stored checksums
    validate_stream_fixity: bool        # Validate embedded stream hashes
    embed_stream_fixity: bool           # Embed MD5 hashes in MKV tags
    output_fixity: bool                 # Generate checksum files
    overwrite_stream_fixity: bool       # Overwrite existing embedded hashes
    checksum_algorithm: str = "md5"     # File-level checksum algorithm
    stream_hash_algorithm: str = "md5"  # Stream-level hash algorithm
```

**ToolsConfig**: Individual tool configurations with different structures for different tool types
```python
@dataclass
class ToolsConfig:
    exiftool: BasicToolConfig                # Standard metadata extraction tool
    ffprobe: BasicToolConfig                 # FFmpeg metadata probe tool
    mediaconch: MediaConchConfig             # Policy-based validation
    mediainfo: BasicToolConfig               # MediaInfo metadata tool
    mediatrace: BasicToolConfig              # MediaTrace container analysis
    qctools: QCToolsConfig                   # QCTools video analysis (run_tool only)
    qct_parse: QCTParseToolConfig            # Advanced QCTools parsing with multiple options
    clams_detection: ClamsDetectionConfig    # CLAMS bars + tone detector (default: off)
```

**Basic Tool Configuration** (ExifTool, FFprobe, MediaInfo, MediaTrace):
```python
@dataclass
class BasicToolConfig:
    check_tool: bool    # Whether to validate output against expected values
    run_tool: bool      # Whether to execute the tool
```

**QCTools Configuration**:
```python
@dataclass
class QCToolsConfig:
    run_tool: bool      # Whether to run QCTools analysis
```

**MediaConch Configuration**:
```python
@dataclass
class MediaConchConfig:
    mediaconch_policy: str    # Filename of XML policy file (e.g., "JPC_AV_NTSC_MKV_2025_03_26.xml")
    run_mediaconch: bool      # Whether to run MediaConch validation
```

**QCT Parse Configuration** (Advanced QCTools parsing):
```python
@dataclass
class QCTParseToolConfig:
    run_tool: bool                       # Whether to run qct-parse
    barsDetection: bool                  # Detect color bars in video
    evaluateBars: bool                   # Compare content to detected bars
    thumbExport: bool                    # Export thumbnail images of failed frames
    audio_analysis: bool = False         # Detect audio clipping, channel imbalance, identical channels, dropout
    detect_clamped_levels: bool = False  # Detect broadcast-range level clamping
```

**CLAMS Detection Configuration** (SSIM-based bars + cross-correlation tone detector):
```python
@dataclass
class ClamsBarsParams:
    threshold: float = 0.7
    sample_ratio: int = 30
    stop_at_frame: int = 9000
    min_frame_count: int = 10
    stop_after_one: bool = True
    merge_gap_seconds: float = 1.0

@dataclass
class ClamsToneParams:
    tolerance: float = 1.0
    min_tone_duration_ms: int = 2000
    stop_at_seconds: int = 3600
    merge_gap_seconds: float = 5.0

@dataclass
class ClamsDetectionConfig:
    run_tool: bool = False
    bars: ClamsBarsParams = field(default_factory=ClamsBarsParams)
    tone: ClamsToneParams = field(default_factory=ClamsToneParams)
```

CLAMS detection runs the bars detector in parallel with qct-parse for side-by-side comparison; the tone detector identifies spans of monotonic audio. qct-parse remains authoritative for downstream BRNG-skip and access-file trim. Numeric tuning of `bars`/`tone` parameters is JSON-only — only `clams_detection.run_tool` is settable from the CLI.

**FrameAnalysisConfig** — controls all per-frame analysis sub-steps under `outputs.frame_analysis`:

```python
@dataclass
class FrameAnalysisConfig:
    # Top-level enable flags for each sub-step
    enable_bitplane_check: bool = True
    enable_border_detection: bool = True
    enable_brng_analysis: bool = True
    enable_signalstats: bool = True
    enable_dropped_sample_detection: bool = True
    enable_duplicate_frame_detection: bool = True

    # Border detection mode and parameters
    border_detection_mode: str = "simple"      # "simple" or "sophisticated"
    simple_border_pixels: int = 25
    sophisticated_threshold: int = 10
    sophisticated_edge_sample_width: int = 100
    sophisticated_sample_frames: int = 30
    sophisticated_padding: int = 5
    auto_retry_borders: bool = True
    max_border_retries: int = 3

    # BRNG analysis parameters
    brng_duration_limit: int = 300       # Max seconds analyzed
    brng_skip_color_bars: bool = True    # Use qct-parse color_bars_end_time to skip head

    # Shared analysis-period settings (BRNG + signalstats)
    analysis_period_duration: int = 60   # Seconds per window
    analysis_period_count: int = 3       # Number of windows

    # Duplicate frame detection
    duplicate_min_run_length: int = 2
```

#### SpexConfig
Defines expected metadata values for validation:

```python
@dataclass
class SpexConfig:
    filename_values: FilenameValues
    mediainfo_values: Dict[str, Union[MediainfoGeneralValues, MediainfoVideoValues, MediainfoAudioValues]]
    exiftool_values: ExiftoolValues
    ffmpeg_values: Dict[str, Union[FFmpegVideoStream, FFmpegAudioStream, FFmpegFormat]]
    mediatrace_values: MediaTraceValues
    qct_parse_values: QCTParseValues
    signalflow_profiles: Dict[str, Dict] = field(default_factory=dict)
```

**Key Components**:
- **FilenameValues**: Expected filename structure and file extension
- **MediaInfo Values**: Separate dataclasses for general, video, and audio metadata expectations
- **ExiftoolValues**: Expected ExifTool metadata fields
- **FFmpeg Values**: Expected stream and format metadata from FFprobe
- **MediaTrace Values**: Expected container-level metadata including encoder settings
- **QCT Parse Values**: Quality control thresholds and analysis profiles

#### FilenameConfig and SignalflowConfig
Management of default and user-created profiles for filenames and embedded signal flow metadata:

```python
@dataclass
class FilenameConfig:
    filename_profiles: Dict[str, FilenameProfile]

@dataclass  
class SignalflowConfig:
    signalflow_profiles: Dict[str, SignalflowProfile] = field(default_factory=dict)
```

Use `replace_config_section()` (not `update_config()`) when applying these profiles, so a smaller replacement profile doesn't inherit leftover fields from the previous one.

#### Custom Tool-Profile Configs (ExiftoolConfig, MediainfoConfig, FfprobeConfig)

Each metadata tool also has its own dedicated config holding named "expected-values" profiles. Each profile is a complete set of expected fields the tool's parser will compare against. Default profiles are bundled in `config/{tool}_config.json`; user-saved profiles are merged in from the user config dir at load time.

```python
@dataclass
class ExiftoolConfig:
    exiftool_profiles: Dict[str, ExiftoolProfile] = field(default_factory=dict)

@dataclass
class MediainfoConfig:
    mediainfo_profiles: Dict[str, MediainfoProfile] = field(default_factory=dict)

@dataclass
class FfprobeConfig:
    ffprobe_profiles: Dict[str, FfprobeProfile] = field(default_factory=dict)
```

Applying a profile (via `apply_exiftool_profile` / `apply_mediainfo_profile` / `apply_ffprobe_profile`) writes the profile's values into the relevant section of `SpexConfig` (`exiftool_values`, `mediainfo_values`, or `ffmpeg_values`). The profile *config* and the *spex* config are kept separate so that switching profiles is non-destructive.

#### ChecksProfilesConfig

```python
@dataclass
class ChecksProfile:
    name: str
    description: str = ""
    validate_filename: bool = True
    outputs: OutputsConfig = field(default_factory=lambda: OutputsConfig(...))
    fixity: FixityConfig = field(default_factory=lambda: FixityConfig(...))
    tools: ToolsConfig = field(default_factory=lambda: ToolsConfig(...))

@dataclass
class ChecksProfilesConfig:
    custom_profiles: Dict[str, ChecksProfile] = field(default_factory=dict)
```

Holds user-created snapshots of `ChecksConfig` that can be re-applied later from the GUI.

## The Merge Process

The ConfigManager class uses a deep merge strategy when loading configurations to combine default settings with user-specific last-used settings.

### Loading Sequence

1. When `get_config()` is called for a config that isn't in cache:
   - Loads the JSON configuration using `_load_json_config()`
   - Creates a dataclass instance from the result using `_deserialize_dataclass()`

### Deep Merge (Recursive) 

The `_update_dict_recursively()` method implements a recursive dictionary merging strategy:

```python
def _update_dict_recursively(self, target: dict, source: dict) -> None:
    """Update dictionary recursively."""
    for key, value in source.items():
        if key in target:
            if isinstance(value, dict) and isinstance(target[key], dict):
                self._update_dict_recursively(target[key], value)
            else:
                target[key] = value
```

This function:
- Traverses both dictionaries recursively
- For nested dictionaries, continues merging deeper
- For non-dictionary values, the source value overwrites the target value

## Config Setup

As described in the previous section, any time `config_mgr = ConfigManager()` is called would instantiate the config, but the `_instance` clause of the `ConfigManager` class prevents more than one instance of the config existing at once.

After this initial instantiation, all other `ConfigManager` instantiations will receive the same singleton instance.

The first call of `get_config()` similarly creates an instance of the respective config if one is not already in the cache dictionary `_configs`.  

```python
config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)
filename_config = config_mgr.get_config('filename', FilenameConfig)
signalflow_config = config_mgr.get_config('signalflow', SignalflowConfig)
```

This calls the `ConfigManager`'s `get_config()` function, with the dataclass defined in `config_setup.py` passed as an argument:

```python
def get_config(self, config_name: str, config_class: Type[T], 
               use_last_used: bool = True) -> T:
    """
    Get configuration as a dataclass instance.
    
    Args:
        config_name: Name of the configuration
        config_class: Dataclass type to instantiate
        use_last_used: Whether to use the last used config if available
        
    Returns:
        An instance of the config_class
    """
    # Return cached config if available
    if config_name in self._configs:
        return self._configs[config_name]
        
    # Load config data from file
    config_data = self._load_json_config(config_name, use_last_used=use_last_used)
    
    # Convert to dataclass and cache
    config = self._deserialize_dataclass(config_class, config_data)
    self._configs[config_name] = config
    
    return config
```

### Enhanced Dataclass Deserialization

The `_deserialize_dataclass()` helper function performs a recursive conversion of the JSON data into proper dataclass instances, handling the complex nested structures now present in the configuration system:

```python
def _deserialize_dataclass(self, cls: Type[T], data: Dict[str, Any]) -> T:
    """Convert a dictionary to a dataclass instance."""
    if data is None:
        return None
    
    # Get type hints for the class
    type_hints = get_type_hints(cls)
    
    # Initialize kwargs for class constructor
    kwargs = {}
    
    for field_name, field_value in data.items():
        if field_name not in type_hints:
            # Skip fields not in dataclass
            continue
        
        field_type = type_hints[field_name]
        kwargs[field_name] = self._process_field(field_type, field_value)
    
    # Create and return the dataclass instance
    return cls(**kwargs)
```

### Advanced Type Handling

The system now handles significantly more complex type structures through specialized helper methods:

```python
def _process_field(self, field_type: Type, field_value: Any) -> Any:
    """Process a field value based on its type."""
    if field_value is None:
        return None
    
    # Get origin and args for complex types
    origin = get_origin(field_type)
    args = get_args(field_type)
    
    # Handle based on type
    if origin is list or origin is List:
        return self._handle_list(args[0], field_value)
    elif origin is dict or origin is Dict:
        return self._handle_dict(args[0], args[1], field_value)
    elif origin is Union:
        return self._handle_union(args, field_value)
    elif hasattr(field_type, '__dataclass_fields__'):  # Is a dataclass
        return self._deserialize_dataclass(field_type, field_value)
    else:
        # Simple type, use as is
        return field_value
```

#### Complex Union Type Handling

The enhanced type system supports Union types commonly used in the SpexConfig:

```python
def _handle_union(self, union_types: tuple, value: Any) -> Any:
    """Handle Union types (including Optional)."""
    if value is None:
        return None
    
    # Try each type in the union
    for union_type in union_types:
        if union_type is type(None):
            continue
            
        if hasattr(union_type, '__dataclass_fields__') and isinstance(value, dict):
            return self._deserialize_dataclass(union_type, value)
        elif isinstance(value, union_type):
            return value
    
    # If no specific handling, return as is
    return value
```

#### Dictionary with Complex Value Types

For handling nested dictionaries with dataclass values (like `mediainfo_values`):

```python
def _handle_dict(self, key_type: Type, value_type: Type, data: Dict) -> Dict:
    """Handle dictionary type conversion."""
    if not isinstance(data, dict):
        return data
    
    if hasattr(value_type, '__dataclass_fields__'):
        return {k: self._deserialize_dataclass(value_type, v) 
               for k, v in data.items() if isinstance(v, dict)}
    return data
```

### Backward-Compatibility Migration

When an older user JSON file is loaded, `_load_json_config()` runs `_migrate_config_data()` against the parsed dict before deserialization. The migration handles two main concerns:

1. **`"yes"`/`"no"` → `bool`** for every boolean field across the checks config (outputs, fixity, every tool's `check_tool`/`run_tool`, `mediaconch.run_mediaconch`, etc.). Done by `_migrate_yes_no_to_bool()`.
2. **Missing-field defaults**, e.g., adding `validate_filename: True` to old checks configs, or normalizing `outputs.qctools_ext` to one of the allowed extensions.

A reverse helper, `_migrate_bool_to_yes_no()`, exists for the temporary case where booleans must be written back as `"yes"`/`"no"` strings (used during the migration window).

```python
def _migrate_yes_no_to_bool(self, value: Any) -> Any:
    if isinstance(value, str):
        if value.lower() == "yes": return True
        if value.lower() == "no":  return False
    return value
```

A separate helper, `_cleanup_corrupted_configs()`, runs at startup and removes any `last_used_*_config.json` files that fail to parse (e.g., truncated writes from a crashed session) so the app can fall back to the bundled defaults instead of refusing to start.

### Configuration File Mapping Examples

#### ChecksConfig JSON Structure
```json
{
  "validate_filename": true,
  "outputs": {
    "access_file": false,
    "report": false,
    "qctools_ext": "qctools.xml.gz",
    "frame_analysis": {
      "enable_border_detection": true,
      "enable_brng_analysis": true,
      "enable_signalstats": true
    }
  },
  "fixity": {
    "check_fixity": false,
    "embed_stream_fixity": true
  },
  "tools": {
    "mediainfo": {
      "check_tool": true,
      "run_tool": true
    },
    "qct_parse": {
      "run_tool": false,
      "barsDetection": true,
      "evaluateBars": false,
      "thumbExport": false
    },
    "clams_detection": {
      "run_tool": false
    }
  }
}
```

#### SpexConfig JSON Structure
```json
{
  "mediainfo_values": {
    "expected_general": {
      "FileExtension": "mkv",
      "Format": "Matroska"
    },
    "expected_video": {
      "Format": "FFV1",
      "Width": "720",
      "Height": "486"
    },
    "expected_audio": {
      "Format": ["FLAC", "PCM"],
      "Channels": "2"
    }
  },
  "ffmpeg_values": {
    "video_stream": {
      "codec_name": "ffv1",
      "width": "720"
    }
  }
}
```

The conversion process handles:
- **Simple types**: Strings, integers, booleans passed through unchanged
- **Lists**: Processed according to their item type, supporting both simple values and nested dataclasses
- **Union types**: Like `Union[MediainfoGeneralValues, MediainfoVideoValues, MediainfoAudioValues]`
- **Nested dataclasses**: Recursively converted using `_deserialize_dataclass()`
- **Optional fields**: Handled through Union type processing

### Entry Points for Configuration Creation

The first instance of the various config types are loaded at different points in the CLI and GUI modes.  

**GUI Mode**
Configurations are first loaded at module-import time in the GUI tab modules (e.g., `gui_main.py`, `gui_checks_tab/gui_checks_window.py`, `gui_complex_window.py`):
```python
config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)
```

**CLI Mode**    
In `av_spex_the_file.py`, the global config_mgr is created, but the first actual retrieval of configurations happens during command processing, in the AVSpexProcessor:   

```python
# In AVSpexProcessor.__init__
self.config_mgr = ConfigManager()
self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
self.spex_config = self.config_mgr.get_config('spex', SpexConfig)
```

## Resource Management

### Logo and Asset Management

The ConfigManager provides centralized access to application assets:

```python
def get_logo_path(self, logo_filename: str) -> Optional[str]:
    """
    Get the full path for a logo file in the bundled logo_image_files directory
    
    Args:
        logo_filename: Name of the logo file
        
    Returns:
        Optional[str]: Full path to the logo file or None if not found
    """
    logo_path = os.path.join(self._logo_files_dir, logo_filename)
    if os.path.exists(logo_path):
        # Quote the path if it contains spaces
        return f'"{logo_path}"' if ' ' in logo_path else logo_path
    return None
```

### MediaConch Policy Management

The system supports both bundled and user-defined MediaConch policy files:

```python
def get_available_policies(self) -> list[str]:
    """Get all available policy files from both bundled and user directories"""
    policies = set()  # Use set to avoid duplicates
    
    # Get bundled policies
    if os.path.exists(self._bundled_policies_dir):
        policies.update(f for f in os.listdir(self._bundled_policies_dir) if f.endswith('.xml'))
        
    # Get user policies
    if os.path.exists(self._user_policies_dir):
        policies.update(f for f in os.listdir(self._user_policies_dir) if f.endswith('.xml'))
        
    return sorted(list(policies))

def get_policy_path(self, policy_name: str) -> Optional[str]:
    """Get the full path for a policy file, checking user dir first then bundled"""
    # Check user directory first
    user_path = os.path.join(self._user_policies_dir, policy_name)
    if os.path.exists(user_path):
        return f'"{user_path}"' if ' ' in user_path else user_path
        
    # Then check bundled policies
    bundled_path = os.path.join(self._bundled_policies_dir, policy_name)
    if os.path.exists(bundled_path):
        return f'"{bundled_path}"' if ' ' in bundled_path else bundled_path
        
    return None
```

### General File Discovery

For flexible file location across bundle and user directories:

```python
def find_file(self, filename: str, user_config: bool = False) -> Optional[str]:
    """
    Find a config file in either the bundled configs or user config directory.
    
    Args:
        filename: Name of the config file
        user_config: If True, look in user config directory instead of bundled configs
    """
    if user_config:
        file_path = os.path.join(self._user_config_dir, filename)
    else:
        file_path = os.path.join(self._bundle_dir, 'config', filename)
        
    return file_path if os.path.exists(file_path) else None
```

All resource management methods include automatic path quoting for filenames containing spaces.

## The Refresh Challenge

When working with the singleton pattern across multiple modules in Python, each module that imports and instantiates the `ConfigManager` at import time will get its own cached copy of the configuration data. This can lead to a critical synchronization issue:

```python
# Module-level instantiation pattern (GUI modules)
config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
```

While the singleton ensures there's only one `ConfigManager` instance per module, configuration changes made in the GUI aren't automatically reflected in the processing modules that load their configurations at different times.

## The Solution: Explicit Configuration Refresh

To address this issue, there is a dedicated method to explicitly refresh the configuration cache:

```python
def refresh_configs(self):
    """Force a reload of all configs from disk."""
    # Clear the cached configurations to force a reload from disk
    self._configs = {}
    logger.debug("Config cache cleared, configs will be reloaded from disk")
```

## Different Instantiation Patterns

The application uses two distinct patterns for configuration management:

### 1. GUI Components (Module-level Instantiation)
```python
# At module level (outside of class)
config_mgr = ConfigManager()
checks_config = config_mgr.get_config('checks', ChecksConfig)
spex_config = config_mgr.get_config('spex', SpexConfig)

class SomeGuiComponent:
    # Access configurations directly
```

### 2. Processing Classes (Instance-level Refresh)
```python
class AVSpexProcessor:
    def __init__(self, signals=None):
        self.signals = signals
        self._cancelled = False
        self._cancel_emitted = False 
        
        # Force a reload of the config from disk
        self.config_mgr = ConfigManager()
        self.config_mgr.refresh_configs()
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        self.spex_config = self.config_mgr.get_config('spex', SpexConfig)
```

This pattern ensures that processing components always have the most current configuration data at the time processing begins, regardless of when configuration changes were made in the GUI.

## Editing Configs

The ConfigManager provides mechanisms for editing configurations through the CLI or UI. These include targeted updates to specific fields, applying predefined profiles, and saving the updated configurations to persist changes between sessions.

### Updating Individual Settings

The `update_config()` method enables updates (as python dictionaries) to the config:

```python
def update_config(self, config_name: str, updates: dict) -> None:
    """
    Update specific fields in a config.
    
    Args:
        config_name: Name of the config to update
        updates: Dictionary of updates to apply
    """
    config = self._configs.get(config_name)
    if not config:
        logger.error(f"No config found for {config_name}, cannot update")
        return
        
    # Convert current config to dict
    config_dict = asdict(config)
    
    # Apply updates recursively
    self._update_dict_recursively(config_dict, updates)
    
    # Convert back to dataclass
    self._configs[config_name] = self._deserialize_dataclass(type(config), config_dict)
    
    # Save as last used
    self.save_config(config_name, is_last_used=True)
```

This method:
- Converts the current dataclass instance to a dictionary
- Applies updates recursively using `_update_dict_recursively()`
- Converts the updated dictionary back to a dataclass using `_deserialize_dataclass()`
- Saves the updated configuration as the last used config

#### GUI Integration

The GUI uses this mechanism to handle checkbox state changes:

```python
def on_checkbox_changed(self, state, path):
    """Handle changes in boolean checkboxes."""
    if self.is_loading:
        return

    new_value = Qt.CheckState(state) == Qt.CheckState.Checked

    if path[0] == "tools" and len(path) > 2:
        tool_name = path[1]
        field = path[2]
        updates = {'tools': {tool_name: {field: new_value}}}
    elif len(path) == 3:  # nested e.g. ['outputs', 'frame_analysis', 'enable_brng_analysis']
        updates = {path[0]: {path[1]: {path[2]: new_value}}}
    else:
        updates = {path[0]: {path[1]: new_value}}

    config_mgr.update_config('checks', updates)
```

This allows each checkbox to directly modify its corresponding field in the configuration. The `is_loading` guard prevents spurious writes when populating widgets during `load_config_values()`.

### Section Replacement

For wholesale replacement of configuration sections, the `replace_config_section()` method provides targeted section updates.

This method is must be used for filename profiles or signalflow configurations! It replaces the entire nested structure. Otherwise, a simpler file name profile with fewer fields would inheret the fields from the previously used profile which were not overwritten.

```python
def replace_config_section(self, config_name: str, section_path: str, new_value: any) -> None:
    """
    Replace an entire section of a config with a new value.
    
    Args:
        config_name: Name of the config to update
        section_path: Dot-separated path to the section (e.g., 'filename_values.fn_sections')
        new_value: New value to replace the entire section with
    """
    config = self._configs.get(config_name)
    if not config:
        logger.error(f"No config found for {config_name}, cannot replace section")
        return
        
    # Convert current config to dict
    config_dict = asdict(config)
    
    # Navigate to the parent of the target section
    path_parts = section_path.split('.')
    target_dict = config_dict
    
    # Navigate to the parent container
    for i, part in enumerate(path_parts[:-1]):
        if part not in target_dict:
            logger.error(f"Section path '{'.'.join(path_parts[:i+1])}' not found in config")
            return
        target_dict = target_dict[part]
        
    # Replace the target section
    final_key = path_parts[-1]
    if final_key not in target_dict:
        logger.error(f"Section '{section_path}' not found in config")
        return
        
    # Replace the section with the new value
    target_dict[final_key] = new_value
    
    # Convert back to dataclass
    self._configs[config_name] = self._deserialize_dataclass(type(config), config_dict)
    
    # Save as last used
    self.save_config(config_name, is_last_used=True)
```

### Applying Predefined Profiles

The system supports applying predefined Checks Config profiles that modify multiple settings at once through the `apply_profile()` function:

```python
def apply_profile(selected_profile):
    """Apply profile changes to checks_config.

    Args:
        selected_profile (dict): The profile configuration to apply
    """
    updates = {}

    # Handle top-level validate_filename
    if 'validate_filename' in selected_profile:
        updates['validate_filename'] = selected_profile['validate_filename']

    # Handle outputs / fixity sections (deep-merged by update_config)
    if 'outputs' in selected_profile:
        updates['outputs'] = selected_profile['outputs']
    if 'fixity' in selected_profile:
        updates['fixity'] = selected_profile['fixity']

    # Handle tools section
    if 'tools' in selected_profile:
        updates['tools'] = dict(selected_profile['tools'])

    if updates:
        config_mgr.update_config('checks', updates)
```

Predefined Checks Config profiles are stored as dictionaries in `config_edit.py` and use native booleans:

```python
profile_step1 = {
    "validate_filename": True,
    "tools": {
        "exiftool":   {"check_tool": True, "run_tool": True},
        "ffprobe":    {"check_tool": True, "run_tool": True},
        "mediaconch": {"mediaconch_policy": "JPC_AV_NTSC_MKV_2025_03_26.xml", "run_mediaconch": True},
        "mediainfo":  {"check_tool": True, "run_tool": True},
        "mediatrace": {"check_tool": True, "run_tool": True},
        "qctools":    {"run_tool": False},
        "qct_parse":  {"run_tool": False, "barsDetection": False, "evaluateBars": False,
                       "thumbExport": False, "audio_analysis": False, "detect_clamped_levels": False},
        "clams_detection": {"run_tool": False},
    },
    "outputs": {"access_file": False, "report": False, "qctools_ext": "qctools.xml.gz"},
    "fixity":  {"check_fixity": False, "validate_stream_fixity": False,
                "embed_stream_fixity": True, "output_fixity": True, "overwrite_stream_fixity": False},
}
```

### Specialized Configuration Updates

For domain-specific configuration areas like signal flow profiles, specialized update functions are provided:

```python
def apply_signalflow_profile(selected_profile: dict):
    """
    Apply signalflow profile changes to spex_config.
    
    Updates encoder settings in both mediatrace and ffmpeg configurations
    with values from the provided profile.
    
    Args:
        selected_profile (dict): The signalflow profile to apply (encoder settings)
    """
    # First refresh configs to ensure we're working with the latest data
    config_mgr.refresh_configs()
    
    # Get the current spex config
    spex_config = config_mgr.get_config('spex', SpexConfig)
    
    # Validate input
    if not isinstance(selected_profile, dict):
        logger.critical(f"Invalid signalflow settings: {selected_profile}")
        return
    
    # Update mediatrace_values.ENCODER_SETTINGS
    # Each key in selected_profile should be a field in ENCODER_SETTINGS (like Source_VTR)
    for key, value in selected_profile.items():
        if hasattr(spex_config.mediatrace_values.ENCODER_SETTINGS, key):
            setattr(spex_config.mediatrace_values.ENCODER_SETTINGS, key, value)
    
    # Now update ffmpeg_values.format.tags.ENCODER_SETTINGS if it exists
    if (hasattr(spex_config, 'ffmpeg_values') and 
        'format' in spex_config.ffmpeg_values and 
        'tags' in spex_config.ffmpeg_values['format']):
        
        # Initialize ENCODER_SETTINGS as a dict if needed
        if 'ENCODER_SETTINGS' not in spex_config.ffmpeg_values['format']['tags'] or \
           spex_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'] is None:
            spex_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'] = {}
            
        # Update the settings
        for key, value in selected_profile.items():
            spex_config.ffmpeg_values['format']['tags']['ENCODER_SETTINGS'][key] = value
    
    # Update the cached config directly
    config_mgr._configs['spex'] = spex_config
    
    # Save the updated config to disk
    config_mgr.save_config('spex', is_last_used=True)
    
    logger.debug(f"Applied signalflow profile to configuration")
```

This function handles the specific complexities of updating the signal flow configuration, ensuring that settings are applied correctly to all relevant parts of the nested structure.

## Configuration Import/Export System

The AV Spex application includes a config import/export system through the `ConfigIO` class, enabling users to share, backup, and restore config settings.

### ConfigIO Class

The `ConfigIO` class stores functions for importing and exporting config files.

#### Export Functionality

**Export All or Specific Configurations:**
```python
def export_configs(self, config_types: Optional[List[str]] = None) -> dict:
    """Export specified configs or all configs if none specified"""
    if isinstance(config_types, str):
        config_types = [config_types]
    elif not config_types:
        config_types = ['spex', 'checks']
    
    export_data = {}
    for config_type in config_types:
        if config_type == 'spex':
            config = self.config_mgr.get_config('spex', SpexConfig)
        elif config_type == 'checks':
            config = self.config_mgr.get_config('checks', ChecksConfig)
        elif config_type == 'filename':
            config = self.config_mgr.get_config('filename', FilenameConfig)
        elif config_type == 'signalflow':
            config = self.config_mgr.get_config('signalflow', SignalflowConfig)
        else:
            continue
        export_data[config_type] = asdict(config)
    
    return export_data
```

**Save to JSON File:**
```python
def save_config_files(self, filename: Optional[str] = None, config_types: Optional[List[str]] = None) -> str:
    """Save configs to JSON file"""
    if not filename:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'av_spex_config_export_{timestamp}.json'
    
    export_data = self.export_configs(config_types)
    
    # Ensure directory exists
    os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else '.', exist_ok=True)
    
    with open(filename, 'w') as f:
        json.dump(export_data, f, indent=2)
    
    return filename
```

#### Import Functionality

The import system handles complex nested structures and ensures proper dataclass reconstruction:

```python
def import_configs(self, config_file: str) -> None:
    """
    Import configs from JSON file.
    
    Loads configs from the provided JSON file and updates the cached configs,
    ensuring the changes are properly saved to disk and reflected in any
    consuming components.
    """
    # Open and parse the config file
    with open(config_file, 'r') as f:
        config_data = json.load(f)
    
    # Process each config type found in the imported data
    if 'spex' in config_data:
        spex_config = self.config_mgr._deserialize_dataclass(SpexConfig, config_data['spex'])
        self.config_mgr._configs['spex'] = spex_config
        self.config_mgr.save_config('spex', is_last_used=True)
    
    if 'checks' in config_data:
        checks_config = self.config_mgr._deserialize_dataclass(ChecksConfig, config_data['checks'])
        self.config_mgr._configs['checks'] = checks_config
        self.config_mgr.save_config('checks', is_last_used=True)
```

**Specialized Profile Import Handling:**

For filename and signalflow configurations, the import system provides specialized handling:

```python
# Filename profiles import
if 'filename' in config_data and 'filename_profiles' in config_data['filename']:
    imported_profiles = {}
    
    for profile_name, profile_data in config_data['filename']['filename_profiles'].items():
        fn_sections = {}
        for section_key, section_data in profile_data['fn_sections'].items():
            fn_sections[section_key] = {
                'value': section_data['value'],
                'section_type': section_data['section_type']
            }
        
        imported_profiles[profile_name] = {
            'fn_sections': fn_sections,
            'FileExtension': profile_data['FileExtension']
        }
    
    self.config_mgr.replace_config_section('filename', 'filename_profiles', imported_profiles)

# Signalflow profiles import  
if 'signalflow' in config_data and 'signalflow_profiles' in config_data['signalflow']:
    imported_profiles = {}
    
    for profile_name, profile_data in config_data['signalflow']['signalflow_profiles'].items():
        imported_profiles[profile_name] = {
            'name': profile_data.get('name', profile_name),
            'Source_VTR': profile_data.get('Source_VTR', []),
            'TBC_Framesync': profile_data.get('TBC_Framesync', []),
            'ADC': profile_data.get('ADC', []),
            'Capture_Device': profile_data.get('Capture_Device', []),
            'Computer': profile_data.get('Computer', [])
        }
    
    self.config_mgr.replace_config_section('signalflow', 'signalflow_profiles', imported_profiles)
```

#### CLI Integration

The command-line interface integrates with ConfigIO through the `handle_config_io()` function:

```python
def handle_config_io(args, config_mgr: ConfigManager):
    """Handle config I/O operations based on arguments"""
    config_io = ConfigIO(config_mgr)
    
    if args.export_config:
        config_types = ['spex', 'checks'] if args.export_config == 'all' else [args.export_config]
        filename = config_io.save_config_files(args.export_file, config_types)
        logger.debug(f"Configs exported to: {filename}")
    
    if args.import_config:
        config_io.import_configs(args.import_config)
        logger.debug(f"Configs imported from: {args.import_config}")
```

**Example CLI Usage:**
```bash
# Export all configurations
av-spex --export-config all --export-file my_config_backup.json

# Export only checks configuration
av-spex --export-config checks --export-file checks_only.json

# Import configurations
av-spex --import-config my_config_backup.json
```

## Configuration Utility Functions

### Configuration Display and Validation

The `config_edit.py` module provides utility functions for configuration management:

#### Configuration Display

```python
def print_config(config_spec='all'):
    """
    Print config state for specified config type(s) and optional subsections.
    
    Args:
        config_spec (str): Specification of what to print. Can be:
            - 'all': Print all configs
            - 'checks' or 'spex': Print entire specified config
            - 'checks,tools' or 'spex,filename_values': Print specific subsection
    """
```

**Example Usage:**
```bash
# Print all configurations
av-spex --printprofile all

# Print only tools section of checks config
av-spex --printprofile checks,tools

# Print filename values from spex config
av-spex --printprofile spex,filename_values
```

#### Configuration Validation

```python
def validate_config_spec(config_spec: str) -> bool:
    """
    Validate the config specification format.
    
    Returns:
        bool: True if valid, False if invalid
    """
    valid_subsections = {
        'spex': ['filename_values', 'mediainfo_values', 'exiftool_values', 
                'ffmpeg_values', 'mediatrace_values', 'qct_parse_values'],
        'checks': ['outputs', 'fixity', 'tools']
    }
```

#### Display Formatting

The system includes sophisticated formatting for configuration display:

```python
def format_config_value(value, indent=0, is_nested=False):
    """Format config values for display."""
    if isinstance(value, dict):
        # Handle nested dictionaries with proper indentation
    if isinstance(value, list):
        return ', '.join(str(item) for item in value)
    if value is True:  return "✅"
    if value is False: return "❌"
    return str(value)
```

### Predefined Configuration Profiles

The system includes several predefined profiles for common workflows:

#### Processing Profiles

**Profile Step 1 — Initial Processing** (metadata extraction + embedded fixity):
```python
profile_step1 = {
    "validate_filename": True,
    "tools": {
        "exiftool":  {"check_tool": True, "run_tool": True},
        "ffprobe":   {"check_tool": True, "run_tool": True},
        "mediaconch": {"run_mediaconch": True},
        "mediainfo": {"check_tool": True, "run_tool": True},
        "mediatrace": {"check_tool": True, "run_tool": True},
        "qctools":   {"run_tool": False},
        "qct_parse": {"run_tool": False},
        "clams_detection": {"run_tool": False},
    },
    "outputs": {"access_file": False, "report": False},
    "fixity":  {"embed_stream_fixity": True, "output_fixity": True},
}
```

**Profile Step 2 — Quality Analysis** (QCTools, qct-parse, frame analysis, HTML report):
```python
profile_step2 = {
    "validate_filename": True,
    "tools": {
        "qctools":   {"run_tool": True},
        "qct_parse": {
            "run_tool": True,
            "barsDetection": True,
            "evaluateBars": True,
            "thumbExport": True,
            "audio_analysis": True,
            "detect_clamped_levels": True,
        },
        "clams_detection": {"run_tool": False},
    },
    "outputs": {
        "report": True,
        "frame_analysis": {
            "enable_border_detection": True,
            "enable_brng_analysis": True,
            "enable_signalstats": True,
        },
    },
    "fixity": {"check_fixity": True, "validate_stream_fixity": True},
}
```

**Profile All Off** (`profile_allOff`): turns every tool/output/fixity option to `False`. Useful for starting from a clean slate before toggling individual options.

#### Signal Flow Equipment Profiles

**JPC AV S-VHS Configuration:**
```python
JPC_AV_SVHS = {
    "Source_VTR": ["SVO5800", "SN 122345", "composite", "analog balanced"],
    "TBC_Framesync": ["DPS575 with flash firmware h2.16", "SN 15230", "SDI", "audio embedded"],
    "ADC": ["DPS575 with flash firmware h2.16", "SN 15230", "SDI"],
    "Capture_Device": ["Black Magic Ultra Jam", "SN B022159", "Thunderbolt"],
    "Computer": ["2023 Mac Mini", "Apple M2 Pro chip", "SN H9HDW53JMV", "OS 14.5", "vrecord v2023-08-07", "ffmpeg"]
}
```

**Sony BVH3100 Configuration:**
```python
BVH3100 = {
    "Source_VTR": ["Sony BVH3100", "SN 10525", "composite", "analog balanced"],
    "TBC_Framesync": ["Sony BVH3100", "SN 10525", "composite", "analog balanced"],
    "ADC": ["Leitch DPS575 with flash firmware h2.16", "SN 15230", "SDI", "embedded"],
    "Capture_Device": ["Blackmagic Design UltraStudio 4K Extreme", "SN B022159", "Thunderbolt"],
    "Computer": ["2023 Mac Mini", "Apple M2 Pro chip", "SN H9HDW53JMV", "OS 14.5", "vrecord v2023-08-07", "ffmpeg"]
}
```

### CLI Checks Config Options

The command-line interface provides options for targeted configuration changes through the `--on` and `--off` flags, which are processed by the `toggle_on()` and `toggle_off()` functions. All settings now take native booleans:

```python
def update_tool_setting(tool_names: List[str], value: bool):
    """
    Update specific tool/fixity settings using config_mgr.update_config.
    Args:
        tool_names: List of strings in format 'tool.field'
        value: True or False
    """
    updates = {'tools': {}, 'fixity': {}}

    for tool_spec in tool_names:
        try:
            tool_name, field = tool_spec.split('.')

            if tool_name == 'fixity':
                if field not in ('check_fixity', 'validate_stream_fixity', 'embed_stream_fixity',
                                 'output_fixity', 'overwrite_stream_fixity'):
                    logger.warning(f"Invalid field '{field}' for fixity settings")
                    continue
                updates['fixity'][field] = value

            elif tool_name == 'mediaconch':
                if field not in ('run_mediaconch',):
                    logger.warning(f"Invalid field '{field}' for mediaconch.")
                    continue
                updates['tools'][tool_name] = {field: value}

            elif tool_name == 'qctools':
                if field not in ('run_tool',):
                    logger.warning(f"Invalid field '{field}' for qctools.")
                    continue
                updates['tools'][tool_name] = {field: value}

            elif tool_name == 'qct_parse':
                if field not in ('run_tool', 'barsDetection', 'evaluateBars', 'thumbExport',
                                 'audio_analysis', 'detect_clamped_levels'):
                    logger.warning(f"Invalid field '{field}' for qct_parse")
                    continue
                updates['tools'][tool_name] = {field: value}

            elif tool_name == 'clams_detection':
                # Only run_tool is settable from CLI; tune bars/tone parameters in JSON.
                if field != 'run_tool':
                    logger.warning(f"Invalid field '{field}' for clams_detection.")
                    continue
                updates['tools'][tool_name] = {field: value}

            else:  # Standard tools with check_tool/run_tool
                if field not in ('check_tool', 'run_tool'):
                    logger.warning(f"Invalid field '{field}' for {tool_name}.")
                    continue
                updates['tools'][tool_name] = {field: value}

        except ValueError:
            logger.warning(f"Invalid format '{tool_spec}'. Expected format: tool.field")

    # Drop any empty top-level sections before updating
    if not updates['tools']:  del updates['tools']
    if not updates['fixity']: del updates['fixity']

    if updates:
        config_mgr.update_config('checks', updates)


def toggle_on(tool_names: List[str]):  update_tool_setting(tool_names, True)
def toggle_off(tool_names: List[str]): update_tool_setting(tool_names, False)
```

This allows precise control from the command line:

```bash
# Turn on mediainfo tool
av-spex --on mediainfo.run_tool

# Turn off exiftool and fixity
av-spex --off exiftool.run_tool --off fixity.check_fixity

# Toggle a qct-parse sub-option
av-spex --on qct_parse.detect_clamped_levels

# Enable CLAMS bars+tone detection (numeric tuning is JSON-only)
av-spex --on clams_detection.run_tool
```

### Persisting Configuration Changes

After modifying a configuration, changes can be persisted to disk using the `save_config()` method, which is used at the end of the previously mentioned `update_config()` and `replace_config_section()` functions:

```python
def save_config(self, config_name: str, is_last_used: bool = False) -> None:
    """
    Save current config to a file.
    
    Args:
        config_name: Name of the config to save
        is_last_used: If True, save as last_used config, otherwise as regular config
    """
    config = self._configs.get(config_name)
    if not config:
        return
        
    filename = f"{'last_used_' if is_last_used else ''}{config_name}_config.json"
    save_path = os.path.join(self._user_config_dir, filename)
    
    try:
        with open(save_path, 'w') as f:
            json.dump(asdict(config), f, indent=2)
        logger.info(f"Saved config to {save_path}")
    except Exception as e:
        logger.error(f"Error saving config to {save_path}: {str(e)}")
```

### Resetting Configurations

The `reset_config()` method provides a way to restore a configuration to its default values:

```python
def reset_config(self, config_name: str, config_class: Type[T]) -> T:
    """
    Reset config to default values.
    
    Args:
        config_name: Name of the config to reset
        config_class: Type of the config class
        
    Returns:
        The reset config instance
    """
    # Remove from cache
    if config_name in self._configs:
        del self._configs[config_name]
        
    # Remove last used config file if it exists
    last_used_path = os.path.join(self._user_config_dir, f"last_used_{config_name}_config.json")
    if os.path.exists(last_used_path):
        try:
            os.remove(last_used_path)
            logger.info(f"Removed last used config file {last_used_path}")
        except Exception as e:
            logger.error(f"Failed to remove {last_used_path}: {str(e)}")
                
    # Load fresh config
    return self.get_config(config_name, config_class, use_last_used=False)
```

This method:
1. Removes the configuration from the in-memory cache
2. Deletes any last-used configuration file for this configuration
3. Loads a fresh configuration from the default config file
4. Returns the reset configuration instance

## Configuration File Relationships

### File Structure Overview

The AV Spex config system uses multiple JSON files to organize different aspects of the application:

```
config/
├── checks_config.json          # Processing workflow controls
├── spex_config.json            # Expected metadata values
├── filename_config.json        # Filename parsing profiles
├── signalflow_config.json      # Equipment signal flow profiles
├── exiftool_config.json        # Custom ExifTool expected-value profiles
├── mediainfo_config.json       # Custom MediaInfo expected-value profiles
├── ffprobe_config.json         # Custom FFprobe expected-value profiles
└── mediaconch_policies/        # MediaConch policy files
    ├── JPC_AV_NTSC_MKV_2025_03_26.xml
    └── [other policy files]
```

### Configuration Loading Priority

The ConfigManager follows a consistent loading priority:

1. **Last-used configurations** (user directory) - highest priority
2. **Default configurations** (bundled with application) - fallback
3. **User policies** (MediaConch policies in user directory) - for policies
4. **Bundled policies** (MediaConch policies in application bundle) - policy fallback

### Custom Configuration Dialogs

The application includes dialog windows for creating five categories of custom profiles through GUI forms:

| Dialog module | Purpose | Persisted to |
|---------------|---------|--------------|
| `gui/gui_custom_filename.py` | Filename pattern profiles | `filename_config.json` |
| `gui/gui_custom_signalflow.py` | Equipment signal flow profiles | `signalflow_config.json` |
| `gui/gui_custom_exiftool.py` | ExifTool expected-value profiles | `exiftool_config.json` |
| `gui/gui_custom_mediainfo.py` | MediaInfo expected-value profiles | `mediainfo_config.json` |
| `gui/gui_custom_ffprobe.py` | FFprobe expected-value profiles | `ffprobe_config.json` |

A shared profile manager (`gui/gui_custom_profiles.py`) lists, loads, and deletes saved profiles for any of these categories.

The metadata-tool dialogs (ExifTool, MediaInfo, FFprobe) are also reachable from the CLI via `--{tool}-from-file <FILE>`, which uses the corresponding helper in `utils/{tool}_import.py` to convert raw tool output (e.g., the JSON written by `mediainfo --Output=JSON`) into a profile, save it, and apply it.

#### Custom Filename Pattern Dialog

This dialog allows users to create filename validation patterns by defining sections separated by underscores:

```python
def get_filename_pattern(self):
    """Create a FilenameProfile dataclass from form inputs"""
    fn_sections = {}
    for i, section in enumerate(self.sections, 1):
        section_type = section['type_combo'].currentText().lower()  # literal, wildcard, regex
        value = section['value_input'].text()
        
        fn_sections[f"section{i}"] = FilenameSection(
            value=value,
            section_type=section_type
        )
        
    return FilenameProfile(
        fn_sections=fn_sections,
        FileExtension=self.extension_input.text()
    )
```

The dialog creates up to 8 filename sections, each with:
- **Section type**: Literal text, wildcard patterns (#,@,*), or regex
- **Value**: The actual pattern or text for that section
- **Live preview**: Shows the resulting filename pattern as you type

When saved, the pattern is applied using `config_edit.apply_filename_profile()`.

#### Custom Signal Flow Profile Dialog

This dialog captures the equipment chain used for video digitization:

```python
def get_profile(self):
    """Build signal flow profile from equipment form inputs"""
    profile = {"name": self.profile_name_input.text()}
    
    # Build equipment chain from form sections
    profile["Source_VTR"] = [vtr_model, serial_number, connection_type, audio_type]
    profile["TBC_Framesync"] = [tbc_model, firmware, serial_number, output_connection]
    profile["Capture_Device"] = [device_model, serial_number, interface_type]
    profile["Computer"] = [computer_model, processor, serial_number, os_version, software]
    
    return profile
```

The dialog organizes equipment into categories:
- **Source VTR**: Video tape recorder details
- **TBC/Frame Sync**: Time base corrector information
- **A/D Converter**: Analog-to-digital conversion (optional separate device)
- **Capture Device**: Digital capture hardware
- **Computer**: System specifications and software

Equipment details are stored as lists containing model, serial numbers, connection types, and other relevant specifications. When saved, the profile updates both `mediatrace_values.ENCODER_SETTINGS` and `ffmpeg_values.format.tags.ENCODER_SETTINGS` using `config_edit.apply_signalflow_profile()`.

#### Form Features

Both dialogs include:
- **Real-time preview**: Shows the resulting configuration as you make changes
- **Form validation**: Ensures required fields are filled before saving
- **Error handling**: Displays helpful error messages if saving fails

#### Profile Selection and Application Workflow

The Checks tab demonstrates a profile selection system that combines GUI interactions with configuration management:

```python
class ProfileHandlers:
    """Profile selection handlers for the Checks tab"""
    
    def __init__(self, parent_tab):
        self.parent_tab = parent_tab
        self.main_window = parent_tab.main_window
    
    def on_profile_selected(self, index):
        """Handle profile selection from dropdown with immediate application."""
        selected_profile = self.main_window.checks_profile_dropdown.currentText()
        
        # Map dropdown selection to predefined profiles
        profile_mapping = {
            "Step 1": config_edit.profile_step1,
            "Step 2": config_edit.profile_step2,
            "All Off": config_edit.profile_allOff
        }
        
        if selected_profile in profile_mapping:
            profile = profile_mapping[selected_profile]
            try:
                # Apply profile through config_edit utility
                config_edit.apply_profile(profile)
                logger.debug(f"Profile '{selected_profile}' applied successfully.")
                
                # Immediately refresh UI to reflect changes
                self.main_window.config_widget.load_config_values()
                
            except ValueError as e:
                logger.critical(f"Error applying profile: {e}")
```

#### Hierarchical GUI Organization Pattern

The Checks tab demonstrates a hierarchical organization pattern using nested handler classes:

```python
class ChecksTab(ThemeableMixin):
    """Main tab class with nested handler organization"""
    
    class ProfileHandlers:
        """Encapsulates all profile-related functionality"""
        def __init__(self, parent_tab):
            self.parent_tab = parent_tab
            self.main_window = parent_tab.main_window
        
        def on_profile_selected(self, index):
            # Profile selection logic
    
    def __init__(self, main_window):
        self.main_window = main_window
        
        # Initialize nested handler classes
        self.profile_handlers = self.ProfileHandlers(self)
        
        # Setup theme handling
        self.setup_theme_handling()
```
## How the ConfigManager is Used

### In Processing Components

Processing classes refresh configs when they start to ensure they have the latest settings from the GUI:

#### AVSpexProcessor
```python
class AVSpexProcessor:
    def __init__(self, signals=None):
        self.signals = signals
        self._cancelled = False
        self._cancel_emitted = False 

        self.config_mgr = ConfigManager()
        # Get latest configs from disk
        self.config_mgr.refresh_configs()
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        self.spex_config = self.config_mgr.get_config('spex', SpexConfig)
```

This ensures processing uses current settings, even if the user changed configs in the GUI after the processor was created.

#### ProcessingManager
```python
class ProcessingManager:
    def __init__(self, signals=None, check_cancelled_fn=None):
        self.signals = signals
        self.check_cancelled = check_cancelled_fn or (lambda: False)
        
        # Load fresh configs from disk
        self.config_mgr = ConfigManager()
        self.config_mgr.refresh_configs()
        self.checks_config = self.config_mgr.get_config('checks', ChecksConfig)
        self.spex_config = self.config_mgr.get_config('spex', SpexConfig)
```

### Config-Driven Processing Logic

The Checks Config values decide which processing steps are run:

```python
# Check if any fixity operations are enabled
fixity_config = self.checks_config.fixity
fixity_enabled = (
    fixity_config.check_fixity or
    fixity_config.validate_stream_fixity or
    fixity_config.embed_stream_fixity or
    fixity_config.output_fixity
)

if fixity_enabled:
    processing_mgmt.process_fixity(source_directory, video_path, video_id)

# Check if any metadata tools are enabled
tools_config = self.checks_config.tools
metadata_tools_enabled = any(
    getattr(getattr(tools_config, name), 'check_tool', False) or
    getattr(getattr(tools_config, name), 'run_tool', False)
    for name in ('mediainfo', 'mediatrace', 'exiftool', 'ffprobe')
)

if metadata_tools_enabled:
    metadata_differences = processing_mgmt.process_video_metadata(
        video_path, destination_directory, video_id
    )
```
### GUI Integration

#### Config Import and UI Synchronization
When users import new configs through the GUI, all the UI elements need to be updated to reflect the new settings:

```python
def import_config(self):
    """Import config from file and update all UI elements to match."""
    # Load the new config from the selected file
    config_io = ConfigIO(config_mgr)
    config_io.import_configs(file_path)
    
    # Tell the main config widget to refresh its checkboxes and values
    self.main_window.config_widget.load_config_values()
    
    # Get the newly imported configs
    checks_config = config_mgr.get_config('checks', ChecksConfig)
    spex_config = config_mgr.get_config('spex', SpexConfig)
    
    # Update dropdown menus to match the imported settings
    # blockSignals prevents the dropdown change from triggering another config update
    if hasattr(self.main_window, 'checks_profile_dropdown'):
        self.main_window.checks_profile_dropdown.blockSignals(True)
        
        # Set dropdown based on what tools are actually enabled
        if checks_config.tools.exiftool.run_tool:
            self.main_window.checks_profile_dropdown.setCurrentText("Step 1")
        else:
            self.main_window.checks_profile_dropdown.setCurrentText("Step 2")
            
        self.main_window.checks_profile_dropdown.blockSignals(False)
```

#### MediaConch Policy Management
The CLI and GUI can accept a custom MediaConch policy file and copy it to the user's config directory:

```python
def setup_mediaconch_policy(user_policy_path: str = None) -> str:
    """Copy user's policy file and update config to use it."""
    config_mgr = ConfigManager()
    
    if user_policy_path:
        # Get just the filename from the full path
        policy_filename = os.path.basename(user_policy_path)
        
        # Copy the file to the user's policies directory
        user_policy_dest = os.path.join(config_mgr._user_policies_dir, policy_filename)
        shutil.copy2(user_policy_path, user_policy_dest)
        
        # Update the config to use this new policy file
        # but keep the existing run_mediaconch setting
        current_config = config_mgr.get_config('checks', ChecksConfig)
        config_mgr.update_config('checks', {
            'tools': {
                'mediaconch': {
                    'mediaconch_policy': policy_filename,
                    'run_mediaconch': current_config.tools.mediaconch.run_mediaconch
                }
            }
        })
    
    return policy_filename
```

#### CLI Config Mapping
The CLI maps the `--profile` argument to predefined Checks Config dictionaries:

```python
PROFILE_MAPPING = {
    "step1": config_edit.profile_step1,   # Metadata extraction profile
    "step2": config_edit.profile_step2,   # Quality analysis profile
    "off":   config_edit.profile_allOff,  # Turn off all tools
}
```

Filename and signal-flow profiles are looked up by **profile name** at runtime — there is no hard-coded dict mapping. The CLI also accepts a small set of legacy short-name aliases for backward compatibility:

```python
_sn_aliases = {'JPC_AV_SVHS': 'JPC_AV_SVHS Signal Flow', 'BVH3100': 'BVH3100 Signal Flow'}
_fn_aliases = {'jpc': 'JPC Filename Profile', 'bowser': 'Bowser Filename Profile'}

# av-spex -fn "JPC Filename Profile"   ← canonical
# av-spex -fn jpc                      ← legacy short alias (still resolves correctly)
```

If the supplied name doesn't match an existing profile, the CLI prints the list of available profile names from the relevant config.

### Configuration Persistence Best Practices

#### Automatic Saving on Application Exit
```python
def save_configs_on_quit():
    """Save configurations when application exits."""
    config_mgr = ConfigManager()
    config_mgr.save_config('checks', is_last_used=True)
    config_mgr.save_config('spex', is_last_used=True)

# Connect to application exit signal
app.aboutToQuit.connect(save_configs_on_quit)
```

#### Strategic Configuration Refresh Points
```python
def on_check_spex_clicked(self):
    """Handle processing initiation with config persistence."""
    # Save current state before processing
    config_mgr.save_config('checks', is_last_used=True)
    config_mgr.save_config('spex', is_last_used=True)
    
    # Initiate processing (which will refresh configs)
    self.main_window.processing.call_process_directories()
```

## Best Practices

1. **Processing Components**: Always call `refresh_configs()` in processing class constructors to ensure latest configuration state
2. **Configuration Persistence**: Save configurations as `last_used` immediately before processing operations and on application exit
3. **Policy Management**: Use ConfigManager's path management methods for external files like MediaConch policies
4. **Dynamic Processing**: Structure processing logic to be configuration-driven rather than hardcoded
5. **Error Handling**: Implement robust error handling for configuration operations, especially in GUI contexts
6. **Signal Blocking**: Use `blockSignals()` when programmatically updating GUI elements to prevent recursive configuration updates
7. **Resource Management**: Leverage ConfigManager's resource discovery methods rather than hardcoding file paths

This pattern balances consistency and freshness, ensuring that all components work with appropriately timed configuration data while providing robust resource management and complex dataclass support.