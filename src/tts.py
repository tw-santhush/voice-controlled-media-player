import logging

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
            logger.warning("TTS is unavailable and will be skipped: %s", exc)
            _engine = False
    elif _engine is not False and voice_id is not None:
        try:
            _engine.setProperty("voice", voice_id)
        except Exception as exc:
            logger.warning("Failed to set TTS voice '%s': %s", voice_id, exc)
    return _engine if _engine is not False else None


def available() -> bool:
    """Return True if a TTS engine could be initialized."""
    return _get_engine() is not None


def speak(text: str, voice_id=None) -> bool:
    """Speak the given text aloud. Returns True on success, False otherwise."""
    if not text:
        return False
    engine = _get_engine(voice_id)
    if engine is None:
        return False
    try:
        engine.say(text)
        engine.runAndWait()
        return True
    except Exception as exc:
        logger.warning("TTS failed while speaking: %s", exc)
        return False