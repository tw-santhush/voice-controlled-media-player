import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_engine = None


def _get_engine(voice_id=None):
    """Return the lazily-initialized pyttsx3 engine (singleton)."""
    global _engine
    if _engine is None:
        try:
            import pyttsx3

            _engine = pyttsx3.init()
            if voice_id is not None:
                _engine.setProperty("voice", voice_id)
        except Exception as exc:
            logger.warning("pyttsx3 TTS is unavailable: %s", exc)
            _engine = False
    elif _engine is not False and voice_id is not None:
        try:
            _engine.setProperty("voice", voice_id)
        except Exception as exc:
            logger.warning("Failed to set TTS voice '%s': %s", voice_id, exc)
    return _engine if _engine is not False else None


def _speak_engine(text: str, voice_id=None) -> bool:
    engine = _get_engine(voice_id)
    if engine is None:
        return False
    try:
        engine.say(text)
        engine.runAndWait()
        return True
    except Exception as exc:
        logger.warning("pyttsx3 failed to speak: %s", exc)
        return False


def _escape_powershell(text: str) -> str:
    return text.replace("'", "''")


def _powershell_speak(text: str) -> bool:
    command = (
        "Add-Type -AssemblyName System.Speech; "
        f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{_escape_powershell(text)}')"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("PowerShell (SAPI) fallback failed to speak: %s", exc)
        return False


def _say_speak(text: str) -> bool:
    try:
        subprocess.run(["say", text], capture_output=True, text=True, timeout=45, check=True)
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("`say` fallback failed to speak: %s", exc)
        return False


def _espeak_speak(text: str) -> bool:
    for command in (["espeak", text], ["spd-say", text]):
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
            return True
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("%s failed: %s", command[0], exc)
    logger.warning("No Linux TTS fallback (espeak/spd-say) could speak")
    return False


def _system_fallback(text: str) -> bool:
    """Speak using the OS's own TTS tool, if available."""
    if os.name == "nt":
        return _powershell_speak(text)
    if sys.platform == "darwin":
        return _say_speak(text)
    return _espeak_speak(text)


def available() -> bool:
    """Return True if any TTS mechanism is available."""
    return _get_engine() is not None or os.name == "nt" or sys.platform == "darwin"


def speak(text: str, voice_id=None, engine: str = "auto", fallback_enabled: bool = True) -> bool:
    """Speak the given text aloud. Returns True on success, False otherwise.

    engine: 'auto' (pyttsx3, then OS fallback), 'pyttsx3', or one of
    'powershell', 'say', 'espeak', 'system'.
    """
    if not text:
        return False
    if engine == "powershell":
        return _powershell_speak(text)
    if engine == "say":
        return _say_speak(text)
    if engine == "espeak":
        return _espeak_speak(text)
    if engine == "system":
        return _system_fallback(text)

    if _speak_engine(text, voice_id):
        return True

    if engine == "pyttsx3" or not fallback_enabled:
        return False
    logger.info("pyttsx3 unavailable; using system TTS fallback")
    return _system_fallback(text)