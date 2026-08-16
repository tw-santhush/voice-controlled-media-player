import json
import logging
import shutil
import struct
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

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

DEFAULT_VOSK_MODEL_DIR = Path.home() / ".cache" / "vosk" / "model-en-us"
VOSK_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"


def _require_speech_recognition() -> None:
    if sr is None:
        raise RuntimeError(
            "speech_recognition is not installed. Activate your virtual environment "
            "and run: pip install -r requirements.txt"
        )


def _rms(raw: bytes) -> float:
    """Return the root-mean-square amplitude of 16-bit little-endian PCM samples."""
    if not raw:
        return 0.0
    usable = len(raw) - (len(raw) % 2)
    if usable == 0:
        return 0.0
    samples = struct.unpack(f"<{usable // 2}h", raw[:usable])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5


def _peak(raw: bytes) -> int:
    """Return the peak (maximum absolute) 16-bit little-endian PCM sample."""
    if not raw:
        return 0
    usable = len(raw) - (len(raw) % 2)
    if usable == 0:
        return 0
    samples = struct.unpack(f"<{usable // 2}h", raw[:usable])
    return max(abs(s) for s in samples) if samples else 0


def _log_audio_stats(audio, energy_threshold=None) -> None:
    try:
        sample_rate = getattr(audio, "sample_rate", None)
        raw = audio.get_raw_data()
        samples = len(raw) // 2
        duration = (len(raw) / (2 * sample_rate)) if sample_rate else 0.0
        amplitude = _rms(raw)
        peak = _peak(raw)
        logger.debug(
            "Audio stats: duration=%.2fs samples=%d sample_rate=%s rms=%.0f peak=%d "
            "energy_threshold=%s",
            duration,
            samples,
            sample_rate,
            amplitude,
            peak,
            energy_threshold,
        )
    except Exception:
        logger.debug("Could not compute audio stats", exc_info=True)


def _ensure_vosk_model(path) -> Path:
    """Return a usable Vosk model directory at `path`, downloading it if needed."""
    path = Path(path)
    if (path / "am" / "final.mdl").exists():
        return path
    logger.warning("Vosk model not found at %s; downloading the small English model...", path)
    return _download_vosk_model(path)


def _download_vosk_model(path: Path) -> Path:
    archive = path.parent / "vosk-model-small-en-us-0.15.zip"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Downloading %s -> %s", VOSK_MODEL_URL, archive)
        with urllib.request.urlopen(VOSK_MODEL_URL, timeout=120) as resp:
            with open(archive, "wb") as fh:
                shutil.copyfileobj(resp, fh)
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            top = names[0].split("/")[0]
            extracted = path.parent / top
            if extracted.exists():
                shutil.rmtree(extracted)
            zf.extractall(path.parent)
        if extracted != path:
            if path.exists():
                shutil.rmtree(path)
            extracted.rename(path)
        logger.info("Vosk model ready at %s", path)
        return path
    except Exception as exc:
        raise LookupError(f"Could not download Vosk model to {path}: {exc}") from exc


def list_microphones() -> list[dict]:
    """Enumerate audio input devices with pyaudio, falling back to SpeechRecognition."""
    devices: list[dict] = []
    try:
        import pyaudio
    except ImportError:
        pyaudio = None

    if pyaudio is not None:
        try:
            pa = pyaudio.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        devices.append(
                            {
                                "index": i,
                                "name": info.get("name"),
                                "sample_rate": info.get("defaultSampleRate"),
                            }
                        )
            finally:
                pa.terminate()
            return devices
        except Exception as exc:
            logger.warning("Could not enumerate microphones with pyaudio: %s", exc)

    if sr is not None:
        try:
            names = sr.Microphone.list_microphone_names()
            return [{"index": i, "name": name, "sample_rate": None} for i, name in enumerate(names)]
        except Exception as exc:
            logger.warning("Could not list microphones: %s", exc)
    return devices


