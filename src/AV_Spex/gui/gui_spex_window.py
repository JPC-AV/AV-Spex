"""Card-based content widget for the Spex tab.

Six ProfileCards in a two-column grid — Filename, Signal Flow, MediaInfo,
Exiftool, FFprobe, and qct-parse thresholds — each showing its active
profile, an honest state line (stored on SpexConfig.active_profiles, never
reverse-guessed), a one-line summary of the expected values, and
View / Edit / New / Delete actions driven by one set of generic handlers.
"""

from dataclasses import asdict

from PyQt6.QtWidgets import QWidget, QGridLayout, QMessageBox, QDialog

from AV_Spex.gui.gui_theme_manager import ThemeManager, ThemeableMixin
from AV_Spex.gui.gui_spex_cards import ProfileCard, DomainSpec
from AV_Spex.gui.gui_spex_view_dialog import SpexViewDialog
from AV_Spex.gui.gui_custom_exiftool import CustomExiftoolDialog
from AV_Spex.gui.gui_custom_mediainfo import CustomMediainfoDialog
from AV_Spex.gui.gui_custom_ffprobe import CustomFfprobeDialog
from AV_Spex.utils import config_edit, spex_summaries
from AV_Spex.utils.config_setup import (
    SpexConfig, ChecksConfig, is_mkv_extension, is_protected_profile
)
from AV_Spex.utils.config_manager import ConfigManager
from AV_Spex.utils.log_setup import logger

config_mgr = ConfigManager()

MISSING = spex_summaries.MISSING


# --- View dialog adapters ---------------------------------------------------
# Each takes the current SpexConfig and returns {section: {field: value str}}.
# All are total: missing keys render as an em dash, nothing raises.

def _fmt(value):
    if value is None or value == "" or value == []:
        return MISSING
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def _plain(obj):
    if obj is None:
        return {}
    if hasattr(obj, '__dataclass_fields__'):
        return asdict(obj)
    if isinstance(obj, dict):
        return obj
    return {k: v for k, v in getattr(obj, '__dict__', {}).items() if not k.startswith('_')}


def _flat_section(values):
    return {key: _fmt(value) for key, value in _plain(values).items()}


def filename_view(spex_config):
    values = _plain(spex_config.filename_values)
    sections = {}
    pattern_rows = {}
    for index, (key, section) in enumerate(sorted((values.get('fn_sections') or {}).items()), 1):
        section = _plain(section)
        section_type = section.get('section_type', MISSING)
        pattern_rows[f"Section {index} ({section_type})"] = _fmt(section.get('value'))
    pattern_rows["File Extension"] = _fmt(values.get('FileExtension'))
    sections["Filename Pattern"] = pattern_rows
    return sections


def mediainfo_view(spex_config):
    values = _plain(spex_config.mediainfo_values)
    return {
        "General": _flat_section(values.get('expected_general')),
        "Video": _flat_section(values.get('expected_video')),
        "Audio": _flat_section(values.get('expected_audio')),
    }


def exiftool_view(spex_config):
    return {"Expected Values": _flat_section(spex_config.exiftool_values)}


def ffprobe_view(spex_config):
    values = _plain(spex_config.ffmpeg_values)
    format_values = dict(_plain(values.get('format')))
    tags = _plain(format_values.pop('tags', None))
    sections = {
        "Video Stream": _flat_section(values.get('video_stream')),
        "Audio Stream": _flat_section(values.get('audio_stream')),
        "Format": _flat_section(format_values),
    }
    if tags:
        tag_rows = {}
        for key, value in tags.items():
            if isinstance(value, dict):
                # ENCODER_SETTINGS: summarize each stage on its own row
                for stage, entries in value.items():
                    tag_rows[f"{key}.{stage}"] = _fmt(entries)
            else:
                tag_rows[key] = _fmt(value)
        sections["Format Tags"] = tag_rows
    return sections


def signalflow_view(spex_config):
    encoder_settings = _plain(spex_config.mediatrace_values.ENCODER_SETTINGS
                              if spex_config.mediatrace_values is not None else None)
    sections = {}
    for stage in ("Source_VTR", "TBC_Framesync", "ADC", "Capture_Device", "Computer"):
        entries = encoder_settings.get(stage) or []
        if isinstance(entries, str):
            entries = [entries]
        sections[stage.replace('_', ' ')] = (
            {str(i): _fmt(entry) for i, entry in enumerate(entries, 1)}
            if entries else {"": MISSING}
        )
    return sections


