import argparse
import logging
import sys

try:
    import keyboard  # noqa: F401
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

try:
    import speech_recognition  # noqa: F401
    HAS_SPEECH = True
except ImportError:
    HAS_SPEECH = False

try:
    import pyaudio  # noqa: F401
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False

try:
    import requests  # noqa: F401
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import vlc  # noqa: F401
    HAS_PYTHON_VLC = True
except (ImportError, OSError):
    HAS_PYTHON_VLC = False

import config
import player_control
from voice_listener import VoiceListener

LOG_FILE = "app.log"

ACTIVATE_HINT = (
    "If you haven't already, activate the virtual environment first:\n"
    "  .venv\\Scripts\\activate    (Windows PowerShell)\n"
    "  source .venv/bin/activate  (Linux/macOS)\n"
    "Then install dependencies with: pip install -r requirements.txt"
)


def setup_logging() -> logging.Logger:
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def dependency_report() -> list[str]:
    statuses = [
        ("speech_recognition", HAS_SPEECH, "pip install SpeechRecognition"),
        ("pyaudio", HAS_PYAUDIO, "install the PyAudio wheel matching your Python version (see README)"),
        ("requests", HAS_REQUESTS, "pip install requests"),
        ("keyboard", HAS_KEYBOARD, "pip install keyboard  (optional: HTTP control works without it)"),
        ("python-vlc", HAS_PYTHON_VLC, "pip install python-vlc  (optional)"),
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
    if not HAS_SPEECH:
        fail("SpeechRecognition is not installed. It is required for voice control.")
    if not HAS_PYAUDIO:
        fail(
            "pyaudio is not installed. SpeechRecognition needs it for microphone input. "
            "On Windows, install the wheel that matches your Python version (a cp311 wheel "
            "is expected for Python 3.11). See README 'Troubleshooting'."
        )
    if not HAS_KEYBOARD:
        log.warning("keyboard module is missing; falling back to HTTP-only control. Optional install: pip install keyboard")


def build_controller(player: str) -> player_control.PlayerController:
    if player == "vlc":
        return player_control.VLCController()
    if player == "mpc":
        return player_control.MPCController()
    return player_control.AutoController()


def run_once(listener: VoiceListener, controller, log: logging.Logger) -> None:
    text = listener.listen_once()
    if not text:
        log.info("Nothing heard")
        return
    handle_command(listener, controller, text, log)


def handle_command(listener, controller, text: str, log: logging.Logger) -> None:
    action = config.match_command(text)
    if not action:
        log.info("No matching command for: %r", text)
        return
    log.info("Recognized command: %r -> %s", text, action)
    if action == "exit":
        log.info("Exit requested")
        listener.stop()
        return
    getattr(controller, action)()


def main(argv: list[str] | None = None) -> None:
    log = setup_logging()
    parser = argparse.ArgumentParser(description="Voice-controlled media player")
    parser.add_argument(
        "--player",
        choices=["vlc", "mpc", "auto"],
        default="auto",
        help="Which player to control (default: auto)",
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
        help="Listen for a single command and exit (for testing)",
    )
    parser.add_argument(
        "--check-deps",
        action="store_true",
        help="Check dependency availability and print a status report, then exit",
    )
    args = parser.parse_args(argv)

    if args.check_deps:
        print_check_deps()
        return

    check_required_dependencies(log)

    if args.single:
        args.continuous = False

    controller = build_controller(args.player)
    listener = VoiceListener()

    log.info("Starting voice-controlled media player (player=%s)", args.player)
    print(f"Listening... player={args.player}. Press Ctrl+C to stop.")

    try:
        if args.continuous:
            listener.listen_loop(lambda text: handle_command(listener, controller, text, log))
            while listener.running:
                listener.wait_stop(timeout=1)
        else:
            run_once(listener, controller, log)
    except KeyboardInterrupt:
        log.info("Shutdown requested (Ctrl+C)")
    finally:
        listener.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()