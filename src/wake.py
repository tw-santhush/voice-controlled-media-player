"""Porcupine wake-word detection with graceful fallback.

Wraps the optional `pvporcupine` package (Picovoice). When Porcupine is not
installed, has no access key, or fails to initialize, `available` is False and
the caller falls back to the config-driven string wake phrases.
"""

import logging
import struct
import time

DEFAULT_KEYWORDS = ["porcupine", "hey google", "alexa"]

logger = logging.getLogger(__name__)


class PorcupineWakeWord:
    """Detect one of several Porcupine keywords on the default microphone."""

    def __init__(
        self,
        access_key=None,
        keywords=None,
        keyword_paths=None,
        sample_rate=None,
        frame_length=None,
        device_index=None,
        log=None,
        enabled=True,
    ):
        self.log = log or logger
        self.access_key = access_key or _env_access_key()
        self.keywords = list(keywords) if keywords else list(DEFAULT_KEYWORDS)
        self.keyword_paths = list(keyword_paths) if keyword_paths else []
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.frame_length = frame_length
        self._porcupine = None
        self._pa = None
        self._stream = None
        self._error = None
        if enabled:
            self._initialize()

    @property
    def available(self) -> bool:
        """True when Porcupine is active and can process wake word audio."""
        return self._porcupine is not None

    @property
    def error(self) -> str | None:
        """Human-readable reason Porcupine is unavailable, if any."""
        return self._error

    def _initialize(self) -> None:
        try:
            import pvporcupine
        except ImportError as exc:
            self._error = "pvporcupine is not installed (pip install pvporcupine)"
            self.log.info("%s; using string wake-word fallback", self._error)
            return
        if not self.access_key:
            self._error = (
                "no Picovoice access key; set wake.porcupine_access_key in config.json "
                "or the PICOVOICE_ACCESS_KEY environment variable"
            )
            self.log.info("%s; using string wake-word fallback", self._error)
            return
        try:
            kwargs = {"access_key": self.access_key}
            if self.keyword_paths:
                kwargs["keyword_paths"] = self.keyword_paths
            else:
                kwargs["keywords"] = self.keywords
            self._porcupine = pvporcupine.create(**kwargs)
            self.sample_rate = self.sample_rate or getattr(self._porcupine, "sample_rate", 16000)
            self.frame_length = self.frame_length or getattr(self._porcupine, "frame_length", 512)
            self._error = None
            self.log.info("Porcupine wake word ready (keywords=%s)", self.keywords or self.keyword_paths)
        except Exception as exc:
            self._error = str(exc)
            self.log.warning("Porcupine initialization failed: %s", exc)

    def process(self, samples) -> str | None:
        """Feed a frame of `frame_length` int16 samples; return the keyword name on a hit."""
        if not self.available:
            return None
        index = self._porcupine.process(samples)
        if index < 0:
            return None
        return self._keyword_label(index)

    def wait_for_wake_word(self, timeout=None, stop_event=None, log=None) -> str | None:
        """Block listening for a wake word.

        Returns the detected keyword name, or None on timeout, stop, or error.
        The microphone stream is owned by Porcupine while this runs and is
        released (closed) before returning so the recognizer can open the mic.
        """
        if not self.available:
            return None
        active_log = log or self.log
        if not self._open_stream():
            return None
        try:
            start = time.monotonic()
            while True:
                if stop_event is not None and stop_event.is_set():
                    return None
                if timeout is not None and time.monotonic() - start > timeout:
                    return None
                try:
                    data = self._stream.read(self.frame_length, exception_on_overflow=False)
                except Exception as exc:
                    active_log.warning("Wake-word microphone read failed (%s); retrying", exc)
                    time.sleep(0.05)
                    continue
                keyword = self.process(struct.unpack(f"<{self.frame_length}h", data))
                if keyword is not None:
                    return keyword
        finally:
            self._close_stream()

    def _keyword_label(self, index: int) -> str:
        names = self.keyword_paths or self.keywords
        if 0 <= index < len(names):
            return names[index]
        return f"wake-word-{index}"

    def _open_stream(self) -> bool:
        try:
            import pyaudio
        except ImportError as exc:
            self.log.warning("pyaudio is not installed; cannot listen for wake words (%s)", exc)
            return False
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                rate=self.sample_rate,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=self.frame_length,
                input_device_index=self.device_index,
            )
            self._pa = pa
            self._stream = stream
            return True
        except Exception as exc:
            self.log.warning("Could not open the microphone for wake-word detection: %s", exc)
            try:
                if self._pa is not None:
                    self._pa.terminate()
            except Exception:
                pass
            self._pa = None
            self._stream = None
            return False

    def _close_stream(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    def close(self) -> None:
        self._close_stream()
        self._porcupine = None


def _env_access_key():
    import os

    return os.environ.get("PICOVOICE_ACCESS_KEY")


def build_porcupine(cfg, log=None, device_index=None, enabled=True) -> PorcupineWakeWord:
    """Create a PorcupineWakeWord from the wake config section, or None-equivalent."""
    wake = getattr(cfg, "wake", None)
    if wake is None:
        return PorcupineWakeWord(enabled=False, log=log)
    access_key = getattr(cfg.wake, "porcupine_access_key", None) or _env_access_key()
    return PorcupineWakeWord(
        access_key=access_key,
        keywords=getattr(cfg.wake, "porcupine_keywords", None),
        keyword_paths=getattr(cfg.wake, "porcupine_keyword_paths", None),
        device_index=device_index,
        log=log,
        enabled=enabled,
    )