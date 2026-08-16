"""System tray icon for background mode.

Requires the optional `pystray` and `Pillow` packages. Every icon function
degrades gracefully: if the libraries are missing, create_tray_icon() returns
None and the caller falls back to terminal mode.
"""

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

GREEN = (76, 175, 80)
RED = (229, 57, 53)
WHITE = (255, 255, 255)
ICON_SIZE = 64


def _tray_available() -> bool:
    """True if the optional pystray library can be imported."""
    try:
        import pystray  # noqa: F401

        return True
    except ImportError:
        return False


def _draw_mic(color: tuple[int, int, int]):
    """Draw a microphone on a `ICON_SIZE`x`ICON_SIZE` background using Pillow."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (ICON_SIZE, ICON_SIZE), color)
    draw = ImageDraw.Draw(img)

    # Microphone capsule.
    draw.rounded_rectangle([24, 6, 40, 30], radius=8, fill=WHITE)
    # Capsule grill lines.
    for y in range(12, 28, 4):
        draw.line([27, y, 37, y], fill=color, width=2)
    # Stand and holder.
    draw.rounded_rectangle([30, 30, 34, 42], radius=2, fill=WHITE)
    draw.rounded_rectangle([22, 32, 42, 36], radius=3, fill=WHITE)
    # Side arms.
    draw.rounded_rectangle([16, 24, 22, 32], radius=3, fill=WHITE)
    draw.rounded_rectangle([42, 24, 48, 32], radius=3, fill=WHITE)
    # Base.
    draw.rounded_rectangle([22, 42, 42, 47], radius=4, fill=WHITE)
    return img


def _show_popup(text: str) -> None:
    """Print the text and, on Windows, also show it in a small popup.

    Safe to call even when the process has no console (pythonw.exe sets
    sys.stdout to None).
    """
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            quoted = text.replace("'", "''")
            command = (
                "(New-Object -ComObject Wscript.Shell).Popup"
                f"('{quoted}', 3, 'Voice Control', 0x40)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            logger.debug("Failed to show status popup", exc_info=True)


def create_tray_icon(listener, controller, state=None, verbose: bool = False):
    """Create a pystray icon controlling the app, or None if unavailable.

    Args:
        listener: object with running/stop attributes (the VoiceListener).
        controller: player controller used only for status reporting.
        state: shared dict with a "listening" boolean. If omitted, a private
            one is used (tray-only toggling has no effect on the app).
        verbose: when True, print "Listener: ON/OFF" to the console on toggle
            so the app's state is visible even with console logging at ERROR.
    """
    try:
        import pystray
    except ImportError:
        logger.warning(
            "pystray is not installed; running without a tray icon "
            "(install with: pip install pystray pillow)"
        )
        return None

    if state is None:
        state = {"listening": True}
    listening = lambda: bool(state.get("listening", True))

    icon = pystray.Icon("voice-controlled-media-player")

    def _refresh_image() -> None:
        icon.icon = _draw_mic(GREEN if listening() else RED)

    def _toggle_listening(icon_item=None, item=None) -> None:
        state["listening"] = not listening()
        logger.info("Tray: listening toggled to %s", "on" if listening() else "off")
        if verbose:
            _print_listener_state(state)
        _show_popup(f"Voice control {'resumed' if listening() else 'paused'}")
        _refresh_image()

    def _show_status(icon_item=None, item=None) -> None:
        lines = [f"Listening: {'on' if listening() else 'off/paused'}"]
        if hasattr(listener, "running"):
            lines.append(f"Listener running: {listener.running}")
        current = getattr(controller, "current", None)
        if current is not None and getattr(current, "name", None):
            lines.append(f"Active player: {current.name}")
        status = "; ".join(lines)
        logger.info("Status: %s", status)
        _show_popup(status)

    def _quit(icon_item=None, item=None) -> None:
        logger.info("Tray: exit requested")
        icon.stop()
        listener.stop()

    _refresh_image()
    icon.menu = pystray.Menu(
        pystray.MenuItem(
            lambda text: "Stop Listening" if listening() else "Start Listening",
            lambda icon_item, item: _toggle_listening(icon_item, item),
        ),
        pystray.MenuItem("Show Status", lambda icon_item, item: _show_status(icon_item, item)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", lambda icon_item, item: _quit(icon_item, item)),
    )

    # Expose internals so main.py can drive the icon from a global hotkey.
    icon._refresh_image = _refresh_image
    icon._toggle_listening = _toggle_listening
    icon._state = state

    return icon


def _print_listener_state(state) -> None:
    """Print 'Listener: ON/OFF' to the console without depending on log level."""
    try:
        if sys.stdout is not None:
            print(f"Listener: {'ON' if state.get('listening', True) else 'OFF'}")
    except Exception:
        pass