import logging
import os
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import main
import requests
import speech_recognition as sr
import tts
from player_control import AutoController, MPCController, PlayerController, VLCController
from voice_listener import VoiceListener


class FakeAudio:
    pass


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


class TestVoiceListener(unittest.TestCase):
    def test_listen_once_returns_recognized_text(self):
        listener = VoiceListener(recognizer=FakeRecognizer("volume up"), source=FakeMicSource())
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


class TestControllers(unittest.TestCase):
    ACTIONS = (
        "play",
        "pause",
        "stop",
        "skip_forward",
        "skip_backward",
        "volume_up",
        "volume_down",
        "toggle_mute",
        "toggle_fullscreen",
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


class FakeVLC(PlayerController):
    name = "vlc"

    def play(self):
        self.calls.append("vlc")


class FakeMPC(PlayerController):
    name = "mpc-hc"

    def play(self):
        self.calls.append("mpc-hc")


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
        self.assertEqual(
            config.detect_wake_phrase("hey player play", ["hey player", "player"]),
            "hey player",
        )

    def test_detects_shortest_phrase_too(self):
        self.assertEqual(
            config.detect_wake_phrase("player volume up", ["hey player", "player"]),
            "player",
        )

    def test_detects_none_when_absent(self):
        self.assertIsNone(config.detect_wake_phrase("just play", ["hey player"]))
        self.assertIsNone(config.detect_wake_phrase("", ["hey player"]))
        self.assertIsNone(config.detect_wake_phrase("play", []))

    def test_strip_phrase_from_front(self):
        self.assertEqual(config.strip_phrase("hey player play", "hey player"), "play")

    def test_strip_phrase_missing_returns_text(self):
        self.assertEqual(config.strip_phrase("play", "hey player"), "play")

    def test_strip_phrase_is_case_insensitive(self):
        self.assertEqual(
            config.strip_phrase("Hey Player play volume up", "hey player"),
            "play volume up",
        )


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
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None)
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
            result = tts.speak("Playing")
        self.assertFalse(result)

    def test_speak_engine_init_failure_returns_false(self):
        fake_module = types.ModuleType("pyttsx3")
        fake_module.init = MagicMock(side_effect=RuntimeError("no audio driver"))
        with patch.dict(sys.modules, {"pyttsx3": fake_module}):
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


class TestMainFeedback(unittest.TestCase):
    def test_handle_command_speaks_feedback_after_success(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Playing", voice_id=None)
        self.assertEqual(controller.calls, ["play"])

    def test_handle_command_speaks_command_failed_on_error(self):
        class BoomController:
            def play(self):
                raise RuntimeError("boom")

        tts_cfg = SimpleNamespace(enabled=True, voice_id=None)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), BoomController(), "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Command failed")

    def test_handle_command_exit_stops_listener_and_speaks(self):
        listener = _FakeStopListener()
        tts_cfg = SimpleNamespace(enabled=True, voice_id=None)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(listener, None, "exit", FAKE_LOG, tts_cfg)
        mock_speak.assert_called_once_with("Exiting", voice_id=None)
        self.assertTrue(listener.stopped)

    def test_tts_disabled_does_not_speak(self):
        controller = _FakeCommandController()
        tts_cfg = SimpleNamespace(enabled=False, voice_id=None)
        with patch("main.tts.speak") as mock_speak:
            main.handle_command(_FakeStopListener(), controller, "play", FAKE_LOG, tts_cfg)
        mock_speak.assert_not_called()

    def test_all_actions_have_tts_feedback(self):
        actions = vars(config.get_config().commands)
        missing = [action for action in actions if action not in main.TTS_PHRASES]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()