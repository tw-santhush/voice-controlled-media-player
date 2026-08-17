from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    requests = None

import config

logger = logging.getLogger(__name__)

VLC_VOLUME_MAX = 200  # VLC's HTTP volume scale is 0-200, where 100 == 100%


def _vlc_volume_from_app(percent: int) -> int:
    """Convert an app-scale 0-100 percent into the VLC HTTP scale (0-200)."""
    return max(0, min(VLC_VOLUME_MAX, int(round(float(percent) * 2))))


def _app_volume_from_vlc(vlc_volume) -> int:
    """Convert a VLC HTTP volume (0-200) back to the app-scale 0-100 percent."""
    return max(0, min(100, int(round(float(vlc_volume) / 2))))

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

    def __init__(self, skip_seconds=None, volume_step=None, fallback_enabled=None):
        cfg = config.get_config()
        self.skip_seconds = (
            skip_seconds if skip_seconds is not None else cfg.player.default_skip_seconds
        )
        self.volume_step = volume_step if volume_step is not None else cfg.player.volume_step
        self.fallback_enabled = (
            cfg.keyboard_fallback.enabled if fallback_enabled is None else fallback_enabled
        )

    def _report(self, action: str) -> None:
        logger.info("[%s] %s", self.name, action)

    def _fallback(self, action: str) -> bool:
        if not self.fallback_enabled:
            logger.info("[%s] Keyboard fallback disabled in config; skipping %s", self.name, action)
            return False
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

    def play(self, **kwargs):
        raise NotImplementedError

    def pause(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def skip_forward(self, seconds=None):
        raise NotImplementedError

    def skip_backward(self, seconds=None):
        raise NotImplementedError

    def volume_up(self, step=None):
        raise NotImplementedError

    def volume_down(self, step=None):
        raise NotImplementedError

    def set_volume(self, percent: int) -> None:
        raise NotImplementedError

    def get_volume(self) -> int | None:
        """Return the current volume as a 0-100 percentage, or None if unknown.

        Implementations must not raise: an unavailable player returns None so
        the preview can quietly fall back to its locally tracked value.
        """
        return None

    def toggle_mute(self):
        raise NotImplementedError

    def toggle_fullscreen(self):
        raise NotImplementedError

    def next(self):
        raise NotImplementedError

    def previous(self):
        raise NotImplementedError


class VLCController(PlayerController):
    name = "vlc"

    def __init__(
        self,
        host=None,
        port=None,
        password=None,
        enabled=None,
        skip_seconds=None,
        volume_step=None,
    ):
        if requests is None:
            raise RuntimeError(REQUESTS_HINT)
        cfg = config.get_config()
        self.enabled = cfg.vlc.enabled if enabled is None else enabled
        self.password = password if password is not None else cfg.vlc.password
        self.base_url = f"http://{host or cfg.vlc.host}:{port or cfg.vlc.port}/requests/status.xml"
        super().__init__(skip_seconds=skip_seconds, volume_step=volume_step)
        self.session = requests.Session()
        self.session.auth = ("", self.password)
        self._muted_volume = None

    def _check_enabled(self) -> bool:
        if not self.enabled:
            logger.warning("[vlc] VLC control is disabled in config (vlc.enabled=false)")
            return False
        return True

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

    def play(self, **kwargs):
        if not self._check_enabled():
            return
        try:
            self._http("pl_play")
        except requests.RequestException:
            logger.exception("[vlc] HTTP play failed")
            self._fallback("play_pause")
            return
        self._report("play")

    def pause(self):
        if not self._check_enabled():
            return
        try:
            self._http("pl_pause")
        except requests.RequestException:
            logger.exception("[vlc] HTTP pause failed")
            self._fallback("play_pause")
            return
        self._report("pause")

    def stop(self):
        if not self._check_enabled():
            return
        try:
            self._http("pl_stop")
        except requests.RequestException:
            logger.exception("[vlc] HTTP stop failed")
            self._fallback("stop")
            return
        self._report("stop")

    def skip_forward(self, seconds=None):
        seconds = self.skip_seconds if seconds is None else seconds
        if not self._check_enabled():
            return
        try:
            self._http("seek", {"val": f"+{seconds}"})
        except requests.RequestException:
            logger.exception("[vlc] HTTP seek forward failed")
            self._fallback("skip_forward")
            return
        self._report(f"skip_forward (+{seconds}s)")

    def skip_backward(self, seconds=None):
        seconds = self.skip_seconds if seconds is None else seconds
        if not self._check_enabled():
            return
        try:
            self._http("seek", {"val": f"-{seconds}"})
        except requests.RequestException:
            logger.exception("[vlc] HTTP seek backward failed")
            self._fallback("skip_backward")
            return
        self._report(f"skip_backward (-{seconds}s)")

    def volume_up(self, step=None):
        step = self.volume_step if step is None else step
        if not self._check_enabled():
            return
        try:
            volume, _ = self._status()
            # The step is an app-scale percent, so it must be doubled for VLC.
            new_volume = min(int(volume) + int(step) * 2, VLC_VOLUME_MAX)
            self._http("volume", {"val": new_volume})
        except requests.RequestException:
            logger.exception("[vlc] HTTP volume up failed")
            self._fallback("volume_up")
            return
        self._report(f"volume_up (+{step})")

    def volume_down(self, step=None):
        step = self.volume_step if step is None else step
        if not self._check_enabled():
            return
        try:
            volume, _ = self._status()
            new_volume = max(int(volume) - int(step) * 2, 0)
            self._http("volume", {"val": new_volume})
        except requests.RequestException:
            logger.exception("[vlc] HTTP volume down failed")
            self._fallback("volume_down")
            return
        self._report(f"volume_down (-{step})")

    def set_volume(self, percent: int) -> None:
        """Set the absolute volume on the VLC HTTP interface.

        The app works in 0-100 percent; VLC's HTTP "volume" command uses a
        0-200 scale where 100 is 100%, so the value is scaled by two on the way
        in (e.g. "volume 50" becomes ``val=100`` = 50% on VLC, not 25%).
        """
        percent = max(0, min(100, int(round(float(percent)))))
        if not self._check_enabled():
            return
        try:
            self._http("volume", {"val": _vlc_volume_from_app(percent)})
        except requests.RequestException:
            logger.exception("[vlc] HTTP set_volume failed")
            return
        self._report(f"set_volume ({percent})")

    def get_volume(self) -> int | None:
        """Return the current VLC volume as a 0-100 percent, or None on failure.

        VLC reports volume on its own 0-200 scale; this divides it by two so
        the app (and the preview volume bar) always work in 0-100.
        """
        self._check_enabled()
        try:
            volume, _ = self._status()
        except (requests.RequestException, ValueError, ET.ParseError):
            logger.debug("[vlc] get_volume failed")
            return None
        return _app_volume_from_vlc(volume)

    def next(self):
        if not self._check_enabled():
            return
        try:
            self._http("pl_next")
        except requests.RequestException:
            logger.exception("[vlc] HTTP next failed")
            self._fallback("next")
            return
        self._report("next")

    def previous(self):
        if not self._check_enabled():
            return
        try:
            self._http("pl_previous")
        except requests.RequestException:
            logger.exception("[vlc] HTTP previous failed")
            self._fallback("previous")
            return
        self._report("previous")

    def toggle_mute(self):
        if not self._check_enabled():
            return
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
        if not self._check_enabled():
            return
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
        "previous": 915,
        "next": 916,
    }

    def __init__(
        self,
        host=None,
        port=None,
        enabled=None,
        skip_seconds=None,
        volume_step=None,
    ):
        if requests is None:
            raise RuntimeError(REQUESTS_HINT)
        cfg = config.get_config()
        self.enabled = cfg.mpc.enabled if enabled is None else enabled
        base_url = f"http://{host or cfg.mpc.host}:{port or cfg.mpc.port}/"
        self.command_url = base_url.rstrip("/") + "/command.html"
        super().__init__(skip_seconds=skip_seconds, volume_step=volume_step)
        self.session = requests.Session()
        self._last_volume = None

    def _check_enabled(self) -> bool:
        if not self.enabled:
            logger.warning("[mpc-hc] MPC-HC control is disabled in config (mpc.enabled=false)")
            return False
        return True

    def _wm(self, command: str):
        command_id = self.COMMANDS[command]
        response = self.session.get(
            self.command_url, params={"wm_command": command_id}, timeout=2
        )
        response.raise_for_status()

    def _volume_html(self) -> str | None:
        """Fetch the volume page and return the '<volume>' raw value, or None."""
        try:
            response = self.session.get(self.command_url, timeout=2)
            response.raise_for_status()
        except requests.RequestException:
            return None
        match = re.search(r"<volume>(-?\d+)</volume>", response.text)
        if not match:
            return None
        return match.group(1)

    def get_volume(self) -> int | None:
        """Return the current MPC-HC volume (0-100), or the last known value."""
        raw = self._volume_html()
        if raw is None:
            return self._last_volume
        try:
            percent = max(0, min(100, int(raw)))
        except ValueError:
            return self._last_volume
        self._last_volume = percent
        return percent

    def _send(self, command: str):
        if not self._check_enabled():
            return
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

    def skip_forward(self, seconds=None):
        self._send("jump_forward")

    def skip_backward(self, seconds=None):
        self._send("jump_backward")

    def volume_up(self, step=None):
        self._send("volume_up")

    def volume_down(self, step=None):
        self._send("volume_down")

    def set_volume(self, percent: int) -> None:
        """Set the absolute volume (0-100) via the MPC-HC web interface."""
        percent = max(0, min(100, int(round(float(percent)))))
        if not self._check_enabled():
            return
        try:
            response = self.session.get(
                self.command_url, params={"volume": percent}, timeout=2
            )
            response.raise_for_status()
        except requests.RequestException:
            logger.exception("[mpc-hc] HTTP set_volume failed")
            return
        self._report(f"set_volume ({percent})")

    def next(self):
        self._send("next")

    def previous(self):
        self._send("previous")

    def toggle_mute(self):
        self._send("mute")

    def toggle_fullscreen(self):
        self._send("fullscreen")


