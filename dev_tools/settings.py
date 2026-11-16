"""
settings.py: where the dev tools keep things on this machine, and who is using them.

Three values, each read from the environment first, then from dev_tools.json in the AV Spex
user config directory (written by `avspex-dev setup`):

    corpus     AVSPEX_CORPUS     the results corpus (a git repository; see dev_tools/README.md)
    staging    AVSPEX_STAGING    local directory for staged collection (collect --stage); optional
    reviewer   AVSPEX_REVIEWER   your name, recorded on the verdicts you import

Without either, the corpus falls back to ~/git/JPC_AV/avspex_corpus, the location the tools
used before there was a settings file.
"""
import getpass
import json
import os
from pathlib import Path

LEGACY_CORPUS = Path.home() / "git/JPC_AV/avspex_corpus"
KEYS = {"corpus": "AVSPEX_CORPUS", "staging": "AVSPEX_STAGING", "reviewer": "AVSPEX_REVIEWER"}


def settings_dir():
    """AVSPEX_DEV_SETTINGS_DIR overrides the location, for tests and trial setups."""
    override = os.environ.get("AVSPEX_DEV_SETTINGS_DIR")
    if override:
        return Path(override).expanduser()
    import appdirs
    return Path(appdirs.user_config_dir("AVSpex", "NMAAHC"))


def settings_file():
    return settings_dir() / "dev_tools.json"


def load():
    """The saved settings, {} when there are none or the file is unreadable."""
    try:
        data = json.loads(settings_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(**values):
    """Merge these values into the settings file. None removes a key."""
    data = load()
    for key, value in values.items():
        if key not in KEYS:
            raise KeyError(key)
        if value is None:
            data.pop(key, None)
        else:
            data[key] = str(value)
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def get(key):
    """(value, where it came from) with where in 'environment', 'settings file', None."""
    value = os.environ.get(KEYS[key])
    if value:
        return value, "environment"
    value = load().get(key)
    if value:
        return value, "settings file"
    return None, None


def corpus():
    value, _ = get("corpus")
    return Path(value).expanduser() if value else LEGACY_CORPUS


def corpus_source():
    return get("corpus")[1] or "default location"


def staging():
    value, _ = get("staging")
    return Path(value).expanduser() if value else None


def reviewer():
    value, _ = get("reviewer")
    return value or getpass.getuser()
