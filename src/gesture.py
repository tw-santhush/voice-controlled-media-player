"""Hand-gesture control using OpenCV and MediaPipe.

Supports both MediaPipe generations:

- **MediaPipe 1.x** (e.g. 1.0.x): the legacy ``solutions`` API was removed, so
  the new Tasks API (``mediapipe.tasks.python.vision.HandLandmarker``) is used.
  The ``hand_landmarker.task`` model is downloaded automatically on first use.
- **MediaPipe 0.x**: the legacy ``mp.solutions.hands`` API is used as before.

The module degrades gracefully: when the libraries or the hand model are
missing, ``GestureController`` sets ``available = False`` and main.py reports
and skips gesture mode.
"""

import importlib
import logging
import math
import os
import shutil
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import cv2
except ImportError:
    cv2 = None

try:
    import mediapipe as mp
except ImportError:
    mp = None

# Hand-landmarker model used by the MediaPipe 1.x Tasks API.
HAND_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
DEFAULT_MODEL_PATH = Path.home() / ".cache" / "mediapipe" / "hand_landmarker.task"

# MediaPipe backends, discovered at import time. At least one being non-None
# means gesture detection can work (the model still has to load at runtime).
_TASKS_VISION = None
_HANDS_SOLUTION = None

if mp is not None:
    for package in ("mediapipe.tasks.python", "mediapipe.tasks"):
        try:
            _TASKS_VISION = importlib.import_module(f"{package}.vision")
            break
        except Exception:
            _TASKS_VISION = None
    if not _TASKS_VISION:
        try:
            _HANDS_SOLUTION = mp.solutions.hands
        except Exception:
            _HANDS_SOLUTION = None

HAS_MP = _TASKS_VISION is not None or _HANDS_SOLUTION is not None

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

# Standard MediaPipe hand skeleton topology, used for the preview overlay.
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4),  # thumb
    (0, 5), (5, 6), (6, 7), (7, 8),  # index
    (5, 9), (9, 10), (10, 11), (11, 12),  # middle
    (9, 13), (13, 14), (14, 15), (15, 16),  # ring
    (13, 17), (17, 18), (18, 19), (19, 20),  # pinky
    (0, 17),
)


def gesture_backend_available() -> bool:
    """True when OpenCV and a MediaPipe hand-tracking backend can both be imported."""
    return cv2 is not None and HAS_MP


