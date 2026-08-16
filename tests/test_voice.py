import json
import logging
import os
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import gesture
import main
import requests
import speech_recognition as sr
import tray
import tts
import voice_listener
import wake
from player_control import AutoController, MPCController, PlayerController, VLCController
from voice_listener import VoiceListener


class FakeAudio:
    sample_rate = 16000

    def get_raw_data(self):
        return b"\x00\x00" * 1600


class FakeRecognizer:
    """Recognizer stub that simulates the microphone pipeline."""

    def __init__(self, text):
        self._text = text

    def adjust_for_ambient_noise(self, source, duration=0.5):
        pass

    def listen(self, source, timeout=None, phrase_time_limit=None):
        return FakeAudio()

    def recognize_google(self, audio):
        text = self._text
        if isinstance(text, Exception):
            raise text
        if text is None:
            raise RuntimeError("no speech detected")
        return text


class FakeMicSource:
    recording = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _MultiChunkAudio:
    sample_rate = 16000

    def __init__(self, data):
        self._data = data

    def get_raw_data(self):
        return self._data


class _RecordRecognizer:
    """Recognizer stub that records fixed-tone audio in chunks."""

    def __init__(self):
        self.durations = []

    def adjust_for_ambient_noise(self, source, duration=0.5):
        pass

    def record(self, source, duration=None):
        self.durations.append(duration)
        return _MultiChunkAudio(struct.pack("<100h", *([100] * 100)))


class TestCommandMapping(unittest.TestCase):
    def test_every_mapped_phrase_resolves_to_its_action(self):
        for action, phrases in config.COMMAND_MAPPINGS.items():
            for phrase in phrases:
                with self.subTest(action=action, phrase=phrase):
                    self.assertEqual(config.match_command(phrase), action)

    def test_unknown_phrase_returns_none(self):
        self.assertIsNone(config.match_command("what is the weather like"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(config.match_command(""))
        self.assertIsNone(config.match_command(None))

    def test_longest_match_wins(self):
        self.assertEqual(config.match_command("stop playing"), "pause")

    def test_punctuation_is_ignored(self):
        self.assertEqual(config.match_command("STOP PLAYING!!!"), "pause")


class TestVolumeParsing(unittest.TestCase):
    def test_set_volume_to_number(self):
        self.assertEqual(config.parse_volume_command("set volume to 50"), 50)

    def test_volume_number(self):
        self.assertEqual(config.parse_volume_command("volume 25"), 25)

    def test_change_volume(self):
        self.assertEqual(config.parse_volume_command("change volume to 10"), 10)

    def test_vol_abbreviation(self):
        self.assertEqual(config.parse_volume_command("vol 30"), 30)

    def test_case_and_percent_ignored(self):
        self.assertEqual(config.parse_volume_command("SET VOLUME TO 80%"), 80)

    def test_above_100_clamped(self):
        self.assertEqual(config.parse_volume_command("volume 150"), 100)

    def test_negative_or_zero_none(self):
        self.assertEqual(config.parse_volume_command("volume -5"), 5)
        self.assertIsNone(config.parse_volume_command("volume zero"))

    def test_no_number_returns_none(self):
        self.assertIsNone(config.parse_volume_command("volume up"))
        self.assertIsNone(config.parse_volume_command("volume"))

    def test_no_keyword_returns_none(self):
        self.assertIsNone(config.parse_volume_command("play"))
        self.assertIsNone(config.parse_volume_command("next track"))
        self.assertIsNone(config.parse_volume_command("skip ahead 10"))

    def test_empty_input_returns_none(self):
        self.assertIsNone(config.parse_volume_command(""))
        self.assertIsNone(config.parse_volume_command(None))


class TestVoiceListener(unittest.TestCase):
    def test_listen_once_returns_recognized_text(self):
        listener = VoiceListener(
            recognizer=FakeRecognizer("volume up"),
            source=FakeMicSource(),
            noise_gate_enabled=False,
        )
        self.assertEqual(listener.listen_once(), "volume up")

    def test_listen_once_unknown_value_returns_none(self):
        listener = VoiceListener(recognizer=FakeRecognizer(sr.UnknownValueError()), source=FakeMicSource())
        self.assertIsNone(listener.listen_once())

    def test_listen_once_request_error_returns_none(self):
        listener = VoiceListener(recognizer=FakeRecognizer(sr.RequestError("boom")), source=FakeMicSource())
        self.assertIsNone(listener.listen_once())

    def test_listen_loop_calls_callback_with_commands(self):
        class StoppingListener(VoiceListener):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.count = 0

            def listen_once(self, timeout=5):
                self.count += 1
                if self.count > 2:
                    self.stop()
                    return None
                return "volume down"

        calls = []
        listener = StoppingListener(recognizer=FakeRecognizer("play"), source=FakeMicSource())
        listener._run_loop(calls.append, 1)
        self.assertEqual(calls, ["volume down", "volume down"])

    def test_pause_blocks_listening_and_resume_restores(self):
        class TrackListener(VoiceListener):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.count = 0

            def listen_once(self, timeout=None):
                self.count += 1
                self.stop()
                return None

        listener = TrackListener(recognizer=FakeRecognizer("play"), source=FakeMicSource())
        listener.pause()
        thread = threading.Thread(target=listener._run_loop, args=(lambda text: None, 1), daemon=True)
        thread.start()
        time.sleep(0.2)
        listener.stop()
        thread.join(timeout=2)
        self.assertEqual(listener.count, 0)
        listener.resume()
        listener._stop_event.clear()
        listener._run_loop(lambda text: None, 1)
        self.assertEqual(listener.count, 1)

    def test_set_cooldown_gates_listening(self):
        listener = VoiceListener(recognizer=FakeRecognizer("play"), source=FakeMicSource())
        listener.set_cooldown(60)
        self.assertTrue(listener._in_cooldown())
        self.assertFalse(listener._should_listen())
        listener.set_cooldown(0)
        self.assertFalse(listener._in_cooldown())
        self.assertTrue(listener._should_listen())

    def test_push_to_talk_blocks_when_key_not_held(self):
        fake_kb = types.ModuleType("keyboard")
        fake_kb.is_pressed = MagicMock(return_value=False)
        listener = VoiceListener(
            recognizer=FakeRecognizer("play"),
            source=FakeMicSource(),
            push_to_talk_enabled=True,
            push_to_talk_key="ctrl",
        )
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            self.assertFalse(listener._should_listen())

    def test_push_to_talk_allows_when_key_held(self):
        fake_kb = types.ModuleType("keyboard")
        fake_kb.is_pressed = MagicMock(return_value=True)
        listener = VoiceListener(
            recognizer=FakeRecognizer("play"),
            source=FakeMicSource(),
            push_to_talk_enabled=True,
            push_to_talk_key="ctrl",
        )
        with patch.dict(sys.modules, {"keyboard": fake_kb}):
            self.assertTrue(listener._should_listen())

    def test_energy_threshold_applied_to_recognizer(self):
        cfg = SimpleNamespace(
            voice=SimpleNamespace(
                timeout_seconds=5,
                phrase_time_limit=3,
                energy_threshold=120,
                dynamic_energy_threshold=False,
            )
        )
        with patch("voice_listener.config.get_config", return_value=cfg):
            listener = VoiceListener(recognizer=FakeRecognizer("x"), source=FakeMicSource())
        self.assertEqual(listener.recognizer.energy_threshold, 120)
        self.assertEqual(listener.recognizer.dynamic_energy_threshold, False)

    def test_energy_threshold_override_param(self):
        listener = VoiceListener(
            recognizer=FakeRecognizer("x"),
            source=FakeMicSource(),
            energy_threshold=250,
        )
        self.assertEqual(listener.recognizer.energy_threshold, 250)

    def test_capture_audio_records_requested_duration(self):
        rec = _RecordRecognizer()
        listener = VoiceListener(recognizer=rec, source=FakeMicSource())
        audio = listener.capture_audio(duration=1.0)
        self.assertEqual(rec.durations, [1.0])
        self.assertEqual(audio.sample_rate, 16000)

    def test_measure_energy_samples_per_interval(self):
        rec = _RecordRecognizer()
        listener = VoiceListener(recognizer=rec, source=FakeMicSource())
        levels = listener.measure_energy(duration=1.0, interval=0.25)
        self.assertEqual(rec.durations, [0.25, 0.25, 0.25, 0.25])
        self.assertEqual(levels, [100.0] * 4)


class _VoskRec:
    def __init__(self, text):
        self._text = text

    def AcceptWaveform(self, data):
        return True

    def FinalResult(self):
        return json.dumps({"text": self._text})


def _make_fake_vosk(text="hey player play"):
    module = types.ModuleType("vosk")
    module.Model = lambda path: object()

    def kaldi(model, sample_rate):
        return _VoskRec(text)

    module.KaldiRecognizer = kaldi
    return module


def _vosk_listener(text="hey player play", recognizer_type="vosk", **kwargs):
    fake = _make_fake_vosk(text)
    listener = VoiceListener(
        recognizer=FakeRecognizer("google fallback"),
        source=FakeMicSource(),
        recognizer_type=recognizer_type,
        vosk_model_path="fake-model",
        noise_gate_enabled=False,
        **kwargs,
    )
    patchers = [
        patch.dict(sys.modules, {"vosk": fake}),
        patch("voice_listener._ensure_vosk_model", return_value="fake-model"),
    ]
    for p in patchers:
        p.start()
    return listener, patchers


class TestRecognizerSelection(unittest.TestCase):
    def test_default_uses_google(self):
        listener = VoiceListener(
            recognizer=FakeRecognizer("volume up"),
            source=FakeMicSource(),
            noise_gate_enabled=False,
        )
        self.assertEqual(listener.recognizer_type, "google")
        self.assertEqual(listener.listen_once(), "volume up")

    def test_vosk_used_when_recognizer_type_vosk(self):
        listener, patchers = _vosk_listener("hey player play")
        try:
            self.assertEqual(listener.listen_once(), "hey player play")
        finally:
            for p in patchers:
                p.stop()

    def test_auto_tries_vosk_first(self):
        listener, patchers = _vosk_listener("hey player pause", recognizer_type="auto")
        try:
            self.assertEqual(listener.listen_once(), "hey player pause")
        finally:
            for p in patchers:
                p.stop()

    def test_vosk_missing_falls_back_to_google(self):
        listener = VoiceListener(
            recognizer=FakeRecognizer("volume up"),
            source=FakeMicSource(),
            recognizer_type="vosk",
            vosk_model_path="fake-model",
            noise_gate_enabled=False,
        )
        with patch.dict(sys.modules, {"vosk": None}):
            self.assertEqual(listener.listen_once(), "volume up")

    def test_vosk_model_missing_falls_back_to_google(self):
        listener = VoiceListener(
            recognizer=FakeRecognizer("play"),
            source=FakeMicSource(),
            recognizer_type="auto",
            vosk_model_path="fake-model",
            noise_gate_enabled=False,
        )
        with patch.dict(sys.modules, {"vosk": _make_fake_vosk()}):
            with patch(
                "voice_listener._ensure_vosk_model",
                side_effect=LookupError("no model"),
            ):
                self.assertEqual(listener.listen_once(), "play")

    def test_vosk_empty_result_falls_back_to_google(self):
        listener, patchers = _vosk_listener("", recognizer_type="vosk")
        try:
            self.assertEqual(listener.listen_once(), "google fallback")
        finally:
            for p in patchers:
                p.stop()


class TestAudioDiagnostics(unittest.TestCase):
    def test_rms_of_silence_is_zero(self):
        self.assertEqual(voice_listener._rms(b"\x00\x00" * 100), 0.0)

    def test_rms_of_constant_sample(self):
        self.assertAlmostEqual(voice_listener._rms(b"\x10\x00" * 100), 16.0, places=3)

    def test_peak_of_silence_is_zero(self):
        self.assertEqual(voice_listener._peak(b"\x00\x00" * 100), 0)

    def test_peak_of_constant_sample(self):
        self.assertEqual(voice_listener._peak(b"\x10\x00" * 100), 16)

    def test_peak_of_mixed_samples(self):
        raw = struct.pack("<3h", -300, 120, 2048)
        self.assertEqual(voice_listener._peak(raw), 2048)

    def test_log_audio_stats_reports_samples_and_threshold(self):
        audio = _MultiChunkAudio(b"\x10\x00" * 800)
        with self.assertLogs("voice_listener", level="DEBUG") as cm:
            voice_listener._log_audio_stats(audio, energy_threshold=150)
        text = "\n".join(cm.output)
        self.assertIn("samples=800", text)
        self.assertIn("rms=16", text)
        self.assertIn("energy_threshold=150", text)

    def test_list_microphones_uses_pyaudio(self):
        class FakePa:
            def get_device_count(self):
                return 3

            def get_device_info_by_index(self, i):
                infos = [
                    {"maxInputChannels": 2, "name": "Mic A", "defaultSampleRate": 44100},
                    {"maxInputChannels": 0, "name": "Speakers", "defaultSampleRate": 44100},
                    {"maxInputChannels": 1, "name": "Webcam Mic", "defaultSampleRate": 48000},
                ]
                return infos[i]

        fake_module = types.ModuleType("pyaudio")
        fake_module.PyAudio = lambda: FakePa()
        with patch.dict(sys.modules, {"pyaudio": fake_module}):
            mics = voice_listener.list_microphones()
        self.assertEqual([m["index"] for m in mics], [0, 2])
        self.assertEqual(mics[0]["name"], "Mic A")
        self.assertEqual(mics[1]["sample_rate"], 48000)

    def test_list_microphones_returns_empty_without_audio(self):
        with patch.dict(sys.modules, {"pyaudio": None, "speech_recognition": None}):
            self.assertEqual(voice_listener.list_microphones(), [])

    def test_device_index_is_stored(self):
        listener = VoiceListener(recognizer=FakeRecognizer("x"), source=FakeMicSource(), device_index=2)
        self.assertEqual(listener.device_index, 2)


class TestControllers(unittest.TestCase):
    ACTIONS = (
        "play",
        "pause",
        "stop",
        "skip_forward",
        "skip_backward",
        "volume_up",
        "volume_down",
        "set_volume",
        "toggle_mute",
        "toggle_fullscreen",
        "next",
        "previous",
    )

    def test_all_controllers_expose_expected_actions(self):
        for controller in (VLCController(), MPCController(), AutoController()):
            for action in self.ACTIONS:
                with self.subTest(controller=type(controller).__name__, action=action):
                    self.assertTrue(callable(getattr(controller, action)))

    def test_mpc_controller_sends_wm_command_over_http(self):
        controller = MPCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.play()
        mock_get.assert_called_once()
        url = mock_get.call_args[0][0]
        params = mock_get.call_args[1]["params"]
        self.assertEqual(url, "http://localhost:13579/command.html")
        self.assertEqual(params, {"wm_command": 887})

    def test_mpc_controller_falls_back_to_keyboard_on_http_failure(self):
        controller = MPCController()
        with patch.object(controller.session, "get", side_effect=requests.ConnectionError("refused")):
            with patch.object(controller, "_fallback") as mock_fallback:
                controller.play()
        mock_fallback.assert_called_once_with("play")

    def test_mpc_next_sends_wm_command(self):
        controller = MPCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.next()
        self.assertEqual(mock_get.call_args[1]["params"], {"wm_command": 916})

    def test_mpc_previous_sends_wm_command(self):
        controller = MPCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.previous()
        self.assertEqual(mock_get.call_args[1]["params"], {"wm_command": 915})

    def test_mpc_set_volume_sends_volume_query(self):
        controller = MPCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.set_volume(42)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], "http://localhost:13579/command.html")
        self.assertEqual(mock_get.call_args[1]["params"], {"volume": 42})

    def test_vlc_set_volume_sends_http_command(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.set_volume(75)
        mock_get.assert_called_once()
        self.assertEqual(mock_get.call_args[0][0], "http://localhost:8080/requests/status.xml")
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "volume", "val": 75})

    def test_vlc_set_volume_clamps_to_100(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.set_volume(500)
        self.assertEqual(mock_get.call_args[1]["params"]["val"], 100)

    def test_vlc_next_sends_pl_next(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.next()
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "pl_next"})


