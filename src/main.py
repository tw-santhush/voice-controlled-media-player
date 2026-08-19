import argparse
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

try:
    import keyboard  # noqa: F401
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

try:
    import requests  # noqa: F401
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import tray  # noqa: F401
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

import config
import config_loader
import gesture as gesture_mod
import player_control
from gesture import GestureController

LOG_FILE = "app.log"

ACTIVATE_HINT = (
    "If you haven't already, activate the virtual environment first:\n"
    "  .venv\\Scripts\\activate    (Windows PowerShell)\n"
    "  source .venv/bin/activate  (Linux/macOS)\n"
    "Then install dependencies with: pip install -r requirements.txt"
)


def setup_logging(console_level: int = logging.INFO) -> logging.Logger:
    """Configure console + file logging. In tray mode pass logging.ERROR for the console."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def _console(text: str) -> None:
    """Print a status line to the console without relying on the log level.

    Tray mode sets the console handler to ERROR, so plain logging.info is not
    visible; these indicators are deliberately printed directly. Safe when the
    process has no console (pythonw.exe sets sys.stdout to None).
    """
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass


def dependency_report() -> list[str]:
    statuses = [
        ("requests", HAS_REQUESTS, "pip install requests"),
        ("keyboard", HAS_KEYBOARD, "pip install keyboard  (optional: HTTP control works without it)"),
        ("opencv-python", gesture_mod.cv2 is not None, "pip install opencv-python"),
        ("mediapipe", gesture_mod.HAS_MP, "pip install mediapipe"),
        ("pystray", HAS_TRAY, "pip install pystray  (optional: system tray icon)"),
    ]
    lines = ["Dependency status:"]
    for name, installed, hint in statuses:
        lines.append(f"  {name:<18} {'Installed' if installed else 'MISSING -> ' + hint}")
    return lines


def print_check_deps() -> None:
    for line in dependency_report():
        print(line)


def check_required_dependencies(log: logging.Logger) -> None:
    def fail(message: str) -> None:
        print(f"ERROR: {message}", file=sys.stderr)
        print(ACTIVATE_HINT, file=sys.stderr)
        sys.exit(1)

    if not HAS_REQUESTS:
        fail("requests is not installed. It is required for HTTP control of VLC and MPC-HC.")
    if not HAS_KEYBOARD:
        log.warning("keyboard module is missing; falling back to HTTP-only control. Optional install: pip install keyboard")


def build_controller(player: str) -> player_control.PlayerController:
    cfg = config.get_config()
    if player == "vlc":
        if not cfg.vlc.enabled:
            print("WARNING: VLC control is disabled in config (vlc.enabled=false).")
        return player_control.VLCController()
    if player == "mpc":
        if not cfg.mpc.enabled:
            print("WARNING: MPC-HC control is disabled in config (mpc.enabled=false).")
        return player_control.MPCController()
    return player_control.AutoController()


GESTURE_FEEDBACK = {
    "play_pause": "play",
    "stop": "stop",
    "skip_backward": "skip_backward",
    "skip_forward": "skip_forward",
    "volume_up": "volume_up",
    "volume_down": "volume_down",
    "volume_up_big": "volume_up",
    "volume_down_big": "volume_down",
    "toggle_mute": "mute",
    "toggle_fullscreen": "fullscreen",
}


def handle_gesture_action(action, controller, log: logging.Logger, state=None) -> None:
    """Execute a command from a detected gesture action.

    Honors the shared "listening" gate so the tray/hotkey pause also stops
    gesture processing. play_pause toggles between the controller's separate
    play() and pause() methods using the shared "playing" state flag.
    """
    if state is None:
        state = {"listening": True}

    if not state.get("listening", True):
        log.info("Detection paused; ignoring gesture: %r", action)
        return

    indicators = bool(state.get("indicators", False))

    if action == "play_pause":
        playing = bool(state.get("playing", False))
        feedback = "pause" if playing else "play"
        try:
            if playing:
                controller.pause()
            else:
                controller.play()
            state["playing"] = not playing
        except Exception:
            log.exception("Gesture command play_pause failed")
            return
        log.info("Gesture action: %s (%s)", "play_pause", feedback)
        if indicators:
            _console(f"Gesture: {feedback.title()}")
        return

    log.info("Gesture action: %s", action)
    if indicators:
        _console(f"Gesture: {action}")
    try:
        if action == "stop":
            controller.stop()
        elif action == "skip_backward":
            controller.skip_backward()
        elif action == "skip_forward":
            controller.skip_forward()
        elif action == "volume_up":
            controller.volume_up()
        elif action == "volume_down":
            controller.volume_down()
        elif action == "volume_up_big":
            controller.volume_up(step=10)
        elif action == "volume_down_big":
            controller.volume_down(step=10)
        elif action == "toggle_mute":
            controller.toggle_mute()
        elif action == "toggle_fullscreen":
            controller.toggle_fullscreen()
            log.info("Fullscreen toggled")
        else:
            log.info("No handler for gesture action: %r", action)
            return
    except Exception:
        log.exception("Gesture command %s failed", action)
        return


def run_continuous_gesture(gesture, controller, log: logging.Logger, state) -> None:
    """Run the gesture detection loop until the gesture controller is stopped."""
    if gesture is None or not gesture.available:
        log.error("Gesture detection unavailable: %s", getattr(gesture, "error", None))
        return
    return gesture.run_loop(lambda action: handle_gesture_action(action, controller, log, state))


def run_once_gesture(gesture, controller, log: logging.Logger, state) -> None:
    """Wait for a single gesture, fire it once, and stop."""
    if gesture is None or not gesture.available:
        log.error("Gesture detection unavailable: %s", getattr(gesture, "error", None))
        return

    def on_action(action) -> None:
        handle_gesture_action(action, controller, log, state)
        gesture.stop()

    gesture.run_loop(on_action)


def startup_shortcut_path() -> Path:
    """Return the path of the run-at-startup shortcut used by --install-startup/--uninstall-startup."""
    if sys.platform != "win32":
        raise OSError("Auto-start is only supported on Windows")
    startup_dir = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    return startup_dir / "Gesture Media Player.lnk"


def install_startup(shortcut_path: Path) -> bool:
    """Create a Startup-folder shortcut that runs src/main.py --tray via pythonw."""
    from tray import _tray_available

    python_dir = Path(sys.executable).parent
    target = python_dir / ("pythonw.exe" if sys.platform == "win32" else "pythonw")

    main_py = (config_loader.PROJECT_ROOT / "src" / "main.py").resolve()
    if not Path(target).exists():
        raise OSError(
            f"pythonw.exe not found at {target}. Auto-start requires a Windows Python "
            "install alongside python.exe."
        )
    if not _tray_available():
        raise OSError("pystray is not installed, so tray mode is unavailable; cannot install auto-start")

    arguments = f'"{main_py}" --tray'
    working = str(main_py.parent)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$w = New-Object -ComObject WScript.Shell;"
                f"$s = $w.CreateShortcut('{shortcut_path.resolve()}');"
                f"$s.TargetPath = '{target}';"
                f"$s.Arguments = '{arguments}';"
                f"$s.WorkingDirectory = '{working}';"
                "$s.Save()"
            ),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise OSError(f"Failed to create shortcut: {result.stderr.strip()}")
    return True


def uninstall_startup(shortcut_path: Path) -> bool:
    """Remove the Startup-folder shortcut if it exists."""
    if shortcut_path.exists():
        shortcut_path.unlink()
        return True
    return False


def _register_hotkey(icon, state, hotkey: str, log: logging.Logger, verbose: bool = False) -> None:
    """Register a global hotkey that toggles gesture detection, even while the app is in the background."""
    if not HAS_KEYBOARD:
        log.warning("keyboard module is not installed; cannot register hotkey %r", hotkey)
        return

    def toggle() -> None:
        try:
            from tray import _show_popup

            state["listening"] = not bool(state.get("listening", True))
            log.info("Hotkey %s: listening toggled to %s", hotkey, "on" if state["listening"] else "off")
            if verbose:
                _console(f"Listener: {'ON' if state['listening'] else 'OFF'}")
            _show_popup(f"Gesture control {'resumed' if state['listening'] else 'paused'}")
            refresh = getattr(icon, "_refresh_image", None)
            if refresh:
                refresh()
        except Exception:
            log.exception("Hotkey toggle failed")

    try:
        keyboard.add_hotkey(hotkey, toggle)
        log.info("Global hotkey registered: %s", hotkey)
    except Exception:
        log.exception("Failed to register global hotkey %s", hotkey)


def run_test_tray(log: logging.Logger) -> None:
    """Run only the tray icon (no gesture listener) so the tray menu can be verified manually.

    The gesture loop is never started; the "Start/Stop Detection" items just
    flip the icon color. The app exits after 30 seconds or when "Exit" is chosen.
    """
    from gesture_tray import create_gesture_tray_icon
    from tray import _tray_available

    if not _tray_available():
        message = "pystray is not installed; cannot run --test-tray (install with: pip install pystray pillow)"
        log.warning("%s", message)
        print(message)
        return

    class _StubGesture:
        camera_id = 0
        show_preview = False
        last_action = None
        running = False

        def stop(self) -> None:
            pass

        def set_camera(self, index: int) -> bool:
            return True

    state = {"listening": True, "indicators": True}
    icon = create_gesture_tray_icon(_StubGesture(), None, state=state, verbose=True)
    if icon is None:
        print("Could not create the tray icon.")
        return

    timer = threading.Timer(30.0, icon.stop)
    timer.daemon = True
    timer.start()

    print("Test-tray mode: the gesture listener is NOT running.")
    print("Use the tray menu 'Start Detection'/'Stop Detection' to check the icon changes.")
    print("The app exits after 30 seconds or when you choose Exit in the tray menu.")
    try:
        icon.run()
    except Exception:
        log.exception("Tray icon failed in test mode")
    finally:
        log.info("Test-tray shutdown complete")


def run_tray(
    gesture,
    controller,
    log: logging.Logger,
    state,
    hotkey=None,
) -> None:
    """Run the gesture loop in a background thread with the tray icon on the main thread."""
    from gesture_tray import create_gesture_tray_icon

    show_indicators = bool(state.get("indicators", False))

    icon = create_gesture_tray_icon(gesture, controller, state=state, verbose=show_indicators)
    if not icon:
        log.warning("Tray is unavailable; falling back to terminal mode")
        run_continuous_gesture(gesture, controller, log, state)
        return

    if hotkey:
        _register_hotkey(icon, state, hotkey, log, verbose=show_indicators)

    worker = threading.Thread(
        target=run_continuous_gesture,
        args=(gesture, controller, log, state),
        daemon=True,
    )
    worker.start()

    log.info(
        "Tray mode active. Look for the camera icon in the system tray. "
        "Exit the app from the tray menu (this console can be closed)."
    )
    if show_indicators:
        _console(f"Listener: {'ON' if state.get('listening', True) else 'OFF'}")
    if sys.stdout is not None:
        print(
            "Tray mode active. Right-click the camera icon in the system tray "
            "to control the app, then close this window."
        )

    try:
        icon.run()
    except Exception:
        log.exception("Tray icon failed")
    finally:
        if gesture is not None:
            gesture.stop()
        worker.join(timeout=2)
        log.info("Shutdown complete")


def run_raw_preview(camera_id: int | None = None) -> int:
    """Debug mode: open the webcam and show the unprocessed frames.

    Tries camera index 0 first, then 1 and -1, prints diagnostics when the
    device is unreadable, and keeps the window open until 'q' is pressed.
    Returns the process exit code.
    """
    cv2 = gesture_mod.cv2
    if cv2 is None:
        print("ERROR: opencv-python is not installed (pip install -r requirements.txt).", file=sys.stderr)
        return 1
    indices = [camera_id] if camera_id is not None else [0, 1, -1]
    cap = None
    for index in indices:
        if index is None:
            continue
        try:
            candidate = cv2.VideoCapture(index)
        except Exception as exc:
            print(f"Camera {index} failed to open: {exc}")
            continue
        if not candidate.isOpened():
            candidate.release()
            continue
        try:
            ok, frame = candidate.read()
        except Exception:
            ok = False
        if not ok or frame is None or frame.size == 0:
            candidate.release()
            continue
        cap = candidate
        print(f"Using camera index {index}")
        break
    if cap is None:
        print("ERROR: no readable webcam found (tried indexes 0, 1, -1).", file=sys.stderr)
        return 1
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    if width and height:
        print(f"Camera reports input size: {width}x{height}")
    try:
        mean, minv, maxv, std = frame.mean(), frame.min(), frame.max(), frame.std()
        print(f"First frame stats: mean={float(mean):.1f} min={int(minv)} max={int(maxv)} std={float(std):.2f}")
    except Exception:
        pass
    if not gesture_mod._frame_has_signal(frame):
        print(
            "WARNING: camera is delivering constant (signal-less) frames - the device is not producing a "
            "real image. Check camera/privacy settings, or that another app hasn't locked the webcam.",
            file=sys.stderr,
        )
    if height and height <= 240:
        print(f"WARNING: camera reports a small {width}x{height} frame; the preview will be scaled up 1.5x.")
        height = int(height * 1.5)
    print("Press q to quit")
    window = "Raw Camera Preview (q to quit)"
    warned_no_signal = False
    try:
        while True:
            try:
                ok, frame = cap.read()
            except Exception:
                ok = False
            if not ok or frame is None:
                continue
            if not gesture_mod._frame_has_signal(frame) and not warned_no_signal:
                print("WARNING: frames keep coming back black/constant - the webcam is not sending a real image.", file=sys.stderr)
                warned_no_signal = True
            if frame.shape[0] != height:
                frame = cv2.resize(frame, (width, height))
            cv2.imshow(window, frame)
            if cv2.waitKey(1) & 0xFF in (ord("q"), ord("Q"), 27):
                break
    except KeyboardInterrupt:
        pass
    finally:
        cap.release()
        try:
            cv2.destroyWindow(window)
        except Exception:
            pass
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Gesture-controlled media player (webcam hand gestures for VLC)"
    )
    parser.add_argument(
        "--player",
        choices=["vlc", "mpc", "auto"],
        default="auto",
        help="Which player to control (default: auto)",
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=None,
        help="Webcam index to use for gesture control (default: config gesture.camera_id or 0)",
    )
    parser.add_argument(
        "--show-preview",
        action="store_true",
        help="Show a live webcam preview window during gesture control (press q to exit)",
    )
    parser.add_argument(
        "--raw-preview",
        action="store_true",
        help="Debug the webcam: open it, show the raw frames, and exit. "
        "Tries indexes 0, 1 and -1 and prints diagnostics; q closes the window",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        default=True,
        help="Run continuously (default: True)",
    )
    parser.add_argument(
        "--single",
        action="store_true",
        help="Detect a single gesture and exit (for testing)",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check dependency availability and print a status report, then exit",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a custom JSON config file (default: config.json in the project root)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG logging",
    )
    parser.add_argument(
        "--gesture-debug",
        action="store_true",
        help="Enable verbose gesture classification logging and relax detection "
        "thresholds (debounce/cooldown/volume interval) for tuning",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Run in the background with a system-tray icon (requires pystray)",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Disable the system-tray icon even if tray.enabled is set in config",
    )
    parser.add_argument(
        "--test-tray",
        action="store_true",
        help="Run only the tray icon without the gesture listener (manual menu test, exits after 30s)",
    )
    parser.add_argument(
        "--hotkey",
        default="ctrl+shift+l",
        help="Global hotkey to toggle gesture detection on/off while in the background "
        "(default: 'ctrl+shift+l'; requires the keyboard module)",
    )
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="Create a Windows Startup-folder shortcut that launches the app at logon, then exit",
    )
    parser.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="Remove the Windows Startup-folder shortcut installed by --install-startup, then exit",
    )
    args = parser.parse_args(argv)

    # tray.enabled in config acts like --tray unless --no-tray is given.
    # Resolve it early so logging sees the right value.
    if args.config:
        config.get_config(config_path=args.config)
    try:
        tray_enabled_cfg = bool(getattr(getattr(config.get_config(), "tray", None), "enabled", False))
    except Exception:
        tray_enabled_cfg = False
    if not args.no_tray and (args.tray or tray_enabled_cfg):
        args.tray = True

    log = setup_logging(console_level=logging.ERROR if args.tray else logging.INFO)

    if args.raw_preview:
        if args.show_preview:
            print("WARNING: --raw-preview already shows the webcam feed; ignoring --show-preview.")
        sys.exit(run_raw_preview(args.camera))

    if args.install_startup or args.uninstall_startup:
        if sys.platform != "win32":
            print("ERROR: --install-startup/--uninstall-startup are only supported on Windows.", file=sys.stderr)
            return
        shortcut = startup_shortcut_path()
        try:
            if args.install_startup:
                install_startup(shortcut)
                print("Auto-start installed. The app will launch with the tray icon at every logon.")
                print(f"  Shortcut: {shortcut}")
            elif uninstall_startup(shortcut):
                print("Auto-start removed.")
                print(f"  Shortcut: {shortcut}")
            else:
                print("No auto-start shortcut was found.")
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
        return

    if args.test_tray:
        run_test_tray(log)
        return

    if args.debug:
        log.info("Debug mode enabled")
        logging.getLogger().setLevel(logging.DEBUG)

    if args.config:
        config.get_config(config_path=args.config)

    cfg = config.get_config()

    if args.check_deps:
        print_check_deps()
        return

    check_required_dependencies(log)

    if args.single:
        args.continuous = False

    controller = build_controller(args.player)
    gesture_cfg = getattr(cfg, "gesture", None)

    gesture_debug = args.gesture_debug or bool(
        getattr(gesture_cfg, "debug", False) if gesture_cfg else False
    )
    if args.gesture_debug:
        # Make the gesture classification debug logs visible on the console
        # even without --debug (the root console handler only forwards INFO+).
        gesture_logger = logging.getLogger("gesture")
        gesture_logger.setLevel(logging.DEBUG)
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG)
        console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        gesture_logger.addHandler(console)
        log.info("Gesture debug mode enabled: verbose classification logs + relaxed thresholds")

    camera_id = args.camera
    if camera_id is None:
        camera_id = getattr(gesture_cfg, "camera_id", 0) if gesture_cfg else 0
    camera_id = int(camera_id)

    debounce_frames = (
        1 if args.gesture_debug else getattr(gesture_cfg, "debounce_frames", 3) if gesture_cfg else 3
    )
    cooldown_seconds = (
        0.2 if args.gesture_debug else getattr(gesture_cfg, "cooldown_seconds", 0.5) if gesture_cfg else 0.5
    )
    volume_interval_seconds = (
        0.2
        if args.gesture_debug
        else getattr(gesture_cfg, "volume_interval_seconds", 0.5) if gesture_cfg else 0.5
    )

    gesture = GestureController(
        camera_id=camera_id,
        show_preview=args.show_preview,
        debounce_frames=debounce_frames,
        cooldown_seconds=cooldown_seconds,
        model_path=getattr(gesture_cfg, "model_path", None) if gesture_cfg else None,
        swipe_window=getattr(gesture_cfg, "swipe_window", None) if gesture_cfg else None,
        swipe_velocity_threshold=(
            getattr(gesture_cfg, "swipe_velocity_threshold", 0.2) if gesture_cfg else 0.2
        ),
        swipe_min_distance=(
            getattr(gesture_cfg, "swipe_min_distance", 0.08) if gesture_cfg else 0.08
        ),
        swipe_consistency_frames=(
            getattr(gesture_cfg, "swipe_consistency_frames", 4) if gesture_cfg else 4
        ),
        swipe_cooldown_seconds=(
            getattr(gesture_cfg, "swipe_cooldown_seconds", 0.3) if gesture_cfg else 0.3
        ),
        thumb_up_angle_threshold=(
            getattr(gesture_cfg, "thumb_up_angle_threshold", 30.0) if gesture_cfg else 30.0
        ),
        thumb_down_angle_threshold=(
            getattr(gesture_cfg, "thumb_down_angle_threshold", 30.0) if gesture_cfg else 30.0
        ),
        pinch_threshold_ratio=(
            getattr(gesture_cfg, "pinch_threshold_ratio", 0.12) if gesture_cfg else 0.12
        ),
        finger_angle_threshold=(
            getattr(gesture_cfg, "finger_angle_threshold", 20) if gesture_cfg else 20
        ),
        gesture_debug=gesture_debug,
        show_feedback=getattr(gesture_cfg, "show_feedback", True) if gesture_cfg else True,
        volume_interval_seconds=volume_interval_seconds,
        volume_step=getattr(gesture_cfg, "volume_step", 5) if gesture_cfg else 5,
        volume_provider=controller.get_volume if controller is not None else None,
    )
    if not gesture.available:
        log.error("Gesture control unavailable: %s", gesture.error)
        print(f"ERROR: Gesture control is unavailable: {gesture.error}", file=sys.stderr)
        print(
            "  Install the dependencies (pip install -r requirements.txt) or check the webcam.",
            file=sys.stderr,
        )
        return

    if args.debug:
        print(f"Camera: index={gesture.camera_id} available={gesture.available}")

    state = {
        "listening": True,
        "indicators": bool(args.tray and args.debug),
        "stop_event": threading.Event(),
        "playing": False,
    }

    startup = f"Starting gesture-controlled media player (camera={camera_id}, player={args.player})"
    hint = f"camera={camera_id}"
    log.info("%s", startup)

    if args.tray and (not HAS_TRAY or not tray._tray_available()):
        log.warning("pystray is not installed; falling back to terminal mode")
        args.tray = False

    if args.tray and sys.platform == "win32" and getattr(getattr(cfg, "tray", None), "auto_start", False):
        try:
            shortcut = startup_shortcut_path()
            if not shortcut.exists():
                install_startup(shortcut)
                log.info("Installed logon autostart shortcut (tray.auto_start)")
        except OSError as exc:
            log.warning("Could not auto-install the startup shortcut: %s", exc)

    if not args.tray:
        print(f"Gesture control... {hint}, player={args.player}. Press Ctrl+C to stop.")

    try:
        if args.tray:
            run_tray(gesture, controller, log, state, hotkey=args.hotkey)
        elif args.continuous:
            run_continuous_gesture(gesture, controller, log, state)
        else:
            run_once_gesture(gesture, controller, log, state)
    except KeyboardInterrupt:
        log.info("Shutdown requested (Ctrl+C)")
    finally:
        state["stop_event"].set()
        if gesture is not None:
            gesture.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()