def count_extended_fingers(landmarks) -> list[bool]:
    """Return [thumb, index, middle, ring, pinky] extended booleans.

    `landmarks` is a sequence of objects with normalized ``.x`` and ``.y``
    attributes (0..1, y grows downward) as produced by MediaPipe.

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


def _download_hand_model(path: Path) -> None:
    """Download the hand-landmarker model to ``path`` if it is missing."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading MediaPipe hand-landmarker model (%s)...", HAND_MODEL_URL)
    request = urllib.request.Request(HAND_MODEL_URL, headers={"User-Agent": "voice-controlled-media-player/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response, open(path, "wb") as out:
        shutil.copyfileobj(response, out)
    logger.info("Hand-landmarker model saved to %s", path)


class _SolutionsDetector:
    """Legacy ``mp.solutions.hands`` backend (MediaPipe 0.x)."""

    name = "solutions"

    def __init__(self):
        self._hands = _HANDS_SOLUTION.Hands(
            static_image_mode=False,
            max_num_hands=1,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

    def process(self, rgb) -> list | None:
        """Return the first hand's landmarks (objects with .x/.y), or None."""
        results = self._hands.process(rgb)
        if not results.multi_hand_landmarks:
            return None
        return results.multi_hand_landmarks[0].landmark

    def close(self) -> None:
        try:
            self._hands.close()
        except Exception:
            pass


class _TasksDetector:
    """New ``mediapipe.tasks.python.vision.HandLandmarker`` backend (MediaPipe 1.x)."""

    name = "tasks"

    def __init__(self, model_path: str):
        from mediapipe.tasks.python import BaseOptions

        options = _TASKS_VISION.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path)),
            running_mode=_TASKS_VISION.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=0.5,
            min_hand_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        self._landmarker = _TASKS_VISION.HandLandmarker.create_from_options(options)
        self._frame_timestamp_ms = 0

    def process(self, rgb) -> list | None:
        """Return the first hand's landmarks (objects with .x/.y), or None."""
        self._frame_timestamp_ms += 33
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = self._landmarker.detect_for_video(image, self._frame_timestamp_ms)
        if not result.hand_landmarks:
            return None
        return result.hand_landmarks[0]

    def close(self) -> None:
        try:
            self._landmarker.close()
        except Exception:
            pass


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
        x0, y0 = self._history[0][1], self._history[0][2]
        dx = cx - x0
        dy = cy - y0
        speed = math.hypot(dx, dy)
        if speed < self.threshold:
            self._stroke = None
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
    """Detect hand gestures from a webcam using MediaPipe.

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
        model_path: str | os.PathLike | None = None,
        swipe_threshold: float | None = None,
    ):
        self.camera_id = camera_id
        self.show_preview = show_preview
        self.debounce_frames = max(1, debounce_frames)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.model_path = model_path
        self.available = False
        self.error = None
        self.last_action = None
        self._stop_event = threading.Event()
        self._thread = None
        self._paused = False
        self._debouncer = Debouncer(self.debounce_frames, self.cooldown_seconds)
        self._swipes = (
            SwipeTracker(threshold=swipe_threshold) if swipe_threshold is not None else SwipeTracker()
        )
        self._cap = None
        self._detector = None
        self._setup()

    def _setup(self) -> None:
        if cv2 is None or not HAS_MP:
            missing = []
            if cv2 is None:
                missing.append("opencv-python")
            if not HAS_MP:
                missing.append("mediapipe")
            self.error = (
                " and ".join(missing) + " "
                + ("are" if len(missing) > 1 else "is")
                + " not installed; run: pip install -r requirements.txt"
            )
            return
        try:
            self._cap = cv2.VideoCapture(self.camera_id)
            if not self._cap.isOpened():
                self._cap.release()
                self._cap = None
                self.error = f"Could not open camera {self.camera_id}"
                return
        except Exception as exc:
            self.error = str(exc)
            logger.exception("Failed to open camera %s", self.camera_id)
            return
        self._detector = self._make_detector()
        if self._detector is None:
            if self._cap is not None:
                self._cap.release()
                self._cap = None
            return
        self.available = True
        logger.info("Gesture control active (camera %s, backend %s)", self.camera_id, self._detector.name)

    def _make_detector(self):
        """Build a detector on whichever MediaPipe backend is available."""
        if _HANDS_SOLUTION is not None:
            try:
                return _SolutionsDetector()
            except Exception as exc:
                logger.exception("Failed to initialize legacy MediaPipe hands: %s", exc)
                if _TASKS_VISION is None:
                    self.error = str(exc)
                    return None
        if _TASKS_VISION is not None:
            model = self._resolve_model_path()
            if model is None:
                return None
            try:
                return _TasksDetector(model)
            except Exception as exc:
                self.error = f"Failed to initialize MediaPipe HandLandmarker: {exc}"
                logger.exception("Failed to initialize MediaPipe HandLandmarker")
                return None
        self.error = (
            "mediapipe is not installed (or has no hand-tracking API); run: pip install -r requirements.txt"
        )
        return None

    def _resolve_model_path(self):
        """Return the hand-landmarker model path, downloading it if needed."""
        path = self.model_path
        if path is None:
            env = os.environ.get("MEDIAPIPE_HAND_MODEL", "").strip()
            path = env if env else DEFAULT_MODEL_PATH
        path = Path(path).expanduser()
        if path.exists():
            return path
        try:
            _download_hand_model(path)
            return path
        except Exception as exc:
            self.error = (
                f"Could not download the MediaPipe hand-landmarker model to {path}: {exc} "
                "(set gesture.model_path in config.json or the MEDIAPIPE_HAND_MODEL "
                "environment variable to a pre-downloaded .task file)"
            )
            logger.exception("Hand-landmarker model download failed")
            return None

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
                self.available = False
            return opened
        except Exception as exc:
            self.error = str(exc)
            self.available = False
            return False

    def _draw_preview(self, frame, landmarks, action) -> None:
        if cv2 is None or not self.show_preview or frame is None:
            return
        if landmarks is not None:
            h, w = frame.shape[:2]
            points = [(int(lm.x * w), int(lm.y * h)) for lm in landmarks]
            for a, b in HAND_CONNECTIONS:
                cv2.line(frame, points[a], points[b], (0, 255, 0), 2)
            for p in points:
                cv2.circle(frame, p, 3, (0, 0, 255), -1)
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
        if not self.available or self._cap is None or self._detector is None:
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
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = self._detector.process(rgb)
        except Exception as exc:
            logger.debug("Hand detection failed: %s", exc)
            return None
        now = time.time()

        if landmarks is None:
            self._swipes.reset()
            self._debouncer.reset()
            self._draw_preview(frame, None, None)
            return None

        palm = landmarks[_PALM]
        swipe = self._swipes.feed(now, palm.x, palm.y)
        if swipe:
            self._draw_preview(frame, landmarks, swipe)
            action = self._debouncer.feed(swipe)
            if action:
                self.last_action = action
            return action

        action = None
        if not self._swipes.ignore_static(now):
            action = classify_hand(landmarks)
        self._draw_preview(frame, landmarks, action)
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
        if self._detector is not None:
            self._detector.close()
            self._detector = None
        if self.show_preview and cv2 is not None:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

    @property
    def running(self) -> bool:
        return not self._stop_event.is_set()