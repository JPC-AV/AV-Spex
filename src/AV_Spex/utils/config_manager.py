from dataclasses import asdict
from typing import Optional, TypeVar, Type, List, Dict, Union, Any, get_type_hints, get_origin, get_args
import json
import os
import glob
from pathlib import Path
import appdirs
import sys

from AV_Spex.utils.log_setup import logger
from AV_Spex.utils.config_setup import PROTECTED_PROFILES

T = TypeVar('T')

class ConfigManager:
    _instance = None  # Class-level variable to hold single instance
    _configs: Dict[str, Any] = {}  # Shared configuration cache
    _config_classes: Dict[str, Any] = {}  # Registry of config name → dataclass type
    
    # The __new__(cls) insures only one instance is ever created
    def __new__(cls):
        if cls._instance is None:
            # If no config class instance exists, create a instance of ConfigManager.
            # super() calls the parent class to get ConfigManager's __new__ from within ConfigManager
            cls._instance = super(ConfigManager, cls).__new__(cls)
            
            # One-time initialization of paths and directories
            if getattr(sys, 'frozen', False):
                cls._instance._bundle_dir = os.path.join(sys._MEIPASS, 'AV_Spex')
            else:
                cls._instance._bundle_dir = os.path.dirname(os.path.dirname(__file__))
            
            # Set up user config directory using appdirs
            cls._instance._user_config_dir = appdirs.user_config_dir(
                appname="AVSpex",
                appauthor="NMAAHC"
            )
            
            # Set up logo files directory path
            cls._instance._logo_files_dir = os.path.join(cls._instance._bundle_dir, 'logo_image_files')
            
            # Set up policies directory paths
            cls._instance._bundled_policies_dir = os.path.join(cls._instance._bundle_dir, 'config', 'mediaconch_policies')
            cls._instance._user_policies_dir = os.path.join(cls._instance._user_config_dir, 'mediaconch_policies')
            
            # Ensure directories exist
            os.makedirs(cls._instance._user_config_dir, exist_ok=True)
            os.makedirs(cls._instance._user_policies_dir, exist_ok=True)
            
            # Verify bundled configs exist
            bundle_config_dir = os.path.join(cls._instance._bundle_dir, 'config')
            if not os.path.exists(bundle_config_dir):
                raise FileNotFoundError(f"Bundled config directory not found at {bundle_config_dir}")
            
        return cls._instance

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
            return logo_path
        return None
    
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
            return user_path
            
        # Then check bundled policies
        bundled_path = os.path.join(self._bundled_policies_dir, policy_name)
        if os.path.exists(bundled_path):
            return bundled_path
            
        return None

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

    def remove_all_last_used_configs(self) -> int:
        """
        Delete every last_used_*_config.json in the user config dir and clear
        the in-memory cache. Returns the number of files removed.

        Globbing (rather than a hardcoded list) keeps this correct as new
        configs are added — it covers checks, spex, filename, signalflow, the
        per-tool expected-value profiles (exiftool/ffprobe/mediainfo), and the
        custom checks profiles (profiles_checks). Used both to recover from
        corrupted configs and to back the CLI --use-default-config reset.
        """
        pattern = os.path.join(self._user_config_dir, "last_used_*_config.json")
        last_used_files = glob.glob(pattern)

        deleted_count = 0
        for file_path in last_used_files:
            try:
                os.remove(file_path)
                logger.info(f"Removed last_used config file: {os.path.basename(file_path)}")
                deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete {file_path}: {str(e)}")

        # Clear the entire config cache to ensure clean state
        self._configs.clear()
        return deleted_count

    def _cleanup_corrupted_configs(self) -> None:
        """
        Delete all last_used config files and clear the config cache.
        This is called when config loading fails to reset to a clean state.
        """
        deleted_count = self.remove_all_last_used_configs()

        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} corrupted config file(s), will use default configs")
        else:
            logger.info("No last_used config files found to clean up")

    def _migrate_yes_no_to_bool(self, value: Any) -> Any:
        """
        Convert "yes"/"no" strings to booleans for backward compatibility.
        
        Args:
            value: The value to potentially convert
            
        Returns:
            Boolean if value was "yes"/"no", otherwise returns value unchanged
        """
        if isinstance(value, str):
            if value.lower() == "yes":
                return True
            elif value.lower() == "no":
                return False
        return value

    def _migrate_bool_to_yes_no(self, value: Any) -> Any:
        """
        Convert boolean values to "yes"/"no" strings for backward compatibility when saving.
        This is used temporarily during the migration period.
        
        Args:
            value: The value to potentially convert
            
        Returns:
            "yes"/"no" if value was boolean, otherwise returns value unchanged
        """
        if isinstance(value, bool):
            return "yes" if value else "no"
        return value

    def _migrate_config_data(self, config_data: dict, config_name: str) -> dict:
        """
        Migrate configuration data from old format to new format.
        Specifically converts "yes"/"no" strings to booleans for checks config.
        
        Args:
            config_data: The raw configuration dictionary
            config_name: Name of the configuration being migrated
            
        Returns:
            Migrated configuration dictionary
        """
        if config_name == 'checks':
            # Add validate_filename if missing (for backward compatibility)
            if 'validate_filename' not in config_data:
                config_data['validate_filename'] = True  # Default to True for backward compatibility
                logger.info("Added missing 'validate_filename' field to checks config (defaulted to True)")
            
            # Migrate outputs section
            if 'outputs' in config_data:
                for key in ['access_file', 'report']:
                    if key in config_data['outputs']:
                        config_data['outputs'][key] = self._migrate_yes_no_to_bool(
                            config_data['outputs'][key]
                        )
                # Validate qctools_ext against allowed values
                if 'qctools_ext' in config_data['outputs']:
                    valid_extensions = ("qctools.xml.gz", "qctools.mkv")
                    if config_data['outputs']['qctools_ext'] not in valid_extensions:
                        logger.info(f"Invalid qctools_ext '{config_data['outputs']['qctools_ext']}', resetting to 'qctools.xml.gz'")
                        config_data['outputs']['qctools_ext'] = "qctools.xml.gz"
                
                # Migrate renamed frame_analysis period fields
                if 'frame_analysis' in config_data['outputs']:
                    fa = config_data['outputs']['frame_analysis']
                    if 'signalstats_duration' in fa and 'analysis_period_duration' not in fa:
                        fa['analysis_period_duration'] = fa.pop('signalstats_duration')
                        logger.info("Migrated 'signalstats_duration' → 'analysis_period_duration'")
                    if 'signalstats_periods' in fa and 'analysis_period_count' not in fa:
                        fa['analysis_period_count'] = fa.pop('signalstats_periods')
                        logger.info("Migrated 'signalstats_periods' → 'analysis_period_count'")
            
            # Migrate fixity section
            if 'fixity' in config_data:
                for key in ['check_fixity', 'validate_stream_fixity', 'embed_stream_fixity', 
                           'output_fixity', 'overwrite_stream_fixity']:
                    if key in config_data['fixity']:
                        config_data['fixity'][key] = self._migrate_yes_no_to_bool(
                            config_data['fixity'][key]
                        )
            
            # Migrate tools section
            if 'tools' in config_data:
                tools = config_data['tools']
                
                # Basic tools (exiftool, ffprobe, mediainfo, mediatrace)
                for tool in ['exiftool', 'ffprobe', 'mediainfo', 'mediatrace']:
                    if tool in tools:
                        for key in ['check_tool', 'run_tool']:
                            if key in tools[tool]:
                                tools[tool][key] = self._migrate_yes_no_to_bool(
                                    tools[tool][key]
                                )
                
                # QCTools
                if 'qctools' in tools:
                    if 'run_tool' in tools['qctools']:
                        tools['qctools']['run_tool'] = self._migrate_yes_no_to_bool(
                            tools['qctools']['run_tool']
                        )
                
                # MediaConch
                if 'mediaconch' in tools:
                    if 'run_mediaconch' in tools['mediaconch']:
                        tools['mediaconch']['run_mediaconch'] = self._migrate_yes_no_to_bool(
                            tools['mediaconch']['run_mediaconch']
                        )
                
                # QCT Parse
                if 'qct_parse' in tools:
                    for key in ['run_tool', 'barsDetection', 'evaluateBars', 'thumbExport']:
                        if key in tools['qct_parse']:
                            tools['qct_parse'][key] = self._migrate_yes_no_to_bool(
                                tools['qct_parse'][key]
                            )
                    # Migrate detect_audio_clipping + detect_channel_imbalance → audio_analysis
                    if 'detect_audio_clipping' in tools['qct_parse'] or 'detect_channel_imbalance' in tools['qct_parse']:
                        if 'audio_analysis' not in tools['qct_parse']:
                            old_clipping = self._migrate_yes_no_to_bool(tools['qct_parse'].get('detect_audio_clipping', False))
                            old_imbalance = self._migrate_yes_no_to_bool(tools['qct_parse'].get('detect_channel_imbalance', False))
                            tools['qct_parse']['audio_analysis'] = old_clipping or old_imbalance
                        tools['qct_parse'].pop('detect_audio_clipping', None)
                        tools['qct_parse'].pop('detect_channel_imbalance', None)
        
        return config_data

    # Profile-bearing configs, mapped to the key holding their profiles dict.
    _PROFILE_CONFIG_KEYS = {
        'exiftool': 'exiftool_profiles',
        'mediainfo': 'mediainfo_profiles',
        'ffprobe': 'ffprobe_profiles',
        'filename': 'filename_profiles',
        'signalflow': 'signalflow_profiles',
    }

    def _restore_protected_profiles(self, config_data: dict, config_name: str) -> dict:
        """Re-seed built-in profiles from the bundled config.

        save_config writes every profile — built-ins included — into the
        user's last_used_*_config.json, so from the first save onward that
        copy shadows the bundle. Any drift in it (a hand-edit, or a stale
        copy left by an older release) would silently redefine a profile the
        app documents and enforces as protected, and selecting that profile
        would apply the altered values. Sourcing the built-ins from the
        bundle on every load keeps "protected" true end to end. Custom
        profiles are never touched.
        """
        profiles_key = self._PROFILE_CONFIG_KEYS.get(config_name)
        protected_names = PROTECTED_PROFILES.get(config_name, ())
        if not profiles_key or not protected_names:
            return config_data

        profiles = config_data.get(profiles_key)
        if not isinstance(profiles, dict):
            return config_data

        try:
            bundled_path = os.path.join(self._bundle_dir, 'config', f"{config_name}_config.json")
            with open(bundled_path, 'r') as f:
                bundled = json.load(f).get(profiles_key) or {}
        except Exception as e:
            logger.warning(f"Could not read bundled {config_name} profiles: {str(e)}")
            return config_data

        for name in protected_names:
            if name in bundled and profiles.get(name) != bundled[name]:
                logger.warning(
                    f"Built-in {config_name} profile '{name}' differed from the bundled "
                    "definition and was restored (built-in profiles are read-only)")
                profiles[name] = bundled[name]
        return config_data

    def _load_json_config(self, config_name: str, use_last_used: bool = False) -> dict:
        """Load JSON config from file with migration support."""
        config_path = None
        loaded_last_used = False

        if use_last_used:
            last_used_path = os.path.join(self._user_config_dir, f"last_used_{config_name}_config.json")
            if os.path.exists(last_used_path):
                config_path = last_used_path
                loaded_last_used = True

        if not config_path:
            # Use bundled config
            config_path = os.path.join(self._bundle_dir, 'config', f"{config_name}_config.json")

        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found at {config_path}")

        try:
            with open(config_path, 'r') as f:
                config_data = json.load(f)

            # Apply migration for backward compatibility
            config_data = self._migrate_config_data(config_data, config_name)

            # The bundled copy is authoritative for built-in profiles.
            if loaded_last_used:
                config_data = self._restore_protected_profiles(config_data, config_name)

            return config_data
        except json.JSONDecodeError as e:
            raise ValueError(f"Error parsing config file {config_path}: {str(e)}")

    # Dataclass conversion functions
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

    def _handle_list(self, item_type: Type, values: List) -> List:
        """Handle list type conversion."""
        if not isinstance(values, list):
            return values
        
        if hasattr(item_type, '__dataclass_fields__'):
            return [self._deserialize_dataclass(item_type, item) 
                   for item in values if isinstance(item, dict)]
        return values

    def _handle_dict(self, key_type: Type, value_type: Type, data: Dict) -> Dict:
        """Handle dictionary type conversion."""
        if not isinstance(data, dict):
            return data
        
        if hasattr(value_type, '__dataclass_fields__'):
            return {k: self._deserialize_dataclass(value_type, v) 
                   for k, v in data.items() if isinstance(v, dict)}
        return data

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

    def get_config(self, config_name: str, config_class: Type[T], 
                   use_last_used: bool = True) -> T:
        """
        Get configuration as a dataclass instance with automatic recovery from corrupted configs.
        
        Args:
            config_name: Name of the configuration
            config_class: Dataclass type to instantiate
            use_last_used: Whether to use the last used config if available
            
        Returns:
            An instance of the config_class
        """
        # Register the config class so update_config can reload after cache clears
        self._config_classes[config_name] = config_class

        # Return cached config if available
        if config_name in self._configs:
            return self._configs[config_name]

        try:
            # Load config data from file (with migration)
            config_data = self._load_json_config(config_name, use_last_used=use_last_used)

            # Convert to dataclass and cache
            config = self._deserialize_dataclass(config_class, config_data)
            self._configs[config_name] = config

            return config
            
        except Exception as e:
            # If we were trying to use last_used config and it failed,
            # clean up corrupted configs and try again with defaults
            if use_last_used:
                logger.warning(f"Failed to load last_used {config_name} config: {str(e)}")
                logger.info("Detected incompatible config format - cleaning up and using defaults")
                
                # Clean up corrupted configs
                self._cleanup_corrupted_configs()
                
                # Retry with default config (recursive call with use_last_used=False)
                try:
                    return self.get_config(config_name, config_class, use_last_used=False)
                except Exception as fallback_error:
                    logger.error(f"Failed to load default {config_name} config after cleanup: {str(fallback_error)}")
                    raise fallback_error
            else:
                # If default config also failed, this is a more serious problem
                logger.error(f"Failed to load default {config_name} config: {str(e)}")
                raise

    def save_config(self, config_name: str, is_last_used: bool = False) -> None:
        """
        Save current config to a file.
        
        Args:
            config_name: Name of the config to save
            is_last_used: If True, save as last_used config, otherwise as regular config
        """
        config = self._configs.get(config_name)
        if not config:
            # logger.error(f"No config found for {config_name}, cannot save")
            return
            
        filename = f"{'last_used_' if is_last_used else ''}{config_name}_config.json"
        save_path = os.path.join(self._user_config_dir, filename)
        
        try:
            # Convert to dictionary
            config_dict = asdict(config)
            
            # Note: We're now saving booleans directly. 
            # During migration period, you could use _migrate_bool_to_yes_no if needed
            # to maintain compatibility with older versions
            
            with open(save_path, 'w') as f:
                json.dump(config_dict, f, indent=2)
            logger.info(f"Saved config to {save_path}")
        except Exception as e:
            logger.error(f"Error saving config to {save_path}: {str(e)}")

    def update_config(self, config_name: str, updates: dict) -> None:
        """
        Update specific fields in a config.
        
        Args:
            config_name: Name of the config to update
            updates: Dictionary of updates to apply
        """
        config = self._configs.get(config_name)
        if config is None and config_name in self._config_classes:
            config = self.get_config(config_name, self._config_classes[config_name])
        if config is None:
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
        
    def _update_dict_recursively(self, target: dict, source: dict) -> None:
        """Update dictionary recursively."""
        for key, value in source.items():
            if key in target:
                if isinstance(value, dict) and isinstance(target[key], dict):
                    self._update_dict_recursively(target[key], value)
                else:
                    target[key] = value

    def replace_config_section(self, config_name: str, section_path: str, new_value: any) -> None:
        """
        Replace an entire section of a config with a new value.
        
        Args:
            config_name: Name of the config to update
            section_path: Dot-separated path to the section (e.g., 'filename_values.fn_sections')
            new_value: New value to replace the entire section with
        """
        config = self._configs.get(config_name)
        if config is None and config_name in self._config_classes:
            config = self.get_config(config_name, self._config_classes[config_name])
        if config is None:
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
    
    def refresh_configs(self):
        """Force reload of all configs from disk"""
        self._configs = {}
        logger.debug(f"Config cache cleared, configs will be reloaded from disk\n")