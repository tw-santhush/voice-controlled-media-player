import argparse
import difflib
import logging
import sys
import time
from types import SimpleNamespace

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
    import pyttsx3  # noqa: F401
    HAS_TTS = True
except ImportError:
    HAS_TTS = False

try:
    import vlc  # noqa: F401
    HAS_PYTHON_VLC = True
except (ImportError, OSError):
    HAS_PYTHON_VLC = False

try:
    import vosk  # noqa: F401
    HAS_VOSK = True
except (ImportError, OSError):
    HAS_VOSK = False

import config
import player_control
import tts
from voice_listener import VoiceListener, list_microphones

LOG_FILE = "app.log"

ACTIVATE_HINT = (
    "If you haven't already, activate the virtual environment first:\n"
    "  .venv\\Scripts\\activate    (Windows PowerShell)\n"
    "  source .venv/bin/activate  (Linux/macOS)\n"
    "Then install dependencies with: pip install -r requirements.txt"
)

TTS_PHRASES = {
    "play": "Playing",
    "pause": "Paused",
    "stop": "Stopped",
    "skip_forward": "Skipped forward",
    "skip_backward": "Skipped backward",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "mute": "Muted",
    "fullscreen": "Fullscreen",
    "exit": "Exiting",
}


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
        ("pyttsx3", HAS_TTS, "pip install pyttsx3  (optional: text-to-speech feedback)"),
        ("python-vlc", HAS_PYTHON_VLC, "pip install python-vlc  (optional)"),
        ("vosk", HAS_VOSK, "pip install vosk  (optional: offline recognition, --recognizer vosk)"),
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


def check_tts_engine(engine: str) -> bool:
    logger = logging.getLogger("tts")
    result = tts.speak("test", engine=engine, fallback_enabled=True)
    logger.info("TTS speak test with engine=%s: %s", engine, "OK" if result else "FAILED")
    return result


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


def speak_feedback(tts_cfg, action: str | None = None, failed: bool = False) -> None:
    if not tts_cfg.enabled:
        return
    if failed:
        tts.speak(
            "Command failed",
            voice_id=tts_cfg.voice_id,
            engine=tts_cfg.engine,
            fallback_enabled=tts_cfg.fallback_enabled,
        )
        return
    phrase = TTS_PHRASES.get(action) if action else None
    if phrase:
        tts.speak(
            phrase,
            voice_id=tts_cfg.voice_id,
            engine=tts_cfg.engine,
            fallback_enabled=tts_cfg.fallback_enabled,
        )


def handle_command(listener, controller, text: str, log: logging.Logger, tts_cfg) -> None:
    action = config.match_command(text)
    if not action:
        log.info("No matching command for: %r", text)
        return
    log.info("Recognized command: %r -> %s", text, action)
    if action == "exit":
        log.info("Exit requested")
        speak_feedback(tts_cfg, action)
        listener.stop()
        return
    try:
        getattr(controller, action)()
    except Exception:
        log.exception("Command %s failed", action)
        speak_feedback(tts_cfg, failed=True)
        return
    speak_feedback(tts_cfg, action)


def process_recognized(listener, controller, text: str, log: logging.Logger, tts_cfg, wake_cfg, state) -> None:
    """Handle a recognized utterance, applying wake-word filtering if enabled."""
    if not text:
        return

    if wake_cfg.enabled:
        now = time.time()
        if state["armed"]:
            if now - state["armed_at"] > wake_cfg.timeout_seconds:
                state["armed"] = False
                log.info("Wake command timeout; listening for a wake phrase again")
            else:
                state["armed"] = False
                handle_command(listener, controller, text, log, tts_cfg)
                return

        phrase, remainder = config.detect_wake_phrase(text, wake_cfg.phrases)
        if phrase is None:
            log.info("Ignored speech without wake phrase: %r", text)
            return
        if not remainder:
            state["armed"] = True
            state["armed_at"] = now
            log.info(
                "Wake phrase only; listening for a command (within %.1fs)...",
                wake_cfg.timeout_seconds,
            )
            return
        log.debug("Executing command from wake remainder: %r", remainder)
        handle_command(listener, controller, remainder, log, tts_cfg)
        return

    handle_command(listener, controller, text, log, tts_cfg)