class FakeVLC(PlayerController):
    name = "vlc"

    def play(self):
        self.calls.append("vlc")

    def set_volume(self, percent):
        self.calls.append(("set_volume", percent))

    def next(self):
        self.calls.append("vlc next")


class FakeMPC(PlayerController):
    name = "mpc-hc"

    def play(self):
        self.calls.append("mpc-hc")

    def set_volume(self, percent):
        self.calls.append(("set_volume", percent))

    def next(self):
        self.calls.append("mpc next")


class TestAutoController(unittest.TestCase):
    def _auto_with_fakes(self):
        vlc = FakeVLC()
        vlc.calls = []
        mpc = FakeMPC()
        mpc.calls = []
        auto = AutoController(controllers={"vlc": vlc, "mpc-hc": mpc})
        return auto, vlc, mpc

    def test_routes_to_detected_player(self):
        auto, vlc, mpc = self._auto_with_fakes()
        with patch("player_control.detect_active_player", return_value="mpc-hc"):
            auto.play()
        self.assertEqual(mpc.calls, ["mpc-hc"])
        self.assertEqual(vlc.calls, [])

    def test_routes_set_volume_to_detected_player(self):
        auto, vlc, mpc = self._auto_with_fakes()
        with patch("player_control.detect_active_player", return_value="vlc"):
            auto.set_volume(60)
        self.assertEqual(vlc.calls, [("set_volume", 60)])
        self.assertEqual(mpc.calls, [])

    def test_routes_next_to_detected_player(self):
        auto, vlc, mpc = self._auto_with_fakes()
        with patch("player_control.detect_active_player", return_value="mpc-hc"):
            auto.next()
        self.assertEqual(mpc.calls, ["mpc next"])

    def test_warns_when_no_player_running(self):
        auto, vlc, mpc = self._auto_with_fakes()
        with patch("player_control.detect_active_player", return_value=None):
            with patch("player_control.logger") as mock_logger:
                auto.play()
        self.assertTrue(mock_logger.warning.called)
        self.assertEqual(vlc.calls, [])
        self.assertEqual(mpc.calls, [])


