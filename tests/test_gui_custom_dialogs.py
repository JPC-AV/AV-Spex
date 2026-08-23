"""Characterization tests for the three custom-profile dialogs.

These exist to make the dialog-unification refactor verifiable. The three
dialogs (ExifTool / MediaInfo / FFprobe) are ~72% duplicated and have no test
coverage, so these lock in the behavior that must survive being collapsed into
one parameterized dialog:

- construction in new vs. edit mode
- which fields each dialog offers, and which get a dropdown
- how form state becomes a profile dataclass (single value, multi-value, empty)
- required-field validation
- load -> read-back round trips
- the per-domain normalizations (MediaInfo audio Format list, FFprobe audio
  list fields and the format tags skeleton)
- what on_save_clicked stores for the caller

Structural access (reaching into the widget dicts) is confined to the helpers
at the top. The refactor should only need those updated; the tests below are
written against behavior and should pass unchanged.

Not covered here, deliberately: styling and palette. The offscreen platform
reports a different palette than cocoa, so color assertions would be
misleading (see CLAUDE.md on disabled-state styling).
"""

import dataclasses
import typing

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QComboBox, QLineEdit

from AV_Spex.gui.gui_custom_exiftool import CustomExiftoolDialog
from AV_Spex.gui.gui_custom_mediainfo import CustomMediainfoDialog
from AV_Spex.gui.gui_custom_ffprobe import CustomFfprobeDialog
from AV_Spex.utils.config_setup import (
    ExiftoolProfile,
    MediainfoProfile, MediainfoGeneralValues, MediainfoVideoValues, MediainfoAudioValues,
    FfprobeProfile, FFmpegVideoStream, FFmpegAudioStream, FFmpegFormat,
)


# ---------------------------------------------------------------------------
# Helpers — the only place that knows the dialogs' internal widget layout
# ---------------------------------------------------------------------------

def _inputs_for(dialog, section=None):
    """Return the {field_name: [widget, ...]} map for a dialog section."""
    if isinstance(dialog, CustomExiftoolDialog):
        assert section is None, "the ExifTool profile is flat, not sectioned"
        return dialog.field_inputs
    return getattr(dialog, f"{section}_inputs")


def _containers_for(dialog, section=None):
    if isinstance(dialog, CustomExiftoolDialog):
        return dialog.field_containers
    return getattr(dialog, f"{section}_containers")


def _widget_text(widget):
    return widget.currentText() if isinstance(widget, QComboBox) else widget.text()


def _set_widget_text(widget, value):
    if isinstance(widget, QComboBox):
        widget.setCurrentText(str(value))
    else:
        widget.setText(str(value))


def set_field(dialog, field_name, *values, section=None):
    """Set a field to one or more values, growing/shrinking rows as needed."""
    inputs = _inputs_for(dialog, section)
    containers = _containers_for(dialog, section)
    widgets = inputs[field_name]

    while len(widgets) > len(values):
        widgets.pop().deleteLater()
    while len(widgets) < len(values):
        if isinstance(dialog, CustomExiftoolDialog):
            dialog.add_textbox_row(field_name, "", containers[field_name])
        else:
            dialog.add_textbox_row(field_name, inputs, containers, "")

    for widget, value in zip(inputs[field_name], values):
        _set_widget_text(widget, value)


def get_field(dialog, field_name, section=None):
    """Read a field back as a list of non-empty strings."""
    return [t for t in (_widget_text(w) for w in _inputs_for(dialog, section)[field_name]) if t]


def field_names(dialog, section=None):
    return set(_inputs_for(dialog, section).keys())