def run_once(listener: VoiceListener, controller, log: logging.Logger, tts_cfg, wake_cfg, state) -> None:
    text = listener.listen_once()
    if not text:
        log.info("Nothing heard")
        return
    process_recognized(listener, controller, text, log, tts_cfg, wake_cfg, state)


def build_wake_cfg(args, cfg, log) -> SimpleNamespace:
    """Build the wake-word config from CLI args and config; disable wake in --single mode."""
    enabled = cfg.wake.enabled and not args.no_wake
    if args.single:
        if enabled:
            log.info("Single-command mode disables the wake word automatically")
        enabled = False
    return SimpleNamespace(
        enabled=enabled,
        phrases=list(cfg.wake.phrases),
        timeout_seconds=cfg.wake.timeout_seconds,
    )


def analyze_wake_training(heard: list[str], phrases: list[str]) -> list[str]:
    """Review recorded wake phrase repetitions and return suggestion lines."""
    lines: list[str] = []
    if not heard:
        lines.append("No speech was recognized in any attempt.")
        lines.append("Make sure your microphone works (try --list-mics) and speak clearly.")
        return lines

    matched = sum(1 for text in heard if config.detect_wake_phrase(text, phrases)[0])
    lines.append(f"Wake phrase recognized {matched}/{len(heard)} times.")
    if matched == len(heard):
        lines.append("Recognition is consistent - your current wake phrase is fine.")
        return lines

    close = []
    for phrase in phrases:
        for text in heard:
            ratio = difflib.SequenceMatcher(None, phrase, text).ratio()
            if 0.55 <= ratio < 1.0:
                close.append((phrase, text, ratio))
    for phrase, text, ratio in sorted(close, key=lambda item: -item[2]):
        lines.append(f"  Saying {phrase!r} is usually heard as {text!r} ({ratio:.0%} similar).")

    if matched == 0:
        lines.append("No repetition matched a wake phrase. Try a shorter, more distinctive phrase")
        lines.append("(like 'player'), or switch offline recognition with --recognizer vosk.")
    else:
        lines.append("Results are inconsistent. Speak more clearly or shorten the wake phrase.")
    return lines


