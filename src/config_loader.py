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
        "energy_threshold": 20,
        "dynamic_energy_threshold": False,
        "noise_gate_enabled": True,
        "noise_gate_threshold": 10.0,
        "confidence_threshold": 0.5,
    },
    "recognizer": {
        "engine": "auto",
        "vosk_model_path": None,
    },
    "player": {
        "default_skip_seconds": 10,
        "volume_step": 5,
    },
    "gesture": {
        "camera_id": 0,
        "debounce_frames": 3,
        "cooldown_seconds": 0.5,
        "swipe_window": 0.4,
        "swipe_velocity_threshold": 0.5,
        "swipe_min_distance": 0.25,
        "swipe_consistency_frames": 3,
        "pinch_threshold_ratio": 0.12,
        "finger_angle_threshold": 25,
        "volume_interval_seconds": 0.5,
        "volume_step": 5,
        "show_feedback": True,
        "model_path": None,
        "debug": False,
    },
    "tray": {
        "enabled": False,
        "auto_start": False,
    },
    "push_to_talk": {
        "enabled": False,
        "key": "ctrl",
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
        "cooldown_seconds": 1.5,
    },
    "wake": {
        "enabled": True,
        "engine": "porcupine",
        "phrases": ["hey player", "hello player", "player", "hey"],
        "porcupine_keywords": ["porcupine", "hey google"],
        "porcupine_keyword_paths": [],
        "porcupine_access_key": None,
        "timeout_seconds": 5,
    },
    "commands": {
        "play": ["play", "continue", "start", "play movie", "play video"],
        "pause": ["pause", "hold", "stop playing", "freeze", "pause movie", "pause video"],
        "stop": ["stop", "quit", "end", "stop movie", "exit playback", "stop video"],
        "skip_forward": [
            "skip forward",
            "forward 10 seconds",
            "jump forward",
            "next 10 seconds",
            "advance",
            "go forward",
            "skip ahead",
            "fast forward",
        ],
        "skip_backward": [
            "skip backward",
            "backward 10 seconds",
            "go back",
            "rewind 10 seconds",
            "go backward",
            "skip behind",
            "rewind",
        ],
        "volume_up": [
            "volume up",
            "louder",
            "increase volume",
            "turn up",
            "raise volume",
            "more volume",
        ],
        "volume_down": [
            "volume down",
            "softer",
            "decrease volume",
            "turn down",
            "lower volume",
            "less volume",
        ],
        "mute": ["mute", "silence", "no sound", "turn off sound", "mute audio"],
        "fullscreen": [
            "fullscreen",
            "full screen",
            "maximize",
            "go fullscreen",
            "enter fullscreen",
            "expand",
        ],
        "exit": ["exit", "quit app", "close", "shutdown", "terminate", "exit application"],
        "next": [
            "next",
            "next chapter",
            "next track",
            "skip to next",
            "next video",
            "next episode",
            "forward chapter",
        ],
        "previous": [
            "previous",
            "previous chapter",
            "previous track",
            "go back a chapter",
            "previous video",
            "back chapter",
            "go to previous",
        ],
        "listen_on": [
            "listen",
            "start listening",
            "resume",
            "turn on",
            "enable",
        ],
        "listen_off": [
            "stop listening",
            "pause listening",
            "turn off",
            "disable",
            "go silent",
        ],
        "volume_set": [],
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