def profile_of(cls, **overrides):
    """Build one of the value dataclasses, whose fields are all required.

    Fills every field the caller did not name: empty list for list-typed
    fields, empty dict for dict-typed, empty string otherwise.
    """
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in overrides:
            kwargs[f.name] = overrides[f.name]
            continue
        origin = typing.get_origin(hints.get(f.name))
        kwargs[f.name] = [] if origin is list else ({} if origin is dict else "")
    return cls(**kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def exiftool_dialog(qapp, silent_dialogs):
    dialog = CustomExiftoolDialog()
    yield dialog
    dialog.close()


@pytest.fixture
def mediainfo_dialog(qapp, silent_dialogs):
    dialog = CustomMediainfoDialog()
    yield dialog
    dialog.close()


@pytest.fixture
def ffprobe_dialog(qapp, silent_dialogs):
    dialog = CustomFfprobeDialog()
    yield dialog
    dialog.close()


def _valid_exiftool(dialog):
    dialog.profile_name_input.setText("Test Profile")
    set_field(dialog, "FileType", "MKV")
    set_field(dialog, "FileTypeExtension", "mkv")
    set_field(dialog, "MIMEType", "video/x-matroska")


def _valid_mediainfo(dialog):
    dialog.profile_name_input.setText("Test Profile")
    set_field(dialog, "FileExtension", "mkv", section="general")
    set_field(dialog, "Format", "Matroska", section="general")
    set_field(dialog, "Format", "FFV1", section="video")


def _valid_ffprobe(dialog):
    dialog.profile_name_input.setText("Test Profile")
    set_field(dialog, "codec_name", "ffv1", section="video")
    set_field(dialog, "format_name", "matroska webm", section="format")


# ---------------------------------------------------------------------------
# Construction: new vs. edit mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [CustomExiftoolDialog, CustomMediainfoDialog, CustomFfprobeDialog])
def test_new_mode_leaves_name_empty_and_editable(qapp, silent_dialogs, cls):
    dialog = cls()
    try:
        assert dialog.profile_name_input.text() == ""
        assert dialog.profile_name_input.isEnabled()
        assert dialog.edit_mode is False
        assert dialog.get_profile() is None
    finally:
        dialog.close()


@pytest.mark.parametrize("cls", [CustomExiftoolDialog, CustomMediainfoDialog, CustomFfprobeDialog])
def test_edit_mode_prefills_and_locks_the_name(qapp, silent_dialogs, cls):
    """Renaming in place would orphan the original profile, so the field locks."""
    dialog = cls(edit_mode=True, profile_name="Existing Profile")
    try:
        assert dialog.profile_name_input.text() == "Existing Profile"
        assert not dialog.profile_name_input.isEnabled()
        assert dialog.edit_mode is True
        assert dialog.original_profile_name == "Existing Profile"
        assert "Existing Profile" in dialog.windowTitle()
    finally:
        dialog.close()


# ---------------------------------------------------------------------------
# Field inventory — each dialog offers exactly its dataclass's fields
# ---------------------------------------------------------------------------

def test_exiftool_offers_every_profile_field(exiftool_dialog):
    assert field_names(exiftool_dialog) == set(ExiftoolProfile.__dataclass_fields__)


def test_mediainfo_sections_are_subsets_of_their_dataclasses(mediainfo_dialog):
    for section, cls in (("general", MediainfoGeneralValues),
                         ("video", MediainfoVideoValues),
                         ("audio", MediainfoAudioValues)):
        offered = field_names(mediainfo_dialog, section)
        assert offered <= set(cls.__dataclass_fields__), f"{section} offers unknown fields"
        assert offered, f"{section} offers no fields"


def test_ffprobe_sections_are_subsets_of_their_dataclasses(ffprobe_dialog):
    for section, cls in (("video", FFmpegVideoStream),
                         ("audio", FFmpegAudioStream),
                         ("format", FFmpegFormat)):
        offered = field_names(ffprobe_dialog, section)
        assert offered <= set(cls.__dataclass_fields__), f"{section} offers unknown fields"
        assert offered, f"{section} offers no fields"


@pytest.mark.parametrize("fixture_name,section", [
    ("mediainfo_dialog", "video"),
    ("ffprobe_dialog", "video"),
])
def test_dropdown_fields_get_an_editable_combobox(request, fixture_name, section):
    """Known-value fields offer a dropdown but must still accept free text."""
    dialog = request.getfixturevalue(fixture_name)
    inputs = _inputs_for(dialog, section)
    dropdown_fields = [f for f in inputs if f in dialog.DROPDOWN_OPTIONS]
    assert dropdown_fields, "expected at least one dropdown field in this section"
    for field_name in dropdown_fields:
        widget = inputs[field_name][0]
        assert isinstance(widget, QComboBox)
        assert widget.isEditable(), f"{field_name} dropdown must accept free text"


def test_non_dropdown_fields_get_a_line_edit(mediainfo_dialog):
    inputs = _inputs_for(mediainfo_dialog, "video")
    plain = [f for f in inputs if f not in mediainfo_dialog.DROPDOWN_OPTIONS]
    assert plain
    assert all(isinstance(inputs[f][0], QLineEdit) for f in plain)


# ---------------------------------------------------------------------------
# Collecting form state into a profile
# ---------------------------------------------------------------------------

def test_exiftool_single_value_becomes_a_string(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    profile = exiftool_dialog.get_exiftool_profile()
    assert profile.FileType == "MKV"


def test_exiftool_multiple_values_become_a_list(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, "CodecID", "A_FLAC", "A_PCM/INT/LIT")
    profile = exiftool_dialog.get_exiftool_profile()
    assert profile.CodecID == ["A_FLAC", "A_PCM/INT/LIT"]


def test_exiftool_blank_field_becomes_empty_string(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, "CodecID", "")
    profile = exiftool_dialog.get_exiftool_profile()
    assert profile.CodecID == ""


def test_whitespace_only_value_counts_as_empty(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, "CodecID", "   ")
    assert exiftool_dialog.get_exiftool_profile().CodecID == ""


def test_values_are_stripped(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, "CodecID", "  A_FLAC  ")
    assert exiftool_dialog.get_exiftool_profile().CodecID == "A_FLAC"


def test_blank_rows_are_dropped_from_multi_value_fields(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, "CodecID", "A_FLAC", "", "A_PCM/INT/LIT")
    assert exiftool_dialog.get_exiftool_profile().CodecID == ["A_FLAC", "A_PCM/INT/LIT"]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name,getter", [
    ("exiftool_dialog", "get_exiftool_profile"),
    ("mediainfo_dialog", "get_mediainfo_profile"),
    ("ffprobe_dialog", "get_ffprobe_profile"),
])
def test_missing_profile_name_is_rejected(request, silent_dialogs, fixture_name, getter):
    dialog = request.getfixturevalue(fixture_name)
    dialog.profile_name_input.setText("")
    assert getattr(dialog, getter)() is None
    assert any(kind == "warning" for kind, _, _ in silent_dialogs)


