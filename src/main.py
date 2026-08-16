import argparse
import difflib
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
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

try:
    import tray  # noqa: F401
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

import config
import config_loader
import gesture as gesture_mod
import player_control
import tts
from gesture import GestureController
from voice_listener import VoiceListener, _peak, list_microphones
from wake import build_porcupine

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
    "next": "Next track",
    "previous": "Previous track",
    "volume_set": "Volume set",
    "listen_on": "Listening resumed",
    "listen_off": "Listening paused",
}


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


def check_required_dependencies(log: logging.Logger, mode: str = "gesture") -> None:
    def fail(message: str) -> None:
        print(f"ERROR: {message}", file=sys.stderr)
        print(ACTIVATE_HINT, file=sys.stderr)
        sys.exit(1)

    if not HAS_REQUESTS:
        fail("requests is not installed. It is required for HTTP control of VLC and MPC-HC.")
    if mode in ("voice", "both"):
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


def _set_tts_cooldown(tts_cfg, state) -> None:
    """Record when the assistant last spoke so incoming audio can be ignored briefly."""
    if state is None or not tts_cfg.enabled:
        return
    seconds = getattr(tts_cfg, "cooldown_seconds", 0) or 0
    if seconds > 0:
        state["tts_cooldown_until"] = time.time() + seconds


