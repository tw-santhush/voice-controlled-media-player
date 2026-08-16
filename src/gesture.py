"""Hand-gesture control using OpenCV and MediaPipe.

Requires the optional `opencv-python`, `mediapipe`, and `numpy` packages. The
module degrades gracefully: if the libraries are missing, `GestureController`
sets ``available = False`` and main.py reports and skips gesture mode.
"""

import logging
import math
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import mediapipe as mp

    _HANDS_SOLUTION = mp.solutions.hands
except ImportError:
    mp = None
    _HANDS_SOLUTION = None

# Map a swipe direction to a player action string.
SWIPE_ACTIONS = {
    "left": "skip_backward",
    "right": "skip_forward",
    "up": "volume_up_big",
    "down": "volume_down_big",
}

# MediaPipe hand-landmark indices referenced below.
_WRIST = 0
_THUMB_CMC, _THUMB_MCP, _THUMB_IP, _THUMB_TIP = 1, 2, 3, 4
_INDEX_MCP, _INDEX_PIP, _INDEX_TIP = 5, 6, 8
_MIDDLE_PIP, _MIDDLE_TIP = 10, 12
_RING_PIP, _RING_TIP = 14, 16
_PINKY_PIP, _PINKY_TIP = 18, 20
_PALM = 9


def gesture_backend_available() -> bool:
    """True when OpenCV and MediaPipe can both be imported."""
    return cv2 is not None and _HANDS_SOLUTION is not None


def count_extended_fingers(landmarks) -> list[bool]:
    """Return [thumb, index, middle, ring, pinky] extended booleans.

    `landmarks` is a sequence of objects with normalized ``.x`` and ``.y``
    attributes (0..1, y grows downward) as produced by MediaPipe Hands.

    The index-to-pinky fingers count as extended when their tip is above their
    PIP joint. The thumb uses a rotation-independent distance test to the index
    MCP joint, which works for both palms and either handedness.
    """
    index = landmarks[_INDEX_TIP].y < landmarks[_INDEX_PIP].y
    middle = landmarks[_MIDDLE_TIP].y < landmarks[_MIDDLE_PIP].y
    ring = landmarks[_RING_TIP].y < landmarks[_RING_PIP].y
    pinky = landmarks[_PINKY_TIP].y < landmarks[_PINKY_PIP].y

    ix = landmarks[_INDEX_MCP].x
    iy = landmarks[_INDEX_MCP].y
    d_tip = math.hypot(landmarks[_THUMB_TIP].x - ix, landmarks[_THUMB_TIP].y - iy)
    d_ip = math.hypot(landmarks[_THUMB_IP].x - ix, landmarks[_THUMB_IP].y - iy)
    thumb = d_tip > d_ip
    return [thumb, index, middle, ring, pinky]


def classify_hand(landmarks) -> str | None:
    """Classify a hand shape into an action string, or None if unrecognized."""
    ext = count_extended_fingers(landmarks)
    count = sum(ext)
    if count == 5:
        return "play_pause"          # Open hand -> play/pause toggle
    if count == 0:
        return "stop"                # Closed fist -> stop
    if ext[0] and not any(ext[1:]):
        wrist_y = landmarks[_WRIST].y
        if landmarks[_THUMB_TIP].y < wrist_y:
            return "volume_up"       # Thumbs up -> volume up (+5)
        if landmarks[_THUMB_TIP].y > wrist_y:
            return "volume_down"     # Thumbs down -> volume down (-5)
        return None
    if ext[1] and ext[2] and not ext[3] and not ext[4]:
        return "toggle_mute"         # Peace sign -> mute/unmute
    if ext[1] and not ext[2] and not ext[3] and not ext[4]:
        return "toggle_fullscreen"   # Index pointing up -> fullscreen
    return None


