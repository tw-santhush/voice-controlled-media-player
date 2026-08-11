import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import requests
import speech_recognition as sr
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


if __name__ == "__main__":
    unittest.main()