def speak_feedback(
    tts_cfg,
    action: str | None = None,
    failed: bool = False,
    volume: int | None = None,
    state=None,
) -> None:
    if not tts_cfg.enabled:
        return
    _set_tts_cooldown(tts_cfg, state)
    if failed:
        tts.speak(
            "Command failed",
            voice_id=tts_cfg.voice_id,
            engine=tts_cfg.engine,
            fallback_enabled=tts_cfg.fallback_enabled,
        )
        return
    if volume is not None:
        tts.speak(
            f"Volume set to {volume}",
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


def handle_command(listener, controller, text: str, log: logging.Logger, tts_cfg, state=None) -> None:
    """Execute a recognized command.

    `state` is a shared dict whose "listening" boolean gates processing: while
    paused, only the listen_on/listen_off commands are honored. The tray icon
    and those two voice commands toggle the flag.
    """
    if state is None:
        state = {"listening": True}

    indicators = bool(state.get("indicators", False))

    action = config.match_command(text)

    if action == "listen_on":
        state["listening"] = True
        log.info("Listening resumed (voice command)")
        if indicators:
            _console("Listener: ON")
        speak_feedback(tts_cfg, action, state=state)
        return
    if action == "listen_off":
        state["listening"] = False
        log.info("Listening paused (voice command)")
        if indicators:
            _console("Listener: OFF")
        speak_feedback(tts_cfg, action, state=state)
        return

    if not state.get("listening", True):
        log.info("Listener paused; ignoring command: %r", text)
        return

    if action is None:
        volume = config.parse_volume_command(text)
        if volume is None:
            log.info("No matching command for: %r", text)
            return
        log.info("Numeric volume command: %r -> %d%%", text, volume)
        if indicators:
            _console(f"Command: set_volume to {volume}")
        try:
            controller.set_volume(volume)
        except Exception:
            log.exception("Command set_volume failed")
            speak_feedback(tts_cfg, failed=True, state=state)
            return
        speak_feedback(tts_cfg, volume=volume, state=state)
        return

    log.info("Recognized command: %r -> %s", text, action)
    if indicators:
        _console(f"Command: {action} ({text!r})")
    if action == "exit":
        log.info("Exit requested")
        speak_feedback(tts_cfg, action, state=state)
        listener.stop()
        return
    try:
        getattr(controller, action)()
    except Exception:
        log.exception("Command %s failed", action)
        speak_feedback(tts_cfg, failed=True, state=state)
        return
    speak_feedback(tts_cfg, action, state=state)


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


def handle_gesture_action(action, controller, log: logging.Logger, tts_cfg, state=None) -> None:
    """Execute a command from a detected gesture action.

    Mirrors ``handle_command`` for voice: honors the shared "listening" gate so
    the tray/hotkey pause also stops gesture processing. play_pause toggles
    between the controller's separate play() and pause() methods using the
    shared "playing" state flag.
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
            speak_feedback(tts_cfg, failed=True, state=state)
            return
        log.info("Gesture action: %s (%s)", "play_pause", feedback)
        if indicators:
            _console(f"Gesture: {feedback.title()}")
        speak_feedback(tts_cfg, feedback, state=state)
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
        else:
            log.info("No handler for gesture action: %r", action)
            return
    except Exception:
        log.exception("Gesture command %s failed", action)
        speak_feedback(tts_cfg, failed=True, state=state)
        return
    speak_feedback(tts_cfg, GESTURE_FEEDBACK.get(action), state=state)


def process_recognized(
    listener,
    controller,
    text: str,
    log: logging.Logger,
    tts_cfg,
    wake_cfg,
    state,
    wake_bypassed=False,
) -> None:
    """Handle a recognized utterance, applying wake-word filtering if enabled.

    `wake_bypassed` is used by the Porcupine engine: it already detected the
    wake word on the raw audio stream, so string-based wake matching is skipped.
    The listen_on/listen_off toggles are always honored, even when the listener
    is paused or no wake word was spoken.
    """
    if not text:
        return

    cmd = config.match_command(text)
    if cmd in ("listen_on", "listen_off"):
        log.debug("Always-active toggle command handled: %r", text)
        handle_command(listener, controller, text, log, tts_cfg, state)
        return

    verbose = getattr(wake_cfg, "wake_debug", False) or bool(state.get("indicators", False))
    if verbose:
        _console(f"Raw recognized text: {text!r}")

    if wake_bypassed:
        log.debug("Wake word already detected by Porcupine; dispatching command %r", text)
        handle_command(listener, controller, text, log, tts_cfg, state)
        return

    if wake_cfg.enabled:
        now = time.time()
        if state["armed"]:
            if now - state["armed_at"] > wake_cfg.timeout_seconds:
                state["armed"] = False
                log.info("Wake command timeout; listening for a wake phrase again")
            else:
                state["armed"] = False
                handle_command(listener, controller, text, log, tts_cfg, state)
                return

        phrase, remainder = config.detect_wake_phrase(text, wake_cfg.phrases)
        if phrase is None:
            if verbose:
                _console("No wake phrase detected")
            log.info("Ignored speech without wake phrase: %r", text)
            return
        if verbose:
            _console(f"Wake phrase detected: {phrase!r}; remaining command: {remainder!r}")
        if not remainder:
            state["armed"] = True
            state["armed_at"] = now
            log.info(
                "Wake phrase only; listening for a command (within %.1fs)...",
                wake_cfg.timeout_seconds,
            )
            return
        log.debug("Executing command from wake remainder: %r", remainder)
        handle_command(listener, controller, remainder, log, tts_cfg, state)
        return

    handle_command(listener, controller, text, log, tts_cfg, state)


def _in_tts_cooldown(state) -> bool:
    """True while the assistant's own voice could still be picked up by the mic."""
    until = state.get("tts_cooldown_until", 0.0)
    return time.time() < until


def _sync_listener_cooldown(listener, state) -> None:
    """Push the state-tracked TTS cooldown into the listener, if it supports it.

    The listener's internal ``_should_listen`` gate consults its own
    ``cooldown_until``; mirror the shared state value so push-to-talk and the
    TTS cooldown both work inside ``listen_loop``.
    """
    setter = getattr(listener, "set_cooldown", None)
    if callable(setter):
        try:
            setter(state.get("tts_cooldown_until", 0.0) - time.time())
        except Exception:
            pass


def _listen_gate(state, tts_cfg, ptt_cfg, log):
    """Return a gate callable for the listener loop: None blocks listening.

    The gate is consulted before each listen attempt. It blocks while a TTS
    response is still being spoken (so the mic does not re-hear it) and, with
    push-to-talk, while the PTT key is not held.
    """
    def gate():
        if _in_tts_cooldown(state):
            log.debug("Ignoring audio: TTS cooldown active")
            return False
        if ptt_cfg is not None and getattr(ptt_cfg, "enabled", False):
            key = getattr(ptt_cfg, "key", "ctrl")
            if not HAS_KEYBOARD:
                log.warning("push_to_talk is enabled but the keyboard module is missing; ignoring PTT")
            else:
                try:
                    if not keyboard.is_pressed(key):
                        log.debug("Ignoring audio: push-to-talk key %r not held", key)
                        return False
                except Exception:
                    log.debug("Could not read push-to-talk key state", exc_info=True)
                    return False
        return True

    return gate


def run_once(listener: VoiceListener, controller, log: logging.Logger, tts_cfg, wake_cfg, state) -> None:
    text = listener.listen_once()
    if not text:
        log.info("Nothing heard")
        return
    process_recognized(listener, controller, text, log, tts_cfg, wake_cfg, state)


def run_continuous(
    listener: VoiceListener,
    controller,
    log: logging.Logger,
    tts_cfg,
    wake_cfg,
    state,
    ptt_cfg=None,
) -> None:
    """Run the listen loop until the listener is stopped. Used by normal and tray modes."""
    porcupine = getattr(wake_cfg, "porcupine", None)
    if ptt_cfg is not None:
        listener.push_to_talk_enabled = bool(getattr(ptt_cfg, "enabled", False))
        listener.push_to_talk_key = getattr(ptt_cfg, "key", "ctrl")
    _sync_listener_cooldown(listener, state)
    if porcupine is not None:
        _run_continuous_porcupine(listener, controller, log, tts_cfg, wake_cfg, state, ptt_cfg)
        return

    def on_text(text) -> None:
        process_recognized(listener, controller, text, log, tts_cfg, wake_cfg, state)
        _sync_listener_cooldown(listener, state)

    listener.listen_loop(on_text, stop_event=state.get("stop_event"))
    while listener.running:
        listener.wait_stop(timeout=1)


def _run_continuous_porcupine(
    listener: VoiceListener,
    controller,
    log: logging.Logger,
    tts_cfg,
    wake_cfg,
    state,
    ptt_cfg=None,
) -> None:
    """Continuous loop driven by the Porcupine wake-word engine.

    Blocks on Porcupine reading raw audio until a wake word fires; each
    instance of ``listener.listen_once()`` then captures the command. When the
    keyword is heard we treat it as the wake gating already satisfied and pass
    ``wake_bypassed=True`` so process_recognized skips string wake matching.
    """
    porcupine = wake_cfg.porcupine
    gate = _listen_gate(state, tts_cfg, ptt_cfg, log)
    log.info(
        "Wake engine: Porcupine (keywords=%s, engine=%s)",
        porcupine.keywords if porcupine.keywords else porcupine.keyword_paths,
        wake_cfg.engine,
    )
    while listener.running:
        if not gate():
            listener.wait_stop(timeout=0.5)
            continue
        keyword = porcupine.wait_for_wake_word(
            timeout=wake_cfg.timeout_seconds,
            stop_event=state.get("stop_event"),
        )
        if keyword is None:
            continue
        log.debug("Wake word detected by Porcupine: %s", keyword)
        text = listener.listen_once()
        if not text:
            log.info("Nothing heard after wake word")
            continue
        process_recognized(
            listener,
            controller,
            text,
            log,
            tts_cfg,
            wake_cfg,
            state,
            wake_bypassed=True,
        )


def run_continuous_gesture(gesture, controller, log: logging.Logger, tts_cfg, state) -> None:
    """Run the gesture detection loop until the gesture controller is stopped."""
    if gesture is None or not gesture.available:
        log.error("Gesture detection unavailable: %s", getattr(gesture, "error", None))
        return
    return gesture.run_loop(lambda action: handle_gesture_action(action, controller, log, tts_cfg, state))


def run_once_gesture(gesture, controller, log: logging.Logger, tts_cfg, state) -> None:
    """Wait for a single gesture, fire it once, and stop."""
    if gesture is None or not gesture.available:
        log.error("Gesture detection unavailable: %s", getattr(gesture, "error", None))
        return

    def on_action(action) -> None:
        handle_gesture_action(action, controller, log, tts_cfg, state)
        gesture.stop()

    gesture.run_loop(on_action)


def run_continuous_mode(
    mode: str,
    listener,
    gesture,
    controller,
    log: logging.Logger,
    tts_cfg,
    wake_cfg,
    state,
    ptt_cfg=None,
) -> None:
    """Dispatch the continuous run loop based on the input mode.

    "both" runs the gesture loop in a background thread while the voice loop
    blocks on the main thread; stopping either input stops the app.
    """
    if mode == "gesture":
        run_continuous_gesture(gesture, controller, log, tts_cfg, state)
        return
    if mode == "voice":
        run_continuous(listener, controller, log, tts_cfg, wake_cfg, state, ptt_cfg=ptt_cfg)
        return

    if gesture is None or not gesture.available:
        log.warning("Gesture control unavailable (%s); continuing with voice only", getattr(gesture, "error", None))
        run_continuous(listener, controller, log, tts_cfg, wake_cfg, state, ptt_cfg=ptt_cfg)
        return

    gesture_thread = threading.Thread(
        target=run_continuous_gesture,
        args=(gesture, controller, log, tts_cfg, state),
        daemon=True,
    )
    gesture_thread.start()
    try:
        run_continuous(listener, controller, log, tts_cfg, wake_cfg, state, ptt_cfg=ptt_cfg)
    finally:
        gesture.stop()
        gesture_thread.join(timeout=2)


def build_wake_cfg(args, cfg, log) -> SimpleNamespace:
    """Build the wake-word config from CLI args and config; disable wake in --single mode.

    The ``engine`` value selects the wake engine: "porcupine" when Porcupine is
    available and configured, otherwise "string". ``porcupine`` holds the live
    PorcupineWakeWord instance (or None) so the continuous loop can block on it.
    """
    enabled = cfg.wake.enabled and not args.no_wake
    if args.single:
        if enabled:
            log.info("Single-command mode disables the wake word automatically")
        enabled = False

    engine = getattr(cfg.wake, "engine", "auto") or "auto"
    porcupine = None
    if enabled and engine in ("porcupine", "auto"):
        porcupine = build_porcupine(
            cfg,
            log=log,
            device_index=getattr(args, "mic_index", None),
            enabled=True,
        )
        if porcupine.available:
            engine = "porcupine"
            log.info("Porcupine wake word active")
        else:
            if engine == "porcupine":
                log.warning("Porcupine wake engine requested but unavailable (%s); using string wake", porcupine.error)
            porcupine = None
            engine = "string"
    if engine in ("porcupine", "auto") and porcupine is None:
        engine = "string"

    return SimpleNamespace(
        enabled=enabled,
        phrases=list(cfg.wake.phrases),
        timeout_seconds=cfg.wake.timeout_seconds,
        wake_debug=bool(getattr(args, "wake_debug", False)),
        engine=engine,
        porcupine=porcupine,
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


def play_wav_with_system(path) -> bool:
    """Open a WAV file with the OS default player as a playback fallback."""
    try:
        if sys.platform == "win32":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", str(path)])
        else:
            subprocess.Popen(["aplay", str(path)])
        return True
    except Exception:
        return False


def play_wav_file(path) -> bool:
    """Play a WAV file with pyaudio, falling back to the system sound player."""
    try:
        import wave as wave_module
        import pyaudio
    except ImportError:
        return play_wav_with_system(path)
    try:
        with wave_module.open(str(path), "rb") as wf:
            pa = pyaudio.PyAudio()
            try:
                stream = pa.open(
                    format=pa.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True,
                )
                try:
                    data = wf.readframes(1024)
                    while data:
                        stream.write(data)
                        data = wf.readframes(1024)
                finally:
                    stream.stop_stream()
                    stream.close()
            finally:
                pa.terminate()
        return True
    except Exception:
        return play_wav_with_system(path)


def run_record_test(listener: VoiceListener, seconds: float = 3.0) -> None:
    """Record a few seconds of audio, save it to test_audio.wav, play it back, and exit."""
    print(f"Recording {seconds:.0f}s of audio from the default microphone...")
    audio = listener.capture_audio(duration=seconds)
    raw = audio.get_raw_data()
    sample_rate = getattr(audio, "sample_rate", 16000)
    duration = (len(raw) / (2.0 * sample_rate)) if sample_rate else 0.0
    peak = _peak(raw)
    out_path = config_loader.PROJECT_ROOT / "test_audio.wav"
    out_path.write_bytes(audio.get_wav_data())
    print(f"Saved {duration:.2f}s of audio to {out_path} (peak amplitude {peak:,})")
    if play_wav_file(out_path):
        print("Played back the recording.")
    else:
        print("Could not play it back; open test_audio.wav in your player manually.")


def run_energy_test(listener: VoiceListener, duration: float = 5.0, interval: float = 0.5) -> None:
    """Measure audio energy and suggest a value for voice.energy_threshold."""
    print(
        f"Energy test: recording for {duration:.0f}s. Stay quiet first, then speak a little "
        "so the test can compare your voice to the background noise."
    )
    readings = listener.measure_energy(duration=duration, interval=interval)
    running_avg = 0.0
    running_peak = 0.0
    for i, level in enumerate(readings, start=1):
        running_avg = (running_avg * (i - 1) + level) / i
        running_peak = max(running_peak, level)
        print(
            f"  t={i * interval:4.1f}s  energy={level:7.1f}  "
            f"average={running_avg:7.1f}  peak={running_peak:7.1f}"
        )
    sorted_readings = sorted(readings)
    median = sorted_readings[len(sorted_readings) // 2]
    suggested = max(round(median * 1.5), 30)
    print()
    print(f"Median ambient energy: {median:.1f}")
    print(f"Suggested energy_threshold: {suggested}  (config default is 20).")
    print(f"Apply it with: python src/main.py --set-energy {suggested}")
    if suggested > 300:
        print("Your environment is noisy; a higher threshold reduces false triggers.")
    elif suggested < 300:
        print("Your microphone is quiet; a lower threshold makes quiet speech easier to detect.")


def startup_shortcut_path() -> Path:
    """Return the path of the run-at-startup shortcut used by --install-startup/--uninstall-startup."""
    if sys.platform != "win32":
        raise OSError("Auto-start is only supported on Windows")
    startup_dir = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs\Startup"
    return startup_dir / "Voice Media Player.lnk"


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
    """Register a global hotkey that toggles listening, even while the app is in the background."""
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
            _show_popup(f"Voice control {'resumed' if state['listening'] else 'paused'}")
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
    """Run only the tray icon (no voice listener) so the tray menu can be verified manually.

    The listener is never started; the "Start/Stop Listening" items just flip the
    icon color. The app exits after 30 seconds or when "Exit" is chosen.
    """
    from tray import create_tray_icon, _tray_available

    if not _tray_available():
        message = "pystray is not installed; cannot run --test-tray (install with: pip install pystray pillow)"
        log.warning("%s", message)
        print(message)
        return

    class _StubListener:
        running = False

        def stop(self) -> None:
            pass

    state = {"listening": True, "indicators": True}
    icon = create_tray_icon(_StubListener(), None, state=state, verbose=True)
    if icon is None:
        print("Could not create the tray icon.")
        return

    timer = threading.Timer(30.0, icon.stop)
    timer.daemon = True
    timer.start()

    print("Test-tray mode: the voice listener is NOT running.")
    print("Use the tray menu 'Start Listening'/'Stop Listening' to check the icon changes.")
    print("The app exits after 30 seconds or when you choose Exit in the tray menu.")
    try:
        icon.run()
    except Exception:
        log.exception("Tray icon failed in test mode")
    finally:
        log.info("Test-tray shutdown complete")


def run_tray(
    listener,
    gesture,
    controller,
    log: logging.Logger,
    tts_cfg,
    wake_cfg,
    state,
    hotkey=None,
    ptt_cfg=None,
    mode: str = "voice",
) -> None:
    """Run the listen loop in a background thread with the tray icon on the main thread."""
    from tray import create_tray_icon, _draw_mic, _show_popup

    show_indicators = bool(state.get("indicators", False))

    if mode in ("gesture", "both") and gesture is not None and gesture.available:
        from gesture_tray import create_gesture_tray_icon

        icon = create_gesture_tray_icon(gesture, controller, state=state, verbose=show_indicators)
    else:
        icon = create_tray_icon(listener, controller, state=state, verbose=show_indicators, mode=mode)
    if not icon:
        log.warning("Tray is unavailable; falling back to terminal mode")
        run_continuous_mode(mode, listener, gesture, controller, log, tts_cfg, wake_cfg, state, ptt_cfg=ptt_cfg)
        return

    if hotkey:
        _register_hotkey(icon, state, hotkey, log, verbose=show_indicators)

    worker = threading.Thread(
        target=run_continuous_mode,
        args=(mode, listener, gesture, controller, log, tts_cfg, wake_cfg, state),
        kwargs={"ptt_cfg": ptt_cfg},
        daemon=True,
    )
    worker.start()

    icon_name = "camera" if mode in ("gesture", "both") else "microphone"
    log.info(
        "Tray mode active. Look for the %s icon in the system tray. "
        "Exit the app from the tray menu (this console can be closed).",
        icon_name,
    )
    if show_indicators:
        _console(f"Listener: {'ON' if state.get('listening', True) else 'OFF'}")
    if sys.stdout is not None:
        print(
            f"Tray mode active. Right-click the {icon_name} icon in the system tray "
            "to control the app, then close this window."
        )

    try:
        icon.run()
    except Exception:
        log.exception("Tray icon failed")
    finally:
        if listener is not None:
            listener.stop()
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
        description="Gesture- or voice-controlled media player (webcam gestures by default)"
    )
    parser.add_argument(
        "--player",
        choices=["vlc", "mpc", "auto"],
        default="auto",
        help="Which player to control (default: auto)",
    )
    parser.add_argument(
        "--mode",
        choices=["voice", "gesture", "both"],
        default="gesture",
        help="Input mode: webcam hand gestures (default), voice, or both at once",
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
    parser.add_argument(
        "--record-test",
        action="store_true",
        help="Record 3 seconds of audio, save it to test_audio.wav, play it back, and exit",
    )
    parser.add_argument(
        "--energy-test",
        action="store_true",
        help="Measure audio energy for 5 seconds and suggest an energy_threshold value, then exit",
    )
    parser.add_argument(
        "--set-energy",
        type=int,
        default=None,
        help="Override voice.energy_threshold for this run (e.g. --set-energy 100)",
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Run in the background with a system-tray icon (requires pystray)",
    )
    parser.add_argument(
        "--test-tray",
        action="store_true",
        help="Run only the tray icon without the voice listener (manual menu test, exits after 30s)",
    )
    parser.add_argument(
        "--hotkey",
        default="ctrl+shift+l",
        help="Global hotkey to toggle listening on/off while in the background "
        "(default: 'ctrl+shift+l'; requires the keyboard module)",
    )
    parser.add_argument(
        "--wake-debug",
        action="store_true",
        help="Print the raw recognized text and whether a wake phrase was detected",
    )
    parser.add_argument(
        "--push-to-talk",
        action="store_true",
        help="Enable push-to-talk: commands are only processed while the PTT key is held "
        "(overrides config; see --ptt-key)",
    )
    parser.add_argument(
        "--ptt-key",
        default=None,
        help="Key to hold for push-to-talk (default: config value, usually 'ctrl')",
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
        cooldown_seconds=getattr(cfg.tts, "cooldown_seconds", 1.5),
    )

    ptt_cfg = SimpleNamespace(
        enabled=bool(args.push_to_talk or getattr(cfg.push_to_talk, "enabled", False)),
        key=args.ptt_key or getattr(cfg.push_to_talk, "key", "ctrl"),
    )
    if ptt_cfg.enabled:
        log.info(
            "Push-to-talk: %s (hold the %r key to be heard)",
            "enabled via CLI" if args.push_to_talk else "enabled via config",
            ptt_cfg.key,
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

    mode = args.mode
    use_voice = mode in ("voice", "both")
    use_gesture = mode in ("gesture", "both")

    if args.show_preview and not use_gesture:
        print(
            "WARNING: --show-preview has no effect without gesture control "
            "(use --mode gesture or --mode both).",
            file=sys.stderr,
        )

    if args.single:
        args.continuous = False

    if not use_voice and (args.record_test or args.energy_test or args.train_wake):
        print(
            "ERROR: --record-test, --energy-test and --train-wake require voice mode "
            "(use --mode voice).",
            file=sys.stderr,
        )
        return

    wake_cfg = build_wake_cfg(args, cfg, log) if use_voice else None

    if tts_enabled and not HAS_TTS and args.tts_engine in (None, "auto", "pyttsx3"):
        log.warning("tts is enabled in config but pyttsx3 is not installed; install with: pip install pyttsx3")

    check_required_dependencies(log, mode=mode)

    controller = build_controller(args.player)
    recognizer_type = args.recognizer or getattr(cfg.recognizer, "engine", "auto")

    listener = (
        VoiceListener(
            timeout=cfg.voice.timeout_seconds,
            phrase_time_limit=cfg.voice.phrase_time_limit,
            recognizer_type=recognizer_type,
            vosk_model_path=getattr(cfg.recognizer, "vosk_model_path", None),
            device_index=args.mic_index,
            energy_threshold=args.set_energy,
        )
        if use_voice
        else None
    )

    gesture_cfg = getattr(cfg, "gesture", None) if use_gesture else None

    camera_id = args.camera
    if camera_id is None:
        camera_id = getattr(gesture_cfg, "camera_id", 0) if gesture_cfg else 0
    camera_id = int(camera_id)

    gesture = (
        GestureController(
            camera_id=camera_id,
            show_preview=args.show_preview,
            debounce_frames=getattr(gesture_cfg, "debounce_frames", 3) if gesture_cfg else 3,
            cooldown_seconds=getattr(gesture_cfg, "cooldown_seconds", 0.5) if gesture_cfg else 0.5,
            model_path=getattr(gesture_cfg, "model_path", None) if gesture_cfg else None,
            swipe_threshold=getattr(gesture_cfg, "swipe_threshold", None) if gesture_cfg else None,
        )
        if use_gesture
        else None
    )
    if gesture is not None and not gesture.available:
        log.error("Gesture control unavailable: %s", gesture.error)
        print(f"ERROR: Gesture control is unavailable: {gesture.error}", file=sys.stderr)
        if mode == "gesture":
            print(
                "  Install the optional dependencies (pip install -r requirements.txt) or "
                "check the webcam, or use --mode voice to fall back to voice control.",
                file=sys.stderr,
            )
            return
        log.warning("Continuing with voice-only input")
        mode = "voice"
        use_gesture = False
        gesture = None

    if args.record_test:
        run_record_test(listener)
        return

    if args.energy_test:
        run_energy_test(listener)
        return

    if args.train_wake:
        run_wake_training(listener)
        return

    if args.debug and listener is not None:
        mic_info = listener.get_microphone_info()
        if mic_info:
            print(
                f"Mic: index={mic_info['index']} sample_rate={mic_info['sample_rate']} "
                f"name={mic_info['name']!r}"
            )
        else:
            print("Mic info unavailable")
    if args.debug and gesture is not None:
        print(f"Camera: index={gesture.camera_id} available={gesture.available}")

    state = {
        "listening": True,
        "armed": False,
        "armed_at": 0.0,
        "indicators": bool(args.tray and args.debug),
        "tts_cooldown_until": 0.0,
        "stop_event": threading.Event(),
        "playing": False,
    }

    if mode == "gesture":
        startup = f"Starting gesture-controlled media player (camera={camera_id}, player={args.player})"
        hint = f"camera={camera_id}"
    elif mode == "voice":
        startup = f"Starting voice-controlled media player (player={args.player}, recognizer={recognizer_type})"
        hint = f"recognizer={recognizer_type}"
    else:
        startup = (
            f"Starting voice + gesture-controlled media player "
            f"(camera={camera_id}, player={args.player}, recognizer={recognizer_type})"
        )
        hint = f"camera={camera_id}, recognizer={recognizer_type}"
    log.info("%s", startup)

    if args.tray and (not HAS_TRAY or not tray._tray_available()):
        log.warning("pystray is not installed; falling back to terminal mode")
        args.tray = False

    if not args.tray:
        print(f"{('Gesture control' if mode != 'voice' else 'Listening')}... {hint}, player={args.player}. Press Ctrl+C to stop.")

    try:
        if args.tray:
            run_tray(
                listener,
                gesture,
                controller,
                log,
                tts_cfg,
                wake_cfg,
                state,
                hotkey=args.hotkey,
                ptt_cfg=ptt_cfg,
                mode=mode,
            )
        elif args.continuous:
            run_continuous_mode(mode, listener, gesture, controller, log, tts_cfg, wake_cfg, state, ptt_cfg=ptt_cfg)
        elif use_gesture:
            run_once_gesture(gesture, controller, log, tts_cfg, state)
        else:
            run_once(listener, controller, log, tts_cfg, wake_cfg, state)
    except KeyboardInterrupt:
        log.info("Shutdown requested (Ctrl+C)")
    finally:
        state["stop_event"].set()
        if listener is not None:
            listener.stop()
        if gesture is not None:
            gesture.stop()
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()