@pytest.mark.parametrize("field_name", ["FileType", "FileTypeExtension", "MIMEType"])
def test_exiftool_required_fields_are_enforced(exiftool_dialog, silent_dialogs, field_name):
    _valid_exiftool(exiftool_dialog)
    set_field(exiftool_dialog, field_name, "")
    assert exiftool_dialog.get_exiftool_profile() is None
    assert any(field_name in text for _, _, text in silent_dialogs)


@pytest.mark.parametrize("section,field_name", [
    ("general", "FileExtension"),
    ("general", "Format"),
    ("video", "Format"),
])
def test_mediainfo_required_fields_are_enforced(mediainfo_dialog, silent_dialogs, section, field_name):
    _valid_mediainfo(mediainfo_dialog)
    set_field(mediainfo_dialog, field_name, "", section=section)
    assert mediainfo_dialog.get_mediainfo_profile() is None


@pytest.mark.parametrize("section,field_name", [
    ("video", "codec_name"),
    ("format", "format_name"),
])
def test_ffprobe_required_fields_are_enforced(ffprobe_dialog, silent_dialogs, section, field_name):
    _valid_ffprobe(ffprobe_dialog)
    set_field(ffprobe_dialog, field_name, "", section=section)
    assert ffprobe_dialog.get_ffprobe_profile() is None


