import logging

from config_loader import load_config

logger = logging.getLogger(__name__)

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
    raise AttributeError(f"module 'config' has no attribute {name!r}")