# VLC HTTP Interface settings
VLC_HOST = "localhost"
VLC_PORT = 8080
VLC_PASSWORD = "admin"  # default, user can change later
VLC_BASE_URL = f"http://{VLC_HOST}:{VLC_PORT}/requests/status.xml"

# MPC-HC HTTP Interface settings (defaults)
MPC_HOST = "localhost"
MPC_PORT = 13579  # default MPC-HC web interface port
MPC_BASE_URL = f"http://{MPC_HOST}:{MPC_PORT}/"

# Keyboard shortcut fallbacks (if HTTP fails)
KEYBOARD_SHORTCUTS = {
    "play_pause": "space",
    "stop": "s",
    "skip_forward": "right",   # 10 sec forward by default
    "skip_backward": "left",   # 10 sec backward
    "volume_up": "up",
    "volume_down": "down",
    "mute": "m",
    "fullscreen": "f",
}

# Voice command mappings (normalize spoken phrases to actions)
COMMAND_MAPPINGS = {
    "play": ["play", "resume", "continue"],
    "pause": ["pause", "hold", "stop playing"],
    "stop": ["stop", "quit", "end"],
    "skip_forward": ["skip forward", "forward 10 seconds", "jump forward", "next 10 seconds", "advance"],
    "skip_backward": ["skip backward", "backward 10 seconds", "go back", "rewind 10 seconds"],
    "volume_up": ["volume up", "louder", "increase volume"],
    "volume_down": ["volume down", "softer", "decrease volume"],
    "mute": ["mute", "silence", "no sound"],
    "fullscreen": ["fullscreen", "full screen", "maximize"],
    "exit": ["exit", "quit app", "close"],
}

import re

_STRIP_PUNCTUATION = re.compile(r"[^a-z0-9\s]")


def match_command(text: str) -> str | None:
    if not text:
        return None
    cleaned = _STRIP_PUNCTUATION.sub("", text.lower())
    best_action = None
    best_len = 0
    for action, phrases in COMMAND_MAPPINGS.items():
        for phrase in phrases:
            if phrase in cleaned and len(phrase) > best_len:
                best_action = action
                best_len = len(phrase)
    return best_action