# ---------------------------------------------------------------------------
# Per-domain normalizations
# ---------------------------------------------------------------------------

def test_mediainfo_audio_format_is_always_a_list(mediainfo_dialog):
    """MediainfoAudioValues.Format is typed as a list even for one codec."""
    _valid_mediainfo(mediainfo_dialog)
    set_field(mediainfo_dialog, "Format", "FLAC", section="audio")
    assert mediainfo_dialog.get_mediainfo_profile().audio.Format == ["FLAC"]


def test_mediainfo_empty_audio_format_is_an_empty_list(mediainfo_dialog):
    _valid_mediainfo(mediainfo_dialog)
    set_field(mediainfo_dialog, "Format", "", section="audio")
    assert mediainfo_dialog.get_mediainfo_profile().audio.Format == []


@pytest.mark.parametrize("field_name", ["codec_name", "codec_long_name"])
def test_ffprobe_audio_list_fields_are_always_lists(ffprobe_dialog, field_name):
    _valid_ffprobe(ffprobe_dialog)
    set_field(ffprobe_dialog, field_name, "flac", section="audio")
    profile = ffprobe_dialog.get_ffprobe_profile()
    assert getattr(profile.audio_stream, field_name) == ["flac"]


def test_ffprobe_format_tags_skeleton_is_always_present(ffprobe_dialog):
    """Downstream comparison expects these keys to exist even when unset."""
    _valid_ffprobe(ffprobe_dialog)
    tags = ffprobe_dialog.get_ffprobe_profile().format.tags
    assert set(tags) == {
        'creation_time', 'ENCODER', 'TITLE', 'ENCODER_SETTINGS',
        'DESCRIPTION', 'ORIGINAL MEDIA TYPE', 'ENCODED_BY',
    }


def test_ffprobe_fills_every_dataclass_field(ffprobe_dialog):
    """Fields the form does not offer still have to be constructible."""
    _valid_ffprobe(ffprobe_dialog)
    profile = ffprobe_dialog.get_ffprobe_profile()
    assert profile is not None
    for cls, obj in ((FFmpegVideoStream, profile.video_stream),
                     (FFmpegAudioStream, profile.audio_stream),
                     (FFmpegFormat, profile.format)):
        for name in cls.__dataclass_fields__:
            assert hasattr(obj, name)


# ---------------------------------------------------------------------------
# Load -> read-back round trips
# ---------------------------------------------------------------------------

def test_exiftool_round_trip(exiftool_dialog):
    original = profile_of(
        ExiftoolProfile,
        FileType="MKV", FileTypeExtension="mkv", MIMEType="video/x-matroska",
        VideoFrameRate="29.97", ImageWidth="720", ImageHeight="486",
        CodecID=["A_FLAC", "A_PCM/INT/LIT"], AudioChannels="2",
    )
    exiftool_dialog.load_profile_data(original)
    exiftool_dialog.profile_name_input.setText("Round Trip")
    assert exiftool_dialog.get_exiftool_profile() == original


def test_mediainfo_round_trip(mediainfo_dialog):
    mediainfo_dialog.profile_name_input.setText("Round Trip")
    original = MediainfoProfile(
        general=profile_of(MediainfoGeneralValues,
                           FileExtension="mkv", Format="Matroska",
                           OverallBitRate_Mode="VBR"),
        video=profile_of(MediainfoVideoValues, Format="FFV1", Width="720", Height="486"),
        audio=profile_of(MediainfoAudioValues, Format=["FLAC", "PCM"], Channels="2"),
    )
    mediainfo_dialog.load_profile_data(original)
    result = mediainfo_dialog.get_mediainfo_profile()
    assert result.general.FileExtension == "mkv"
    assert result.video.Format == "FFV1"
    assert result.video.Width == "720"
    assert result.audio.Format == ["FLAC", "PCM"]


