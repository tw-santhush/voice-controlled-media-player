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
_INDEX_MCP, _INDEX_PIP, _INDEX_DIP, _INDEX_TIP = 5, 6, 7, 8
_MIDDLE_MCP, _MIDDLE_PIP, _MIDDLE_DIP, _MIDDLE_TIP = 9, 10, 11, 12
_RING_MCP, _RING_PIP, _RING_DIP, _RING_TIP = 13, 14, 15, 16
_PINKY_MCP, _PINKY_PIP, _PINKY_DIP, _PINKY_TIP = 17, 18, 19, 20
_PALM = _MIDDLE_MCP

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


def _camera_candidates(camera_id: int) -> list[int]:
    """Ordered camera indices to try: the requested one, then 0, 1, and -1."""
    candidates = []
    for index in (camera_id, 0, 1, -1):
        if index not in candidates:
            candidates.append(index)
    return candidates


def _camera_backends() -> list:
    """Camera backend flags to try, platform-preferred first (DirectShow on Windows)."""
    if cv2 is None:
        return []
    backends = []
    for name in ("CAP_DSHOW", "CAP_MSMF", "CAP_V4L2", "CAP_ANY"):
        backend = getattr(cv2, name, None)
        if backend is not None and backend not in backends:
            backends.append(backend)
    return backends


SIGNAL_DELTA = 2
OPEN_WARMUP_TRIES = 15
CAMERA_WARMUP_FRAMES = 20


def _frame_has_signal(frame, delta: int = SIGNAL_DELTA) -> bool:
    """True when a frame contains real image content.

    Some drivers hand back constant-value (near-black) placeholder frames while
    the camera is unavailable or warming up; those carry valid dimensions but
    bare no signal, so ``read()`` succeeding says nothing about the image. A
    real frame - even in a dark room - has pixel values to spread across at
    least ``delta`` levels. Frames that do not expose min/max (test doubles,
    unusual CV variants) are treated as having signal so the check stays
    friction-free.
    """
    try:
        return float(frame.max()) - float(frame.min()) > delta
    except (AttributeError, TypeError, ValueError):
        return True


def _camera_present_but_silent(camera_id: int) -> bool:
    """True when a device can be opened but none of its frames carry image data.

    Distinguishes "no camera at all" from "camera present but not sending
    frames" (in use by another app, privacy-blocked, shutter closed, or a
    driver that refuses to stream) so the error message can point at the right
    fix.
    """
    if cv2 is None:
        return False
    for index in _camera_candidates(camera_id):
        for backend in _camera_backends():
            try:
                cap = cv2.VideoCapture(index, backend)
            except Exception:
                continue
            opened = bool(cap is not None and cap.isOpened())
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
            if opened:
                return True
    return False


def open_camera(camera_id: int = 0):
    """Open a webcam that reliably delivers frames, or return None.

    Tries the requested index (then 0, 1, -1) with each platform backend
    (DirectShow first on Windows) and reads warm-up frames. Frames that are
    valid-but-constant (black placeholders, no signal) do not count as
    "working": the capture is only accepted once a frame with real content
    arrives, otherwise the next backend/index is tried. The returned capture is
    open and producing real frames.
    """
    if cv2 is None:
        return None
    for index in _camera_candidates(camera_id):
        for backend in _camera_backends():
            try:
                cap = cv2.VideoCapture(index, backend)
            except Exception:
                cap = None
            if cap is None or not cap.isOpened():
                if cap is not None:
                    cap.release()
                continue
            for _ in range(OPEN_WARMUP_TRIES):
                try:
                    ret, frame = cap.read()
                except Exception:
                    ret = False
                if ret and frame is not None and frame.size > 0 and _frame_has_signal(frame):
                    logger.info("Camera %s opened (backend %s)", index, backend)
                    return cap
                time.sleep(0.03)
            cap.release()
    return None