def run_wake_training(listener: VoiceListener, repetitions: int = 5) -> None:
    """Listen to several wake phrase repetitions and print recognition results."""
    print("Wake phrase training: say your wake phrase five times.")
    heard: list[str] = []
    for i in range(1, repetitions + 1):
        print(f"  Repetition {i}/{repetitions} - speak now...")
        text = listener.listen_once()
        if text:
            heard.append(text)
            print(f"    Heard: {text!r}")
        else:
            print("    Nothing heard; try again.")
    print()
    print("Wake phrase training results:")
    for line in analyze_wake_training(heard, list(config.get_config().wake.phrases)):
        print(f"  {line}")


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
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a custom JSON config file (default: config.json in the project root)",
    )
    parser.add_argument(
        "--tts",
        action="store_true",
        help="Force-enable text-to-speech feedback (overrides config)",
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Force-disable text-to-speech feedback (overrides config)",
    )
    parser.add_argument(
        "--no-wake",
        action="store_true",
        help="Disable the wake-word requirement (overrides config)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose DEBUG logging and microphone diagnostics",
    )
    parser.add_argument(
        "--tts-engine",
        choices=["auto", "pyttsx3", "powershell", "say", "espeak", "system"],
        default=None,
        help="Force a specific TTS engine (default: config or auto)",
    )
    parser.add_argument(
        "--recognizer",
        choices=["google", "vosk", "auto"],
        default=None,
        help="Speech recognizer: google, vosk (offline), or auto (default: config or auto)",
    )
    parser.add_argument(
        "--mic-index",
        type=int,
        default=None,
        help="Microphone device index to use (see --list-mics)",
    )
    parser.add_argument(
        "--list-mics",
        action="store_true",
        help="List available microphone devices using pyaudio, then exit",
    )
    parser.add_argument(
        "--test-tts",
        action="store_true",
        help="Speak a test phrase with the configured TTS engine, then exit",
    )
    parser.add_argument(
        "--train-wake",
        action="store_true",
        help="Listen to 5 repetitions of your wake phrase and analyze recognition",
    )
    args = parser.parse_args(argv)

    if args.debug:
        log.info("Debug mode enabled")
        logging.getLogger().setLevel(logging.DEBUG)

    if args.config:
        config.get_config(config_path=args.config)

    cfg = config.get_config()

    if args.check_deps:
        print_check_deps()
        engine = args.tts_engine or getattr(cfg.tts, "engine", "auto")
        ok = check_tts_engine(engine)
        print("TTS speak test:", "OK" if ok else "FAILED (see logs)")
        return

    if args.list_mics:
        mics = list_microphones()
        if not mics:
            print("No microphones found.")
        else:
            print("Available microphones:")
            for mic in mics:
                rate = f" ({mic['sample_rate']} Hz)" if mic.get("sample_rate") else ""
                print(f"  [{mic['index']}] {mic['name']}{rate}")
        return

    tts_enabled = cfg.tts.enabled
    if args.tts:
        tts_enabled = True
    if args.no_tts:
        tts_enabled = False
    if args.tts and args.no_tts:
        log.warning("Both --tts and --no-tts given; --no-tts wins")
    tts_cfg = SimpleNamespace(
        enabled=tts_enabled,
        voice_id=cfg.tts.voice_id,
        engine=args.tts_engine or getattr(cfg.tts, "engine", "auto"),
        fallback_enabled=getattr(cfg.tts, "fallback_enabled", True),
    )

    if args.test_tts:
        tts_log = logging.getLogger("tts")
        ok = tts.speak(
            "Testing one two three",
            voice_id=tts_cfg.voice_id,
            engine=tts_cfg.engine,
            fallback_enabled=tts_cfg.fallback_enabled,
        )
        tts_log.info("TTS test: %s", "OK" if ok else "FAILED")
        print("TTS test:", "OK" if ok else "FAILED (see logs for details)")
        return

    if args.single:
        args.continuous = False

    wake_cfg = build_wake_cfg(args, cfg, log)

    if tts_enabled and not HAS_TTS and args.tts_engine in (None, "auto", "pyttsx3"):
        log.warning("tts is enabled in config but pyttsx3 is not installed; install with: pip install pyttsx3")

    check_required_dependencies(log)

    controller = build_controller(args.player)
    recognizer_type = args.recognizer or getattr(cfg.recognizer, "engine", "auto")
    listener = VoiceListener(
        timeout=cfg.voice.timeout_seconds,
        phrase_time_limit=cfg.voice.phrase_time_limit,
        recognizer_type=recognizer_type,
        vosk_model_path=getattr(cfg.recognizer, "vosk_model_path", None),
        device_index=args.mic_index,
    )

    if args.train_wake:
        run_wake_training(listener)
        return

    if args.debug:
        mic_info = listener.get_microphone_info()
        if mic_info:
            print(
                f"Mic: index={mic_info['index']} sample_rate={mic_info['sample_rate']} "
                f"name={mic_info['name']!r}"
            )
        else:
            print("Mic info unavailable")

    state = {"armed": False, "armed_at": 0.0}

    log.info(
        "Starting voice-controlled media player (player=%s, recognizer=%s)",
        args.player,
        recognizer_type,
    )
    print(f"Listening... player={args.player} recognizer={recognizer_type}. Press Ctrl+C to stop.")

    try:
        if args.continuous:
            listener.listen_loop(
                lambda text: process_recognized(listener, controller, text, log, tts_cfg, wake_cfg, state)
            )
            while listener.running:
                listener.wait_stop(timeout=1)
        else:
            run_once(listener, controller, log, tts_cfg, wake_cfg, state)
    except KeyboardInterrupt:
        log.info("Shutdown requested (Ctrl+C)")
    finally:
        listener.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()