def qct_parse_view(spex_config):
    values = _plain(spex_config.qct_parse_values)
    tag_rows = {key: ("— not set" if value is None else _fmt(value))
                for key, value in _plain(values.get('fullTagList')).items()}
    return {
        "Content Thresholds (fullTagList)": tag_rows,
        "SMPTE Color Bars": _flat_section(values.get('smpte_color_bars')),
    }


class SpexWindow(QWidget, ThemeableMixin):
    """Card grid for the Spex tab; owns all profile interactions."""

    def __init__(self, main_window=None):
        super().__init__()
        self.main_window = main_window
        self.cards = {}
        self._signalflow_enabled = True

        self.specs = self._build_specs()
        self.setup_theme_handling()
        self.setup_ui()
        self.refresh()
        self._init_signalflow_enabled_state()

    # --- spec table ---------------------------------------------------------

    def _build_specs(self):
        return [
            DomainSpec(
                key='filename',
                title="Filename",
                description="The pattern the video file's name must follow.",
                summary_fn=spex_summaries.filename_summary,
                spex_values_fn=lambda spex: spex.filename_values,
                view_fn=filename_view,
                apply_fn=config_edit.apply_filename_profile,
                save_fn=config_edit.save_filename_profile,
                delete_fn=config_edit.delete_filename_profile,
                dialog_factory=self._filename_dialog_factory,
            ),
            DomainSpec(
                key='signalflow',
                title="Signal Flow",
                description="The expected digitization equipment chain, embedded "
                            "as ENCODER_SETTINGS tags in the MKV.",
                summary_fn=spex_summaries.signalflow_summary,
                spex_values_fn=lambda spex: spex.mediatrace_values,
                view_fn=signalflow_view,
                apply_fn=config_edit.apply_signalflow_profile,
                save_fn=config_edit.save_signalflow_profile,
                delete_fn=config_edit.delete_signalflow_profile,
                dialog_factory=self._signalflow_dialog_factory,
            ),
            DomainSpec(
                key='mediainfo',
                title="MediaInfo",
                description="Expected container and stream metadata reported by MediaInfo.",
                summary_fn=spex_summaries.mediainfo_summary,
                spex_values_fn=lambda spex: spex.mediainfo_values,
                view_fn=mediainfo_view,
                apply_fn=config_edit.apply_mediainfo_profile,
                save_fn=config_edit.save_mediainfo_profile,
                delete_fn=config_edit.delete_mediainfo_profile,
                dialog_factory=lambda parent, edit_mode, profile_name:
                    CustomMediainfoDialog(parent, edit_mode=edit_mode, profile_name=profile_name),
            ),
            DomainSpec(
                key='exiftool',
                title="Exiftool",
                description="Expected embedded metadata reported by ExifTool.",
                summary_fn=spex_summaries.exiftool_summary,
                spex_values_fn=lambda spex: spex.exiftool_values,
                view_fn=exiftool_view,
                apply_fn=config_edit.apply_exiftool_profile,
                save_fn=config_edit.save_exiftool_profile,
                delete_fn=config_edit.delete_exiftool_profile,
                dialog_factory=lambda parent, edit_mode, profile_name:
                    CustomExiftoolDialog(parent, edit_mode=edit_mode, profile_name=profile_name),
            ),
            DomainSpec(
                key='ffprobe',
                title="FFprobe",
                description="Expected stream and format values reported by FFprobe.",
                summary_fn=spex_summaries.ffprobe_summary,
                spex_values_fn=lambda spex: spex.ffmpeg_values,
                view_fn=ffprobe_view,
                apply_fn=config_edit.apply_ffprobe_profile,
                save_fn=config_edit.save_ffprobe_profile,
                delete_fn=config_edit.delete_ffprobe_profile,
                dialog_factory=lambda parent, edit_mode, profile_name:
                    CustomFfprobeDialog(parent, edit_mode=edit_mode, profile_name=profile_name),
            ),
            DomainSpec(
                key='qct_parse',
                title="qct-parse Thresholds",
                description="Content thresholds and SMPTE color bars limits used "
                            "by qct-parse analyses.",
                summary_fn=spex_summaries.qct_parse_summary,
                spex_values_fn=lambda spex: spex.qct_parse_values,
                view_fn=qct_parse_view,
                has_profiles=False,
                readonly_note="Read-only — thresholds are set by qct-parse defaults.",
            ),
        ]

    # --- dialog factories for domains whose dialogs predate the shared
    # contract (replaced when edit mode lands in those dialogs) -------------

    def _filename_dialog_factory(self, parent, edit_mode, profile_name):
        from AV_Spex.gui.gui_custom_filename import CustomFilenameDialog
        return CustomFilenameDialog(parent, edit_mode=edit_mode, profile_name=profile_name)

    def _signalflow_dialog_factory(self, parent, edit_mode, profile_name):
        from AV_Spex.gui.gui_custom_signalflow import CustomSignalflowDialog
        return CustomSignalflowDialog(parent, edit_mode=edit_mode, profile_name=profile_name)

    # --- UI -----------------------------------------------------------------

    def setup_ui(self):
        grid = QGridLayout(self)
        grid.setSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        theme_manager = ThemeManager.instance()
        for index, spec in enumerate(self.specs):
            card = ProfileCard(spec)
            theme_manager.style_groupbox(card, "top center")
            card.restyle()

            card.viewRequested.connect(lambda s=spec: self.on_view(s))
            if spec.has_profiles:
                card.profileSelected.connect(lambda name, s=spec: self.on_profile_selected(s, name))
                card.editRequested.connect(lambda s=spec: self.on_edit_or_duplicate(s))
                card.newRequested.connect(lambda s=spec: self.on_new(s))
                card.deleteRequested.connect(lambda s=spec: self.on_delete(s))

            self.cards[spec.key] = card
            grid.addWidget(card, index // 2, index % 2)

        grid.setRowStretch(len(self.specs) // 2 + 1, 1)

    # --- refresh ------------------------------------------------------------

    def refresh(self):
        """Single entry point to re-sync every card with the configs on disk.

        Called after profile changes here, and by the Import tab after a
        config import or reset."""
        config_mgr.refresh_configs()
        for spec in self.specs:
            self.refresh_card(spec.key)

    def refresh_card(self, key):
        spec = next(s for s in self.specs if s.key == key)
        card = self.cards[key]
        spex_config = config_mgr.get_config('spex', SpexConfig)

        card.set_summary(spec.summary_fn(spec.spex_values_fn(spex_config)))

        if not spec.has_profiles:
            return

        names = sorted(config_edit.get_domain_profiles(key).keys())
        state = config_edit.get_active_profile_state(key)
        card.set_profiles(names, state.name if state.exists else None)

        if state.name is None:
            card.set_state_text("No profile recorded — showing current values")
        elif not state.exists:
            card.set_state_text(f'Modified from "{state.name}" (profile no longer exists)')
        elif state.modified:
            card.set_state_text(
                f'Modified from "{state.name}" — current values differ from the saved profile')
        else:
            card.set_state_text(f"Active profile: {state.name}")

        card.set_protected(
            is_protected_profile(key, card.selected_profile() or ""))

        if key == 'signalflow':
            self._apply_signalflow_enabled_state()

    # --- signal flow MKV gating --------------------------------------------

    def _init_signalflow_enabled_state(self):
        try:
            checks_config = config_mgr.get_config('checks', ChecksConfig)
            ext = getattr(checks_config, 'video_file_extension', 'mkv')
            self.set_signalflow_enabled(is_mkv_extension(ext))
        except Exception as e:
            logger.debug(f"Could not read video extension for signal flow gating: {e}")

    def set_signalflow_enabled(self, is_mkv: bool):
        """Gray the signal-flow card for non-MKV input (its tags are
        Matroska-only). Called by the Checks tab when the extension changes."""
        self._signalflow_enabled = bool(is_mkv)
        self._apply_signalflow_enabled_state()

    def _apply_signalflow_enabled_state(self):
        card = self.cards.get('signalflow')
        if card is None:
            return
        card.set_card_enabled(self._signalflow_enabled)
        if not self._signalflow_enabled:
            card.set_state_text("Signal flow tags require MKV input")

    # --- generic card handlers ---------------------------------------------

    def on_profile_selected(self, spec, name):
        profile = config_edit.get_domain_profiles(spec.key).get(name)
        if profile is None:
            return
        try:
            spec.apply_fn(profile, profile_name=name)
            config_mgr.save_config('spex', is_last_used=True)
        except Exception as e:
            logger.error(f"Error applying {spec.key} profile '{name}': {e}")
            QMessageBox.critical(self, "Error", f"Error applying profile: {e}")
        self.refresh_card(spec.key)

    def on_view(self, spec):
        spex_config = config_mgr.get_config('spex', SpexConfig)
        try:
            sections = spec.view_fn(spex_config)
        except Exception as e:
            logger.error(f"Error building {spec.key} view: {e}")
            sections = {"Error": {"Could not read values": str(e)}}
        dialog = SpexViewDialog(f"{spec.title} Values", sections, self)
        dialog.exec()

    def on_new(self, spec):
        spex_config = config_mgr.get_config('spex', SpexConfig)
        dialog = spec.dialog_factory(self.main_window or self, False, None)
        if hasattr(dialog, 'load_profile_data'):
            try:
                dialog.load_profile_data(spec.spex_values_fn(spex_config))
            except Exception as e:
                logger.debug(f"Could not preload current {spec.key} values: {e}")
        self._run_profile_dialog(spec, dialog)

    def on_edit_or_duplicate(self, spec):
        card = self.cards[spec.key]
        name = card.selected_profile()
        if name is None:
            QMessageBox.information(self, "No Profile Selected",
                                    "Select a profile to edit first.")
            return
        profile = config_edit.get_domain_profiles(spec.key).get(name)
        if profile is None:
            QMessageBox.warning(self, "Profile Not Found",
                                f'Profile "{name}" no longer exists.')
            self.refresh_card(spec.key)
            return

        protected = is_protected_profile(spec.key, name)
        if protected:
            # Built-ins can't be edited — open a create-mode dialog seeded
            # with the profile's values instead.
            dialog = spec.dialog_factory(self.main_window or self, False, None)
            if hasattr(dialog, 'profile_name_input'):
                dialog.profile_name_input.setText(f"{name} (copy)")
        else:
            dialog = spec.dialog_factory(self.main_window or self, True, name)
        if hasattr(dialog, 'load_profile_data'):
            try:
                dialog.load_profile_data(profile)
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Error loading profile: {e}")
                return
        self._run_profile_dialog(spec, dialog)

    def _run_profile_dialog(self, spec, dialog):
        """Shared accept path: save the profile, apply it, refresh the card."""
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile_info = dialog.get_profile() if hasattr(dialog, 'get_profile') else None
        if not profile_info:
            return
        name = profile_info['name']
        data = profile_info['data']

        if is_protected_profile(spec.key, name):
            QMessageBox.warning(
                self, "Protected Profile",
                f'"{name}" is a built-in profile and cannot be overwritten. '
                "Choose a different name.")
            return
        if not spec.save_fn(name, data):
            QMessageBox.warning(self, "Error", f'Could not save profile "{name}".')
            return
        try:
            spec.apply_fn(data, profile_name=name)
            config_mgr.save_config('spex', is_last_used=True)
        except Exception as e:
            logger.error(f"Error applying {spec.key} profile '{name}': {e}")
            QMessageBox.critical(self, "Error", f"Error applying profile: {e}")
        self.refresh_card(spec.key)

    def on_delete(self, spec):
        card = self.cards[spec.key]
        name = card.selected_profile()
        if name is None or is_protected_profile(spec.key, name):
            return
        reply = QMessageBox.question(
            self, "Delete Profile",
            f'Delete the profile "{name}"? This cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        if not spec.delete_fn(name):
            QMessageBox.warning(self, "Error", f'Could not delete profile "{name}".')
            return
        if config_edit.get_active_profile(spec.key) == name:
            config_edit.set_active_profile(spec.key, None)
            config_mgr.save_config('spex', is_last_used=True)
        self.refresh_card(spec.key)

    # --- theming ------------------------------------------------------------

    def on_theme_changed(self, palette):
        self.setPalette(palette)
        theme_manager = ThemeManager.instance()
        for card in self.cards.values():
            theme_manager.style_groupbox(card)
            card.restyle()
