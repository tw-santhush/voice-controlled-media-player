# Gesture-Controlled VLC Player

Control the VLC media player using **hand gestures** through your webcam. No voice, no remote — just wave your hand.

The app reads your webcam feed with OpenCV + MediaPipe, classifies the gesture, and drives VLC over its HTTP web interface. Fingers do the talking; the audio stays yours.

> Voice control was removed from this project. If you have an older checkout, ignore the old `--mode voice` flags — they are legacy and no longer supported.

## Features

| Gesture                          | Action              | Behavior                          |
| -------------------------------- | ------------------- | --------------------------------- |
| Index finger (pointing up)       | Play / Pause        | Toggle once                       |
| Thumbs up                        | Volume +5%          | Continuous while held             |
| Thumbs down                      | Volume -5%          | Continuous while held             |
| Open hand swipe right            | Skip forward +10s   | Trigger once                      |
| Open hand swipe left             | Skip backward -10s  | Trigger once                      |
| Pinch (index + thumb)            | Fullscreen          | Toggle once                       |
| Peace sign (index + middle)      | Mute                | Toggle once                       |

A closed fist deliberately does **nothing** — it is a resting pose, not a command.

## Prerequisites

- Windows (Linux/macOS may work but is not officially tested)
- Python 3.8+
- VLC media player installed
- Webcam
- VLC web interface enabled (guide below)

## Installation

```bash
git clone https://github.com/tw-santhush/voice-controlled-media-player.git
cd voice-controlled-media-player
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Configuration

### Enable the VLC web interface

1. Open VLC → Tools → Preferences.
2. Switch **Show settings** to **All** → Interface → Main interfaces.
3. Check **Web**.
4. Under **Lua → Lua HTTP**, set the **Password** to `admin` (or match whatever you put in `config.json`).
5. Restart VLC.

The app talks to VLC at `http://localhost:8080` with password `admin` by default — all of that lives in `config.json`.

### Edit config.json

A `config.json` is auto-created with defaults on first run (the committed `config.example.json` is the safe template). Adjust the webcam index, gesture thresholds, and VLC connection there:

```json
"vlc": {
    "host": "localhost",
    "port": 8080,
    "password": "admin",
    "enabled": true
},
"gesture": {
    "camera_id": 0,
    "debounce_frames": 3,
    "cooldown_seconds": 0.5,
    "volume_step": 5
}
```

Key gesture settings:

- `camera_id` — default webcam index (the `--camera` flag overrides it).
- `debounce_frames` — consecutive frames a static gesture must be held before it fires.
- `cooldown_seconds` — minimum pause between commands.
- `volume_step` — % volume change per step (also under the `player` section).
- `swipe_*`, `thumb_*_angle_threshold`, `pinch_threshold_ratio`, `finger_angle_threshold` — detection tuning; lower/raise if gestures feel too twitchy or too stiff.

## Running

### Preview mode (test the gestures first)

```bash
python src/main.py --mode gesture --show-preview
```

Opens a live preview window with your hand landmarks and the detected gesture (press `q` to exit). If the feed is black, run with `--raw-preview` to check the camera alone.

### System tray mode (background operation)

```bash
python src/main.py --tray
```

Runs in the background with a tray icon (install `pystray` + `pillow` first: `pip install pystray pillow`). The tray menu can start/stop listening, switch the webcam, and toggle the preview. Keep `--mode gesture` when you want gesture control explicitly.

### Auto-start on Windows

```bash
python src/main.py --install-startup
```

Creates a Startup-folder shortcut that launches the app in tray mode at logon. Remove it with `python src/main.py --uninstall-startup`.

### Other useful flags

| Flag              | Description                                                    |
| ----------------- | -------------------------------------------------------------- |
| `--camera`        | Webcam index to use (default: `gesture.camera_id`, else `0`).  |
| `--show-preview`  | Show the live gesture preview window.                          |
| `--raw-preview`   | Debug the webcam: raw feed + diagnostics, then exit.           |
| `--gesture-debug` | Verbose gesture classification logs and relaxed test thresholds. |
| `--single`        | Detect one gesture and exit (quick tests).                     |
| `--config`        | Use a custom config file instead of `config.json`.             |
| `--check-deps`    | Print a dependency status report and exit.                     |

## Gesture Mapping

| Gesture                          | Action              | Behavior                          |
| -------------------------------- | ------------------- | --------------------------------- |
| Index finger (pointing up)       | Play / Pause        | Toggle once                       |
| Thumbs up                        | Volume +5%          | Continuous while held             |
| Thumbs down                      | Volume -5%          | Continuous while held             |
| Open hand swipe right            | Skip forward +10s   | Trigger once                      |
| Open hand swipe left             | Skip backward -10s  | Trigger once                      |
| Pinch (index + thumb together)   | Fullscreen          | Toggle once                       |
| Peace sign (index + middle)      | Mute                | Toggle once                       |

Static gestures (index up, thumbs, peace, pinch) must be held for ~3 consecutive frames to fire, and commands are rate-limited so a quick motion can't double-trigger. Hold your hand up, make the shape, and move it back out of view between commands. Thumbs up/down are judged by the thumb's pointing angle in the camera frame for diagonal tolerance, and swipes fire when the palm moves fast/far enough in one direction.

## Troubleshooting

- **Camera not working** → check the camera index with `--camera 1`, `--camera 2`, etc. (or the tray menu's **Next Camera**). Run `python src/main.py --raw-preview` to confirm the webcam itself produces frames. If it is black there too, the camera is busy or privacy-blurred.
- **VLC not responding** → make sure VLC is running, the web interface is enabled (Tools → Preferences → All → Interface → Main interfaces → **Web**), and the password matches `config.json`.
- **False triggers** → hold each gesture still for about half a second, keep your hand centered and well-lit, and move it out of frame between commands. Tune the thresholds under `gesture.*` in `config.json`, or run with `--gesture-debug` to see why each frame did or didn't fire.
- **Volume stuck at 78%** → VLC's own **Maximum volume** may be capped below 100%. Raise it under VLC → Preferences → All → Audio → **Maximum volume** (e.g. to 200%). The `volume_set` cap is a VLC-side setting the HTTP interface can't change.
- **`opencv-python`/`mediapipe` not found** → install them: `pip install opencv-python mediapipe numpy`.
- **MediaPipe 1.x errors about `solutions`** → the app auto-switches to the new Tasks API. The hand-landmarker model (~8 MB) downloads on first run to `~/.cache/mediapipe/hand_landmarker.task`; point `gesture.model_path` at a pre-downloaded file to skip that.

## Contributing

Issues and pull requests are welcome. Keep changes focused on gesture control and VLC; tests live in `tests/` (run with `pytest`).

## License

MIT