"""System tray icon for gesture (webcam) mode.

Small, gesture-specific wrapper around the shared tray helpers. Provides the
same start/stop toggle as the voice tray, plus a webcam-preview toggle and a
camera switcher. Everything degrades gracefully when pystray is missing.
"""

import logging
import threading
import time

import tray

logger = logging.getLogger(__name__)


def _tray_available() -> bool:
    return tray._tray_available()


def _toggle_preview(gesture) -> None:
    gesture.show_preview = not bool(getattr(gesture, "show_preview", False))
    logger.info("Gesture preview toggled to %s", gesture.show_preview)
    tray._show_popup(f"Webcam preview {'on' if gesture.show_preview else 'off'}")


def _next_camera(gesture) -> None:
    camera = int(getattr(gesture, "camera_id", 0))
    candidate = camera + 1
    for index in range(candidate, candidate + 5):
        if gesture.set_camera(index):
            tray._show_popup(f"Camera {gesture.camera_id}")
            return
    gesture.set_camera(0)
    tray._show_popup("Could not open any other camera; back to 0")


def create_gesture_tray_icon(gesture, controller, state=None, verbose: bool = False):
    """Create the gesture-mode tray icon, or None if pystray is unavailable."""
    try:
        import pystray
    except ImportError:
        logger.warning(
            "pystray is not installed; running without a tray icon "
            "(install with: pip install pystray pillow)"
        )
        return None

    icon = tray.create_tray_icon(
        gesture,
        controller,
        state=state,
        verbose=verbose,
        mode="gesture",
    )
    if icon is None:
        return None

    gesture_active = lambda: bool((state or {}).get("listening", True))

    def _show_status(icon_item=None, item=None) -> None:
        lines = [f"Camera: {getattr(gesture, 'camera_id', '?')}"]
        lines.append(f"Preview: {'on' if gesture.show_preview else 'off'}")
        last = getattr(gesture, "last_action", None)
        if last:
            lines.append(f"Last gesture: {last}")
        status = "; ".join(lines)
        logger.info("Status: %s", status)
        tray._show_popup(status)

    icon.menu = pystray.Menu(
        pystray.MenuItem(
            lambda text: "Stop Detection" if gesture_active() else "Start Detection",
            lambda icon_item, item: icon._toggle_listening(icon_item, item),
        ),
        pystray.MenuItem(
            "Toggle Webcam Preview",
            lambda icon_item, item: _toggle_preview(gesture),
        ),
        pystray.MenuItem(
            "Next Camera",
            lambda icon_item, item: _next_camera(gesture),
        ),
        pystray.MenuItem("Show Status", lambda icon_item, item: _show_status(icon_item, item)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Exit",
            lambda icon_item, item: icon.stop() or (gesture.stop() if gesture else None),
        ),
    )
    return icon