def _angle_degrees(vertex, a, b) -> float:
    """Return the angle (0..180 degrees) at ``vertex`` between points ``a`` and ``b``.

    Computed from the vectors ``vertex -> a`` and ``vertex -> b`` via the dot
    product, so the result is independent of the hand's position and rotation
    (normalized landmark coordinates are fine; the metric is scale-invariant).
    """
    vx1 = a.x - vertex.x
    vy1 = a.y - vertex.y
    vx2 = b.x - vertex.x
    vy2 = b.y - vertex.y
    n1 = math.hypot(vx1, vy1)
    n2 = math.hypot(vx2, vy2)
    if n1 < 1e-9 or n2 < 1e-9:
        return 180.0
    cos_a = max(-1.0, min(1.0, (vx1 * vx2 + vy1 * vy2) / (n1 * n2)))
    return math.degrees(math.acos(cos_a))


def finger_extended(landmarks, tip_idx, pip_idx, mcp_idx, dip_idx, threshold_angle: float = 30.0) -> bool:
    """Return True when a long finger counts as extended.

    Measures the angle at the PIP joint formed by the MCP, PIP and DIP points.
    For a straight finger that angle is close to 180 degrees; when the finger
    curls into the palm it drops well below it. A finger is "extended" when the
    joint is within ``threshold_angle`` degrees of straight - so a finger that
    is only slightly raised (e.g. the ring finger while pointing) no longer
    counts, unlike a plain tip-vs-PIP height comparison.
    """
    angle = _angle_degrees(landmarks[pip_idx], landmarks[mcp_idx], landmarks[dip_idx])
    return angle >= 180.0 - max(0.0, min(180.0, threshold_angle))


def count_extended_fingers(landmarks, finger_angle_threshold: float = 30.0) -> list[bool]:
    """Return [thumb, index, middle, ring, pinky] extended booleans.

    `landmarks` is a sequence of objects with normalized ``.x`` and ``.y``
    attributes (0..1, y grows downward) as produced by MediaPipe.

    The index-to-pinky fingers use the rotation-independent angle at the PIP
    joint (see :func:`finger_extended`). The thumb uses a rotation-independent
    distance test to the index MCP joint, which works for both palms and either
    handedness.
    """
    threshold = max(0.0, min(180.0, finger_angle_threshold))
    index = finger_extended(landmarks, _INDEX_TIP, _INDEX_PIP, _INDEX_MCP, _INDEX_DIP, threshold)
    middle = finger_extended(landmarks, _MIDDLE_TIP, _MIDDLE_PIP, _MIDDLE_MCP, _MIDDLE_DIP, threshold)
    ring = finger_extended(landmarks, _RING_TIP, _RING_PIP, _RING_MCP, _RING_DIP, threshold)
    pinky = finger_extended(landmarks, _PINKY_TIP, _PINKY_PIP, _PINKY_MCP, _PINKY_DIP, threshold)

    ix = landmarks[_INDEX_MCP].x
    iy = landmarks[_INDEX_MCP].y
    d_tip = math.hypot(landmarks[_THUMB_TIP].x - ix, landmarks[_THUMB_TIP].y - iy)
    d_ip = math.hypot(landmarks[_THUMB_IP].x - ix, landmarks[_THUMB_IP].y - iy)
    thumb = d_tip > d_ip
    return [thumb, index, middle, ring, pinky]


def classify_thumb_direction(landmarks) -> str | None:
    """Return ``"up"`` or ``"down"`` for an extended thumb, or None.

    The hand's vertical axis runs from the wrist to the middle-finger MCP (the
    direction the fingers point along). The thumb vector (IP -> tip) is compared
    to that axis: aligned means "thumbs up", anti-aligned means "thumbs down".
    Comparing the thumb to the hand's own axis - rather than to the image's
    vertical - keeps the classification correct when the hand is tilted or
    rotated. A thumb pointing roughly perpendicular to the axis (>70 degrees
    from it) is ambiguous and returns None.
    """
    hx = landmarks[_PALM].x - landmarks[_WRIST].x
    hy = landmarks[_PALM].y - landmarks[_WRIST].y
    tx = landmarks[_THUMB_TIP].x - landmarks[_THUMB_IP].x
    ty = landmarks[_THUMB_TIP].y - landmarks[_THUMB_IP].y
    hn = math.hypot(hx, hy)
    tn = math.hypot(tx, ty)
    if hn < 1e-9 or tn < 1e-9:
        return None
    cos_a = max(-1.0, min(1.0, (hx * tx + hy * ty) / (hn * tn)))
    if cos_a > 0.2:
        return "up"
    if cos_a < -0.2:
        return "down"
    return None