def test_ffprobe_round_trip_from_dataclass(ffprobe_dialog):
    ffprobe_dialog.profile_name_input.setText("Round Trip")
    original = FfprobeProfile(
        video_stream=profile_of(FFmpegVideoStream, codec_name="ffv1",
                                width="720", height="486"),
        audio_stream=profile_of(FFmpegAudioStream, codec_name=["flac"]),
        format=profile_of(FFmpegFormat, format_name="matroska webm"),
    )
    ffprobe_dialog.load_profile_data(original)
    result = ffprobe_dialog.get_ffprobe_profile()
    assert result.video_stream.codec_name == "ffv1"
    assert result.video_stream.width == "720"
    assert result.audio_stream.codec_name == ["flac"]
    assert result.format.format_name == "matroska webm"


def test_ffprobe_loads_from_plain_dicts(ffprobe_dialog):
    """spex_config.ffmpeg_values stores plain dicts, not dataclasses."""
    ffprobe_dialog.profile_name_input.setText("From Dict")
    ffprobe_dialog.load_profile_data({
        "video_stream": {"codec_name": "ffv1", "width": "720"},
        "audio_stream": {"codec_name": ["flac"]},
        "format": {"format_name": "matroska webm"},
    })
    result = ffprobe_dialog.get_ffprobe_profile()
    assert result.video_stream.codec_name == "ffv1"
    assert result.format.format_name == "matroska webm"


def test_loading_replaces_rather_than_appends(exiftool_dialog):
    """A second load must not leave the first load's rows behind."""
    many = profile_of(
        ExiftoolProfile,
        FileType="MKV", FileTypeExtension="mkv", MIMEType="video/x-matroska",
        CodecID=["A_FLAC", "A_PCM/INT/LIT", "A_AAC"],
    )
    exiftool_dialog.load_profile_data(many)
    assert len(get_field(exiftool_dialog, "CodecID")) == 3

    few = profile_of(
        ExiftoolProfile,
        FileType="MKV", FileTypeExtension="mkv", MIMEType="video/x-matroska",
        CodecID="A_FLAC",
    )
    exiftool_dialog.load_profile_data(few)
    assert get_field(exiftool_dialog, "CodecID") == ["A_FLAC"]


def test_load_existing_profile_sets_name_and_values(exiftool_dialog):
    profile = profile_of(
        ExiftoolProfile,
        FileType="MOV", FileTypeExtension="mov", MIMEType="video/quicktime",
    )
    exiftool_dialog.load_existing_profile("Named", profile)
    assert exiftool_dialog.profile_name_input.text() == "Named"
    assert get_field(exiftool_dialog, "FileType") == ["MOV"]


# ---------------------------------------------------------------------------
# Multi-value row management
# ---------------------------------------------------------------------------

def test_adding_a_row_grows_the_field(exiftool_dialog):
    before = len(_inputs_for(exiftool_dialog)["CodecID"])
    exiftool_dialog.add_textbox_row("CodecID", "extra",
                                    _containers_for(exiftool_dialog)["CodecID"])
    assert len(_inputs_for(exiftool_dialog)["CodecID"]) == before + 1


def test_removing_a_row_keeps_at_least_one(exiftool_dialog):
    """A field must always keep one editable row, or it becomes unfillable."""
    for _ in range(5):
        exiftool_dialog.remove_textbox_row("CodecID")
    assert len(_inputs_for(exiftool_dialog)["CodecID"]) == 1


# ---------------------------------------------------------------------------
# Save handling — what the caller receives
# ---------------------------------------------------------------------------

