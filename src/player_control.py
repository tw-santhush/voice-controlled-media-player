from __future__ import annotations

import logging
import os
import subprocess
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None

import config

logger = logging.getLogger(__name__)

VLC_VOLUME_MAX = 300

REQUESTS_HINT = (
    "requests is not installed. Activate your virtual environment and run: "
    "pip install -r requirements.txt"
)


def _process_running(image_name: str) -> bool:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return image_name.lower() in result.stdout.lower()
        result = subprocess.run(
            ["pgrep", "-x", image_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        logger.exception("Failed to check whether %s is running", image_name)
        return False


def detect_active_player() -> str | None:
    """Return 'vlc' or 'mpc-hc' if that player process is running, else None."""
    if _process_running("vlc.exe"):
        return "vlc"
    if _process_running("mpc-hc.exe") or _process_running("mpc-hc64.exe"):
        return "mpc-hc"
    return None


class PlayerController:
    name = "generic"

    def _report(self, action: str) -> None:
        logger.info("[%s] %s", self.name, action)

    def _fallback(self, action: str) -> bool:
        try:
            import keyboard
        except ImportError:
            logger.warning(
                "[%s] keyboard module is not installed; skipping keyboard fallback for %s "
                "(HTTP control still works). Install with: pip install keyboard",
                self.name,
                action,
            )
            return False
        shortcut = config.KEYBOARD_SHORTCUTS.get(action)
        if not shortcut:
            logger.warning("[%s] No keyboard fallback defined for %s", self.name, action)
            return False
        try:
            keyboard.press_and_release(shortcut)
        except Exception:
            logger.exception("[%s] Failed to send keyboard shortcut %s", self.name, shortcut)
            return False
        logger.info("[%s] Keyboard fallback: %s -> %s", self.name, action, shortcut)
        return True

    @staticmethod
    def detect_active_player() -> str | None:
        return detect_active_player()

    def play(self):
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def skip_forward(self, seconds=10):
        raise NotImplementedError

    def skip_backward(self, seconds=10):
        raise NotImplementedError

    def volume_up(self, step=5):
        raise NotImplementedError

    def volume_down(self, step=5):
        raise NotImplementedError

    def toggle_mute(self):
        raise NotImplementedError

    def toggle_fullscreen(self):
        raise NotImplementedError


class VLCController(PlayerController):
    name = "vlc"

    def __init__(self, base_url: str | None = None, password: str | None = None):
        if requests is None:
            raise RuntimeError(REQUESTS_HINT)
        self.base_url = base_url or config.VLC_BASE_URL
        self.password = password if password is not None else config.VLC_PASSWORD
        self.session = requests.Session()
        self.session.auth = ("", self.password)
        self._muted_volume = None

    def _http(self, command: str, params: dict | None = None) -> requests.Response:
        query = {"command": command}
        if params:
            query.update(params)
        response = self.session.get(self.base_url, params=query, timeout=2)
        response.raise_for_status()
        return response

    def _status(self):
        response = self.session.get(self.base_url, timeout=2)
        response.raise_for_status()
        root = ET.fromstring(response.text)
        volume = float(root.findtext("volume") or 0)
        muted = (root.findtext("muted") or "false").strip().lower() == "true"
        return volume, muted

    def play(self):
        try:
            self._http("pl_play")
        except requests.RequestException:
            logger.exception("[vlc] HTTP play failed")
            self._fallback("play_pause")
            return
        self._report("play")

    def pause(self):
        try:
            self._http("pl_pause")
        except requests.RequestException:
            logger.exception("[vlc] HTTP pause failed")
            self._fallback("play_pause")
            return
        self._report("pause")

    def stop(self):
        try:
            self._http("pl_stop")
        except requests.RequestException:
            logger.exception("[vlc] HTTP stop failed")
            self._fallback("stop")
            return
        self._report("stop")

    def skip_forward(self, seconds=10):
        try:
            self._http("seek", {"val": f"+{seconds}"})
        except requests.RequestException:
            logger.exception("[vlc] HTTP seek forward failed")
            self._fallback("skip_forward")
            return
        self._report(f"skip_forward (+{seconds}s)")

    def skip_backward(self, seconds=10):
        try:
            self._http("seek", {"val": f"-{seconds}"})
        except requests.RequestException:
            logger.exception("[vlc] HTTP seek backward failed")
            self._fallback("skip_backward")
            return
        self._report(f"skip_backward (-{seconds}s)")

    def volume_up(self, step=5):
        try:
            volume, _ = self._status()
            new_volume = min(int(volume) + step, VLC_VOLUME_MAX)
            self._http("volume", {"val": new_volume})
        except requests.RequestException:
            logger.exception("[vlc] HTTP volume up failed")
            self._fallback("volume_up")
            return
        self._report(f"volume_up (+{step})")

    def volume_down(self, step=5):
        try:
            volume, _ = self._status()
            new_volume = max(int(volume) - step, 0)
            self._http("volume", {"val": new_volume})
        except requests.RequestException:
            logger.exception("[vlc] HTTP volume down failed")
            self._fallback("volume_down")
            return
        self._report(f"volume_down (-{step})")

    def toggle_mute(self):
        try:
            volume, muted = self._status()
            if muted:
                self._muted_volume = self._muted_volume if self._muted_volume is not None else volume
                self._http("volume", {"val": self._muted_volume})
            else:
                self._muted_volume = volume
                self._http("volume", {"val": 0})
        except requests.RequestException:
            logger.exception("[vlc] HTTP mute failed")
            self._fallback("mute")
            return
        self._report("toggle_mute")

    def toggle_fullscreen(self):
        try:
            self._http("fullscreen")
        except requests.RequestException:
            logger.exception("[vlc] HTTP fullscreen failed")
            self._fallback("fullscreen")
            return
        self._report("toggle_fullscreen")


class MPCController(PlayerController):
    name = "mpc-hc"

    COMMANDS = {
        "play": 887,
        "pause": 888,
        "play_pause": 889,
        "stop": 890,
        "jump_forward": 902,
        "jump_backward": 901,
        "volume_up": 907,
        "volume_down": 908,
        "mute": 909,
        "fullscreen": 830,
    }

    def __init__(self, base_url: str | None = None):
        if requests is None:
            raise RuntimeError(REQUESTS_HINT)
        self.base_url = base_url or config.MPC_BASE_URL
        self.command_url = self.base_url.rstrip("/") + "/command.html"
        self.session = requests.Session()

    def _wm(self, command: str):
        command_id = self.COMMANDS[command]
        response = self.session.get(
            self.command_url, params={"wm_command": command_id}, timeout=2
        )
        response.raise_for_status()

    def _send(self, command: str):
        try:
            self._wm(command)
        except (requests.RequestException, KeyError):
            logger.exception("[mpc-hc] HTTP command %s failed", command)
            self._fallback(command)
            return
        self._report(command)

    def play(self):
        self._send("play")

    def pause(self):
        self._send("pause")

    def stop(self):
        self._send("stop")

    def skip_forward(self, seconds=10):
        self._send("jump_forward")

    def skip_backward(self, seconds=10):
        self._send("jump_backward")

    def volume_up(self, step=5):
        self._send("volume_up")

    def volume_down(self, step=5):
        self._send("volume_down")

    def toggle_mute(self):
        self._send("mute")

    def toggle_fullscreen(self):
        self._send("fullscreen")


class AutoController:
    """Detects the active player and routes commands to it."""

    def __init__(self, controllers: dict | None = None):
        self.controllers = controllers or {
            "vlc": VLCController(),
            "mpc-hc": MPCController(),
        }
        self.current = None

    def resolve(self) -> PlayerController | None:
        active = detect_active_player()
        if active in self.controllers:
            self.current = self.controllers[active]
            logger.info("[auto] Routing to %s", active)
            return self.current
        logger.warning(
            "No active player detected. Launch VLC or MPC-HC first, then try again."
        )
        return None

    def _route(self, action: str, *args, **kwargs) -> None:
        controller = self.current if self.current is not None else self.resolve()
        if controller is None:
            return
        getattr(controller, action)(*args, **kwargs)

    def play(self):
        self._route("play")

    def pause(self):
        self._route("pause")

    def stop(self):
        self._route("stop")

    def skip_forward(self, seconds=10):
        self._route("skip_forward", seconds)

    def skip_backward(self, seconds=10):
        self._route("skip_backward", seconds)

    def volume_up(self, step=5):
        self._route("volume_up", step)

    def volume_down(self, step=5):
        self._route("volume_down", step)

    def toggle_mute(self):
        self._route("toggle_mute")

    def toggle_fullscreen(self):
        self._route("toggle_fullscreen")