class Debouncer:
    """Confirm a gesture across several frames and prevent double triggers.

    An action is emitted only after the same candidate is seen for `frames`
    consecutive frames, and never twice in a row (the user must release the
    gesture first). After firing, all gestures are ignored for `cooldown`
    seconds so a quick hand motion can't fire two commands at once.
    """

    def __init__(self, frames: int = 3, cooldown: float = 0.5, now_fn=time.time):
        self.frames = max(1, frames)
        self.cooldown = max(0.0, cooldown)
        self._now = now_fn
        self._candidate = None
        self._count = 0
        self._last_fired = None
        self._cooldown_until = 0.0

    def feed(self, action: str | None) -> str | None:
        """Feed one frame's candidate; return the action to fire, or None."""
        now = self._now()
        if action is None:
            self._candidate = None
            self._count = 0
            self._last_fired = None
            return None
        if action != self._candidate:
            self._candidate = action
            self._count = 1
        else:
            self._count += 1
        if self._count < self.frames:
            return None
        if action == self._last_fired:
            self._candidate = None
            self._count = 0
            return None
        if now < self._cooldown_until:
            return None
        self._cooldown_until = now + self.cooldown
        self._last_fired = action
        self._candidate = None
        self._count = 0
        return action

    def reset(self) -> None:
        self._candidate = None
        self._count = 0


class SwipeTracker:
    """Detect directional hand swipes from a rolling position history.

    Feeds timed (x, y) palm positions. A swipe fires once per stroke: the
    speed must exceed `threshold` across the `window`, and the next swipe in the
    same direction is only reported after the hand slows down again.
    """

    def __init__(self, window: float = 0.5, threshold: float = 0.12):
        self.window = max(0.05, window)
        self.threshold = max(0.01, threshold)
        self._history = deque(maxlen=60)
        self._stroke = None
        self._ignore_static_until = 0.0

    def reset(self) -> None:
        self._history.clear()
        self._stroke = None

    def ignore_static(self, now: float) -> bool:
        """True shortly after a swipe, so the held hand pose isn't also fired."""
        return now < self._ignore_static_until

    def feed(self, now: float, cx: float, cy: float) -> str | None:
        self._history.append((now, cx, cy))
        while self._history and self._history[0][0] < now - self.window:
            self._history.popleft()
        if len(self._history) < 2:
            return None
        t0, x0, y0 = self._history[0]
        dx = cx - x0
        dy = cy - y0
        speed = math.hypot(dx, dy)
        if speed < self.threshold:
            self._stroke = None
            _ = t0
            return None
        if abs(dx) >= abs(dy):
            direction = "left" if dx < 0 else "right"
        else:
            direction = "up" if dy < 0 else "down"
        if direction == self._stroke:
            return None
        self._stroke = direction
        self._ignore_static_until = now + 0.6
        return SWIPE_ACTIONS[direction]


