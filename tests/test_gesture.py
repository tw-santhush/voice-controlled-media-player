import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config
import config_loader
import gesture
import main
import requests
import tray
from player_control import AutoController, MPCController, PlayerController, VLCController


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
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "volume", "val": 150})

    def test_vlc_set_volume_scales_app_percent_to_vlc_range(self):
        # App 0-100 maps to VLC's 0-200 range: 50% -> val 100, 100% -> val 200.
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        for app_percent, vlc_value in [(0, 0), (50, 100), (100, 200)]:
            with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
                controller.set_volume(app_percent)
            self.assertEqual(mock_get.call_args[1]["params"]["val"], vlc_value)

    def test_vlc_set_volume_clamps_to_100_percent(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.set_volume(500)
        self.assertEqual(mock_get.call_args[1]["params"]["val"], 200)

    def test_vlc_set_volume_100_percent_reaches_full_200_range(self):
        # The user-visible 100% must map to VLC's max raw value (200), not 78%'s 156.
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.set_volume(100)
        self.assertEqual(mock_get.call_args[1]["params"]["val"], 200)

    def test_vlc_get_volume_full_200_range_is_100_percent(self):
        controller = VLCController()
        with patch.object(controller, "_status", return_value=(200, False)):
            self.assertEqual(controller.get_volume(), 100)

    def test_vlc_get_volume_scales_to_app_percent(self):
        controller = VLCController()
        with patch.object(controller, "_status", return_value=(140, False)):
            self.assertEqual(controller.get_volume(), 70)

    def test_vlc_get_volume_clamps_to_100_percent(self):
        controller = VLCController()
        with patch.object(controller, "_status", return_value=(420, False)):
            self.assertEqual(controller.get_volume(), 100)

    def test_vlc_get_volume_returns_none_on_failure(self):
        controller = VLCController()
        with patch.object(controller, "_status", side_effect=requests.ConnectionError("refused")):
            self.assertIsNone(controller.get_volume())

    def test_vlc_volume_up_doubles_app_step_on_vlc_scale(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller, "_status", return_value=(100, False)):
            with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
                controller.volume_up(step=5)
        self.assertEqual(mock_get.call_args[1]["params"]["val"], 110)

    def test_vlc_volume_down_doubles_app_step_on_vlc_scale(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller, "_status", return_value=(100, False)):
            with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
                controller.volume_down(step=5)
        self.assertEqual(mock_get.call_args[1]["params"]["val"], 90)

    def test_vlc_volume_up_goes_through_set_volume_in_percent(self):
        controller = VLCController()
        with patch.object(controller, "get_volume", return_value=50):
            with patch.object(controller, "set_volume") as mock_set:
                controller.volume_up(step=5)
        mock_set.assert_called_once_with(55)

    def test_vlc_volume_down_goes_through_set_volume_in_percent(self):
        controller = VLCController()
        with patch.object(controller, "get_volume", return_value=50):
            with patch.object(controller, "set_volume") as mock_set:
                controller.volume_down(step=5)
        mock_set.assert_called_once_with(45)

    def test_vlc_volume_up_falls_back_when_read_fails(self):
        controller = VLCController()
        with patch.object(controller, "get_volume", return_value=None):
            with patch.object(controller, "_fallback") as mock_fallback:
                controller.volume_up(step=5)
        mock_fallback.assert_called_once_with("volume_up")

    def test_vlc_volume_down_falls_back_when_read_fails(self):
        controller = VLCController()
        with patch.object(controller, "get_volume", return_value=None):
            with patch.object(controller, "_fallback") as mock_fallback:
                controller.volume_down(step=5)
        mock_fallback.assert_called_once_with("volume_down")

    def test_vlc_next_sends_pl_next(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.next()
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "pl_next"})

    def test_vlc_volume_scale_helpers_round_trip(self):
        from player_control import _app_volume_from_vlc, _vlc_volume_from_app

        self.assertEqual(_app_volume_from_vlc(156), 78)
        self.assertEqual(_vlc_volume_from_app(78), 156)
        for percent in range(0, 101, 5):
            self.assertLessEqual(abs(_app_volume_from_vlc(_vlc_volume_from_app(percent)) - percent), 1)

    def test_vlc_status_returns_raw_volume_unscaled(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        mock_response.text = (
            "<root><fullscreen>false</fullscreen><volume>156</volume>"
            "<muted>false</muted><length>0</length></root>"
        )
        with patch.object(controller.session, "get", return_value=mock_response):
            volume, muted = controller._status()
        self.assertEqual((volume, muted), (156.0, False))

    def test_vlc_get_volume_logs_raw_and_scaled(self):
        controller = VLCController()
        with patch.object(controller, "_status", return_value=(156, False)):
            with self.assertLogs("player_control", level="DEBUG") as cm:
                self.assertEqual(controller.get_volume(), 78)
        text = "\n".join(cm.output)
        self.assertIn("VLC raw volume: 156", text)
        self.assertIn("scaled: 78", text)

    def test_vlc_set_volume_logs_percent_and_raw(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response):
            with self.assertLogs("player_control", level="DEBUG") as cm:
                controller.set_volume(75)
        text = "\n".join(cm.output)
        self.assertIn("Setting VLC volume: 75% -> raw 150", text)

    def test_vlc_toggle_fullscreen_sends_fullscreen_command(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        mock_response.url = "http://localhost:8080/requests/status.xml?command=fullscreen"
        mock_response.status_code = 200
        state = {"fullscreen": False}

        def flip_state():
            state["fullscreen"] = not state["fullscreen"]
            return state["fullscreen"]

        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            with patch.object(controller, "_fullscreen_state", side_effect=flip_state):
                with self.assertLogs("player_control", level="DEBUG") as cm:
                    self.assertTrue(controller.toggle_fullscreen())
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "fullscreen"})
        self.assertEqual(mock_get.call_count, 1)
        text = "\n".join(cm.output)
        self.assertIn("command=fullscreen", text)
        self.assertIn("HTTP 200", text)

    def test_vlc_toggle_fullscreen_retries_alternate_command(self):
        # If command=fullscreen raises, the alternate command name is tried and
        # its effect verified before success is reported.
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        mock_response.url = "http://localhost:8080/requests/status.xml?command=toggle_fullscreen"
        mock_response.status_code = 200
        states = iter([False, True])  # before, after(toggle_fullscreen)
        with patch.object(controller.session, "get", side_effect=[requests.ConnectionError("refused"), mock_response]) as mock_get:
            with patch.object(controller, "_fullscreen_state", side_effect=lambda: next(states)):
                self.assertTrue(controller.toggle_fullscreen())
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[1][1]["params"], {"command": "toggle_fullscreen"})

    def test_vlc_toggle_fullscreen_ignores_noop_commands_then_keyboard(self):
        # command=fullscreen returns 200 but VLC ignores it (state never
        # changes); every candidate is tried, then the keyboard fallback fires.
        controller = VLCController()
        mock_ok = unittest.mock.MagicMock()
        mock_ok.url = "http://localhost:8080/requests/status.xml?command=fullscreen"
        mock_ok.status_code = 200
        with patch.object(controller, "_fullscreen_state", return_value=False):
            with patch.object(controller.session, "get", return_value=mock_ok) as mock_get:
                with patch.object(controller, "_fallback") as mock_fallback:
                    self.assertFalse(controller.toggle_fullscreen())
        self.assertEqual(mock_get.call_count, 3)
        commands = [call[1]["params"]["command"] for call in mock_get.call_args_list]
        self.assertEqual(commands, ["fullscreen", "toggle_fullscreen", "fullscreen_toggle"])
        mock_fallback.assert_called_once_with("fullscreen")

    def test_vlc_toggle_fullscreen_assumes_success_when_state_unreadable(self):
        controller = VLCController()
        mock_response = unittest.mock.MagicMock()
        mock_response.url = "http://localhost:8080/requests/status.xml?command=fullscreen"
        mock_response.status_code = 200
        with patch.object(controller, "_fullscreen_state", return_value=None):
            with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
                self.assertTrue(controller.toggle_fullscreen())
        self.assertEqual(mock_get.call_args[1]["params"], {"command": "fullscreen"})

    def test_vlc_toggle_fullscreen_falls_back_to_keyboard_when_all_fail(self):
        controller = VLCController()
        with patch.object(controller.session, "get", side_effect=requests.ConnectionError("refused")):
            with patch.object(controller, "_fallback") as mock_fallback:
                self.assertFalse(controller.toggle_fullscreen())
        mock_fallback.assert_called_once_with("fullscreen")

    def test_mpc_toggle_fullscreen_sends_wm_830(self):
        controller = MPCController()
        mock_response = unittest.mock.MagicMock()
        with patch.object(controller.session, "get", return_value=mock_response) as mock_get:
            controller.toggle_fullscreen()
        self.assertEqual(mock_get.call_args[1]["params"], {"wm_command": 830})


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


