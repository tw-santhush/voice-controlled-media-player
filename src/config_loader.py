import copy
import json
import logging
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

DEFAULT_CONFIG = {
    "vlc": {
        "host": "localhost",
        "port": 8080,
        "password": "admin",
        "enabled": True,
    },
    "mpc": {
        "host": "localhost",
        "port": 13579,
        "enabled": True,
    },
    "voice": {
        "timeout_seconds": 5,
        "phrase_time_limit": 3,
    },
    "player": {
        "default_skip_seconds": 10,
        "volume_step": 5,
    },
    "keyboard_fallback": {
        "enabled": True,
        "shortcuts": {
            "play_pause": "space",
            "stop": "s",
            "skip_forward": "right",
            "skip_backward": "left",
            "volume_up": "up",
            "volume_down": "down",
            "mute": "m",
            "fullscreen": "f",
        },
    },
    "tts": {
        "enabled": True,
        "voice_id": None,
        "engine": "auto",
        "fallback_enabled": True,
    },
    "wake": {
        "enabled": True,
        "phrases": ["hey player", "hello player", "player"],
        "timeout_seconds": 3,
    },
    "commands": {
        "play": ["play", "resume", "continue"],
        "pause": ["pause", "hold", "stop playing"],
        "stop": ["stop", "quit", "end"],
        "skip_forward": ["skip forward", "forward 10 seconds", "jump forward"],
        "skip_backward": ["skip backward", "backward 10 seconds", "go back"],
        "volume_up": ["volume up", "louder", "increase volume"],
        "volume_down": ["volume down", "softer", "decrease volume"],
        "mute": ["mute", "silence"],
        "fullscreen": ["fullscreen", "full screen"],
        "exit": ["exit", "quit app", "close"],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _to_namespace(data):
    if isinstance(data, dict):
        return SimpleNamespace(**{key: _to_namespace(value) for key, value in data.items()})
    if isinstance(data, list):
        return [_to_namespace(item) for item in data]
    return data


def save_default_config(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(DEFAULT_CONFIG, fh, indent=2)
        fh.write("\n")


def load_config(config_path=None) -> SimpleNamespace:
    """Load configuration from a JSON file into an attribute-accessible object.

    If config_path is None, defaults to config.json in the project root. If the
    file does not exist, it is created with the default values.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        save_default_config(path)
        logger.info("Created default config at %s", path)
        return _to_namespace(copy.deepcopy(DEFAULT_CONFIG))

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            user_config = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("Failed to read config %s (%s). Using defaults.", path, exc)
        return _to_namespace(copy.deepcopy(DEFAULT_CONFIG))

    if not isinstance(user_config, dict):
        logger.warning("Config %s is invalid; using defaults.", path)
        return _to_namespace(copy.deepcopy(DEFAULT_CONFIG))

    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    return _to_namespace(merged)