class VoiceListener:
    """Handles microphone input and speech recognition."""

    def __init__(
        self,
        recognizer=None,
        source=None,
        timeout=None,
        phrase_time_limit=None,
        recognizer_type="google",
        vosk_model_path=None,
        device_index=None,
        energy_threshold=None,
        dynamic_energy_threshold=None,
        noise_gate_enabled=None,
        noise_gate_threshold=None,
        confidence_threshold=None,
        push_to_talk_enabled=None,
        push_to_talk_key=None,
    ):
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
        self.recognizer_type = recognizer_type or "google"
        self.vosk_model_path = Path(vosk_model_path) if vosk_model_path else DEFAULT_VOSK_MODEL_DIR
        self.device_index = device_index
        self.energy_threshold = (
            energy_threshold
            if energy_threshold is not None
            else getattr(cfg.voice, "energy_threshold", 20)
        )
        self.dynamic_energy_threshold = (
            dynamic_energy_threshold
            if dynamic_energy_threshold is not None
            else getattr(cfg.voice, "dynamic_energy_threshold", True)
        )
        self.noise_gate_enabled = (
            noise_gate_enabled
            if noise_gate_enabled is not None
            else getattr(cfg.voice, "noise_gate_enabled", False)
        )
        self.noise_gate_threshold = (
            noise_gate_threshold
            if noise_gate_threshold is not None
            else getattr(cfg.voice, "noise_gate_threshold", 10.0)
        )
        self.confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else getattr(cfg.voice, "confidence_threshold", 0.5)
        )
        ptt = getattr(cfg, "push_to_talk", None)
        self.push_to_talk_enabled = (
            push_to_talk_enabled
            if push_to_talk_enabled is not None
            else bool(getattr(ptt, "enabled", False))
        )
        self.push_to_talk_key = (
            push_to_talk_key
            if push_to_talk_key is not None
            else getattr(ptt, "key", "ctrl")
        )
        self.cooldown_until = 0.0
        self._paused = False
        recognizer.energy_threshold = self.energy_threshold
        recognizer.dynamic_energy_threshold = self.dynamic_energy_threshold
        self._model = None
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
                source = sr.Microphone(device_index=self.device_index)
            with source as mic:
                if self.dynamic_energy_threshold:
                    self.recognizer.adjust_for_ambient_noise(mic, duration=0.5)
                logger.debug(
                    "Audio energy: threshold=%s dynamic_threshold=%s",
                    getattr(self.recognizer, "energy_threshold", None),
                    getattr(self.recognizer, "dynamic_energy_threshold", None),
                )
                logger.debug(
                    "Mic: sample_rate=%s chunk_size=%s device_index=%s",
                    getattr(mic, "sample_rate", None),
                    getattr(mic, "chunk_size", None),
                    self.device_index,
                )
                audio = self.recognizer.listen(mic, timeout=timeout, phrase_time_limit=phrase_time_limit)
            _log_audio_stats(audio, getattr(self.recognizer, "energy_threshold", None))
            if self.noise_gate_enabled:
                amplitude = _rms(audio.get_raw_data())
                if amplitude < self.noise_gate_threshold:
                    logger.info(
                        "Noise gate: ignoring audio (rms=%.1f below threshold %.1f)",
                        amplitude,
                        self.noise_gate_threshold,
                    )
                    return None
            text, confidence = self._recognize(audio)
            logger.debug("Raw recognized text: %r (confidence=%s)", text, confidence)
            if not self._passes_confidence(text, confidence):
                return None
            return (text or "").strip().lower()
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

    def capture_audio(self, duration: float = 3.0):
        """Record raw audio for `duration` seconds and return the SpeechRecognition AudioData.

        No speech detection or ambient-noise adjustment is applied; this captures
        whatever the microphone hears (used by --record-test).
        """
        _require_speech_recognition()
        source = self.source
        if source is None:
            source = sr.Microphone(device_index=self.device_index)
        with source as mic:
            return self.recognizer.record(mic, duration=duration)

    def measure_energy(self, duration: float = 5.0, interval: float = 0.5) -> list[float]:
        """Sample the RMS energy of the microphone every `interval` seconds over `duration`.

        Returns one RMS reading per interval so callers can tune the
        recognizer's energy_threshold (used by --energy-test).
        """
        _require_speech_recognition()
        source = self.source
        if source is None:
            source = sr.Microphone(device_index=self.device_index)
        energies: list[float] = []
        with source as mic:
            elapsed = 0.0
            while elapsed < duration:
                chunk = min(interval, duration - elapsed)
                audio = self.recognizer.record(mic, duration=chunk)
                energies.append(_rms(audio.get_raw_data()))
                elapsed += chunk
        return energies

    def _recognize(self, audio):
        """Recognize audio, returning a (text, confidence) pair.

        `confidence` is the recognizer's reported confidence (0..1) when
        available, otherwise None.
        """
        if self.recognizer_type in ("vosk", "auto"):
            try:
                text, confidence = self._recognize_vosk(audio)
                if text:
                    return text, confidence
                logger.warning("Vosk returned no text; falling back to Google")
            except LookupError as exc:
                if self.recognizer_type == "vosk":
                    logger.warning("Vosk unavailable (%s); falling back to Google", exc)
                else:
                    logger.debug("Vosk unavailable (%s); using Google", exc)
        return self._recognize_google(audio)

    def _recognize_vosk(self, audio):
        try:
            import vosk
        except ImportError as exc:
            raise LookupError("vosk package is not installed; run: pip install vosk") from exc
        if self._model is None:
            model_path = _ensure_vosk_model(self.vosk_model_path)
            self._model = vosk.Model(str(model_path))
        sample_rate = getattr(audio, "sample_rate", 16000)
        recognizer = vosk.KaldiRecognizer(self._model, sample_rate)
        recognizer.AcceptWaveform(audio.get_raw_data())
        result = json.loads(recognizer.FinalResult())
        text = (result.get("text") or "").strip().lower()
        confidence = result.get("confidence")
        return text, confidence

    def _recognize_google(self, audio):
        recognizer = self.recognizer
        if self.confidence_threshold:
            try:
                result = recognizer.recognize_google(audio, show_all=True)
            except TypeError:
                text = recognizer.recognize_google(audio)
                return (text or "").strip().lower(), None
            if isinstance(result, dict):
                alternatives = result.get("alternative") or []
                if not alternatives:
                    return "", None
                best = alternatives[0] or {}
                return (
                    (best.get("transcript") or "").strip().lower(),
                    best.get("confidence"),
                )
            if isinstance(result, list) and result:
                best = result[0] or {}
                return (
                    (best.get("transcript") or "").strip().lower(),
                    best.get("confidence"),
                )
        text = recognizer.recognize_google(audio)
        return (text or "").strip().lower(), None

    def _passes_confidence(self, text, confidence):
        """Return True if the recognized text should be acted upon.

        When `confidence_threshold` is set and the recognizer reports a
        confidence score, that score must meet the threshold. Recognizers that
        do not report confidence fall back to a heuristic: very short
        transcripts are treated as likely mis-heard noise. A threshold of 0
        disables confidence filtering entirely.
        """
        threshold = self.confidence_threshold
        if not threshold:
            return True
        if confidence is not None:
            if confidence < threshold:
                logger.info("Low confidence: %.2f below %.2f for %r", confidence, threshold, text)
                return False
            return True
        text = (text or "").strip()
        if len(text) < 3:
            logger.info("Low confidence: transcript too short (%r)", text)
            return False
        return True

    def get_microphone_info(self) -> dict | None:
        """Return device index, sample rate, and name of the selected microphone."""
        try:
            if sr is None:
                return None
            mic = sr.Microphone(device_index=self.device_index)
            index = getattr(mic, "device_index", None)
            if index is None:
                index = self.device_index
            sample_rate = getattr(mic, "sample_rate", None)
            name = None
            try:
                names = sr.Microphone.list_microphone_names()
                if index is not None and 0 <= index < len(names):
                    name = names[index]
            except Exception:
                logger.debug("Could not list microphone names", exc_info=True)
            return {"index": index, "sample_rate": sample_rate, "name": name}
        except Exception as exc:
            logger.warning("Could not query microphone info: %s", exc)
            return None

    def listen_loop(self, callback, stop_event=None, timeout=None) -> None:
        """Start continuous listening in a background thread.

        Args:
            callback: called with each recognized text string.
            stop_event: threading.Event that stops the loop when set. If None,
                the listener's own internal stop event is used.
            timeout: the per-listening-attempt timeout (None = the configured value).
        The loop applies the noise gate (within ``listen_once``), confidence
        filtering, the TTS cooldown, and push-to-talk gating internally.
        """
        if stop_event is not None:
            self._stop_event = stop_event
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
            if self._paused or not self._should_listen():
                self._stop_event.wait(0.25)
                continue
            text = self.listen_once(timeout=timeout)
            if text:
                callback(text)

    def pause(self) -> None:
        """Pause listening without closing the microphone or the loop."""
        self._paused = True

    def resume(self) -> None:
        """Resume listening after pause()."""
        self._paused = False

    def set_cooldown(self, seconds: float) -> None:
        """Ignore incoming audio for `seconds` (e.g. while TTS is speaking)."""
        self.cooldown_until = time.time() + max(0.0, seconds or 0.0)

    def _in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    def _should_listen(self) -> bool:
        """Return False when a listen attempt should be skipped.

        Applies the TTS cooldown and push-to-talk gate before each attempt.
        The noise gate and confidence filtering are applied per-attempt inside
        ``listen_once``.
        """
        if self._in_cooldown():
            logger.debug("Ignoring audio: TTS cooldown active")
            return False
        if self.push_to_talk_enabled:
            try:
                import keyboard as _keyboard
            except ImportError:
                logger.warning(
                    "push_to_talk is enabled but the keyboard module is missing; ignoring PTT"
                )
                return True
            try:
                if not _keyboard.is_pressed(self.push_to_talk_key):
                    logger.debug(
                        "Ignoring audio: push-to-talk key %r not held", self.push_to_talk_key
                    )
                    return False
            except Exception:
                logger.debug("Could not read push-to-talk key state", exc_info=True)
                return False
        return True

    def wait_stop(self, timeout=0.5) -> bool:
        """Block until stop() is called or the timeout elapses."""
        return self._stop_event.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()