class AutoController:
    """Detects the active player and routes commands to it."""

    def __init__(self, controllers: dict | None = None):
        if controllers is None:
            cfg = config.get_config()
            controllers = {}
            if cfg.vlc.enabled:
                controllers["vlc"] = VLCController()
            else:
                logger.info("[auto] VLC control is disabled in config")
            if cfg.mpc.enabled:
                controllers["mpc-hc"] = MPCController()
            else:
                logger.info("[auto] MPC-HC control is disabled in config")
        self.controllers = controllers
        self.current = None

    def resolve(self) -> PlayerController | None:
        active = detect_active_player()
        if active in self.controllers:
            self.current = self.controllers[active]
            logger.info("[auto] Routing to %s", active)
            return self.current
        if active is not None:
            logger.warning(
                "[auto] %s is running but is disabled in config; enable it in config.json",
                active,
            )
            return None
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

    def skip_forward(self, seconds=None):
        self._route("skip_forward", seconds)

    def skip_backward(self, seconds=None):
        self._route("skip_backward", seconds)

    def volume_up(self, step=None):
        self._route("volume_up", step)

    def volume_down(self, step=None):
        self._route("volume_down", step)

    def toggle_mute(self):
        self._route("toggle_mute")

    def toggle_fullscreen(self):
        self._route("toggle_fullscreen")

    def set_volume(self, percent: int) -> None:
        self._route("set_volume", percent)

    def get_volume(self) -> int | None:
        controller = self.current if self.current is not None else self.resolve()
        if controller is None:
            return None
        try:
            return controller.get_volume()
        except Exception:
            return None

    def next(self):
        self._route("next")

    def previous(self):
        self._route("previous")