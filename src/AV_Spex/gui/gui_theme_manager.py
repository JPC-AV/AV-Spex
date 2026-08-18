from PyQt6.QtWidgets import QApplication, QGroupBox, QPushButton, QComboBox, QTextEdit
from PyQt6.QtGui import QPalette, QFont, QPixmap, QColor
from PyQt6.QtCore import QObject, pyqtSignal, Qt, QSize

import os
import sys
import platform

from AV_Spex.utils.log_setup import logger

class ThemeManager(QObject):
    """
    Singleton manager for handling theme changes in a PyQt6 application.
    Provides centralized styling for all UI components.
    """
    
    # Signal emitted when theme changes
    themeChanged = pyqtSignal(QPalette)
    
    # Singleton instance
    _instance = None
    
    @classmethod
    def instance(cls):
        """Get the ThemeManager singleton instance"""
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance
    
    def __new__(cls, *args, **kwargs):
        """Create singleton instance"""
        if cls._instance is None:
            cls._instance = super(ThemeManager, cls).__new__(cls)
            # Initialize the QObject part here
            QObject.__init__(cls._instance)
            # Set initialized flag
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the ThemeManager."""
        # Only initialize once for the singleton
        if not self._initialized:
            # Note: We don't call super().__init__() here because it's
            # already called in __new__
            self._initialized = True
            self.app = QApplication.instance()
            
            # Connect to application's palette change signal if app exists
            if self.app:
                self.app.paletteChanged.connect(self._on_palette_changed)
    
    def _on_palette_changed(self, palette):
        """Handle system palette changes and propagate to connected widgets."""
        self.themeChanged.emit(palette)
    
    # === GROUPBOX STYLING ===
    
    # === DISABLED-STATE COLORS ===

    @staticmethod
    def blend(color_a, color_b, ratio):
        """Blend two QColors. ratio 0.0 returns color_a, 1.0 returns color_b."""
        return QColor(
            round(color_a.red() * (1 - ratio) + color_b.red() * ratio),
            round(color_a.green() * (1 - ratio) + color_b.green() * ratio),
            round(color_a.blue() * (1 - ratio) + color_b.blue() * ratio),
        )

    def disabled_colors(self):
        """Return (background, text, border) hex strings for disabled widgets.

        These are computed rather than read from the palette on purpose. The
        platform palette cannot be relied on for a disabled look: on macOS
        here every Disabled ColorGroup entry matches its Active counterpart,
        and Window/PlaceholderText are identical to Button/ButtonText — so
        palette-derived "disabled" colors render exactly like enabled ones.
        Blending the normal colors toward each other always moves in the
        contrasting direction, so this works in light and dark themes alike.
        """
        palette = self.app.palette() if self.app else QPalette()
        button = palette.color(QPalette.ColorRole.Button)
        text = palette.color(QPalette.ColorRole.ButtonText)
        return (
            self.blend(button, text, 0.07).name(),   # slightly shifted background
            self.blend(text, button, 0.55).name(),   # clearly muted text
            self.blend(text, button, 0.70).name(),   # faint border
        )

    def style_groupbox(self, group_box, title_position=None):
        """
        Apply consistent styling to a group box based on current theme.
        
        Args:
            group_box: The QGroupBox to style
            title_position: Position of the title ("top left", "top center", etc.)
                            If None, maintains the group box's current title position
        """
        if not isinstance(group_box, QGroupBox) or not self.app:
            return
            
        # Get the current palette
        palette = self.app.palette()
        midlight_color = palette.color(palette.ColorRole.Midlight).name()
        text_color = palette.color(palette.ColorRole.Text).name()
        _, disabled_text_color, disabled_border_color = self.disabled_colors()
        
        # If title_position is None, use a simpler approach
        if title_position is None:
            # Store the group title position in the widget property if not already set
            title_pos = group_box.property("title_position")
            if title_pos:
                title_position = title_pos
            else:
                title_position = "top left"  # Default
        else:
            # Store position for future reference
            group_box.setProperty("title_position", title_position)
        
        # Apply style based on current palette with specified or preserved title position
        group_box.setStyleSheet(f"""
            QGroupBox {{
                font-weight: bold;
                font-size: 14px;
                color: {text_color};
                border: 2px solid gray;
                border-radius: 5px;
                margin-top: 10px;
                background-color: {midlight_color};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                subcontrol-position: {title_position};
                padding: 0 10px;
                color: {text_color};
            }}
            QGroupBox:disabled {{
                color: {disabled_text_color};
                border: 2px solid {disabled_border_color};
            }}
            QGroupBox::title:disabled {{
                color: {disabled_text_color};
            }}
        """)

    # === BUTTON STYLING ===
    
    def style_button(self, button, special_style=None):
        """
        Apply styling to a single button.
        
        Args:
            button: The QPushButton to style
            special_style: Optional special style identifier for custom buttons
        """
        if not isinstance(button, QPushButton) or not self.app:
            return
            
        # Get palette colors
        palette = self.app.palette()
        highlight_color = palette.color(palette.ColorRole.Highlight).name()
        highlight_text_color = palette.color(palette.ColorRole.HighlightedText).name()
        button_color = palette.color(palette.ColorRole.Button).name()
        button_text_color = palette.color(palette.ColorRole.ButtonText).name()
        disabled_button_color, disabled_text_color, disabled_border_color = self.disabled_colors()

        # Apply special styling if requested
        if special_style == "check_spex":
            button.setStyleSheet("""
                QPushButton {
                    font-weight: bold;
                    padding: 8px 16px;
                    font-size: 14px;
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
                QPushButton:disabled {
                    background-color: #A5D6A7; 
                    color: #E8F5E9;             
                    opacity: 0.8;               
                }
            """)
        elif special_style == "processing_window":
            button.setStyleSheet("""
                QPushButton {
                    font-weight: bold;
                    padding: 8px 16px;
                    font-size: 14px;
                    background-color: white;
                    color: #4CAF50;
                    border: none;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #d2ffed;
                }
                QPushButton:disabled {
                    background-color: #E8F5E9; 
                    color: #A5D6A7;             
                    opacity: 0.8;               
                }
            """)
        elif special_style == "cancel_processing":
            button.setStyleSheet("""
                QPushButton {
                    font-weight: bold;
                    padding: 8px 16px;
                    font-size: 14px;
                    background-color: #ff9999;
                    color: #4d2b12;
                    border: none;
                    border-radius: 4px;
                }
                QPushButton:hover {
                    background-color: #ff8080;
                }
                QPushButton:disabled {
                    background-color: #f5e9e3; 
                    color: #cd9e7f;             
                    opacity: 0.8;               
                }
            """)
        else:
            # Apply standard button styling. The explicit colors override the
            # native disabled look, so a :disabled rule is required for
            # disabled buttons to visibly gray out.
            button.setStyleSheet(f"""
                QPushButton {{
                    font-weight: bold;
                    padding: 8px;
                    border: 1px solid gray;
                    border-radius: 4px;
                    background-color: {button_color};
                    color: {button_text_color};
                }}
                QPushButton:hover {{
                    background-color: {highlight_color};
                    color: {highlight_text_color};
                }}
                QPushButton:disabled {{
                    background-color: {disabled_button_color};
                    color: {disabled_text_color};
                    border: 1px solid {disabled_border_color};
                }}
            """)
    
    def style_buttons(self, parent_widget):
        """Apply consistent styling to all buttons under a parent widget."""
        if not self.app:
            return
        
        # Apply to all buttons in the widget
        buttons = parent_widget.findChildren(QPushButton)
        for button in buttons:
            self.style_button(button)
    
    # === COMBOBOX STYLING ===
    
    def style_combobox(self, combo_box):
        """
        Apply consistent styling to a combo box based on current theme.
        
        Args:
            combo_box: The QComboBox to style
        """
        if not isinstance(combo_box, QComboBox) or not self.app:
            return
        
        # Get the current palette
        palette = self.app.palette()
        
        # Get colors from palette using ColorRoles
        highlight_color = palette.color(palette.ColorRole.Highlight).name()
        highlight_text_color = palette.color(palette.ColorRole.HighlightedText).name()
        dropdown_bg_color = palette.color(palette.ColorRole.AlternateBase).name()
        button_color = palette.color(palette.ColorRole.Button).name()
        button_text_color = palette.color(palette.ColorRole.ButtonText).name()
        
        # Border color from shadow or mid
        border_color = palette.color(palette.ColorRole.Mid).name()
        disabled_button_color, disabled_text_color, disabled_border_color = self.disabled_colors()

        # Apply style based on current palette. As with buttons, the explicit
        # colors override the native disabled look, so :disabled is required.
        combo_box.setStyleSheet(f"""
            QComboBox {{
                font-weight: bold;
                padding: 8px;
                border: 1px solid gray;
                border-radius: 4px;
                background-color: {button_color};
                color: {button_text_color};
            }}
            QComboBox:hover {{
                background-color: {highlight_color};
                color: {highlight_text_color};
            }}
            QComboBox:disabled {{
                background-color: {disabled_button_color};
                color: {disabled_text_color};
                border: 1px solid {disabled_border_color};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: right;
                width: 20px;
                border-left: 1px solid {border_color};
                border-top-right-radius: 3px;
                border-bottom-right-radius: 3px;
            }}
            QComboBox QAbstractItemView, QComboBox QListView {{
                background-color: {dropdown_bg_color};
            }}
            QComboBox::item:selected {{
                background-color: {highlight_color};
                color: #4CAF50;
            }}
        """)
        
    def style_comboboxes(self, parent_widget):
        """Apply consistent styling to all comboboxes under a parent widget."""
        if not self.app:
            return
        
        # Apply to all comboboxes in the widget
        comboboxes = parent_widget.findChildren(QComboBox)
        for combobox in comboboxes:
            self.style_combobox(combobox)
    
    # === TAB STYLING ===
    
    def style_tabs(self, tab_widget):
        """
        Apply styling to a tab widget.
        
        Args:
            tab_widget: The QTabWidget to style
        """
        if not self.app or not tab_widget:
            return
            
        tab_widget.setStyleSheet(self.get_tab_style())
    
    def get_tab_style(self):
        """
        Generate style for tab widgets based on current palette.
        
        Returns:
            str: CSS stylesheet for QTabWidget and QTabBar
        """
        if not self.app:
            return ""
            
        palette = self.app.palette()
        highlight_color = palette.color(palette.ColorRole.Highlight).name()
        highlight_text_color = palette.color(palette.ColorRole.HighlightedText).name()
        dark_color = palette.color(palette.ColorRole.Mid).name()
        
        return f"""
            QTabBar::tab {{
                padding: 8px 12px;
                margin-right: 2px;
                font-size: 14px;
                font-weight: bold;
                background-color: {dark_color};
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
            }}
            
            QTabBar::tab:selected, QTabBar::tab:hover {{
                background-color: {highlight_color};
                color: {highlight_text_color};
            }}
            QTabBar::tab:selected {{
                border-bottom: 2px solid #0066cc;
            }}
                                
            QTabWidget::pane {{
                border: 1px solid lightgray;
                background-color: none;
            }}
        """
    
    # === TEXT STYLING ===
    
    def style_console_text(self, text_edit):
        """
        Enhanced version with better error handling for packaged apps.
        """
        if not isinstance(text_edit, QTextEdit) or not self.app:
            return
            
        # Get the current palette
        palette = self.app.palette()
        
        # Use robust theme detection
        theme = self.detect_system_theme()
        is_dark = theme == 'Dark'
        
        # Get basic colors with fallbacks
        try:
            text_color = palette.color(QPalette.ColorRole.Text).name()
        except:
            text_color = "#ffffff" if is_dark else "#000000"
        
        # Define background based on theme with fallbacks
        if is_dark:
            try:
                base_color = palette.color(QPalette.ColorRole.Base)
                bg_color = f"rgba({min(base_color.red() + 15, 255)}, "\
                        f"{min(base_color.green() + 15, 255)}, "\
                        f"{min(base_color.blue() + 15, 255)}, 255)"
            except:
                bg_color = "#2a2a2a"  # Fallback dark
                
            try:
                border_color = palette.color(QPalette.ColorRole.Mid).name()
            except:
                border_color = "#555555"  # Fallback border
        else:
            try:
                base_color = palette.color(QPalette.ColorRole.Base)
                bg_color = f"rgba({max(base_color.red() - 15, 0)}, "\
                        f"{max(base_color.green() - 15, 0)}, "\
                        f"{max(base_color.blue() - 15, 0)}, 255)"
            except:
                bg_color = "#f0f0f0"  # Fallback light
                
            try:
                border_color = palette.color(QPalette.ColorRole.Mid).name()
            except:
                border_color = "#cccccc"  # Fallback border
        
        # Get highlight colors with fallbacks
        try:
            highlight_color = palette.color(palette.ColorRole.Highlight).name()
            highlight_text_color = palette.color(palette.ColorRole.HighlightedText).name()
            mid_color = palette.color(palette.ColorRole.Mid).name()
        except:
            if is_dark:
                highlight_color = "#3b82f6"
                highlight_text_color = "#ffffff"
                mid_color = "#666666"
            else:
                highlight_color = "#0066cc"
                highlight_text_color = "#ffffff"
                mid_color = "#999999"
            
        # Apply stylesheet with optimized scrollbar styling
        text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {bg_color};
                color: {text_color};
                border: 1px solid {border_color};
                border-radius: 5px;
                padding: 5px;
                selection-background-color: {highlight_color};
                selection-color: {highlight_text_color};
            }}
            
            /* Vertical Scrollbar */
            QScrollBar:vertical {{
                background: {bg_color};
                width: 10px;
                margin: 0px;
                border: none;
                padding: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(180, 180, 180, 0.7);
                min-height: 30px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(200, 200, 200, 0.9);
            }}
            QScrollBar::handle:vertical:pressed {{
                background: rgba(220, 220, 220, 1.0);
            }}
            
            /* Horizontal Scrollbar */
            QScrollBar:horizontal {{
                background: {bg_color};
                height: 10px;
                margin: 0px;
                border: none;
                padding: 2px;
            }}
            QScrollBar::handle:horizontal {{
                background: rgba(180, 180, 180, 0.7);
                min-width: 30px;
                border-radius: 3px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: rgba(200, 200, 200, 0.9);
            }}
            QScrollBar::handle:horizontal:pressed {{
                background: rgba(220, 220, 220, 1.0);
            }}
            
            /* Remove scrollbar arrows and make pages/corners invisible */
            QScrollBar::add-line, QScrollBar::sub-line {{
                height: 0px;
                width: 0px;
            }}
            QScrollBar::add-page, QScrollBar::sub-page, QScrollBar::corner {{
                background: none;
            }}
        """)
        
        # Clear format cache if the method exists
        if hasattr(text_edit, 'clear_formats'):
            text_edit.clear_formats()
    
    # === PROGRESS BAR STYLING ===
    
    def style_progress_bar(self, progress_bar):
        """
        Apply styling to a progress bar.
        
        Args:
            progress_bar: The QProgressBar to style
        """
        if not self.app or not progress_bar:
            return
            
        progress_bar.setStyleSheet("""
            QProgressBar {
                background-color: palette(Base);
                text-align: center;
                padding: 1px;
            }
            QProgressBar::chunk {
                background-color: palette(Highlight);
            }
        """)
    
    # === LOGO & BRANDING ===
    
    def get_theme_appropriate_logo(self, light_logo_path, dark_logo_path):
        """
        Returns the appropriate logo path based on the current theme.
        
        Args:
            light_logo_path: Path to the logo for light theme
            dark_logo_path: Path to the logo for dark theme
            
        Returns:
            str: Path to the appropriate logo for current theme
        """
        if not self.app:
            return light_logo_path  # Default to light theme if no app
            
        # Use robust theme detection
        theme = self.detect_system_theme()
        is_dark = theme == 'Dark'
        
        # Return appropriate logo path
        return dark_logo_path if is_dark else light_logo_path
    
    def load_logo(self, label, logo_path, width=None, height=None):
        """
        Load a logo image into a QLabel.
        
        Args:
            label: QLabel to place the logo into
            logo_path: Path to the logo image file
            width: Optional width to scale to (maintains aspect ratio if height is None)
            height: Optional height to scale to (maintains aspect ratio if width is None)
        
        Returns:
            bool: True if successful, False otherwise
        """
        if not os.path.exists(logo_path):
            return False
            
        pixmap = QPixmap(logo_path)
        if pixmap.isNull():
            return False
            
        # Scale the pixmap if requested
        if width is not None and height is not None:
            scaled_pixmap = pixmap.scaled(QSize(width, height), 
                                         Qt.AspectRatioMode.KeepAspectRatio, 
                                         Qt.TransformationMode.SmoothTransformation)
        elif width is not None:
            scaled_pixmap = pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
        elif height is not None:
            scaled_pixmap = pixmap.scaledToHeight(height, Qt.TransformationMode.SmoothTransformation)
        else:
            scaled_pixmap = pixmap
            
        label.setPixmap(scaled_pixmap)
        return True
    
    # === UTILITY METHODS ===
    
    def get_current_palette(self):
        """Return the current application palette"""
        return self.app.palette() if self.app else None
        
    def apply_theme_to_all(self, widget):
        """
        Apply theming to all appropriate widgets under a parent widget.
        This is a utility method that combines multiple styling methods.
        
        Args:
            widget: Parent widget containing elements to style
        """
        if not widget:
            return
            
        # Style groups
        for group in widget.findChildren(QGroupBox):
            self.style_groupbox(group)
        
        # Style buttons
        self.style_buttons(widget)
        
        # Style comboboxes
        self.style_comboboxes(widget)
        
        # Find and style text edits
        for text_edit in widget.findChildren(QTextEdit):
            self.style_console_text(text_edit)

    def detect_system_theme(self):
        """
        Robust theme detection that works in packaged apps.
        Combines the best of both fallback approaches.
        """
        # Method 1: PyQt6 palette detection (most reliable for packaged apps)
        try:
            if self.app:
                palette = self.app.palette()
                window_color = palette.color(palette.ColorRole.Window)
                
                # Get RGB values and calculate luminance
                r, g, b = window_color.red(), window_color.green(), window_color.blue()
                luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
                
                # Validate reasonable result
                if 0 <= luminance <= 1:
                    is_dark = luminance < 0.5
                    theme = 'Dark' if is_dark else 'Light'
                    # logger.debug(f"PyQt6 palette detection: {theme} (luminance: {luminance:.3f})")
                    return theme
                else:
                    logger.warning(f"PyQt6 returned invalid luminance: {luminance}")
                    
        except Exception as e:
            logger.warning(f"PyQt6 theme detection failed: {e}")
        
        # Method 2: macOS system detection with timeout
        if platform.system() == 'Darwin':
            try:
                # Try NSUserDefaults first (if PyObjC available)
                try:
                    from Foundation import NSUserDefaults
                    defaults = NSUserDefaults.standardUserDefaults()
                    appearance_name = defaults.stringForKey_('AppleInterfaceStyle')
                    
                    if appearance_name:
                        theme = 'Dark' if 'Dark' in appearance_name else 'Light'
                        logger.info(f"macOS NSUserDefaults: {theme}")
                        return theme
                    else:
                        logger.info("macOS NSUserDefaults: Light (no dark mode key)")
                        return 'Light'
                        
                except ImportError:
                    logger.debug("PyObjC not available, trying subprocess")
                    
                # Fallback to subprocess with timeout
                import subprocess
                result = subprocess.run(
                    ['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                    capture_output=True,
                    text=True,
                    timeout=2  # Short timeout for packaged apps
                )
                
                if result.returncode == 0 and result.stdout.strip():
                    theme = 'Dark'
                    logger.info(f"macOS subprocess: {theme}")
                    return theme
                else:
                    theme = 'Light'
                    logger.info(f"macOS subprocess: {theme} (no dark mode)")
                    return theme
                    
            except Exception as e:
                logger.warning(f"macOS detection failed: {e}")
        
        # Method 3: Environment variable override (useful for CI/testing)
        try:
            env_theme = os.environ.get('AVSPEX_THEME', None)
            if env_theme in ['Dark', 'Light']:
                logger.info(f"Environment override: {env_theme}")
                return env_theme
        except Exception as e:
            logger.debug(f"Environment check failed: {e}")
        
        # Method 4: Time-based fallback (keep this - it's creative and practical)
        try:
            from datetime import datetime
            current_hour = datetime.now().hour
            
            if 18 <= current_hour or current_hour < 6:
                theme = 'Dark'
                logger.info(f"Time-based fallback: {theme} (hour: {current_hour})")
            else:
                theme = 'Light'
                logger.info(f"Time-based fallback: {theme} (hour: {current_hour})")
                
            return theme
            
        except Exception as e:
            logger.debug(f"Time-based detection failed: {e}")
        
        # Ultimate fallback
        logger.warning("All theme detection methods failed, defaulting to Dark")
        return 'Dark'
    
    def get_theme_with_fallback(self):
        """
        Get current theme with fallback detection methods.
        Returns 'Dark' or 'Light'
        
        This is an alias for detect_system_theme() for backward compatibility.
        """
        return self.detect_system_theme()



class ThemeableMixin:
    """
    Mixin class for objects that need theme support.
    Provides automatic connection to theme change signals.
    """
    
    def setup_theme_handling(self):
        """Connect to theme change notifications"""
        theme_manager = ThemeManager.instance()
        
        # Connect to the theme changed signal
        if hasattr(theme_manager, 'themeChanged'):
            theme_manager.themeChanged.connect(self.on_theme_changed)
        
        # Apply current theme immediately using QApplication's palette
        self.on_theme_changed(QApplication.palette())
        
    def cleanup_theme_handling(self):
        """Disconnect from theme change notifications"""
        theme_manager = ThemeManager.instance()
        try:
            # Disconnect from whichever signal exists
            if hasattr(theme_manager, 'themeChanged'):
                theme_manager.themeChanged.disconnect(self.on_theme_changed)
        except TypeError:
            # Already disconnected or never connected
            pass
            
    def on_theme_changed(self, palette):
        """
        Override this method to handle theme changes.
        
        This default implementation will:
        1. Apply palette to self if it has setPalette method
        2. Propagate theme change to child components with their own handlers
        3. Apply standard theming to known components
        """
        # Apply the palette to this widget
        if hasattr(self, 'setPalette'):
            self.setPalette(palette)
        
        # Propagate theme change to child components that have their own handlers
        for attr_name in dir(self):
            # Skip special methods and avoid potential recursive calls
            if attr_name.startswith('__') or attr_name == 'on_theme_changed':
                continue
                
            try:
                attr = getattr(self, attr_name)
                # Check if this attribute has an on_theme_changed method
                if hasattr(attr, 'on_theme_changed') and callable(attr.on_theme_changed):
                    # Call the method with the palette argument
                    attr.on_theme_changed(palette)
            except (AttributeError, TypeError) as e:
                # Safely handle any errors in propagation
                pass
        
        # Apply any common styling for known component types
        # Call theme-specific styling implementations if defined
        theme_specific_methods = [
            '_style_groupboxes', 
            '_style_buttons',
            '_style_comboboxes',
            '_style_tabs', 
            '_style_text_edits',
            '_style_progress_bars',
            '_refresh_logo'
        ]
        
        for method_name in theme_specific_methods:
            method = getattr(self, method_name, None)
            if callable(method):
                method()