def test_save_stores_name_data_and_edit_flag(exiftool_dialog):
    _valid_exiftool(exiftool_dialog)
    exiftool_dialog.profile_name_input.setText("Saved Profile")
    exiftool_dialog.on_save_clicked()

    stored = exiftool_dialog.get_profile()
    assert stored["name"] == "Saved Profile"
    assert stored["is_edit"] is False
    assert isinstance(stored["data"], ExiftoolProfile)
    assert stored["data"].FileType == "MKV"


def test_save_in_edit_mode_uses_the_original_name(qapp, silent_dialogs):
    """The name input is disabled in edit mode, so the original name is used."""
    dialog = CustomExiftoolDialog(edit_mode=True, profile_name="Original Name")
    try:
        set_field(dialog, "FileType", "MKV")
        set_field(dialog, "FileTypeExtension", "mkv")
        set_field(dialog, "MIMEType", "video/x-matroska")
        dialog.on_save_clicked()

        stored = dialog.get_profile()
        assert stored["name"] == "Original Name"
        assert stored["is_edit"] is True
    finally:
        dialog.close()


def test_save_with_invalid_input_stores_nothing(exiftool_dialog, silent_dialogs):
    exiftool_dialog.profile_name_input.setText("Incomplete")
    set_field(exiftool_dialog, "FileType", "")
    exiftool_dialog.on_save_clicked()
    assert exiftool_dialog.get_profile() is None


@pytest.mark.parametrize("fixture_name,setup", [
    ("mediainfo_dialog", _valid_mediainfo),
    ("ffprobe_dialog", _valid_ffprobe),
])
def test_save_stores_a_profile_for_every_dialog(request, fixture_name, setup):
    dialog = request.getfixturevalue(fixture_name)
    setup(dialog)
    dialog.profile_name_input.setText("Saved")
    dialog.on_save_clicked()

    stored = dialog.get_profile()
    assert stored is not None and stored["name"] == "Saved"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------

def test_preview_reflects_the_profile_name(exiftool_dialog):
    exiftool_dialog.profile_name_input.setText("My Profile")
    exiftool_dialog.update_preview()
    assert "My Profile" in exiftool_dialog.preview_text.text()


def test_preview_has_a_placeholder_before_a_name_is_typed(exiftool_dialog):
    exiftool_dialog.profile_name_input.setText("")
    exiftool_dialog.update_preview()
    assert exiftool_dialog.preview_text.text().strip() != ""


# ---------------------------------------------------------------------------
# Import from file
# ---------------------------------------------------------------------------

@pytest.fixture
def exiftool_json(tmp_path):
    path = tmp_path / "JPC_AV_00001_exiftool_output.json"
    path.write_text(
        '[{"FileType": "MKV", "FileTypeExtension": "mkv", '
        '"MIMEType": "video/x-matroska", "ImageWidth": 720, "ImageHeight": 486}]'
    )
    return path


def test_import_populates_fields_and_suggests_a_name(exiftool_dialog, exiftool_json, monkeypatch):
    monkeypatch.setattr(
        "AV_Spex.gui.gui_custom_exiftool.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: (str(exiftool_json), "")),
    )
    exiftool_dialog.import_from_file()

    assert get_field(exiftool_dialog, "FileType") == ["MKV"]
    assert get_field(exiftool_dialog, "ImageWidth") == ["720"]
    assert exiftool_json.stem in exiftool_dialog.profile_name_input.text()


def test_cancelling_the_file_picker_changes_nothing(exiftool_dialog, monkeypatch):
    monkeypatch.setattr(
        "AV_Spex.gui.gui_custom_exiftool.QFileDialog.getOpenFileName",
        staticmethod(lambda *a, **k: ("", "")),
    )
    exiftool_dialog.profile_name_input.setText("Untouched")
    exiftool_dialog.import_from_file()
    assert exiftool_dialog.profile_name_input.text() == "Untouched"