class TestWakePhraseHelpers(unittest.TestCase):
    def test_detects_phrase_in_text(self):
        phrase, remainder = config.detect_wake_phrase("hey player play", ["hey player", "player"])
        self.assertEqual(phrase, "hey player")
        self.assertEqual(remainder, "play")

    def test_detects_shortest_phrase_too(self):
        phrase, remainder = config.detect_wake_phrase("player volume up", ["hey player", "player"])
        self.assertEqual(phrase, "player")
        self.assertEqual(remainder, "volume up")

    def test_detects_none_when_absent(self):
        self.assertEqual(config.detect_wake_phrase("just play", ["hey player"]), (None, "just play"))
        self.assertEqual(config.detect_wake_phrase("", ["hey player"]), (None, ""))
        self.assertEqual(config.detect_wake_phrase("play", []), (None, "play"))

    def test_longest_match_wins(self):
        phrase, remainder = config.detect_wake_phrase("hello player play", ["player", "hello player"])
        self.assertEqual(phrase, "hello player")
        self.assertEqual(remainder, "play")

    def test_phrase_requires_word_boundaries(self):
        phrase, remainder = config.detect_wake_phrase("playerunknown play", ["player"])
        self.assertIsNone(phrase)
        self.assertEqual(remainder, "playerunknown play")

    def test_punctuation_and_prefix_words_ok(self):
        phrase, remainder = config.detect_wake_phrase("um, hey player - play", ["hey player", "player"])
        self.assertEqual(phrase, "hey player")
        self.assertEqual(remainder, "um play")

    def test_case_insensitive(self):
        phrase, remainder = config.detect_wake_phrase("HEY PLAYER play volume up", ["hey player"])
        self.assertEqual(phrase, "hey player")
        self.assertEqual(remainder, "play volume up")


class TestWakePhraseVariations(unittest.TestCase):
    def test_hey_stands_alone(self):
        phrase, remainder = config.detect_wake_phrase("hey play", ["hey player", "player", "hey"])
        self.assertEqual(phrase, "hey")
        self.assertEqual(remainder, "play")

    def test_wake_phrase_anywhere_including_end(self):
        phrase, remainder = config.detect_wake_phrase("play hey player", ["hey player", "hey"])
        self.assertEqual(phrase, "hey player")
        self.assertEqual(remainder, "play")

    def test_hey_player_wins_over_hey(self):
        phrase, _ = config.detect_wake_phrase("hey player pause", ["hey player", "hey"])
        self.assertEqual(phrase, "hey player")

    def test_extra_spaces_and_punctuation_stripped(self):
        phrase, remainder = config.detect_wake_phrase(" hey   player !!! pause ", ["hey player"])
        self.assertEqual(phrase, "hey player")
        self.assertEqual(remainder, "pause")

    def test_hey_arms_following_command(self):
        phrases = ["hey player", "hello player", "player", "hey"]
        _, remainder = config.detect_wake_phrase("hey, turn it down", phrases)
        self.assertEqual(remainder, "turn it down")


FAKE_LOG = logging.getLogger("test")


class _FakeCommandController:
    def __init__(self):
        self.calls = []

    def play(self):
        self.calls.append("play")

    def pause(self):
        self.calls.append("pause")

    def volume_up(self):
        self.calls.append("volume_up")