class GestureController:
    """Detect hand gestures from a webcam using MediaPipe Hands.

    Attributes:
        available: True when OpenCV/MediaPipe loaded and the camera opened.
        error: description of why detection is unavailable (else None).
        running: True while the loop is alive (mirrors VoiceListener).
    """

    def __init__(
        self,
        camera_id: int = 0,
        show_preview: bool = False,
        debounce_frames: int = 3,
        cooldown_seconds: float = 0.5,
    ):
        self.camera_id = camera_id
        self.show_preview = show_preview
        self.debounce_frames = max(1, debounce_frames)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.available = False
        self.error = None
        self.last_action = None
        self._stop_event = threading.Event()
        self._thread = None
        self._paused = False
        self._debouncer = Debouncer(self.debounce_frames, self.cooldown_seconds)
        self._swipes = SwipeTracker()
        self._cap = None
        self._hands = None
        self._setup()

    def _setup(self) -> None:
        if cv2 is None or _HANDS_SOLUTION is None:
            self.error = (
                "opencv-python and/or mediapipe are not installed; "
                "run: pip install -r requirements.txt"
            )
            return
        try:
            self._cap = cv2.VideoCapture(self.camera_id)
            if not self._cap.isOpened():
                self._cap.release()
                self._cap = None
                self.error = f"Could not open camera {self.camera_id}"
                return
            self._hands = _HANDS_SOLUTION.Hands(
                static_image_mode=False,
                max_num_hands=1,
                min_detection_confidence=0.5,
                min_tracking_confidence=0.5,
            )
            self.available = True
            logger.info("Gesture control active (camera %s)", self.camera_id)
        except Exception as exc:
            self.error = str(exc)
            logger.exception("Failed to initialize gesture control: %s", exc)

    def set_camera(self, camera_id: int) -> bool:
        """Re-open a different camera; returns True on success."""
        self.camera_id = camera_id
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if cv2 is None:
            return False
        try:
            self._cap = cv2.VideoCapture(camera_id)
            opened = bool(self._cap and self._cap.isOpened())
            self.available = opened
            if opened:
                logger.info("Gesture control switched to camera %s", camera_id)
            else:
                self._cap.release()
                self._cap = None
                self.error = f"Could not open camera {camera_id}"
            return opened
        except Exception as exc:
            self.error = str(exc)
            return False

    def _draw_preview(self, frame, results, action) -> None:
        if cv2 is None or not self.show_preview:
            return
        if results.multi_hand_landmarks:
            drawing_utils = mp.solutions.drawing_utils
            for hand_landmarks in results.multi_hand_landmarks:
                drawing_utils.draw_landmarks(
                    frame,
                    hand_landmarks,
                    _HANDS_SOLUTION.HAND_CONNECTIONS,
                )
        label = f"Gesture: {action}" if action else "Gesture: (none)"
        cv2.putText(
            frame,
            label,
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow("Gesture Control", frame)

    def detect_gesture(self) -> str | None:
        """Capture one frame and return an action string, or None.

        Raises no exceptions when the camera is unavailable; it returns None
        and logs at debug level.
        """
        if not self.available or self._cap is None or self._hands is None:
            return None
        try:
            ok, frame = self._cap.read()
        except Exception as exc:
            logger.debug("Camera read failed: %s", exc)
            return None
        if not ok:
            logger.debug("Camera returned no frame")
            return None

        frame = cv2.flip(frame, 1)  # mirror like a selfie view
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self._hands.process(rgb)
        now = time.time()

        if not results.multi_hand_landmarks:
            self._swipes.reset()
            self._debouncer.reset()
            self._draw_preview(frame, results, None)
            return None

        landmarks = results.multi_hand_landmarks[0].landmark
        palm = landmarks[_PALM]
        swipe = self._swipes.feed(now, palm.x, palm.y)
        if swipe:
            self._draw_preview(frame, results, swipe)
            action = self._debouncer.feed(swipe)
            if action:
                self.last_action = action
            return action

        action = None
        if not self._swipes.ignore_static(now):
            action = classify_hand(landmarks)
        self._draw_preview(frame, results, action)
        action = self._debouncer.feed(action)
        if action:
            self.last_action = action
        return action

    def run_loop(self, callback) -> None:
        """Continuously capture frames and call ``callback(action)``.

        Blocks until ``stop()`` is called. A preview window is shown when
        ``show_preview`` is enabled; pressing ``q`` in the window stops the loop.
        """
        if not self.available:
            logger.error("Gesture detection unavailable: %s", self.error)
            return
        self._stop_event.clear()
        while not self._stop_event.is_set():
            if self._paused:
                self._stop_event.wait(0.25)
                continue
            action = self.detect_gesture()
            if action:
                try:
                    callback(action)
                except Exception:
                    logger.exception("Gesture callback failed for %r", action)
            if self.show_preview and cv2 is not None:
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    logger.info("Preview window closed; stopping detection")
                    self._stop_event.set()

    def pause(self) -> None:
        """Pause detection without closing the camera."""
        self._paused = True

    def resume(self) -> None:
        """Resume detection after pause()."""
        self._paused = False

    def wait_stop(self, timeout=0.5) -> bool:
        """Block until stop() is called or the timeout elapses."""
        return self._stop_event.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if self.show_preview and cv2 is not None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()