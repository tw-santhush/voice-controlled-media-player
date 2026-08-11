import logging
import re

from config_loader import load_config

logger = logging.getLogger(__name__)

_STRIP_PUNCTUATION = re.compile(r"[^a-z0-9\s]")
_EXTRA_SPACES = re.compile(r"\s+")
_VOLUME_KEYWORD = re.compile(r"\b(volume|vol|set|change)\b")
_VOLUME_NUMBER = re.compile(r"\b(\d{1,3})\b")

_config = None
_config_path = None


def _normalize(text: str) -> str:
    """Lower-case, strip punctuation, and collapse runs of whitespace."""
    return _EXTRA_SPACES.sub(" ", _STRIP_PUNCTUATION.sub("", str(text).lower())).strip()


def get_config(config_path=None, force_reload=False):
    """Return the loaded configuration, loading it on first use.

    Pass config_path to load from a custom file. Pass force_reload=True to
    reload from disk (e.g. after the user edits config.json).
    """
    global _config, _config_path
    if config_path is not None:
        _config_path = str(config_path)
    if _config is None or force_reload or config_path is not None:
        _config = load_config(_config_path)
    return _config


def detect_wake_phrase(text: str, phrases: list[str]) -> tuple[str | None, str]:
    """Detect a wake phrase in text and return (matched_phrase, remaining_text).

    Normalizes both sides (lower-case, no punctuation, collapsed whitespace),
    matches at word boundaries, and prefers the longest matching phrase.
    Returns (None, text) if no wake phrase is found.
    """
    if not text:
        return None, ""
    normalized = _normalize(text)
    if not normalized or not phrases:
        return None, text

    best_phrase = None
    best_match = None
    for phrase in phrases:
        if not phrase:
            continue
        candidate = _normalize(phrase)
        if not candidate:
            continue
        match = re.search(r"\b" + re.escape(candidate) + r"\b", normalized)
        if match and (best_match is None or len(candidate) > len(_normalize(best_phrase))):
            best_phrase = phrase
            best_match = match

    if best_phrase is None or best_match is None:
        logger.debug("No wake phrase found in: %r", text)
        return None, text

    remainder = (normalized[: best_match.start()] + normalized[best_match.end():])
    remainder = _EXTRA_SPACES.sub(" ", remainder).strip()
    logger.info("Wake phrase detected: %r; remaining command: %r", best_phrase, remainder)
    return best_phrase, remainder


def match_command(text: str) -> str | None:
    if not text:
        return None
    commands = {action: list(phrases) for action, phrases in vars(get_config().commands).items()}
    cleaned = _STRIP_PUNCTUATION.sub("", text.lower())
    best_action = None
    best_len = 0
    for action, phrases in commands.items():
        for phrase in phrases:
            if phrase in cleaned and len(phrase) > best_len:
                best_action = action
                best_len = len(phrase)
    return best_action


def parse_volume_command(text: str) -> int | None:
    """Return an absolute volume 0-100 from a phrase like "set volume to 50".

    Matches a number anywhere in the text, but only when the text also contains a
    volume-related keyword ("volume", "vol", "set", or "change"). Returns None
    when no number or no volume keyword is present, or when the phrase matched a
    normal command instead (e.g. "volume up").
    """
    if not text:
        return None
    normalized = _normalize(text)
    if not normalized:
        return None
    if not _VOLUME_KEYWORD.search(normalized):
        return None
    match = _VOLUME_NUMBER.search(normalized)
    if not match:
        return None
    return max(0, min(100, int(match.group(1))))


def __getattr__(name: str):
    """Provide legacy module-level constants backed by the loaded config."""
    if name.startswith("_"):
        raise AttributeError(name)
    cfg = get_config()
    if name == "VLC_HOST":
        return cfg.vlc.host
    if name == "VLC_PORT":
        return cfg.vlc.port
    if name == "VLC_PASSWORD":
        return cfg.vlc.password
    if name == "VLC_ENABLED":
        return cfg.vlc.enabled
    if name == "VLC_BASE_URL":
        return f"http://{cfg.vlc.host}:{cfg.vlc.port}/requests/status.xml"
    if name == "MPC_HOST":
        return cfg.mpc.host
    if name == "MPC_PORT":
        return cfg.mpc.port
    if name == "MPC_ENABLED":
        return cfg.mpc.enabled
    if name == "MPC_BASE_URL":
        return f"http://{cfg.mpc.host}:{cfg.mpc.port}/"
    if name == "KEYBOARD_SHORTCUTS":
        shortcuts = cfg.keyboard_fallback.shortcuts
        return {key: getattr(shortcuts, key) for key in vars(shortcuts)}
    if name == "COMMAND_MAPPINGS":
        return {action: list(phrases) for action, phrases in vars(cfg.commands).items()}
    raise AttributeError(f"module 'config' has no attribute {name!r}")