class _FakeStopListener:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class TestWakeIntegration(unittest.TestCase):
    def _setup(self, wake_enabled=True):
        controller = _FakeCommandController()
        listener = _FakeStopListener()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        wake_cfg = SimpleNamespace(
            enabled=wake_enabled,
            phrases=["hey player", "hello player", "player"],
            timeout_seconds=3,
        )
        state = {"armed": False, "armed_at": 0.0}
        return listener, controller, tts_cfg, wake_cfg, state

    def test_wake_phrase_with_command_executes(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        main.process_recognized(listener, controller, "hey player play", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, ["play"])
        self.assertFalse(state["armed"])

    def test_wake_phrase_with_command_in_middle(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        main.process_recognized(listener, controller, "please player pause", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, ["pause"])

    def test_wake_enabled_ignores_plain_command(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        main.process_recognized(listener, controller, "play", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, [])

    def test_wake_phrase_only_arms_for_next_command(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        main.process_recognized(listener, controller, "hey player", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, [])
        self.assertTrue(state["armed"])
        main.process_recognized(listener, controller, "volume up", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, ["volume_up"])
        self.assertFalse(state["armed"])

    def test_armed_command_expires_after_timeout(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        state["armed"] = True
        state["armed_at"] = time.time() - 60
        main.process_recognized(listener, controller, "pause", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, [])
        self.assertFalse(state["armed"])

    def test_wake_disabled_processes_directly(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup(wake_enabled=False)
        main.process_recognized(listener, controller, "play", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, ["play"])


class TestWakeConfig(unittest.TestCase):
    def _cfg(self, enabled=True):
        return SimpleNamespace(
            wake=SimpleNamespace(enabled=enabled, phrases=["hey player"], timeout_seconds=3)
        )

    def test_wake_enabled_by_default(self):
        args = SimpleNamespace(single=False, no_wake=False)
        cfg = main.build_wake_cfg(args, self._cfg(), FAKE_LOG)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.phrases, ["hey player"])

    def test_single_mode_disables_wake(self):
        args = SimpleNamespace(single=True, no_wake=False)
        cfg = main.build_wake_cfg(args, self._cfg(), FAKE_LOG)
        self.assertFalse(cfg.enabled)

    def test_no_wake_flag_disables_wake(self):
        args = SimpleNamespace(single=False, no_wake=True)
        cfg = main.build_wake_cfg(args, self._cfg(), FAKE_LOG)
        self.assertFalse(cfg.enabled)

    def test_wake_debug_flag_is_passed_through(self):
        args = SimpleNamespace(single=False, no_wake=False, wake_debug=True)
        cfg = main.build_wake_cfg(args, self._cfg(), FAKE_LOG)
        self.assertTrue(cfg.wake_debug)

    def test_wake_debug_off_by_default(self):
        args = SimpleNamespace(single=False, no_wake=False, wake_debug=False)
        cfg = main.build_wake_cfg(args, self._cfg(), FAKE_LOG)
        self.assertFalse(cfg.wake_debug)


class TestConsoleIndicators(unittest.TestCase):
    def _state(self, indicators):
        return {"listening": True, "armed": False, "armed_at": 0.0, "indicators": indicators}

    def test_handle_command_prints_command_indicator(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        state = self._state(True)
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, state)
        self.assertIn("Command: play", buffer.getvalue())
        self.assertEqual(controller.calls, ["play"])

    def test_no_command_indicator_without_flag(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        self.assertEqual(buffer.getvalue(), "")

    def test_listen_off_prints_listener_state(self):
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.handle_command(
                _FakeStopListener(), _FakeCommandController(), "stop listening", FAKE_LOG, tts_cfg, self._state(True)
            )
        self.assertIn("Listener: OFF", buffer.getvalue())

    def test_listen_on_prints_listener_state(self):
        state = self._state(True)
        state["listening"] = False
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.handle_command(
                _FakeStopListener(), _FakeCommandController(), "start listening", FAKE_LOG, tts_cfg, state
            )
        self.assertIn("Listener: ON", buffer.getvalue())

    def test_wake_debug_prints_raw_text_and_detection(self):
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        wake_cfg = SimpleNamespace(
            enabled=True,
            phrases=["hey player", "hello player", "player", "hey"],
            timeout_seconds=3,
            wake_debug=True,
        )
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.process_recognized(
                _FakeStopListener(), _FakeCommandController(), "hey player, pause",
                FAKE_LOG, tts_cfg, wake_cfg, self._state(False),
            )
        out = buffer.getvalue()
        self.assertIn("Raw recognized text: 'hey player, pause'", out)
        self.assertIn("Wake phrase detected: 'hey player'", out)
        self.assertIn("remaining command: 'pause'", out)

    def test_wake_debug_prints_when_no_phrase(self):
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        wake_cfg = SimpleNamespace(
            enabled=True,
            phrases=["hey player"],
            timeout_seconds=3,
            wake_debug=True,
        )
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.process_recognized(
                _FakeStopListener(), _FakeCommandController(), "volume up",
                FAKE_LOG, tts_cfg, wake_cfg, self._state(False),
            )
        out = buffer.getvalue()
        self.assertIn("Raw recognized text: 'volume up'", out)
        self.assertIn("No wake phrase detected", out)

    def test_no_wake_debug_no_output(self):
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        wake_cfg = SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3)
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.process_recognized(
                _FakeStopListener(), _FakeCommandController(), "hey player pause",
                FAKE_LOG, tts_cfg, wake_cfg, self._state(False),
            )
        self.assertEqual(buffer.getvalue(), "")


class TestHotkey(unittest.TestCase):
    def test_hotkey_registers_and_toggles_listening(self):
        fake_icon = MagicMock()
        fake_icon._refresh_image = MagicMock()
        state = {"listening": True, "armed": False, "armed_at": 0.0, "indicators": False}
        fake_kb = types.ModuleType("keyboard")
        fake_kb.add_hotkey = MagicMock(return_value=None)
        with patch.object(main, "HAS_KEYBOARD", True):
            with patch.object(main, "keyboard", fake_kb):
                with patch("tray.subprocess.Popen") as mock_popen:
                    with patch("tray._show_popup") as mock_popup:
                        main._register_hotkey(fake_icon, state, "ctrl+shift+l", FAKE_LOG)
                        hotkey, callback = fake_kb.add_hotkey.call_args[0]
                        self.assertEqual(hotkey, "ctrl+shift+l")
                        callback()
                        mock_popup.assert_called_once()
        self.assertFalse(state["listening"])
        self.assertEqual(fake_icon._refresh_image.call_count, 1)

    def test_hotkey_skipped_without_keyboard(self):
        fake_icon = MagicMock()
        state = {"listening": True}
        with patch.object(main, "HAS_KEYBOARD", False):
            main._register_hotkey(fake_icon, state, "ctrl+shift+l", FAKE_LOG)
        self.assertTrue(state["listening"])


class TestTestTray(unittest.TestCase):
    def test_test_tray_creates_icon_without_listener(self):
        fake_icon = MagicMock()
        with patch("tray._tray_available", return_value=True):
            with patch("tray.create_tray_icon", return_value=fake_icon) as mock_create:
                with patch("threading.Timer", MagicMock()):
                    with redirect_stdout(StringIO()):
                        main.run_test_tray(FAKE_LOG)
        mock_create.assert_called_once()
        args, kwargs = mock_create.call_args
        listener = args[0]
        self.assertFalse(listener.running)
        self.assertTrue(kwargs["verbose"])
        fake_icon.run.assert_called_once()

    def test_test_tray_warns_without_pystray(self):
        buffer = StringIO()
        with patch("tray._tray_available", return_value=False):
            with redirect_stdout(buffer):
                main.run_test_tray(FAKE_LOG)
        self.assertIn("pystray is not installed", buffer.getvalue())


class TestWakeTrainingAnalysis(unittest.TestCase):
    def test_all_matched_is_consistent(self):
        lines = main.analyze_wake_training(["hey player", "hey player"], ["hey player"])
        self.assertTrue(any("consistent" in line for line in lines))

    def test_no_speech_suggests_mic_check(self):
        lines = main.analyze_wake_training([], ["hey player"])
        self.assertTrue(any("--list-mics" in line for line in lines))

    def test_none_matched_suggests_alternatives(self):
        lines = main.analyze_wake_training(["go forward", "advance"], ["hey player"])
        self.assertTrue(any("0/2" in line for line in lines))
        self.assertTrue(any("vosk" in line or "player" in line for line in lines))

    def test_partial_match_reports_inconsistency(self):
        lines = main.analyze_wake_training(
            ["hey player play", "hey player pause", "just nonsense"], ["hey player"]
        )
        self.assertTrue(any("2/3" in line for line in lines))
        self.assertTrue(any("inconsist" in line for line in lines))

    def test_run_wake_training_prints_results(self):
        class FakeListener:
            def __init__(self):
                self.index = 0

            def listen_once(self):
                texts = ["hey player"] * 5
                self.index += 1
                return texts[self.index - 1]

        fake_config = SimpleNamespace(wake=SimpleNamespace(phrases=["hey player"]))
        buffer = StringIO()
        with patch("main.config.get_config", return_value=fake_config):
            with redirect_stdout(buffer):
                main.run_wake_training(FakeListener(), repetitions=5)
        out = buffer.getvalue()
        self.assertIn("Heard: 'hey player'", out)
        self.assertIn("consistent", out)


class TestTTS(unittest.TestCase):
    def setUp(self):
        tts._engine = None

    def test_speak_uses_engine_and_runs(self):
        engine = MagicMock()
        fake_module = types.ModuleType("pyttsx3")
        fake_module.init = MagicMock(return_value=engine)
        with patch.dict(sys.modules, {"pyttsx3": fake_module}):
            result = tts.speak("Playing")
        self.assertTrue(result)
        engine.say.assert_called_once_with("Playing")
        engine.runAndWait.assert_called_once()

    def test_speak_missing_pyttsx3_returns_false(self):
        with patch.dict(sys.modules, {"pyttsx3": None}):
            result = tts.speak("Playing", fallback_enabled=False)
        self.assertFalse(result)

    def test_speak_engine_init_failure_returns_false(self):
        fake_module = types.ModuleType("pyttsx3")
        fake_module.init = MagicMock(side_effect=RuntimeError("no audio driver"))
        with patch.dict(sys.modules, {"pyttsx3": fake_module}):
            result = tts.speak("Playing", fallback_enabled=False)
        self.assertFalse(result)

    def test_speak_uses_system_fallback_when_pyttsx3_missing(self):
        with patch.dict(sys.modules, {"pyttsx3": None}):
            with patch("tts.os.name", "nt"):
                with patch("tts.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run:
                    result = tts.speak("Playing")
        self.assertTrue(result)
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args[0][0][0], "powershell")

    def test_speak_no_fallback_when_disabled(self):
        with patch.dict(sys.modules, {"pyttsx3": None}):
            with patch("tts.os.name", "nt"):
                result = tts.speak("Playing", fallback_enabled=False)
        self.assertFalse(result)

    def test_speak_force_specific_engine(self):
        with patch("tts._powershell_speak", return_value=True) as mock_ps:
            result = tts.speak("Playing", engine="powershell")
        self.assertTrue(result)
        mock_ps.assert_called_once_with("Playing")

    def test_speak_fallback_failure_returns_false(self):
        with patch.dict(sys.modules, {"pyttsx3": None}):
            with patch("tts.os.name", "nt"):
                with patch("tts.subprocess.run", side_effect=OSError("no powershell")):
                    result = tts.speak("Playing")
        self.assertFalse(result)

    def test_engine_is_singleton(self):
        engine = MagicMock()
        fake_module = types.ModuleType("pyttsx3")
        fake_module.init = MagicMock(return_value=engine)
        with patch.dict(sys.modules, {"pyttsx3": fake_module}):
            tts.speak("one")
            tts.speak("two")
        self.assertEqual(fake_module.init.call_count, 1)


class TestSyncTools(unittest.TestCase):
    class _Recorder:
        def __init__(self, peak=200):
            self._peak = peak
            self.duration = None

        def capture_audio(self, duration):
            self.duration = duration
            return TestSyncTools._Audio(self._peak)

    class _Audio:
        sample_rate = 16000

        def __init__(self, peak):
            self._peak = peak

        def get_raw_data(self):
            return struct.pack("<4h", 0, 0, self._peak, -self._peak)

        def get_wav_data(self):
            return b"RIFF-fake-wav"

    def test_record_test_saves_wav_and_prints_stats(self):
        recorder = self._Recorder(peak=200)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("main.config_loader.PROJECT_ROOT", Path(tmp)):
                with patch("main.play_wav_file", return_value=True) as mock_play:
                    buffer = StringIO()
                    with redirect_stdout(buffer):
                        main.run_record_test(recorder, seconds=3.0)
            saved = Path(tmp) / "test_audio.wav"
            self.assertEqual(saved.read_bytes(), b"RIFF-fake-wav")
        text = buffer.getvalue()
        self.assertEqual(recorder.duration, 3.0)
        self.assertIn("peak amplitude 200", text)
        self.assertIn("test_audio.wav", text)
        self.assertTrue(mock_play.called)

    def test_energy_test_prints_suggestion(self):
        class _Measurer:
            def measure_energy(self, duration, interval):
                self.duration = duration
                self.interval = interval
                return [16.0, 18.0, 20.0, 800.0]

        measurer = _Measurer()
        buffer = StringIO()
        with redirect_stdout(buffer):
            main.run_energy_test(measurer, duration=2.0, interval=0.5)
        text = buffer.getvalue()
        self.assertEqual((measurer.duration, measurer.interval), (2.0, 0.5))
        self.assertIn("Suggested energy_threshold: 30", text)
        self.assertIn("--set-energy 30", text)


class _FakeVolumeController:
    def __init__(self):
        self.volume = None

    def set_volume(self, percent):
        self.volume = percent


class TestNumericVolumeCommands(unittest.TestCase):
    def _handle(self, text, controller=None):
        controller = controller or _FakeVolumeController()
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), controller, text, FAKE_LOG, tts_cfg)
        return controller, mock_speak

    def test_set_volume_command_sets_and_speaks(self):
        controller, mock_speak = self._handle("set volume to 50")
        self.assertEqual(controller.volume, 50)
        mock_speak.assert_called_once_with(
            "Volume set to 50", voice_id=None, engine="auto", fallback_enabled=True
        )

    def test_bare_volume_number(self):
        controller, _ = self._handle("volume 100")
        self.assertEqual(controller.volume, 100)

    def test_out_of_range_is_clamped(self):
        controller, mock_speak = self._handle("volume 150")
        self.assertEqual(controller.volume, 100)
        mock_speak.assert_called_once_with(
            "Volume set to 100", voice_id=None, engine="auto", fallback_enabled=True
        )

    def test_relative_volume_uses_mapped_command(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        main.handle_command(_FakeStopListener(), controller, "volume up", FAKE_LOG, tts_cfg)
        self.assertEqual(controller.calls, ["volume_up"])

    def test_non_volume_text_ignored(self):
        class Boom:
            def set_volume(self, percent):
                raise AssertionError("set_volume should not be called")

        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        main.handle_command(_FakeStopListener(), Boom(), "hello there", FAKE_LOG, tts_cfg)

    def test_set_volume_failure_speaks_command_failed(self):
        class Boom:
            def set_volume(self, percent):
                raise RuntimeError("boom")

        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), Boom(), "set volume to 30", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with(
            "Command failed", voice_id=None, engine="auto", fallback_enabled=True
        )


class TestListenerToggleCommands(unittest.TestCase):
    def _setup(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        listener = _FakeStopListener()
        state = {"listening": True, "armed": False, "armed_at": 0.0}
        return listener, controller, tts_cfg, state

    def test_all_listen_phrases_map_to_their_action(self):
        for phrase in ["start listening", "listen", "turn on listening", "resume listening"]:
            self.assertEqual(config.match_command(phrase), "listen_on")
        for phrase in ["stop listening", "pause listening", "turn off listening"]:
            self.assertEqual(config.match_command(phrase), "listen_off")

    def test_listen_off_pauses(self):
        _, _, tts_cfg, state = self._setup()
        main.handle_command(_FakeStopListener(), _FakeCommandController(), "stop listening", FAKE_LOG, tts_cfg, state)
        self.assertFalse(state["listening"])

    def test_listen_on_resumes(self):
        _, _, tts_cfg, state = self._setup()
        state["listening"] = False
        main.handle_command(_FakeStopListener(), _FakeCommandController(), "start listening", FAKE_LOG, tts_cfg, state)
        self.assertTrue(state["listening"])

    def test_commands_ignored_while_paused(self):
        _, controller, tts_cfg, state = self._setup()
        state["listening"] = False
        main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, state)
        main.handle_command(_FakeStopListener(), controller, "set volume to 30", FAKE_LOG, tts_cfg, state)
        self.assertEqual(controller.calls, [])

    def test_paused_listener_still_honors_listen_on(self):
        _, controller, tts_cfg, state = self._setup()
        state["listening"] = False
        main.handle_command(_FakeStopListener(), controller, "yes play", FAKE_LOG, tts_cfg, state)
        self.assertEqual(controller.calls, [])
        main.handle_command(_FakeStopListener(), controller, "listen", FAKE_LOG, tts_cfg, state)
        self.assertTrue(state["listening"])
        main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, state)
        self.assertEqual(controller.calls, ["play"])

    def test_toggle_through_process_recognized(self):
        _, controller, tts_cfg, state = self._setup()
        wake_cfg = SimpleNamespace(enabled=False, phrases=[], timeout_seconds=3)
        main.process_recognized(_FakeStopListener(), controller, "turn off listening", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertFalse(state["listening"])
        main.process_recognized(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, [])
        main.process_recognized(_FakeStopListener(), controller, "turn on listening", FAKE_LOG, tts_cfg, wake_cfg, state)
        main.process_recognized(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertEqual(controller.calls, ["play"])

    def test_listen_off_speaks_feedback(self):
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        state = {"listening": True, "armed": False, "armed_at": 0.0}
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), _FakeCommandController(), "stop listening", FAKE_LOG, tts_cfg, state)
        mock_speak.assert_called_once_with("Listening paused", voice_id=None, engine="auto", fallback_enabled=True)

    def test_state_defaults_to_listening(self):
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        controller = _FakeCommandController()
        main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        self.assertEqual(controller.calls, ["play"])


class TestStartupHelpers(unittest.TestCase):
    def test_uninstall_startup_returns_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(main.uninstall_startup(Path(tmp) / "missing.lnk"))

    def test_uninstall_startup_removes_existing_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            shortcut = Path(tmp) / "Voice Media Player.lnk"
            shortcut.write_text("stub")
            self.assertTrue(main.uninstall_startup(shortcut))
            self.assertFalse(shortcut.exists())

    def test_install_startup_requires_tray_support(self):
        with patch("tray._tray_available", return_value=False), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                main.install_startup(Path(tmp) / "Voice Media Player.lnk")


class TestMainFeedback(unittest.TestCase):
    def test_handle_command_speaks_feedback_after_success(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Playing", voice_id=None, engine="auto", fallback_enabled=True)
        self.assertEqual(controller.calls, ["play"])

    def test_handle_command_speaks_command_failed_on_error(self):
        class BoomController:
            def play(self):
                raise RuntimeError("boom")

        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), BoomController(), "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Command failed", voice_id=None, engine="auto", fallback_enabled=True)

    def test_handle_command_exit_stops_listener_and_speaks(self):
        listener = _FakeStopListener()
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(listener, None, "exit", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Exiting", voice_id=None, engine="auto", fallback_enabled=True)
        self.assertTrue(listener.stopped)

    def test_tts_disabled_does_not_speak(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_not_called()

    def test_all_actions_have_tts_feedback(self):
        actions = vars(config.get_config().commands)
        missing = [action for action in actions if action not in main.TTS_PHRASES]
        self.assertEqual(missing, [])


class TestNoiseGateAndConfidence(unittest.TestCase):
    def _listener(self, recognizer=None, **kwargs):
        cfg = SimpleNamespace(
            voice=SimpleNamespace(
                timeout_seconds=5,
                phrase_time_limit=3,
                energy_threshold=300,
                dynamic_energy_threshold=False,
                noise_gate_enabled=False,
                noise_gate_threshold=10.0,
                confidence_threshold=0.5,
            )
        )
        with patch("voice_listener.config.get_config", return_value=cfg):
            return VoiceListener(
                recognizer=recognizer or FakeRecognizer("volume up"),
                source=FakeMicSource(),
                energy_threshold=300,
                dynamic_energy_threshold=False,
                **kwargs,
            )

    def test_noise_gate_enabled_blocks_quiet_audio(self):
        listener = self._listener(noise_gate_enabled=True, noise_gate_threshold=20.0)
        self.assertIsNone(listener.listen_once())

    def test_noise_gate_enabled_passes_loud_audio(self):
        class LoudRecognizer(FakeRecognizer):
            def listen(self, source, timeout=None, phrase_time_limit=None):
                audio = FakeAudio()
                audio.get_raw_data = lambda: b"\xff\x7f" * 1600
                return audio

        listener = self._listener(
            recognizer=LoudRecognizer("volume up"),
            noise_gate_enabled=True,
            noise_gate_threshold=20.0,
        )
        self.assertEqual(listener.listen_once(), "volume up")

    def test_noise_gate_disabled_by_default(self):
        listener = self._listener()
        self.assertEqual(listener.listen_once(), "volume up")

    def test_low_confidence_transcript_rejected(self):
        class LowConfidence(FakeRecognizer):
            def recognize_google(self, audio, show_all=False):
                return {
                    "alternative": [{"transcript": "volume up", "confidence": 0.1}],
                    "final": True,
                }

        listener = self._listener(recognizer=LowConfidence("volume up"))
        self.assertIsNone(listener.listen_once())

    def test_high_confidence_transcript_accepted(self):
        class HighConfidence(FakeRecognizer):
            def recognize_google(self, audio, show_all=False):
                return {
                    "alternative": [{"transcript": "volume up", "confidence": 0.95}],
                    "final": True,
                }

        listener = self._listener(recognizer=HighConfidence("volume up"))
        self.assertEqual(listener.listen_once(), "volume up")

    def test_no_confidence_short_text_heuristic_rejected(self):
        listener = self._listener(recognizer=FakeRecognizer("ok"))
        self.assertIsNone(listener.listen_once())

    def test_no_confidence_normal_text_heuristic_accepted(self):
        listener = self._listener(recognizer=FakeRecognizer("pause"))
        self.assertEqual(listener.listen_once(), "pause")

    def test_threshold_zero_disables_filtering(self):
        listener = self._listener(
            recognizer=FakeRecognizer("ok"),
            confidence_threshold=0,
        )
        self.assertEqual(listener.listen_once(), "ok")


class TestPorcupineWake(unittest.TestCase):
    def test_porcupine_unavailable_without_package(self):
        with patch.dict(sys.modules, {"pvporcupine": None}):
            wake_word = wake.PorcupineWakeWord(access_key="abc", log=FAKE_LOG)
        self.assertFalse(wake_word.available)
        self.assertIsNone(wake_word.wait_for_wake_word(timeout=1))

    def test_porcupine_unavailable_without_access_key(self):
        fake_pv = types.ModuleType("pvporcupine")
        fake_pv.create = MagicMock()
        with patch.dict(sys.modules, {"pvporcupine": fake_pv}):
            with patch("wake._env_access_key", return_value=None):
                wake_word = wake.PorcupineWakeWord(keywords=["porcupine"], log=FAKE_LOG)
        self.assertFalse(wake_word.available)
        self.assertIsNone(wake_word.wait_for_wake_word())

    def test_porcupine_available_labels_keyword(self):
        class FakePorcupine:
            sample_rate = 16000
            frame_length = 512

            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def process(self, frame):
                return 0 if frame[0] == 2 else -1

        fake_pv = types.ModuleType("pvporcupine")
        fake_pv.create = MagicMock(return_value=FakePorcupine())
        with patch.dict(sys.modules, {"pvporcupine": fake_pv}):
            wake_word = wake.PorcupineWakeWord(
                access_key="abc",
                keywords=["porcupine", "hey google"],
                log=FAKE_LOG,
            )
        self.assertTrue(wake_word.available)
        self.assertEqual(wake_word.process([2] + [0] * 511), "porcupine")
        self.assertEqual(wake_word.process([0] * 512), None)

    def test_wait_for_wake_word_reads_frames(self):
        class FakePorcupine:
            sample_rate = 16000
            frame_length = 512

            def process(self, frame):
                return 0

        class FakePortaudio:
            paInt16 = 8

            def PyAudio(self):
                return self

            def open(self, **kwargs):
                return self

            def read(self, frame_count, exception_on_overflow=False):
                return b"\x01\x00" * frame_count

            def stop_stream(self):
                pass

            def close(self):
                pass

            def terminate(self):
                pass

        fake_pv = types.ModuleType("pvporcupine")
        fake_pv.create = MagicMock(return_value=FakePorcupine())
        fake_pa = FakePortaudio()
        with patch.dict(sys.modules, {"pvporcupine": fake_pv, "pyaudio": fake_pa}):
            wake_word = wake.PorcupineWakeWord(
                access_key="abc",
                keywords=["porcupine"],
                sample_rate=16000,
                frame_length=512,
                log=FAKE_LOG,
            )
        self.assertTrue(wake_word.available)
        self.assertEqual(wake_word.wait_for_wake_word(timeout=2), "porcupine")

    def test_build_porcupine_creates_instance(self):
        with patch("wake.PorcupineWakeWord") as mock_cls:
            mock_cls.return_value = "instance"
            cfg = SimpleNamespace(
                wake=SimpleNamespace(
                    porcupine_access_key="key",
                    porcupine_keywords=["porcupine"],
                    porcupine_keyword_paths=[],
                )
            )
            result = wake.build_porcupine(cfg, log=FAKE_LOG, enabled=True)
        self.assertEqual(result, "instance")
        mock_cls.assert_called_once()
        self.assertEqual(mock_cls.call_args.kwargs["access_key"], "key")


class TestPushToTalkGate(unittest.TestCase):
    def _tts_cfg(self, enabled=True):
        return SimpleNamespace(enabled=enabled, voice_id=None, engine="auto", fallback_enabled=True, cooldown_seconds=1.5)

    def _ptt(self, enabled=True, key="ctrl"):
        return SimpleNamespace(enabled=enabled, key=key)

    def test_gate_allows_when_ptt_disabled(self):
        state = {"tts_cooldown_until": 0.0}
        gate = main._listen_gate(state, self._tts_cfg(False), self._ptt(False), FAKE_LOG)
        self.assertTrue(gate())

    def test_gate_blocks_when_key_not_held(self):
        state = {"tts_cooldown_until": 0.0}
        fake_kb = types.ModuleType("keyboard")
        fake_kb.is_pressed = MagicMock(return_value=False)
        with patch.object(main, "HAS_KEYBOARD", True):
            with patch.object(main, "keyboard", fake_kb):
                gate = main._listen_gate(state, self._tts_cfg(False), self._ptt(True), FAKE_LOG)
                self.assertFalse(gate())

    def test_gate_allows_when_key_held(self):
        state = {"tts_cooldown_until": 0.0}
        fake_kb = types.ModuleType("keyboard")
        fake_kb.is_pressed = MagicMock(return_value=True)
        with patch.object(main, "HAS_KEYBOARD", True):
            with patch.object(main, "keyboard", fake_kb):
                gate = main._listen_gate(state, self._tts_cfg(False), self._ptt(True), FAKE_LOG)
                self.assertTrue(gate())

    def test_gate_blocks_during_tts_cooldown(self):
        state = {"tts_cooldown_until": time.time() + 10}
        gate = main._listen_gate(state, self._tts_cfg(True), self._ptt(False), FAKE_LOG)
        self.assertFalse(gate())

    def test_gate_allows_after_tts_cooldown(self):
        state = {"tts_cooldown_until": time.time() - 10}
        gate = main._listen_gate(state, self._tts_cfg(True), self._ptt(False), FAKE_LOG)
        self.assertTrue(gate())


class TestTTSAction(unittest.TestCase):
    def test_tts_cooldown_set_after_successful_command(self):
        controller = _FakeCommandController()
        state = {"listening": True, "tts_cooldown_until": 0.0}
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None, engine="auto", fallback_enabled=True, cooldown_seconds=2.0)
        with patch("main.tts.speak"):
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, state)
        self.assertGreater(state["tts_cooldown_until"], time.time())
        self.assertEqual(controller.calls, ["play"])

    def test_no_cooldown_when_tts_disabled(self):
        controller = _FakeCommandController()
        state = {"listening": True, "tts_cooldown_until": 0.0}
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True, cooldown_seconds=2.0)
        main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg, state)
        self.assertEqual(state["tts_cooldown_until"], 0.0)