class _Landmark:
    __slots__ = ("x", "y")

    def __init__(self, x=0.0, y=0.0):
        self.x = x
        self.y = y


def _hand(fingers=(True, True, True, True, True), rotate=0.0, thumb_tip=None, thumb_ip=None):
    """Build a 21-keypoint hand landmark list with realistic joint geometry.

    ``fingers = (thumb, index, middle, ring, pinky)``. A ``True`` long finger is
    straight (PIP joint angle ~180deg), a ``False`` one is curled into the palm
    (PIP joint angle ~18deg) so the angle-based detector can tell them apart.
    The thumb sticks up-left when extended and tucks down near its IP joint when
    folded. ``rotate`` spins the whole hand about the wrist (degrees) to model a
    tilted/rotated hand, and ``thumb_tip``/``thumb_ip`` override those landmarks
    (used for thumbs up/down and pinch fixtures).
    """
    wrist = (0.5, 0.85)
    lm = [_Landmark(*wrist) for _ in range(21)]

    # Thumb
    lm[1] = _Landmark(0.35, 0.75)  # CMC
    lm[2] = _Landmark(0.33, 0.62)  # MCP
    lm[3] = _Landmark(*thumb_ip) if thumb_ip is not None else _Landmark(0.35, 0.50)  # IP
    if thumb_tip is not None:
        lm[4] = _Landmark(*thumb_tip)
    elif fingers[0]:
        lm[4] = _Landmark(0.26, 0.38)  # extended: up-left of the thumb IP
    else:
        lm[4] = _Landmark(0.38, 0.53)  # folded: tucked near the thumb IP

    # Long fingers: (mcp_idx, mcp_x, pip_idx, dip_idx, tip_idx)
    bases = [
        (5, 0.44, 6, 7, 8),
        (9, 0.50, 10, 11, 12),
        (13, 0.56, 14, 15, 16),
        (17, 0.62, 18, 19, 20),
    ]
    mcp_y = 0.60
    for mcp_i, mcp_x, pip_i, dip_i, tip_i in bases:
        if fingers[(mcp_i - 5) // 4 + 1]:
            pip = (mcp_x, mcp_y - 0.09)
            dip = (mcp_x, mcp_y - 0.16)
            tip = (mcp_x, mcp_y - 0.21)
        else:
            pip = (mcp_x, mcp_y - 0.07)
            dip = (mcp_x + 0.04, mcp_y + 0.05)
            tip = (mcp_x + 0.06, mcp_y + 0.10)
        lm[mcp_i] = _Landmark(mcp_x, mcp_y)
        lm[pip_i] = _Landmark(*pip)
        lm[dip_i] = _Landmark(*dip)
        lm[tip_i] = _Landmark(*tip)

    if rotate:
        angle = math.radians(rotate)
        ca, sa = math.cos(angle), math.sin(angle)
        for i, p in enumerate(lm):
            dx, dy = p.x - wrist[0], p.y - wrist[1]
            lm[i] = _Landmark(wrist[0] + dx * ca - dy * sa, wrist[1] + dx * sa + dy * ca)
    return lm


class TestGestureRecognition(unittest.TestCase):
    def test_open_hand_counts_five_fingers(self):
        lm = _hand()
        self.assertEqual(gesture.count_extended_fingers(lm), [True, True, True, True, True])

    def test_fist_counts_no_fingers(self):
        lm = _hand((False, False, False, False, False))
        self.assertEqual(gesture.count_extended_fingers(lm), [False, False, False, False, False])

    def test_peace_only_index_and_middle(self):
        lm = _hand((False, True, True, False, False))
        self.assertEqual(gesture.count_extended_fingers(lm), [False, True, True, False, False])

    def test_classify_open_hand_none(self):
        self.assertEqual(gesture.classify_hand(_hand()), None)

    def test_classify_fist_does_nothing(self):
        self.assertIsNone(gesture.classify_hand(_hand((False, False, False, False, False))))

    def test_classify_fist_debug_logs_ignored(self):
        with self.assertLogs("gesture", level="DEBUG") as cm:
            gesture.classify_hand(_hand((False, False, False, False, False)), debug=True)
        text = "\n".join(cm.output)
        self.assertIn("Classification: None (closed fist ignored (no action))", text)

    def test_classify_peace_mute(self):
        self.assertEqual(gesture.classify_hand(_hand((False, True, True, False, False))), "toggle_mute")

    def test_classify_index_only_play_pause(self):
        self.assertEqual(gesture.classify_hand(_hand((False, True, False, False, False))), "play_pause")

    def test_classify_pinch_fullscreen(self):
        lm = _hand((True, True, False, False, False))
        # Duplicate pinch: only thumb + index extended, tips close together.
        lm[gesture._INDEX_PIP] = _Landmark(0.40, 0.51)
        lm[gesture._INDEX_DIP] = _Landmark(0.36, 0.44)
        lm[gesture._INDEX_TIP] = _Landmark(0.32, 0.37)
        lm[gesture._THUMB_TIP] = _Landmark(0.34, 0.36)
        self.assertEqual(gesture.classify_hand(lm), "toggle_fullscreen")

    def test_pinch_requires_other_fingers_folded(self):
        lm = _hand((True, True, True, False, False))
        lm[gesture._INDEX_PIP] = _Landmark(0.40, 0.51)
        lm[gesture._INDEX_DIP] = _Landmark(0.36, 0.44)
        lm[gesture._INDEX_TIP] = _Landmark(0.32, 0.37)
        lm[gesture._THUMB_TIP] = _Landmark(0.34, 0.36)
        self.assertNotEqual(gesture.classify_hand(lm), "toggle_fullscreen")

    def test_pinch_open_gap_not_a_pinch(self):
        lm = _hand((True, True, False, False, False))
        lm[gesture._THUMB_TIP] = _Landmark(0.55, 0.20)  # far from the index tip
        self.assertIsNone(gesture.classify_hand(lm))

    def test_classify_thumbs_up_volume_up(self):
        lm = _hand((True, False, False, False, False))
        self.assertEqual(gesture.classify_hand(lm), "volume_up")

    def test_classify_thumbs_down_volume_down(self):
        lm = _hand((True, False, False, False, False))
        lm[gesture._THUMB_TIP] = _Landmark(0.30, 0.72)
        self.assertEqual(gesture.classify_hand(lm), "volume_down")

    def test_thumbs_up_survives_hand_rotation(self):
        lm = _hand((True, False, False, False, False), rotate=45)
        self.assertEqual(gesture.classify_hand(lm), "volume_up")

    def test_thumbs_down_survives_hand_rotation(self):
        lm = _hand((True, False, False, False, False), rotate=45)
        lm[gesture._THUMB_TIP] = _Landmark(0.30, 0.72)
        self.assertEqual(gesture.classify_hand(lm), "volume_down")

    def test_slightly_raised_ring_finger_stays_folded(self):
        # The classic misclassification: only the index is pointing, but the
        # ring finger's tip sits above its PIP. A plain tip-vs-PIP height test
        # would call it extended; the PIP-joint angle test sees a curled finger.
        lm = _hand((False, True, False, False, False))
        lm[gesture._RING_PIP] = _Landmark(0.57, 0.53)
        lm[gesture._RING_DIP] = _Landmark(0.52, 0.46)
        lm[gesture._RING_TIP] = _Landmark(0.50, 0.40)
        self.assertFalse(gesture.count_extended_fingers(lm)[3])
        self.assertEqual(gesture.classify_hand(lm), "play_pause")

    def test_stricter_angle_threshold_rejects_curved_thumb_only(self):
        # With a steep threshold a partially curled finger no longer counts as
        # extended when it is really just raised rather than straight.
        lm = _hand((False, True, False, False, False))
        lm[gesture._MIDDLE_PIP] = _Landmark(0.50, 0.53)
        lm[gesture._MIDDLE_DIP] = _Landmark(0.44, 0.50)
        lm[gesture._MIDDLE_TIP] = _Landmark(0.43, 0.45)
        self.assertFalse(gesture.count_extended_fingers(lm, finger_angle_threshold=45.0)[2])

    def test_classify_hand_debug_logs_reason(self):
        lm = _hand((True, False, False, False, False))
        with self.assertLogs("gesture", level="DEBUG") as cm:
            gesture.classify_hand(lm, debug=True)
        text = "\n".join(cm.output)
        self.assertIn("thumb: extended=yes", text)
        self.assertIn("index finger: PIP angle", text)
        self.assertIn("Extended fingers: [True, False, False, False, False] (count=1)", text)
        self.assertIn("Thumb-only: relative direction=up", text)
        self.assertIn("Classification: volume_up", text)

    def test_classify_hand_debug_logs_play_pause_reason(self):
        lm = _hand((False, True, False, False, False))
        with self.assertLogs("gesture", level="DEBUG") as cm:
            gesture.classify_hand(lm, debug=True)
        text = "\n".join(cm.output)
        self.assertIn("Classification: play_pause (index finger only)", text)

    def test_classify_hand_no_debug_log_by_default(self):
        logger = logging.getLogger("gesture")
        previous_level = logger.level
        captured = []
        handler = logging.Handler()
        handler.emit = lambda record: captured.append(record.getMessage())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            gesture.classify_hand(_hand((True, False, False, False, False)))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)
        self.assertEqual(captured, [])

    def test_thumb_direction_vertical_up_when_axis_ambiguous(self):
        # Sideways hand: the hand axis is horizontal and the thumb points
        # straight up, so the axis dot-product is ~0 (ambiguous). The
        # camera-vertical angle must decide "up".
        lm = _hand((True, False, False, False, False))
        lm[gesture._PALM] = _Landmark(0.70, 0.85)
        lm[gesture._THUMB_IP] = _Landmark(0.70, 0.80)
        lm[gesture._THUMB_TIP] = _Landmark(0.70, 0.70)
        self.assertEqual(gesture.classify_thumb_direction(lm), "up")

    def test_thumb_direction_vertical_down_when_axis_ambiguous(self):
        lm = _hand((True, False, False, False, False))
        lm[gesture._PALM] = _Landmark(0.70, 0.85)
        lm[gesture._THUMB_IP] = _Landmark(0.70, 0.80)
        lm[gesture._THUMB_TIP] = _Landmark(0.70, 0.95)
        self.assertEqual(gesture.classify_thumb_direction(lm), "down")

    def test_thumb_direction_returns_none_when_ambiguous(self):
        # Thumb midway between up and down (~49deg above horizontal) while the
        # hand axis is horizontal too: neither the camera-vertical window nor
        # the hand-axis fallback recognizes it, so nothing fires.
        lm = _hand((True, False, False, False, False))
        lm[gesture._PALM] = _Landmark(0.70, 0.85)
        lm[gesture._THUMB_IP] = _Landmark(0.72, 0.80)
        lm[gesture._THUMB_TIP] = _Landmark(0.79, 0.72)
        self.assertIsNone(gesture.classify_thumb_direction(lm))

    def test_thumb_direction_diagonal_down_within_tolerance_fires(self):
        # A thumb pointing 70deg from camera vertical (=20deg off straight
        # down) must still read as "down": the widened window absorbs the
        # diagonals a real thumb makes instead of requiring near-perfect
        # vertical pointing.
        lm = _hand((True, False, False, False, False))
        lm[gesture._THUMB_IP] = _Landmark(0.70, 0.80)
        lm[gesture._THUMB_TIP] = _Landmark(0.70 + 0.15 * math.cos(math.radians(70)),
                                           0.80 + 0.15 * math.sin(math.radians(70)))
        self.assertEqual(gesture.classify_thumb_direction(lm), "down")

    def test_thumb_direction_debug_logs_camera_angle_and_decision(self):
        lm = _hand((True, False, False, False, False))
        with self.assertLogs("gesture", level="DEBUG") as cm:
            self.assertEqual(gesture.classify_thumb_direction(lm, debug=True), "up")
        text = "\n".join(cm.output)
        self.assertIn("camera angle=", text)
        self.assertIn("up -", text)
        self.assertIn("down +", text)

    def test_thumb_direction_hysteresis_holds_direction_through_flicker(self):
        now = [100.0]
        holder = gesture.ThumbDirectionHysteresis(hold_seconds=0.3, now_fn=lambda: now[0])
        self.assertEqual(holder.feed("down"), "down")
        # A briefly-ambiguous frame is still reported as "down" for 0.3s.
        self.assertEqual(holder.feed(None), "down")
        now[0] += 0.1
        self.assertEqual(holder.feed(None), "down")
        # Once the hold expires, the new (empty) verdict passes through.
        now[0] += 0.4
        self.assertIsNone(holder.feed(None))

    def test_thumb_direction_hysteresis_yields_to_steady_opposite(self):
        now = [100.0]
        holder = gesture.ThumbDirectionHysteresis(hold_seconds=0.3, now_fn=lambda: now[0])
        holder.feed("up")
        now[0] += 0.1
        # A mid-hold flip is kept sticky...
        self.assertEqual(holder.feed("down"), "up")
        # ...until the hold window passes, then the new direction takes over.
        now[0] += 0.4
        self.assertEqual(holder.feed("down"), "down")

    def test_thumb_direction_hysteresis_reset_clears_hold(self):
        holder = gesture.ThumbDirectionHysteresis(hold_seconds=0.3)
        holder.feed("down")
        holder.reset()
        self.assertIsNone(holder.feed(None))


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
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.7, 0.5), "skip_forward")

    def test_swipe_left_skips_backward(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.2, 0.5), "skip_backward")

    def test_swipe_up_volume_up_big(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.5, 0.2), "volume_up_big")

    def test_swipe_down_volume_down_big(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.5, 0.8), "volume_down_big")

    def test_same_direction_does_not_repeat(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.7, 0.5), "skip_forward")
        self.assertIsNone(tracker.feed(100.2, 0.9, 0.5))

    def test_slow_drift_is_not_a_swipe(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=3.0, min_distance=0.01)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))

    def test_small_movement_is_not_a_swipe(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.01, min_distance=0.2)
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))

    def test_back_and_forth_oscillation_is_not_a_swipe(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.01, min_distance=0.15)
        tracker.feed(100.0, 0.5, 0.5)
        tracker.feed(100.1, 0.6, 0.5)
        self.assertIsNone(tracker.feed(100.2, 0.5, 0.5))

    def test_consistent_accepts_monotonic_motion(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.01, min_distance=0.01)
        tracker.feed(100.0, 0.5, 0.5)
        tracker.feed(100.1, 0.7, 0.5)
        tracker.feed(100.2, 0.8, 0.5)
        self.assertTrue(tracker._consistent("right", 0.8, 0.5))

    def test_consistent_rejects_wobble(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.01, min_distance=0.01)
        tracker.feed(100.0, 0.5, 0.5)
        tracker.feed(100.1, 0.7, 0.5)
        tracker.feed(100.2, 0.6, 0.5)  # snaps back left - not a consistent swipe
        self.assertFalse(tracker._consistent("right", 0.6, 0.5))

    def test_requires_consistent_direction_before_firing(self):
        tracker = gesture.SwipeTracker(window=0.5, velocity_threshold=0.01, min_distance=0.01, consistency_frames=3)
        tracker.feed(100.0, 0.5, 0.5)
        tracker.feed(100.05, 0.7, 0.5)
        # Overall the hand travelled right, but the last step wobbles the other
        # way, so no swipe is reported for this frame.
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))

    def test_quick_flick_fires_within_hold_window(self):
        # A fast flick whose total travel stays under min_distance is armed on
        # the first fast frame and fires on the next consistent fast frame, so
        # a flick that stops immediately is still recognized.
        tracker = gesture.SwipeTracker(
            window=0.5, velocity_threshold=0.2, min_distance=0.15, consistency_frames=2, hold=0.15
        )
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))      # fast but short: armed
        self.assertEqual(tracker.feed(100.2, 0.61, 0.5), "skip_forward")

    def test_swipe_hold_expires_when_hand_rests(self):
        tracker = gesture.SwipeTracker(
            window=0.5, velocity_threshold=0.2, min_distance=0.2, consistency_frames=2, hold=0.15
        )
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.6, 0.5))      # armed
        self.assertIsNone(tracker.feed(100.3, 0.61, 0.5))     # past the hold window

    def test_swipe_debug_logs_velocity_components(self):
        tracker = gesture.SwipeTracker(
            window=0.5, velocity_threshold=0.2, min_distance=0.1, consistency_frames=2, debug=True
        )
        tracker.feed(100.0, 0.5, 0.5)
        with self.assertLogs("gesture", level="DEBUG") as cm:
            tracker.feed(100.1, 0.7, 0.5)
        text = "\n".join(cm.output)
        self.assertIn("Swipe: dx=", text)
        self.assertIn("travel=", text)
        self.assertIn("velocity=", text)
        self.assertIn("direction=right", text)
        self.assertIn("consistent=True", text)

    def test_post_swipe_cooldown_blocks_repeat_trigger(self):
        # After a swipe fires, the tail of the same fast movement is ignored
        # for 0.3s so one flick cannot fire two commands back-to-back.
        tracker = gesture.SwipeTracker(
            window=0.5, velocity_threshold=0.01, min_distance=0.01, consistency_frames=2, cooldown=0.3
        )
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.7, 0.5), "skip_forward")
        self.assertIsNone(tracker.feed(100.15, 0.9, 0.5))
        self.assertIsNone(tracker.feed(100.2, 0.95, 0.5))

    def test_post_swipe_cooldown_allows_later_stroke(self):
        # Once the hand rests below the velocity threshold and the cooldown
        # passes, a new deliberate swipe in the same direction fires again.
        tracker = gesture.SwipeTracker(
            window=0.5, velocity_threshold=0.5, min_distance=0.01, consistency_frames=2, cooldown=0.3
        )
        tracker.feed(100.0, 0.5, 0.5)
        self.assertEqual(tracker.feed(100.1, 0.7, 0.5), "skip_forward")
        tracker.feed(100.5, 0.72, 0.5)  # resting: clears the stroke
        tracker.feed(100.6, 0.73, 0.5)  # resting
        self.assertEqual(tracker.feed(100.7, 0.9, 0.5), "skip_forward")

    def test_small_quick_flick_fires_with_relaxed_defaults(self):
        # Default thresholds (min_distance 0.08, velocity 0.2) recognize a
        # short flick that travels only ~0.05 normalized units.
        tracker = gesture.SwipeTracker()
        tracker.feed(100.0, 0.5, 0.5)
        self.assertIsNone(tracker.feed(100.1, 0.52, 0.5))  # fast but short: armed
        self.assertEqual(tracker.feed(100.2, 0.55, 0.5), "skip_forward")


