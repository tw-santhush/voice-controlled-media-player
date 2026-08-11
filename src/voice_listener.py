import json
import logging
import shutil
import struct
import threading
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


def _log_audio_stats(audio) -> None:
    try:
        sample_rate = getattr(audio, "sample_rate", None)
        raw = audio.get_raw_data()
        duration = (len(raw) / (2 * sample_rate)) if sample_rate else 0.0
        amplitude = _rms(raw)
        logger.debug(
            "Audio stats: duration=%.2fs average_amplitude=%.0f sample_rate=%s",
            duration,
            amplitude,
            sample_rate,
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
            _log_audio_stats(audio)
            text = self._recognize(audio)
            logger.debug("Raw recognized text: %r", text)
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

    def _recognize(self, audio) -> str:
        if self.recognizer_type in ("vosk", "auto"):
            try:
                text = self._recognize_vosk(audio)
                if text:
                    return text
                logger.warning("Vosk returned no text; falling back to Google")
            except LookupError as exc:
                if self.recognizer_type == "vosk":
                    logger.warning("Vosk unavailable (%s); falling back to Google", exc)
                else:
                    logger.debug("Vosk unavailable (%s); using Google", exc)
        return self.recognizer.recognize_google(audio)

    def _recognize_vosk(self, audio) -> str:
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
        return (result.get("text") or "").strip().lower()

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