def _relative_pinch_distance(landmarks) -> float:
    """Gap between the thumb and index tips, normalized by the hand span.

    Hand span is the wrist -> middle-MCP distance, so the ratio stays stable as
    the hand moves closer to or further from the camera (both the span and the
    gap shrink together) instead of needing an absolute pixel threshold.
    """
    span = math.hypot(landmarks[_PALM].x - landmarks[_WRIST].x,
                      landmarks[_PALM].y - landmarks[_WRIST].y)
    if span < 1e-6:
        return 1.0
    d = math.hypot(landmarks[_THUMB_TIP].x - landmarks[_INDEX_TIP].x,
                   landmarks[_THUMB_TIP].y - landmarks[_INDEX_TIP].y)
    return d / span


def _debug_finger_report(landmarks, ext, threshold) -> None:
    """Log the joint angles and extension verdicts behind a classification."""
    fingers = ("index", "middle", "ring", "pinky")
    for name, tip, pip, mcp, dip in zip(
        fingers,
        (_INDEX_TIP, _MIDDLE_TIP, _RING_TIP, _PINKY_TIP),
        (_INDEX_PIP, _MIDDLE_PIP, _RING_PIP, _PINKY_PIP),
        (_INDEX_MCP, _MIDDLE_MCP, _RING_MCP, _PINKY_MCP),
        (_INDEX_DIP, _MIDDLE_DIP, _RING_DIP, _PINKY_DIP),
    ):
        angle = _angle_degrees(landmarks[pip], landmarks[mcp], landmarks[dip])
        logger.debug(
            "%s finger: PIP angle=%.1fdeg (extended=%s)",
            name, angle, "yes" if angle >= 180.0 - threshold else "no",
        )
    logger.debug("thumb: extended=%s", "yes" if ext[0] else "no")


def classify_hand(
    landmarks,
    pinch_threshold_ratio: float = 0.25,
    finger_angle_threshold: float = 30.0,
    debug: bool = False,
) -> str | None:
    """Classify a hand shape into an action string, or None if unrecognized.

    Gesture mapping:
    - index finger only -> ``play_pause`` (toggle)
    - thumb only (pointing along/against the hand axis) -> ``volume_up`` /
      ``volume_down`` (continuous)
    - index + thumb pinched together (only those two extended) ->
      ``toggle_fullscreen``; the tip gap is compared against ``pinch_threshold_ratio``
      fractions of the hand span rather than an absolute distance
    - fist -> ``stop``
    - peace sign -> ``toggle_mute``

    Finger extension uses the PIP-joint angle (see :func:`count_extended_fingers`)
    with ``finger_angle_threshold`` degrees of allowed curvature. With ``debug``
    enabled the joint angles and verdicts are logged so thresholds can be tuned.
    A fully-open hand returns None: the open-hand swipes are handled by
    ``SwipeTracker`` so a stationary open palm deliberately fires nothing.
    """
    threshold = max(0.0, min(180.0, finger_angle_threshold))
    ext = count_extended_fingers(landmarks, threshold)
    if debug:
        _debug_finger_report(landmarks, ext, threshold)

    if not any(ext):
        return "stop"                        # Closed fist -> stop
    if ext[0] and ext[1] and not ext[2] and not ext[3] and not ext[4]:
        ratio = _relative_pinch_distance(landmarks)
        if debug:
            logger.debug("Pinch candidate: tip gap=%d%% of hand span (threshold %.0f%%)",
                         round(ratio * 100), round(pinch_threshold_ratio * 100))
        if ratio < pinch_threshold_ratio:
            return "toggle_fullscreen"       # Pinch -> fullscreen toggle
    if ext[1] and not ext[0] and not ext[2] and not ext[3] and not ext[4]:
        return "play_pause"                  # Index finger only -> play/pause toggle
    if ext[0] and not any(ext[1:]):
        direction = classify_thumb_direction(landmarks)
        if debug:
            logger.debug("Thumb-only: relative direction=%s", direction)
        if direction == "up":
            return "volume_up"               # Thumbs up -> volume up (continuous)
        if direction == "down":
            return "volume_down"             # Thumbs down -> volume down (continuous)
        return None
    if ext[1] and ext[2] and not ext[3] and not ext[4]:
        return "toggle_mute"                 # Peace sign -> mute/unmute
    return None