class TestWakeEngineConfig(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(single=False, no_wake=False, wake_debug=False, mic_index=None)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_porcupine_engine_selected_when_available(self):
        porcupine = SimpleNamespace(available=True, error=None)
        fake_wake = SimpleNamespace(wake=SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3, engine="auto"))
        with patch("main.build_porcupine", return_value=porcupine) as mock_build:
            cfg = main.build_wake_cfg(self._args(), fake_wake, FAKE_LOG)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.engine, "porcupine")
        self.assertIs(cfg.porcupine, porcupine)
        mock_build.assert_called_once()

    def test_string_engine_fallback_when_unavailable(self):
        porcupine = SimpleNamespace(available=False, error="no key")
        fake_wake = SimpleNamespace(wake=SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3, engine="auto"))
        with patch("main.build_porcupine", return_value=porcupine):
            cfg = main.build_wake_cfg(self._args(), fake_wake, FAKE_LOG)
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.engine, "string")
        self.assertIsNone(cfg.porcupine)

    def test_porcupine_engine_forced_and_missing_logs_warning(self):
        porcupine = SimpleNamespace(available=False, error="no access key")
        fake_wake = SimpleNamespace(wake=SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3, engine="porcupine"))
        log = logging.getLogger("test.wake.cfg")
        with patch("main.build_porcupine", return_value=porcupine):
            cfg = main.build_wake_cfg(self._args(), fake_wake, log)
        self.assertEqual(cfg.engine, "string")

    def test_wake_disabled_never_uses_porcupine(self):
        fake_wake = SimpleNamespace(wake=SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3, engine="porcupine"))
        cfg = main.build_wake_cfg(self._args(no_wake=True), fake_wake, FAKE_LOG)
        self.assertFalse(cfg.enabled)
        self.assertIsNone(cfg.porcupine)


