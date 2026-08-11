import logging
import threading

try:
    import speech_recognition as sr
except ImportError:
    sr = None

import config

logger = logging.getLogger(__name__)

if sr is not None:
    WaitTimeoutError = sr.WaitTimeoutError
    UnknownValueError = sr.UnknownValueError
    RequestError = sr.RequestError
else:
    WaitTimeoutError = RuntimeError
    UnknownValueError = RuntimeError
    RequestError = RuntimeError


def _require_speech_recognition() -> None:
    if sr is None:
        raise RuntimeError(
            "speech_recognition is not installed. Activate your virtual environment "
            "and run: pip install -r requirements.txt"
        )


class VoiceListener:
    """Handles microphone input and speech recognition."""

    def __init__(self, recognizer=None, source=None, timeout=None, phrase_time_limit=None):
        if recognizer is None:
            _require_speech_recognition()
            recognizer = sr.Recognizer()
        cfg = config.get_config()
        self.timeout = timeout if timeout is not None else cfg.voice.timeout_seconds
        self.phrase_time_limit = (
            phrase_time_limit
            if phrase_time_limit is not None
            else cfg.voice.phrase_time_limit
        )
        self.recognizer = recognizer
        self.source = source
        self._stop_event = threading.Event()
        self._thread = None

    def listen_once(self, timeout=None, phrase_time_limit=None) -> str | None:
        """Listen for a single utterance and return the recognized text."""
        try:
            timeout = self.timeout if timeout is None else timeout
            phrase_time_limit = (
                self.phrase_time_limit if phrase_time_limit is None else phrase_time_limit
            )
            source = self.source
            if source is None:
                _require_speech_recognition()
                source = sr.Microphone()
            with source as mic:
                self.recognizer.adjust_for_ambient_noise(mic, duration=0.5)
                audio = self.recognizer.listen(mic, timeout=timeout, phrase_time_limit=phrase_time_limit)
            text = self.recognizer.recognize_google(audio)
            return text.strip().lower()
        except WaitTimeoutError:
            return None
        except UnknownValueError:
            logger.warning("Could not understand the audio")
            return None
        except RequestError as exc:
            logger.error("Speech recognition service error: %s", exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected error during voice recognition: %s", exc)
            return None

    def listen_loop(self, callback, timeout=None) -> None:
        """Start continuous listening in a background thread."""
        self._stop_event.clear()
        timeout = self.timeout if timeout is None else timeout
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(callback, timeout),
            name="voice-listener",
            daemon=True,
        )
        self._thread.start()

    def _run_loop(self, callback, timeout) -> None:
        while not self._stop_event.is_set():
            text = self.listen_once(timeout=timeout)
            if text:
                callback(text)

    def wait_stop(self, timeout=0.5) -> bool:
        """Block until stop() is called or the timeout elapses."""
        return self._stop_event.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()