def draw_volume_bar(frame, percent, show_label: bool = True) -> None:
    """Draw a horizontal volume bar across the bottom of ``frame``.

    ``percent`` is clamped to 0-100. The bar is a filled rectangle with a
    percentage label; it is drawn in place on the given frame.
    """
    if cv2 is None or frame is None or percent is None:
        return
    percent = max(0, min(100, int(round(float(percent)))))
    h, w = frame.shape[:2]
    bar_h = 14
    y = h - bar_h - 8
    margin = 12
    x0, x1 = margin, w - margin
    full = x1 - x0
    cv2.rectangle(frame, (x0, y), (x1, y + bar_h), (50, 50, 50), -1)
    fill = int(round(full * percent / 100.0))
    if fill > 0:
        color = (0, 200, 90) if percent >= 50 else (0, 130, 220)
        cv2.rectangle(frame, (x0, y), (x0 + fill, y + bar_h), color, -1)
    cv2.rectangle(frame, (x0, y), (x1, y + bar_h), (220, 220, 220), 1)
    if show_label:
        label = f"Volume: {percent}%"
        cv2.putText(
            frame,
            label,
            (x0, y - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )


# BGR colors used by the on-preview gesture feedback overlay.
_FEEDBACK_GREEN = (60, 200, 60)
_FEEDBACK_BLUE = (255, 150, 40)
_FEEDBACK_ORANGE = (0, 165, 255)
_FEEDBACK_RED = (40, 40, 230)
_FEEDBACK_YELLOW = (40, 210, 230)
_FEEDBACK_CYAN = (220, 210, 40)

# action -> (human-readable gesture name, short description, BGR color).
# Volume descriptions are refined at draw time with the configured step.
GESTURE_FEEDBACK = {
    "play_pause": ("Index Finger Up", "Play / Pause", _FEEDBACK_GREEN),
    "stop": ("Closed Fist", "Stop", _FEEDBACK_RED),
    "volume_up": ("Thumbs Up", "Volume +{step}", _FEEDBACK_BLUE),
    "volume_down": ("Thumbs Down", "Volume -{step}", _FEEDBACK_BLUE),
    "toggle_mute": ("Peace Sign", "Mute / Unmute", _FEEDBACK_YELLOW),
    "toggle_fullscreen": ("Pinch", "Toggle Fullscreen", _FEEDBACK_CYAN),
    "skip_forward": ("Swipe Right", "Skip Forward", _FEEDBACK_ORANGE),
    "skip_backward": ("Swipe Left", "Skip Backward", _FEEDBACK_ORANGE),
    "volume_up_big": ("Swipe Up", "Volume +10", _FEEDBACK_ORANGE),
    "volume_down_big": ("Swipe Down", "Volume -10", _FEEDBACK_ORANGE),
}


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
    """Detect deliberate directional hand swipes from a rolling palm history.

    A swipe must satisfy all three conditions to fire (rejecting small jitters,
    tremors and direction flips that a plain distance check would swallow):

    - the palm travelled at least ``min_distance`` normalized units,
    - its average speed over the window is at least ``velocity_threshold``
      normalized units per second,
    - the most recent ``consistency_frames`` samples all moved toward the latest
      position in the same dominant direction.

    A swipe fires once per stroke: after firing, the same direction is not
    reported again until the hand slows back down below the thresholds.
    """

    _TOLERANCE = 0.005  # allowed landmark wobble against the dominant axis

    def __init__(
        self,
        window: float = 0.4,
        velocity_threshold: float = 0.5,
        min_distance: float = 0.12,
        consistency_frames: int = 3,
        threshold: float | None = None,
    ):
        self.window = max(0.05, window)
        self.velocity_threshold = max(0.01, threshold if threshold is not None else velocity_threshold)
        self.min_distance = max(0.005, min_distance)
        self.consistency_frames = max(2, int(consistency_frames))
        self._history = deque(maxlen=64)
        self._stroke = None
        self._ignore_static_until = 0.0

    def reset(self) -> None:
        self._history.clear()
        self._stroke = None

    def ignore_static(self, now: float) -> bool:
        """True shortly after a swipe, so the held hand pose isn't also fired."""
        return now < self._ignore_static_until

    def _consistent(self, direction: str, cx: float, cy: float) -> bool:
        """True when the recent samples kept moving toward (cx, cy) in `direction`.

        For a horizontal swipe the current x must be greater (or smaller) than
        every recent sample's x; the same applies to y for vertical swipes. A
        tiny backwards wobble within ``_TOLERANCE`` is accepted so single-frame
        landmark jitter doesn't kill a real swipe.
        """
        check_x = direction in ("left", "right")
        sign = 1.0 if direction in ("right", "down") else -1.0
        samples = list(self._history)[-(self.consistency_frames + 1):-1]
        if not samples:
            return True
        for _, x, y in samples:
            delta = ((cx - x) if check_x else (cy - y)) * sign
            if delta < -self._TOLERANCE:
                return False
        return True

    def feed(self, now: float, cx: float, cy: float) -> str | None:
        self._history.append((now, cx, cy))
        while self._history and self._history[0][0] < now - self.window:
            self._history.popleft()
        if len(self._history) < 2:
            return None

        first_t, first_x, first_y = self._history[0]
        elapsed = now - first_t
        dx = cx - first_x
        dy = cy - first_y
        distance = math.hypot(dx, dy)
        velocity = distance / elapsed if elapsed > 0 else 0.0

        # The hand is (mostly) at rest: allow the next stroke in the same
        # direction to fire again.
        if distance < self.min_distance or velocity < self.velocity_threshold:
            self._stroke = None
            return None

        if abs(dx) >= abs(dy):
            direction = "left" if dx < 0 else "right"
        else:
            direction = "up" if dy < 0 else "down"

        if not self._consistent(direction, cx, cy):
            return None

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
        show_feedback: bool = True,
        debounce_frames: int = 3,
        cooldown_seconds: float = 0.5,
        model_path: str | os.PathLike | None = None,
        swipe_window: float | None = None,
        swipe_velocity_threshold: float = 0.5,
        swipe_min_distance: float = 0.12,
        swipe_consistency_frames: int = 3,
        pinch_threshold_ratio: float = 0.25,
        finger_angle_threshold: float = 30.0,
        gesture_debug: bool = False,
        volume_interval_seconds: float = 0.5,
        volume_step: int = 5,
        volume_provider=None,
    ):
        self.camera_id = camera_id
        self.show_preview = show_preview
        self.show_feedback = bool(show_feedback)
        self.debounce_frames = max(1, debounce_frames)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.model_path = model_path
        self.pinch_threshold_ratio = max(0.01, pinch_threshold_ratio)
        self.finger_angle_threshold = max(0.0, min(180.0, finger_angle_threshold))
        self.gesture_debug = bool(gesture_debug)
        self.volume_interval_seconds = max(0.1, volume_interval_seconds)
        self.volume_step = max(1, int(volume_step))
        self.available = False
        self.error = None
        self.last_action = None
        self.volume_percent = 100
        self.volume_provider = volume_provider
        self._volume_hold_action = None
        self._volume_next_at = 0.0
        self._volume_cache = None
        self._volume_cache_at = 0.0
        self._stop_event = threading.Event()
        self._thread = None
        self._paused = False
        self._debouncer = Debouncer(self.debounce_frames, self.cooldown_seconds)
        self._swipes = SwipeTracker(
            window=swipe_window if swipe_window is not None else 0.4,
            velocity_threshold=swipe_velocity_threshold,
            min_distance=swipe_min_distance,
            consistency_frames=swipe_consistency_frames,
        )
        self._cap = None
        self._detector = None
        self._read_failures = 0
        self.max_read_failures = 10
        self.warm_up_frames = CAMERA_WARMUP_FRAMES
        self._frames_read = 0
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
        if not self._open_camera(self.camera_id):
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

    def _open_camera(self, camera_id: int | None = None) -> bool:
        """Open (or reopen) the webcam with backend and index fallback.

        Returns True when a validated camera is open; on failure the capture is
        released and ``self.error`` records why.
        """
        requested = self.camera_id if camera_id is None else camera_id
        self.camera_id = requested
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        if cv2 is None:
            self.available = False
            self.error = "opencv-python is not installed"
            return False
        cap = open_camera(requested)
        if cap is None:
            self.available = False
            if _camera_present_but_silent(requested):
                self.error = (
                    f"Camera {requested} was found but is not sending frames. Another app may be using the "
                    "webcam, Windows may be blocking camera access (Settings > Privacy > Camera), or the "
                    "physical shutter/camera hotkey is closed. Close other apps and try again."
                )
            else:
                self.error = f"Could not open camera {requested} with any backend"
            logger.error("%s", self.error)
            return False
        self._cap = cap
        self.available = True
        self.error = None
        self._frames_read = 0
        logger.info("Camera %s opened", requested)
        return True

    def set_camera(self, camera_id: int) -> bool:
        """Re-open camera ``camera_id`` (with fallback); returns True on success."""
        return self._open_camera(camera_id)

    def _annotate_frame(self, frame, landmarks, action) -> None:
        """Draw the hand skeleton and a gesture label onto ``frame`` in place."""
        if cv2 is None or frame is None:
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

    def _draw_preview(self, frame, landmarks, action) -> None:
        if frame is None:
            return
        self._annotate_frame(frame, landmarks, action)
        if self.show_feedback:
            self._draw_feedback(frame, action)
        if self.show_preview and cv2 is not None:
            draw_volume_bar(frame, self._current_volume())
            cv2.imshow("Gesture Control", frame)

    def _current_volume(self) -> int:
        """Return the volume to display (0-100), refreshing from the provider.

        The provider (see ``volume_provider``) is polled at most every 0.5s so a
        slow HTTP status read can't stall the preview loop. Without a provider a
        local counter tracked on each volume change is used.
        """
        now = time.time()
        if self.volume_provider is not None:
            if self._volume_cache is None or now - self._volume_cache_at >= 0.5:
                try:
                    value = self.volume_provider()
                    if value is not None:
                        self.volume_percent = max(0, min(100, int(round(float(value)))))
                except Exception:
                    logger.debug("Volume provider failed; using tracked volume %d", self.volume_percent)
                self._volume_cache_at = now
                self._volume_cache = self.volume_percent
            return self._volume_cache
        return self.volume_percent

    def _draw_feedback(self, frame, action) -> None:
        """Overlay the human-readable gesture name, action and volume on ``frame``.

        Drawn at the top-left below the gesture label when ``show_feedback`` is
        enabled. Volume actions include the configured step and the current
        volume; each gesture type gets its own color (green play, blue volume,
        orange skip, red stop, yellow mute, cyan fullscreen).
        """
        if cv2 is None or frame is None:
            return
        entry = GESTURE_FEEDBACK.get(action)
        if entry is None:
            return
        name, description, color = entry
        description = description.replace("{step}", str(self.volume_step))
        lines = [name, description]
        lines.append(f"Volume: {self._current_volume()}%")
        y = 66
        for i, text in enumerate(lines):
            cv2.putText(
                frame,
                text,
                (12, y + i * 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7 if i == 0 else 0.55,
                color if i == 0 else (255, 255, 255),
                2 if i == 0 else 1,
                cv2.LINE_AA,
            )

    def _track_volume(self, action) -> None:
        """Keep the local volume estimate in sync when a volume action fires."""
        if action == "volume_up":
            self.volume_percent = max(0, min(100, self.volume_percent + self.volume_step))
        elif action == "volume_down":
            self.volume_percent = max(0, min(100, self.volume_percent - self.volume_step))
        self._volume_cache = None

    def get_preview_frame(self):
        """Return one annotated frame for display, or None when unavailable.

        Used by ``--raw-preview``-style debugging and external preview loops:
        the frame is always returned (even when no hand is detected), with the
        hand skeleton overlay drawn when landmarks are present.
        """
        if not self.available or self._cap is None or self._detector is None:
            return None
        try:
            ok, frame = self._cap.read()
        except Exception:
            return None
        if not ok or frame is None:
            return None
        frame = cv2.flip(frame, 1)
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            landmarks = self._detector.process(rgb)
        except Exception:
            landmarks = None
        action = None
        if landmarks is not None:
            now = time.time()
            palm = landmarks[_PALM]
            swipe = self._swipes.feed(now, palm.x, palm.y)
            if swipe:
                action = swipe
            elif not self._swipes.ignore_static(now):
                action = classify_hand(
                    landmarks,
                    pinch_threshold_ratio=self.pinch_threshold_ratio,
                    finger_angle_threshold=self.finger_angle_threshold,
                    debug=self.gesture_debug,
                )
        self._annotate_frame(frame, landmarks, action)
        if self.show_feedback:
            self._draw_feedback(frame, action)
        if self.show_preview:
            draw_volume_bar(frame, self._current_volume())
        return frame

    def detect_gesture(self) -> str | None:
        """Capture one frame and return an action string, or None.

        Raises no exceptions when the camera is unavailable; it returns None
        and logs at debug level. After several consecutive failed reads the
        camera is re-opened automatically.
        """
        if not self.available or self._cap is None or self._detector is None:
            return None
        try:
            ok, frame = self._cap.read()
        except Exception as exc:
            logger.debug("Camera read failed: %s", exc)
            ok = False
        if not ok or frame is None:
            self._read_failures += 1
            if self._read_failures >= self.max_read_failures:
                logger.warning("Camera read failed %d times; re-opening the camera", self._read_failures)
                self._open_camera()
                self._read_failures = 0
            return None
        self._read_failures = 0
        self._frames_read += 1

        if not _frame_has_signal(frame) and self._frames_read > self.warm_up_frames:
            logger.debug("Camera frame has no signal (constant value); treating as a read failure")
            self._read_failures += 1
            if self._read_failures >= self.max_read_failures:
                logger.warning(
                    "Camera has delivered %d signal-less frames; re-opening the camera", self._read_failures
                )
                if not self._open_camera():
                    logger.error("Could not re-open the camera; stopping gesture detection")
                    self._stop_event.set()
                self._read_failures = 0
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
            self._volume_hold_action = None
            self._volume_next_at = 0.0
            self._draw_preview(frame, None, None)
            return None

        palm = landmarks[_PALM]
        swipe = self._swipes.feed(now, palm.x, palm.y)
        if swipe:
            # Swipes are inherently single-shot (SwipeTracker fires once per
            # stroke), so they fire immediately instead of going through the
            # frame debouncer, which would swallow the single event.
            self._volume_hold_action = None
            self._volume_next_at = 0.0
            self._draw_preview(frame, landmarks, swipe)
            self.last_action = swipe
            logger.info("Detected swipe: %s", swipe)
            return swipe

        action = None
        if not self._swipes.ignore_static(now):
            action = classify_hand(
                landmarks,
                pinch_threshold_ratio=self.pinch_threshold_ratio,
                finger_angle_threshold=self.finger_angle_threshold,
                debug=self.gesture_debug,
            )
        self._draw_preview(frame, landmarks, action)

        if action in ("volume_up", "volume_down"):
            self._debouncer.reset()
            if self._volume_hold_action != action:
                self._volume_hold_action = action
                self._volume_next_at = 0.0
            if action == "volume_up" and self._current_volume() >= 100:
                return None
            if action == "volume_down" and self._current_volume() <= 0:
                return None
            if now < self._volume_next_at:
                return None
            self._volume_next_at = now + self.volume_interval_seconds
            logger.info("Detected gesture: %s (continuous)", action)
            self._track_volume(action)
            self.last_action = action
            return action

        self._volume_hold_action = None
        self._volume_next_at = 0.0
        action = self._debouncer.feed(action)
        if action:
            self.last_action = action
            logger.info("Detected gesture: %s", action)
        return action

    def run_loop(self, callback) -> None:
        """Continuously capture frames and call ``callback(action)``.

        Blocks until ``stop()`` is called. A preview window is shown when
        ``show_preview`` is enabled; pressing ``q`` in the window stops the loop.
        """
        if not self.available:
            logger.error("Gesture detection unavailable: %s", self.error)
            return
        if self.show_preview and cv2 is not None:
            try:
                cv2.namedWindow("Gesture Control", cv2.WINDOW_NORMAL)
            except Exception:
                pass
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