class TestAlwaysActiveListenCommands(unittest.TestCase):
    def _setup(self):
        controller = _FakeCommandController()
        wake_cfg = SimpleNamespace(enabled=True, phrases=["hey player"], timeout_seconds=3)
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        state = {"listening": True, "armed": False, "armed_at": 0.0}
        return _FakeStopListener(), controller, tts_cfg, wake_cfg, state

    def test_listen_off_works_without_wake_word(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        main.process_recognized(listener, controller, "stop listening", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertFalse(state["listening"])

    def test_listen_off_works_while_paused(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        state["listening"] = False
        main.process_recognized(listener, controller, "turn off", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertFalse(state["listening"])

    def test_listen_on_works_while_paused_without_wake(self):
        listener, controller, tts_cfg, wake_cfg, state = self._setup()
        state["listening"] = False
        main.process_recognized(listener, controller, "listen", FAKE_LOG, tts_cfg, wake_cfg, state)
        self.assertTrue(state["listening"])

    def test_new_phrase_forms_map_to_toggles(self):
        self.assertEqual(config.match_command("resume"), "listen_on")
        self.assertEqual(config.match_command("turn on"), "listen_on")
        self.assertEqual(config.match_command("enable"), "listen_on")
        self.assertEqual(config.match_command("disable"), "listen_off")
        self.assertEqual(config.match_command("go silent"), "listen_off")


class _Landmark:
    __slots__ = ("x", "y")

    def __init__(self, x=0.0, y=0.0):
        self.x = x
        self.y = y


def _hand(open_fingers=(True, True, True, True, True)):
    """Build a 21-keypoint hand landmark list.

    open_fingers = (thumb, index, middle, ring, pinky). Tips point up (smaller
    y) when open and curl down near the PIP when closed. The thumb extends out
    to the side when open.
    """
    lm = [_Landmark(0.5, 0.2) for _ in range(21)]
    lm[0] = _Landmark(0.5, 0.9)  # wrist
    lm[1] = _Landmark(0.36, 0.75)  # thumb CMC
    lm[2] = _Landmark(0.34, 0.6)  # thumb MCP
    lm[3] = _Landmark(0.36, 0.46)  # thumb IP
    lm[4] = _Landmark(0.30, 0.30) if open_fingers[0] else _Landmark(0.36, 0.48)
    lm[5] = _Landmark(0.44, 0.55)  # index MCP
    lm[6] = _Landmark(0.46, 0.42)  # index PIP
    lm[8] = _Landmark(0.47, 0.22) if open_fingers[1] else _Landmark(0.47, 0.50)
    lm[7] = _Landmark(0.50, 0.55)  # middle MCP
    lm[9] = _Landmark(0.50, 0.55)  # palm (middle MCP)
    lm[10] = _Landmark(0.51, 0.42)  # middle PIP
    lm[11] = _Landmark(0.51, 0.28)
    lm[12] = _Landmark(0.52, 0.22) if open_fingers[2] else _Landmark(0.52, 0.50)
    lm[13] = _Landmark(0.56, 0.55)  # ring MCP
    lm[14] = _Landmark(0.57, 0.44)  # ring PIP
    lm[15] = _Landmark(0.57, 0.30)
    lm[16] = _Landmark(0.58, 0.24) if open_fingers[3] else _Landmark(0.58, 0.52)
    lm[17] = _Landmark(0.62, 0.55)  # pinky MCP
    lm[18] = _Landmark(0.63, 0.46)  # pinky PIP
    lm[19] = _Landmark(0.63, 0.32)
    lm[20] = _Landmark(0.64, 0.26) if open_fingers[4] else _Landmark(0.64, 0.54)
    return lm


class TestGestureRecognition(unittest.TestCase):
    def test_open_hand_counts_five_fingers(self):
        lm = _hand((True, True, True, True, True))
        self.assertEqual(gesture.count_extended_fingers(lm), [True, True, True, True, True])

    def test_fist_counts_no_fingers(self):
        lm = _hand((False, False, False, False, False))
        self.assertEqual(gesture.count_extended_fingers(lm), [False, False, False, False, False])

    def test_peace_only_index_and_middle(self):
        lm = _hand((False, True, True, False, False))
        self.assertEqual(gesture.count_extended_fingers(lm), [False, True, True, False, False])

    def test_classify_open_hand_play_pause(self):
        self.assertEqual(gesture.classify_hand(_hand((True, True, True, True, True))), "play_pause")

    def test_classify_fist_stop(self):
        self.assertEqual(gesture.classify_hand(_hand((False, False, False, False, False))), "stop")

    def test_classify_peace_mute(self):
        self.assertEqual(gesture.classify_hand(_hand((False, True, True, False, False))), "toggle_mute")

    def test_classify_index_only_fullscreen(self):
        self.assertEqual(gesture.classify_hand(_hand((False, True, False, False, False))), "toggle_fullscreen")

    def test_classify_thumbs_up_volume_up(self):
        lm = _hand((True, False, False, False, False))
        lm[4] = _Landmark(0.30, 0.30)  # thumb tip above the wrist (0.9)
        self.assertEqual(gesture.classify_hand(lm), "volume_up")

    def test_classify_thumbs_down_volume_down(self):
        lm = _hand((True, False, False, False, False))
        lm[0] = _Landmark(0.5, 0.5)  # raise the wrist so the thumb is below it
        lm[4] = _Landmark(0.30, 0.75)
        self.assertEqual(gesture.classify_hand(lm), "volume_down")


class TestGestureDebouncer(unittest.TestCase):
    def test_requires_consecutive_frames(self):
        debouncer = gesture.Debouncer(frames=3, cooldown=10)
        self.assertIsNone(debouncer.feed("stop"))
        self.assertIsNone(debouncer.feed("stop"))
        self.assertEqual(debouncer.feed("stop"), "stop")

    def test_candidate_change_resets_count(self):
        debouncer = gesture.Debouncer(frames=3, cooldown=10)
        debouncer.feed("stop")
        self.assertIsNone(debouncer.feed("play"))
        self.assertIsNone(debouncer.feed("play"))
        self.assertEqual(debouncer.feed("play"), "play")

    def test_same_action_requires_release(self):
        debouncer = gesture.Debouncer(frames=1, cooldown=0)
        self.assertEqual(debouncer.feed("stop"), "stop")
        self.assertIsNone(debouncer.feed("stop"))
        debouncer.feed(None)
        self.assertEqual(debouncer.feed("stop"), "stop")

    def test_cooldown_blocks_other_actions(self):
        now = [100.0]
        debouncer = gesture.Debouncer(frames=1, cooldown=5, now_fn=lambda: now[0])
        self.assertEqual(debouncer.feed("stop"), "stop")
        debouncer.feed(None)
        self.assertIsNone(debouncer.feed("play"))
        now[0] += 6
        self.assertEqual(debouncer.feed("play"), "play")


class TestGestureSwipe(unittest.TestCase):
    def test_swipe_right_skips_forward(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.05)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.7, 0.5), "skip_forward")

    def test_swipe_left_skips_backward(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.05)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.2, 0.5), "skip_backward")

    def test_swipe_up_volume_up_big(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.05)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.5, 0.2), "volume_up_big")

    def test_swipe_down_volume_down_big(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.05)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.5, 0.8), "volume_down_big")

    def test_same_direction_does_not_repeat(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.05)
        tracker.feed(100.0, 0.5, 0.5)
        tracker.feed(100.1, 0.7, 0.5)
        self.assertIsNone(tracker.feed(100.2, 0.9, 0.5))

    def test_slow_motion_not_a_swipe(self):
        tracker = gesture.SwipeTracker(window=0.5, threshold=0.3)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))


