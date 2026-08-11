import re

from config_loader import load_config

_STRIP_PUNCTUATION = re.compile(r"[^a-z0-9\s]")

_config = None
_config_path = None


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