class TestGestureFeedback(unittest.TestCase):
    def test_every_gesture_action_has_feedback(self):
        actions = [
            "play_pause",
            "volume_up",
            "volume_down",
            "toggle_mute",
            "toggle_fullscreen",
            "skip_forward",
            "skip_backward",
            "volume_up_big",
            "volume_down_big",
        ]
        for action in actions:
            self.assertIn(action, gesture.GESTURE_FEEDBACK)
            name, description, color = gesture.GESTURE_FEEDBACK[action]
            self.assertTrue(name)
            self.assertTrue(description)
            self.assertEqual(len(color), 3)

    def test_feedback_defaults_to_enabled(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController()
        self.assertTrue(controller.show_feedback)

    def test_feedback_can_be_disabled(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController(show_feedback=False)
        self.assertFalse(controller.show_feedback)

    def _feedback_controller(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            return gesture.GestureController()

    def test_draw_feedback_overlays_name_action_and_volume(self):
        controller = self._feedback_controller()
        controller.volume_step = 5
        controller._current_volume = MagicMock(return_value=45)
        cv2 = MagicMock()
        frame = MagicMock()
        frame.shape = (480, 640, 3)
        with patch.object(gesture, "cv2", cv2):
            controller._draw_feedback(frame, "volume_up")
        texts = [call.args[1] for call in cv2.putText.call_args_list]
        self.assertIn("Thumbs Up", texts)
        self.assertIn("Volume +5", texts)
        self.assertIn("Volume: 45%", texts)

    def test_draw_feedback_uses_config_step_for_volume_desc(self):
        controller = self._feedback_controller()
        controller.volume_step = 2
        controller._current_volume = MagicMock(return_value=40)
        cv2 = MagicMock()
        frame = MagicMock()
        frame.shape = (480, 640, 3)
        with patch.object(gesture, "cv2", cv2):
            controller._draw_feedback(frame, "volume_down")
        texts = [call.args[1] for call in cv2.putText.call_args_list]
        self.assertIn("Volume -2", texts)

    def test_draw_feedback_ignores_unknown_action(self):
        controller = self._feedback_controller()
        controller._current_volume = MagicMock(return_value=50)
        cv2 = MagicMock()
        frame = MagicMock()
        frame.shape = (480, 640, 3)
        with patch.object(gesture, "cv2", cv2):
            controller._draw_feedback(frame, "wiggle")
        cv2.putText.assert_not_called()


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

    def test_default_config_has_gesture_feedback_flag(self):
        self.assertIs(config_loader.DEFAULT_CONFIG["gesture"]["show_feedback"], True)

    def test_default_config_has_tray_section(self):
        self.assertIn("tray", config_loader.DEFAULT_CONFIG)
        self.assertIs(config_loader.DEFAULT_CONFIG["tray"]["enabled"], False)
        self.assertIs(config_loader.DEFAULT_CONFIG["tray"]["auto_start"], False)

    def test_default_config_has_no_voice_sections(self):
        for section in ("voice", "recognizer", "tts", "wake", "commands", "push_to_talk"):
            self.assertNotIn(section, config_loader.DEFAULT_CONFIG)

    def test_default_config_gesture_thresholds_are_tuned(self):
        g = config_loader.DEFAULT_CONFIG["gesture"]
        self.assertEqual(g["finger_angle_threshold"], 20)
        self.assertEqual(g["pinch_threshold_ratio"], 0.12)
        self.assertEqual(g["swipe_min_distance"], 0.08)
        self.assertEqual(g["swipe_velocity_threshold"], 0.2)
        self.assertEqual(g["swipe_consistency_frames"], 4)
        self.assertEqual(g["swipe_cooldown_seconds"], 0.3)
        self.assertEqual(g["thumb_up_angle_threshold"], 30)
        self.assertEqual(g["thumb_down_angle_threshold"], 30)

    def test_load_config_merges_missing_tray_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_cfg = {"vlc": {"enabled": False}}
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(user_cfg), encoding="utf-8")
            cfg = config_loader.load_config(path)
        self.assertIs(cfg.vlc.enabled, False)
        self.assertIs(cfg.tray.enabled, False)
        self.assertIs(cfg.tray.auto_start, False)
        self.assertIs(cfg.gesture.show_feedback, True)


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

    def test_detect_gesture_logs_raw_classification_in_debug(self):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            controller = gesture.GestureController(gesture_debug=True)
        controller._cap = _FakeCap([(True, _Frame())])
        detector = MagicMock()
        detector.process.return_value = _hand()
        controller._detector = detector
        controller.available = True
        cv2 = MagicMock()
        cv2.flip.side_effect = lambda frame, code: frame
        cv2.cvtColor.side_effect = lambda frame, code: frame
        with patch.object(gesture, "cv2", cv2):
            with self.assertLogs("gesture", level="DEBUG") as cm:
                self.assertIsNone(controller.detect_gesture())
        text = "\n".join(cm.output)
        self.assertIn("Raw classification (pre-debounce): None", text)
        self.assertIn("Classification: None (no matching shape)", text)


class TestGestureControllerSwipe(unittest.TestCase):
    def _controller(self, debounce_frames=5):
        with patch.object(gesture, "cv2", None), patch.object(gesture, "HAS_MP", False):
            return gesture.GestureController(
                debounce_frames=debounce_frames,
                cooldown_seconds=0.0,
                swipe_min_distance=0.2,
            )

    def _palm(self, x):
        lm = _hand()
        lm[gesture._PALM] = _Landmark(x, 0.6)
        return lm

    def _frame_cv2(self):
        cv2 = MagicMock()
        cv2.flip.side_effect = lambda frame, code: frame
        cv2.cvtColor.side_effect = lambda frame, code: frame
        return cv2

    def test_swipe_fires_immediately_even_with_long_debounce(self):
        # Regression: swipes used to go through the frame debouncer, so a single
        # deliberate swipe was dropped unless the hand happened to hold still for
        # debounce_frames frames. A swipe is inherently single-shot and must fire
        # as soon as it crosses the velocity threshold.
        controller = self._controller(debounce_frames=5)
        controller._cap = _FakeCap([(True, _Frame()) for _ in range(8)])
        detector = MagicMock()
        detector.process.side_effect = [self._palm(0.5), self._palm(0.66), self._palm(0.82), None]
        controller._detector = detector
        controller.available = True
        with patch.object(gesture, "cv2", self._frame_cv2()):
            self.assertIsNone(controller.detect_gesture())
            self.assertIsNone(controller.detect_gesture())
            time.sleep(0.01)
            self.assertEqual(controller.detect_gesture(), "skip_forward")

    def test_swipe_does_not_fire_then_again_as_static_gesture(self):
        controller = self._controller()
        controller._cap = _FakeCap([(True, _Frame()) for _ in range(8)])
        detector = MagicMock()
        # The swipe is followed by a held open palm, which must not produce an
        # action: an open hand is a no-op and the swing-back already fired.
        detector.process.side_effect = [self._palm(0.5), self._palm(0.66), self._palm(0.82), self._palm(0.82)]
        controller._detector = detector
        controller.available = True
        with patch.object(gesture, "cv2", self._frame_cv2()):
            self.assertIsNone(controller.detect_gesture())
            self.assertIsNone(controller.detect_gesture())
            time.sleep(0.01)
            self.assertEqual(controller.detect_gesture(), "skip_forward")
            self.assertIsNone(controller.detect_gesture())


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


class TestGestureActions(unittest.TestCase):
    def _controller(self):
        return _RecordingController()

    def _state(self):
        return {"listening": True, "indicators": False}

    def test_play_pause_toggles(self):
        controller = self._controller()
        state = self._state()
        main.handle_gesture_action("play_pause", controller, FAKE_LOG, state)
        self.assertEqual(controller.calls, ["play"])
        main.handle_gesture_action("play_pause", controller, FAKE_LOG, state)
        self.assertEqual(controller.calls, ["play", "pause"])

    def test_stop_action(self):
        controller = self._controller()
        main.handle_gesture_action("stop", controller, FAKE_LOG, self._state())
        self.assertEqual(controller.calls, ["stop"])

    def test_toggle_fullscreen_action(self):
        controller = self._controller()
        with self.assertLogs("test", level="INFO") as cm:
            main.handle_gesture_action("toggle_fullscreen", controller, FAKE_LOG, self._state())
        self.assertEqual(controller.calls, ["toggle_fullscreen"])
        self.assertIn("Fullscreen toggled", "\n".join(cm.output))

    def test_volume_up_big_steps_ten(self):
        controller = _RecordingController()
        main.handle_gesture_action("volume_up_big", controller, FAKE_LOG, self._state())
        self.assertEqual(controller.calls, [("volume_up", 10)])

    def test_paused_state_ignores_gesture(self):
        controller = self._controller()
        state = {"listening": False}
        main.handle_gesture_action("stop", controller, FAKE_LOG, state)
        self.assertEqual(controller.calls, [])

    def test_unknown_action_ignored(self):
        controller = self._controller()
        main.handle_gesture_action("wiggle", controller, FAKE_LOG, self._state())
        self.assertEqual(controller.calls, [])


class TestHotkey(unittest.TestCase):
    def test_hotkey_registers_and_toggles_listening(self):
        fake_icon = MagicMock()
        fake_icon._refresh_image = MagicMock()
        state = {"listening": True, "indicators": False}
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
            with patch("gesture_tray.create_gesture_tray_icon", return_value=fake_icon) as mock_create:
                with patch("threading.Timer", MagicMock()):
                    with redirect_stdout(StringIO()):
                        main.run_test_tray(FAKE_LOG)
        mock_create.assert_called_once()
        args, kwargs = mock_create.call_args
        gesture_stub = args[0]
        self.assertFalse(gesture_stub.running)
        self.assertTrue(kwargs["verbose"])
        fake_icon.run.assert_called_once()

    def test_test_tray_warns_without_pystray(self):
        buffer = StringIO()
        with patch("tray._tray_available", return_value=False):
            with redirect_stdout(buffer):
                main.run_test_tray(FAKE_LOG)
        self.assertIn("pystray is not installed", buffer.getvalue())


class TestStartupHelpers(unittest.TestCase):
    def test_uninstall_startup_returns_false_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(main.uninstall_startup(Path(tmp) / "missing.lnk"))

    def test_uninstall_startup_removes_existing_shortcut(self):
        with tempfile.TemporaryDirectory() as tmp:
            shortcut = Path(tmp) / "Gesture Media Player.lnk"
            shortcut.write_text("stub")
            self.assertTrue(main.uninstall_startup(shortcut))
            self.assertFalse(shortcut.exists())

    def test_install_startup_requires_tray_support(self):
        with patch("tray._tray_available", return_value=False), tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                main.install_startup(Path(tmp) / "Gesture Media Player.lnk")


class TestTrayIconStates(unittest.TestCase):
    def test_green_when_listening(self):
        self.assertEqual(tray._current_color({"listening": True}), tray.GREEN)

    def test_red_when_paused(self):
        self.assertEqual(tray._current_color({"listening": False}), tray.RED)


FAKE_LOG = logging.getLogger("test")


if __name__ == "__main__":
    unittest.main()