class TestGestureSetup(unittest.TestCase):
    def test_degrades_cleanly_without_libraries(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        self.assertFalse(controller.available)
        self.assertIsNotNone(controller.error)
        self.assertIn("not installed", controller.error)

    def test_error_mentions_both_when_everything_missing(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        self.assertIn("opencv-python", controller.error)
        self.assertIn("mediapipe", controller.error)

    def test_detect_returns_none_when_unavailable(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        self.assertIsNone(controller.detect_gesture())

    def test_default_model_path_under_home(self):
        self.assertTrue(gesture.DEFAULT_MODEL_PATH.is_absolute())

    def test_backend_availability_is_boolean(self):
        self.assertIsInstance(gesture.gesture_backend_available(), bool)


class _Frame:
    size = 4800
    shape = (480, 640, 3)

    def min(self):
        return 10

    def max(self):
        return 250

    def mean(self):
        return 120.0

    def std(self):
        return 60.0


class _ConstantFrame(_Frame):
    """A valid-size frame with no image signal (max - min <= SIGNAL_DELTA)."""

    def min(self):
        return 2

    def max(self):
        return 2

    def mean(self):
        return 2.0

    def std(self):
        return 0.0


class _FakeCap:
    """Reads a fixed list of (ok, frame) tuples; then fails forever."""

    def __init__(self, results, opened=True):
        self.results = list(results)
        self.opened = opened
        self.released = False
        self.reads = 0

    def isOpened(self):
        return self.opened

    def read(self):
        self.reads += 1
        if self.results:
            return self.results.pop(0)
        return False, None

    def release(self):
        self.released = True


def _fake_cv2(factory):
    cv2 = MagicMock()
    cv2.CAP_DSHOW = 700
    cv2.CAP_MSMF = 1400
    cv2.CAP_V4L2 = 200
    cv2.CAP_ANY = 0
    cv2.VideoCapture = MagicMock(side_effect=factory)
    return cv2


class TestGestureCameraOpen(unittest.TestCase):
    def test_candidates_order_requested_then_defaults(self):
        self.assertEqual(gesture._camera_candidates(5), [5, 0, 1, -1])
        self.assertEqual(gesture._camera_candidates(0), [0, 1, -1])

    def test_backends_are_deduplicated(self):
        seen = gesture._camera_backends()
        self.assertEqual(len(seen), len(set(seen)))

    def test_open_camera_falls_back_to_next_backend(self):
        calls = []

        def factory(index, backend):
            calls.append((index, backend))
            return _FakeCap([(True, _Frame())], opened=(backend != 700))

        with patch.object(gesture, "cv2", _fake_cv2(factory)):
            cap = gesture.open_camera(0)
        self.assertIsNotNone(cap)
        self.assertEqual(calls[0], (0, 700))
        self.assertEqual(len(calls), 2)

    def test_open_camera_accepts_frames_with_signal(self):
        cap = _FakeCap([(True, _Frame()) for _ in range(3)])
        with patch.object(gesture, "cv2", _fake_cv2(lambda index, backend: cap)):
            result = gesture.open_camera(0)
        self.assertIs(result, cap)
        self.assertEqual(cap.reads, 1)

    def test_open_camera_returns_none_when_everything_fails(self):
        caps = []

        def factory(index, backend):
            cap = _FakeCap([], opened=False)
            caps.append(cap)
            return cap

        with patch.object(gesture, "cv2", _fake_cv2(factory)):
            result = gesture.open_camera(0)
        self.assertIsNone(result)
        self.assertTrue(all(c.released for c in caps))

    def test_open_camera_skips_constant_frames_then_accepts_signal(self):
        cap = _FakeCap([(True, _ConstantFrame()), (True, _ConstantFrame()), (True, _Frame())])
        with patch.object(gesture, "cv2", _fake_cv2(lambda index, backend: cap)):
            result = gesture.open_camera(0)
        self.assertIs(result, cap)
        self.assertEqual(cap.reads, 3)

    def test_open_camera_rejects_camera_with_only_constant_frames(self):
        caps = []

        def factory(index, backend):
            cap = _FakeCap([(True, _ConstantFrame())])
            caps.append(cap)
            return cap

        with patch.object(gesture, "cv2", _fake_cv2(factory)):
            result = gesture.open_camera(0)
        self.assertIsNone(result)
        self.assertTrue(all(c.released for c in caps))

    def test_frame_has_signal_distinguishes_real_from_constant(self):
        self.assertTrue(gesture._frame_has_signal(_Frame()))
        self.assertFalse(gesture._frame_has_signal(_ConstantFrame()))

    def test_open_camera_error_points_at_silent_device(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        with patch.object(gesture, "cv2", _fake_cv2(lambda i, b: _FakeCap([(True, _ConstantFrame())]))):
            opened = controller._open_camera(0)
        self.assertFalse(opened)
        self.assertFalse(controller.available)
        self.assertIn("not sending frames", controller.error)

    def test_open_camera_error_when_no_device(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        with patch.object(gesture, "cv2", _fake_cv2(lambda i, b: _FakeCap([], opened=False))):
            opened = controller._open_camera(0)
        self.assertFalse(opened)
        self.assertNotIn("not sending frames", controller.error)
        self.assertIn("Could not open camera", controller.error)

    def test_controller_tolerates_constant_frames_during_warmup(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        constant = _FakeCap([(True, _ConstantFrame()) for _ in range(5)])
        controller._cap = constant
        detector = MagicMock()
        detector.process.return_value = None
        controller._detector = detector
        controller.available = True
        controller._frames_read = 0
        with patch.object(gesture, "cv2", MagicMock()):
            for _ in range(5):
                self.assertIsNone(controller.detect_gesture())
        self.assertIs(controller._cap, constant)
        self.assertEqual(controller._read_failures, 0)

    def test_controller_counts_constant_frames_as_failures_after_warmup(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        constant = _FakeCap([(True, _ConstantFrame())])
        controller._cap = constant
        detector = MagicMock()
        detector.process.return_value = None
        controller._detector = detector
        controller.available = True
        controller._frames_read = controller.warm_up_frames + 1
        with patch.object(gesture, "cv2", MagicMock()):
            self.assertIsNone(controller.detect_gesture())
        self.assertEqual(controller._read_failures, 1)

    def test_controller_reopens_on_constant_frames_after_warmup(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        constant = _FakeCap([(True, _ConstantFrame())])
        controller._cap = constant
        detector = MagicMock()
        detector.process.return_value = None
        controller._detector = detector
        controller.available = True
        controller._frames_read = controller.warm_up_frames + 1
        good = _FakeCap([(True, _Frame())])
        with patch.object(gesture, "cv2", MagicMock()), patch.object(gesture, "open_camera", return_value=good):
            for _ in range(controller.max_read_failures):
                self.assertIsNone(controller.detect_gesture())
        self.assertIs(controller._cap, good)
        self.assertEqual(controller._read_failures, 0)
        self.assertTrue(controller.available)

    def test_controller_reopens_after_repeated_failures(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        dead = _FakeCap([])
        controller._cap = dead
        controller._detector = MagicMock()
        controller.available = True
        good = _FakeCap([(True, _Frame())])
        with patch.object(gesture, "cv2", _fake_cv2(lambda i, b: good)) as cv2_mock, patch.object(
            gesture, "open_camera", return_value=good
        ):
            for _ in range(controller.max_read_failures):
                self.assertIsNone(controller.detect_gesture())
            self.assertIs(controller._cap, good)
            self.assertEqual(controller._read_failures, 0)
            self.assertTrue(controller.available)

    def test_controller_does_not_reopen_before_limit(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        dead = _FakeCap([])
        controller._cap = dead
        controller._detector = MagicMock()
        controller.available = True
        with patch.object(gesture, "open_camera", return_value=dead):
            for _ in range(controller.max_read_failures - 1):
                self.assertIsNone(controller.detect_gesture())
        self.assertIs(controller._cap, dead)
        self.assertEqual(controller._read_failures, controller.max_read_failures - 1)

    def test_get_preview_frame_returns_none_when_unavailable(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        self.assertIsNone(controller.get_preview_frame())


class TestGestureActions(unittest.TestCase):
    def _controller(self):
        return _RecordingController()

    def _state(self):
        return {"listening": True, "indicators": False}

    def test_play_pause_toggles(self):
        controller = self._controller()
        state = self._state()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True)
        main.handle_gesture_action("play_pause", controller, FAKE_LOG, tts_cfg, state)
        self.assertEqual(controller.calls, ["play"])
        main.handle_gesture_action("play_pause", controller, FAKE_LOG, tts_cfg, state)
        self.assertEqual(controller.calls, ["play", "pause"])

    def test_stop_action(self):
        controller = self._controller()
        main.handle_gesture_action("stop", controller, FAKE_LOG, tts_cfg_disabled(), self._state())
        self.assertEqual(controller.calls, ["stop"])

    def test_volume_up_big_steps_ten(self):
        controller = _RecordingController()
        main.handle_gesture_action("volume_up_big", controller, FAKE_LOG, tts_cfg_disabled(), self._state())
        self.assertEqual(controller.calls, [("volume_up", 10)])

    def test_paused_state_ignores_gesture(self):
        controller = self._controller()
        state = {"listening": False}
        main.handle_gesture_action("stop", controller, FAKE_LOG, tts_cfg_disabled(), state)
        self.assertEqual(controller.calls, [])

    def test_unknown_action_ignored(self):
        controller = self._controller()
        main.handle_gesture_action("wiggle", controller, FAKE_LOG, tts_cfg_disabled(), self._state())
        self.assertEqual(controller.calls, [])


def tts_cfg_disabled():
    return SimpleNamespace(enabled=False, voice_id=None, engine="auto", fallback_enabled=True, cooldown_seconds=0)


class _RecordingController:
    def __init__(self):
        self.calls = []

    def play(self):
        self.calls.append("play")

    def pause(self):
        self.calls.append("pause")

    def stop(self):
        self.calls.append("stop")

    def skip_forward(self, seconds=None):
        self.calls.append(("skip_forward", seconds))

    def skip_backward(self, seconds=None):
        self.calls.append(("skip_backward", seconds))

    def volume_up(self, step=None):
        self.calls.append(("volume_up", step))

    def volume_down(self, step=None):
        self.calls.append(("volume_down", step))

    def toggle_mute(self):
        self.calls.append("toggle_mute")

    def toggle_fullscreen(self):
        self.calls.append("toggle_fullscreen")


class TestTrayIconStates(unittest.TestCase):
    def test_green_when_listening(self):
        self.assertEqual(tray._current_color({"listening": True}), tray.GREEN)

    def test_red_when_paused(self):
        self.assertEqual(tray._current_color({"listening": False}), tray.RED)

    def test_yellow_during_tts_cooldown(self):
        state = {"listening": True, "tts_cooldown_until": time.time() + 5}
        self.assertEqual(tray._current_color(state), tray.YELLOW)

    def test_green_after_cooldown_expires(self):
        state = {"listening": True, "tts_cooldown_until": time.time() - 5}
        self.assertEqual(tray._current_color(state), tray.GREEN)


if __name__ == "__main__":
    unittest.main()