# Voice-Controlled Media Player

A Python project that controls VLC and MPC-HC (Media Player Classic - Home Cinema) using **webcam hand
gestures** by default, with voice control available (and combinable) via `--mode`.

Control works over each player's HTTP web interface, with keyboard shortcut simulation as a fallback when HTTP is unavailable.

## Prerequisites

- Python 3.9+
- A webcam (for gesture control, the default mode) and/or a microphone (for voice control)
- VLC and/or MPC-HC installed, each with its web interface enabled (see Troubleshooting)

## Virtual Environment Setup

> Important: always activate the virtual environment before running the app. Without it, Python
> uses your global environment, where the dependencies are likely missing.

```bash
# Create and activate venv (Windows)
python -m venv .venv
.venv\Scripts\activate
# or on Linux/macOS
# source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the app (gesture control, the default; requires a webcam)
python src/main.py --player auto --continuous

# Or run voice control instead
python src/main.py --mode voice --player auto --continuous
```

On PowerShell, activation is `.\.venv\Scripts\Activate.ps1`.

### pyAudio Note

`pyaudio` is required for microphone input in `SpeechRecognition`. Python 3.10+ has no official wheel on some platforms (this project targets Python 3.11, which has a `cp311` wheel). If plain `pip install` fails, use `pipwin`:

```powershell
pip install pipwin
pipwin install pyaudio
```

Or download the matching `.whl` from the [PyAudio releases](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio), then:

```powershell
pip install path/to/PyAudio-*.whl
```

### keyboard Note

`keyboard` is optional. It is only used as a fallback when HTTP control fails. On Windows, global key simulation may require running the app as administrator. If it is missing or lacks permissions, the app logs a warning and continues with HTTP-only control.

## Run

```powershell
# Gesture control (default)
python src/main.py --player auto --continuous

# Voice control
python src/main.py --mode voice --player auto --continuous

# Both at once (gesture runs in a background thread alongside voice)
python src/main.py --mode both --player auto --continuous
```

### Command-line flags

| Flag           | Description                                                     |
| -------------- | --------------------------------------------------------------- |
| `--player`     | `vlc`, `mpc`, or `auto` (default: `auto`). Auto detects the running player. |
| `--mode`       | Input mode: `gesture` (webcam, default), `voice`, or `both`.    |
| `--camera`     | Webcam index to use for gesture control (default: `0`).         |
| `--show-preview` | Show a live webcam preview window with hand landmarks and the detected gesture (press `q` to exit). |
| `--raw-preview`  | Debug the webcam: open it, show the unprocessed frames, and exit. Tries indexes 0, 1 and -1, prints diagnostics, and closes on `q`. Useful when the gesture preview looks black or the camera won't open. |
| `--continuous` | Run continuously, listening for commands (default: `True`). |
| `--single`     | Listen for a single command and exit (useful for testing).      |
| `--config`     | Path to a custom JSON config file (default: `config.json`).     |
| `--tts`        | Force-enable text-to-speech feedback (overrides config).        |
| `--no-tts`     | Force-disable text-to-speech feedback (overrides config).       |
| `--tts-engine` | TTS backend: `auto` (default), `pyttsx3`, `powershell`, `say`, `espeak`, or `system`. |
| `--no-wake`    | Disable the wake-word requirement (overrides config).           |
| `--wake-debug` | Print the raw recognized text and whether a wake phrase was detected. |
| `--push-to-talk` | Enable push-to-talk: commands are only processed while the PTT key is held (overrides config). |
| `--ptt-key`    | Key to hold for push-to-talk (default: config value, usually `ctrl`). |
| `--debug`      | Enable debug logging (raw recognized text, mic details, audio levels, noise-gate rejections). |
| `--recognizer` | Speech recognizer: `google` (online), `vosk` (offline), or `auto` (default: config or auto). |
| `--mic-index`  | Microphone device index to use (list them with `--list-mics`).  |
| `--list-mics`  | List all available microphones (via pyaudio) and exit.          |
| `--test-tts`   | Speak a test phrase with the configured TTS engine, then exit (no microphone needed). |
| `--train-wake` | Listen to 5 repetitions of your wake phrase and analyze how well it is recognized. |
| `--record-test`| Record 3 seconds of microphone audio to `test_audio.wav`, play it back, and exit. |
| `--energy-test`| Measure audio energy for 5 seconds and suggest a value for `voice.energy_threshold`, then exit. |
| `--set-energy` | Override `voice.energy_threshold` for this run (e.g. `--set-energy 100`). |
| `--tray`       | Run in the background with a system-tray icon (requires `pystray`; automatic terminal fallback if missing). |
| `--test-tray`  | Run only the tray icon **without** the voice listener, so the tray menu can be verified manually; exits after 30 seconds or via the tray menu. |
| `--hotkey`     | Global hotkey that toggles listening on/off while running in the background (default: `ctrl+shift+l`; requires `keyboard`). |
| `--install-startup` | Windows only: create a Startup-folder shortcut that launches the app with the tray icon at logon, then exit. |
| `--uninstall-startup` | Windows only: remove the Startup-folder shortcut created by `--install-startup`, then exit. |
| `--check-deps` | Print a dependency status report and exit (no microphone needed). |

> Voice-only flags (`--single`, `--recognizer`, `--mic-index`, `--no-wake`, `--wake-debug`,
> `--push-to-talk`, `--record-test`, `--energy-test`, `--train-wake`) require `--mode voice` or `--mode both`.

### Gesture Control

Webcam hand gestures are the default input. Uses OpenCV + MediaPipe (offline, free). Install the optional extras:

```bash
pip install opencv-python mediapipe numpy
```

Two MediaPipe generations are supported automatically: MediaPipe **0.x** uses the legacy
`mediapipe.solutions.hands` API, while MediaPipe **1.x** (which removed `solutions`) uses the new
Tasks API (`HandLandmarker`). With 1.x, the hand-landmarker model (~8 MB) is downloaded automatically on
the first run to `~/.cache/mediapipe/hand_landmarker.task`. To use a pre-downloaded model instead, set
`gesture.model_path` in `config.json` or the `MEDIAPIPE_HAND_MODEL` environment variable.

The gesture-to-action mapping:

| Gesture            | Action               | Notes                          |
| ------------------ | -------------------- | ------------------------------ |
| Index finger up    | Play / Pause toggle  | Toggles between play and pause |
| Closed fist        | Stop                 |                                |
| Swipe left         | Skip backward        | Open hand swipe                |
| Swipe right        | Skip forward         | Open hand swipe                |
| Thumbs up          | Volume up (+5)       | Hold for continuous adjustment |
| Thumbs down        | Volume down (-5)     | Hold for continuous adjustment |
| Peace sign         | Mute / unmute        | Index + middle fingers         |
| Pinch              | Toggle fullscreen    | Index + thumb pinch            |

A gesture must be held for ~3 consecutive frames to fire, and commands are rate-limited (~0.5 s) so a quick
motion can't double-trigger. Hold your hand up, make the shape, and move it back out of view between commands.

- `--camera` selects which webcam to read (default: config `gesture.camera_id`, else `0`). The tray menu also has a
  **Next Camera** item when running in gesture tray mode. If the requested index (or backend) fails, the app
  automatically falls back to other camera indexes (`0`, then `1`, then `-1`) and to every available video backend
  (DirectShow first on Windows), so a busy or half-initialized webcam is rarely fatal.
- `--show-preview` opens a preview window showing your hand's landmarks, the detected gesture, and a live volume bar at the bottom; press `q` to quit.
- `--raw-preview` skips gesture detection entirely and just shows the raw webcam feed with diagnostics. Use it first
  when the gesture preview is black or empty: it proves whether the camera itself produces frames. A camera that
  never starts or goes unreadable mid-run is automatically re-opened.
- If the gesture dependencies, model, or camera are unavailable, the app prints a clear error and exits; use
  `--mode voice` to fall back to voice control, or `--mode both` to run both inputs at once.
- For quick tests, `--mode gesture --single` waits for one gesture, fires it, and exits.

Gesture tuning lives under the `gesture` section in `config.json`:

```json
"gesture": {
    "camera_id": 0,
    "debounce_frames": 3,
    "cooldown_seconds": 0.5,
    "swipe_threshold": 50,
    "pinch_threshold": 0.05,
    "volume_interval_seconds": 0.5,
    "volume_step": 5,
    "model_path": null
}
```

- `camera_id`: default webcam index (the `--camera` flag overrides it).
- `debounce_frames`: consecutive frames a gesture must be held before it fires (default `3`).
- `cooldown_seconds`: minimum pause between commands in seconds (default `0.5`).
- `swipe_threshold`: distance palm movement that counts as a swipe (default `50`).
- `pinch_threshold`: distance between thumb and index finger to count as a pinch (default `0.05`).
- `volume_interval_seconds`: how often continuous volume adjustment triggers when holding thumbs up/down (default `0.5`).
- `volume_step`: how much volume changes per step (default `5`).
- `model_path`: path to a pre-downloaded `hand_landmarker.task`; `null` auto-downloads and caches it.

### Health check

Before running, diagnose your environment:

```powershell
python src/main.py --check-deps
```

It reports which of `speech_recognition`, `pyaudio`, `requests`, `keyboard`, `python-vlc`, `vosk`, and `pyttsx3`
are available, and tests the TTS setup (it speaks a short test phrase if TTS works).

## Background Mode and System Tray

The app can run in the background with an icon in the Windows system tray. Install the optional extra:

```powershell
pip install pystray pillow
```

Then launch it in tray mode:

```powershell
python src/main.py --tray
```

- In the default gesture mode a **camera** icon appears in the tray; in voice mode it is a **microphone** icon.
  Either way: **green** while it is actively listening, **yellow** while the TTS response is still being spoken
  (the cooldown window), and **red** while paused. Listening starts **on** by default — you do not have to enable
  it after starting the app.
- Right-click for the menu:
  - **Start Listening/Stop Listening** — toggle detection without closing the app (same as the
    `listen_on`/`listen_off` commands). The label shows **Stop Listening** while listening is on and
    **Start Listening** while it is paused.
  - **Show Status** — pop-up with whether it is listening and which player it found.
  - **Next Camera** — switch to the next webcam (gesture mode only).
  - **Toggle Webcam Preview** — show/hide the live gesture preview window (gesture mode only).
  - **Exit** — shuts the app down.
- The console window stays open for logs. If `pystray` is not installed, `--tray` falls back to terminal mode.

### Global hotkey to toggle listening

While in tray mode you can toggle listening on/off with a keyboard shortcut, even when the app is in the
background — handy if the wake word isn't working, or to quickly mute listening during a loud scene:

```powershell
python src/main.py --tray                     # default hotkey: Ctrl+Shift+L
python src/main.py --tray --hotkey ctrl+alt+v # pick a different combination
```

Pressing the hotkey toggles the listener and updates the tray icon (green/red). It requires the `keyboard`
module; if it is missing, the hotkey is skipped with a warning.

### Debug indicators in the console

Run tray mode with `--debug` to see live status lines in the console window:

```powershell
python src/main.py --tray --debug
```

- `Listener: ON` / `Listener: OFF` — printed when the app starts and every time you toggle listening
  (tray menu, hotkey, or voice command).
- `Wake phrase detected: '...'; remaining command: '...'` — printed when a wake phrase is heard.
- `Command: <action>` — printed whenever a command is recognized and executed, so you can confirm the app works.
- `Raw recognized text: '...'` — use `--wake-debug` (below) to always print this, even outside tray mode.

### Isolate tray problems with `--test-tray`

If tray mode "doesn't respond to voice", first check the tray menu itself in isolation:

```powershell
python src/main.py --test-tray
```

This runs the tray icon with **no voice listener** (no microphone needed). You can click **Start
Listening**/**Stop Listening** to verify the icon changes color and the menu works. The app exits after 30
seconds or when you choose **Exit**. If the menu works here but voice is still ignored, the problem is the
listener, not the tray.

### Run without a console window (Windows)

Launch via `pythonw` so no console is created — only errors are logged (to a file). From inside the
activated virtual environment:

```powershell
.venv\Scripts\pythonw.exe src\main.py --tray
```

### Start at every logon (Windows)

```powershell
# Creates a shortcut in the Startup folder that runs pythonw + --tray
python src/main.py --install-startup

# Remove it again
python src/main.py --uninstall-startup
```

`--install-startup` checks that `pythonw.exe` exists next to your `python.exe` (a standard Windows Python) and that
`pystray` is installed, and refuses otherwise.

## Configuration

All settings live in a single JSON file. The app automatically creates `config.json` in the project root
(with defaults) the first time it runs, so you can start without any setup.

To customize:

1. Copy the template to get a personal config:
   ```powershell
   Copy-Item config.example.json config.json
   ```
2. Edit `config.json`. Supported sections:

   | Section            | What it controls                                                        |
   | ------------------ | ----------------------------------------------------------------------- |
   | `vlc`              | VLC host, HTTP port, password, and whether VLC control is enabled        |
   | `mpc`              | MPC-HC host, web-interface port, and whether MPC-HC control is enabled   |
   | `voice`            | Listening timeout, phrase time limit (seconds), `energy_threshold`, plus the **noise gate** and **confidence threshold** (see below) |
   | `recognizer`       | Speech engine: `google`, `vosk` (offline), or `auto`, plus the Vosk model path |
   | `gesture`          | Gesture control: `camera_id`, `debounce_frames`, `cooldown_seconds`, `swipe_threshold`, and `model_path` (see Gesture Control) |
   | `player`           | Default skip seconds and volume step                                     |
   | `push_to_talk`     | Push-to-talk: `enabled` and the `key` to hold                             |
   | `keyboard_fallback` | Whether keyboard fallback is allowed, and the shortcut keys              |
   | `commands`         | Spoken phrases → action mappings for voice commands (see Voice Commands) |
   | `tts`              | Text-to-speech feedback: `enabled` toggles it, `voice_id` selects a voice (null = default), `cooldown_seconds` sets the post-speech silence window |
   | `wake`             | Wake-word support: `enabled`, `engine` (`auto`/`porcupine`/`string`), `phrases` (e.g. "hey player"), Porcupine keywords, and `timeout_seconds` |

3. Run the app; it reads `config.json` on startup.

Use a different file with `--config`:

```powershell
python src/main.py --config my-settings.json
```

> Note: `config.json` is ignored by Git on purpose — it may contain your VLC password and other personal
> settings. The committed `config.example.json` is the safe template. If you change the config while the app
> is running, restart the app (or force a reload) to pick up the new values.

### Enable the player web interfaces

VLC: Tools → Preferences → Show all settings → Interface → Main interfaces → Web, then set an HTTP port/password (default 8080 / `admin`).

MPC-HC: View → Options → Player → Web Interface → tick "Listen on port" (default 13579).

## Text-to-Speech (TTS) Feedback

After executing a command the app speaks it aloud (e.g. "Playing", "Paused"). TTS is optional and uses the
offline `pyttsx3` library — install it with:

```bash
pip install pyttsx3
```

Control it via the `tts` config section:

```json
"tts": {
    "enabled": true,
    "voice_id": null,
    "engine": "auto",
    "fallback_enabled": true,
    "cooldown_seconds": 1.5
}
```

- `enabled`: set to `false` to disable spoken feedback.
- `voice_id`: optional voice identifier (e.g. Windows SAPI token such as
  `HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices\Tokens\TTS_MS_EN-US_ZIRA_11.0`,
  or `com.apple.voice.compact.en-US` on macOS). Leave `null` for the default voice.
- `engine`: `auto` tries `pyttsx3` first, then falls back to an OS tool. Or pick one explicitly:
  `pyttsx3`, `powershell` (Windows SAPI), `say` (macOS), `espeak` (Linux, with `spd-say` as a secondary),
  or `system` (best tool for the current OS). `pyttsx3` never falls back.
- `fallback_enabled`: when `true` and `pyttsx3` is unavailable, the OS-level fallback (`powershell`, `say`,
  or `espeak` depending on platform) is used automatically. Set `false` to disable the fallback entirely.
- `cooldown_seconds`: how long (in seconds) after the assistant speaks that incoming audio is ignored, so the
  app doesn't re-hear its own TTS response (default `1.5`). The tray icon shows **yellow** during this window.

Override on the command line: `--tts` forces it on, `--no-tts` forces it off, and `--tts-engine`
selects the backend for this run.

## Wake Word

By default the app only acts on commands spoken after a wake phrase, so it won't trigger on normal
conversation. Configure it under `wake`:

```json
"wake": {
    "enabled": true,
    "engine": "porcupine",
    "phrases": ["hey player", "hello player", "player", "hey"],
    "porcupine_keywords": ["porcupine", "hey google"],
    "porcupine_keyword_paths": [],
    "porcupine_access_key": null,
    "timeout_seconds": 5
}
```

- `enabled`: set to `false` to process every utterance as a command.
- `engine`: the wake engine. `porcupine` (default) uses **Porcupine**, falling back to string phrase matching
  with a warning if it can't initialize. `auto` uses Porcupine when it is available and configured, and falls
  back to string phrase matching otherwise. `string` always uses phrase matching.
- `phrases`: strings matched against the recognized text (see below). Used by the string engine and as the
  fallback.
- `porcupine_keywords`: built-in Porcupine keywords to listen for (e.g. `porcupine`, `hey google`, `alexa`).
- `porcupine_keyword_paths`: optional custom keyword files (`.ppn` from the Picovoice Console). When non-empty,
  they replace `porcupine_keywords`.
- `porcupine_access_key`: your free Picovoice **AccessKey**. Leave `null` (or omit) to read the
  `PICOVOICE_ACCESS_KEY` environment variable instead.
- `timeout_seconds`: if you say just the wake word, the app waits this long for the follow-up command.

With the **string engine**, detection strips punctuation and extra spaces, then matches the phrase **anywhere**
in the text (not just at the start) at word boundaries; the longest matching phrase wins. The defaults include the
short, easy-to-recognize variants `player` and `hey` on their own.

### Porcupine setup

For a proper wake word that doesn't run full speech recognition on every utterance:

1. Install the optional library:
   ```bash
   pip install pvporcupine
   ```
2. Get a free **AccessKey** from <https://console.picovoice.ai>.
3. Either set it in `config.json` (`wake.porcupine_access_key`) or export the environment variable:
   ```bash
   # PowerShell
   $env:PICOVOICE_ACCESS_KEY = "your-access-key"
   ```
4. For a quick test, use the built-in keywords (default `porcupine`, `hey google`, `alexa`). Say one of them
   instead of your custom phrase. With `engine=auto` the app logs "Porcupine wake word active" when it connects.
5. To use a custom "Hey Player" keyword, train it in the Picovoice Console, download the `.ppn` file, and point
   `wake.porcupine_keyword_paths` at it.

If `pvporcupine` is missing, has no AccessKey, or fails to initialize, the app logs a warning and falls back to
the string engine automatically, so the app keeps working without Porcupine.

To see exactly what the recognizer heard and whether it counted as a wake phrase, run with `--wake-debug`:

```bash
python src/main.py --player vlc --wake-debug --single
```

Examples:

```bash
# Wake required (default) — "hey player play" works, bare "play" is ignored
python src/main.py --player vlc --single

# Disable the wake requirement for this run
python src/main.py --player vlc --single --no-wake
```

Enable/disable TTS exactly as above or in `config.json`.

## Voice Commands

The `commands` config section maps spoken phrases to player actions. Each action accepts multiple phrases,
and the longest matching phrase wins ("stop playing" pauses, while "stop" alone stops). Default mappings:

| Action          | Example phrases                                                                 |
| --------------- | ------------------------------------------------------------------------------- |
| `play`          | play, continue, start, play movie, play video                                |
| `pause`         | pause, hold, stop playing, freeze, pause movie, pause video                      |
| `stop`          | stop, quit, end, stop movie, exit playback, stop video                           |
| `skip_forward`  | skip forward, forward 10 seconds, jump forward, advance, fast forward            |
| `skip_backward` | skip backward, rewind, go back, rewind 10 seconds                                |
| `volume_up`     | volume up, louder, turn up, raise volume                                         |
| `volume_down`   | volume down, softer, turn down, lower volume                                     |
| `mute`          | mute, silence, no sound, turn off sound                                          |
| `fullscreen`    | fullscreen, full screen, maximize, enter fullscreen                              |
| `exit`          | exit, quit app, close, shutdown, terminate                                       |
| `next`          | next, next track, next video, next episode, skip to next                         |
| `previous`      | previous, previous track, go back a chapter, back chapter                        |
| `listen_on`     | listen, start listening, resume, turn on, enable                                 |
| `listen_off`    | stop listening, pause listening, turn off, disable, go silent                    |
| `volume_set`    | (special — handled by numeric parsing, see below)                                |

> Note: `silence` maps to `mute` (above), not to `listen_off` — the longest matching phrase wins, so the mute
> action is preserved.

Extend any action by adding phrases to the list in `config.json`, e.g.:

```json
"commands": {
    "play": ["play", "resume", "continue", "go"],
    "next": ["next", "next track", "skip to next"]
}
```

### Absolute volume ("set volume to 30")

Phrases that contain a volume-related keyword ("volume", "vol", "set", or "change") **and** a number are
parsed as an absolute volume, clamped to 0-100. Any number found in the phrase is used (the first one):

```bash
"hey player, set volume to 30"   → 30%
"hey player, volume 50"          → 50%
"hey player, change volume 100"  → 100%
"hey player, volume up"          → relative +5 (not absolute)
```

These are processed only when the phrase does **not** match a regular command first, so relative commands like
"volume up"/"volume down" win over numeric parsing. Numeric volume is sent to the player through its web
interface (`command=volume&val=N` for VLC, `volume=N` for MPC-HC).

### Pause and resume listening

The `listen_on`/`listen_off` commands and the tray menu toggle the same "listening" flag. When paused, every
utterance except `listen_on` is ignored, and the tray icon turns red. The app says "Listening paused"/"Listening
resumed" so the toggle is audible. This is also the cleanest way to stop the app from reacting while you talk
over a movie.

## Speech Recognition

Recognition defaults to Google (online). For fully offline recognition you can use **Vosk**, an open-source
model-based engine that works without internet — often a good fit when Google mishears your accent or commands.

```json
"recognizer": {
    "engine": "auto",
    "vosk_model_path": null
}
```

- `engine`: `google`, `vosk`, or `auto` (`auto` tries Vosk first and falls back to Google).
- `vosk_model_path`: path to a local Vosk model. If `null`, the app auto-downloads the small English model
  (`vosk-model-small-en-us-0.15`, ~50 MB) into `~/.cache/vosk/model-en-us` on first use.

### Vosk setup

1. Install the library (optional — Google still works without it):
   ```bash
   pip install vosk
   ```
2. Run once with Vosk to download the model automatically, or provide your own:
   ```bash
   python src/main.py --recognizer vosk --player auto --single
   # or point config.json at an existing model:
   # "vosk_model_path": "C:/models/vosk-model-small-en-us-0.15"
   ```
3. If Vosk is not installed (or the model download fails), the app logs a warning and falls back to Google.

### Verify your microphone

```bash
# List every audio input device and its index
python src/main.py --list-mics

# Use a specific mic if you have several
python src/main.py --mic-index 1

# Confirm audio is being captured (watch for high amplitude when you speak)
python src/main.py --debug
```

### Record and play back a sample (`--record-test`)

Records 3 seconds from the default microphone, saves it to `test_audio.wav` in the project root,
plays it back (via pyaudio, or the system sound player), and prints the peak amplitude and duration:

```bash
python src/main.py --record-test
```

This is the fastest way to confirm the microphone actually captures sound. If the peak amplitude
stays near `0` while you speak, the mic is muted, not the default device, or too quiet.

### Tune the speech-detection threshold (`--energy-test`)

SpeechRecognition uses `recognizer.energy_threshold` to decide when speech starts (default `20` in
this app's config).
If it is too high, your voice is ignored; too low and background noise triggers false commands.

```bash
python src/main.py --energy-test
```

It listens for 5 seconds, printing the energy every 0.5 seconds (current, running average, and peak).
Stay quiet at first, then speak a little, so it can compare speech to the noise floor. At the end it
suggests a threshold — apply it with:

```bash
python src/main.py --set-energy 100
```

Or set it permanently in the `voice` section of `config.json`:

```json
"voice": {
    "timeout_seconds": 5,
    "phrase_time_limit": 3,
    "energy_threshold": 20,
    "dynamic_energy_threshold": false,
    "noise_gate_enabled": true,
    "noise_gate_threshold": 10.0,
    "confidence_threshold": 0.5
}
```

- `energy_threshold`: the energy level above which a sound is treated as the start of speech (default `20`, tuned
  for a quiet room with the noise gate on; see `--energy-test`).
- `dynamic_energy_threshold`: when `true`, the recognizer re-adjusts the threshold to the
  ambient noise on every listen. Defaults to `false`, so the exact `energy_threshold` value is kept and the
  noise gate does the quiet-sound filtering. Set to `true` to let SpeechRecognition auto-tune instead.
- `noise_gate_enabled`: when `true` (default), audio blocks whose RMS level is below
  `noise_gate_threshold` are treated as silence and skipped **before** any speech recognition runs.
  This stops the app from wasting recognition attempts on keyboard clicks and faint room noise.
- `noise_gate_threshold`: the RMS level (0-100 scale used by the microphone meters) below which an audio
  block is considered noise. If your mic is unusually quiet, lower it; if the gate never opens, raise it.
- `confidence_threshold`: `0` disables confidence filtering. Otherwise, recognitions whose confidence is
  below this value are discarded as too uncertain (Google provides a confidence score; engines that don't,
  like Vosk, fall back to a length heuristic — very short "recognitions" like a single stray word are
  rejected). Raise it for stricter filtering, lower it if real commands are being discarded.

### Test text-to-speech

Speaks a test phrase through the configured TTS engine and exits. No microphone is needed, so it is the quickest
way to confirm TTS is set up correctly:

```bash
python src/main.py --test-tts
```

## Wake Phrase Training

If the recognizer keeps missing your wake phrase, train it:

```bash
python src/main.py --train-wake
```

It listens for 5 repetitions of your wake phrase, prints exactly what the recognizer heard each time, and suggests
shorter or more distinctive alternatives when recognition is inconsistent. Run it with `--debug` or
`--recognizer vosk` for extra insight.

### Single-command mode

`--single` disables the wake word automatically, so a bare command like "play" is processed directly — ideal for
quick testing. Use `--no-wake` to get the same behavior explicitly.

## Project Structure

```
.
+-- src/                    # Source code (config, gesture/voice listeners, player controllers, tray icon, main)
+-- tests/                  # Unit tests (mocked microphone and gestures)
+-- config.example.json     # Committed template; copy to config.json and edit
+-- requirements.txt
+-- README.md
```

## Troubleshooting

- **`ModuleNotFoundError: No module named 'keyboard'`** → Reactivate the virtual environment (`.venv\Scripts\activate`) and run `pip install -r requirements.txt`, or install it globally. `keyboard` is optional; HTTP control still works without it.
- **`pyaudio not found`** → Install the wheel that matches your Python version (e.g. `PyAudio‑0.2.14‑cp311‑cp311‑win_amd64.whl` for Python 3.11 on 64-bit Windows), or use `pipwin install pyaudio`.
- **`opencv-python`/`mediapipe` not installed (gesture mode exits with an error)** → Install them: `pip install opencv-python mediapipe numpy`. Or fall back to voice with `--mode voice`. If you hit a `protobuf` conflict, install the exact versions from `requirements.txt`.
- **MediaPipe 1.x errors about `solutions`** → MediaPipe 1.x removed the legacy `mp.solutions` API; this app automatically switches to the Tasks API. If you still see initialization errors, make sure the hand-landmarker model can be downloaded (the app caches it in `~/.cache/mediapipe/hand_landmarker.task`), or point `gesture.model_path` / `MEDIAPIPE_HAND_MODEL` at a pre-downloaded `hand_landmarker.task` file.
- **`Could not open camera 0` / the preview window is black** → The app already retries several camera indexes
  (`--camera` index, then `0`, `1`, `-1`) and every video backend, and it rejects cameras that only deliver
  constant near-black frames (a "signal-less" feed some drivers emit while the webcam is busy or unavailable) -
  the still-black remnant of such a camera reads as a frame but contains no image. Run
  `python src/main.py --raw-preview` to see the raw feed and diagnostics (frame size plus mean/min/max/std, and a
  warning when frames are constant/black); press `q` to exit. If the raw preview is black too, the webcam is busy
  (closed by another app), privacy-blurred, or index 0 isn't your webcam — try `--camera 1`, `--camera 2`, etc.,
  or in tray mode use the **Next Camera** menu item.
- **Gestures are flaky / wrong commands fire** → Hold each gesture still for about half a second so the debounce registers it, keep your hand roughly centered in view and well-lit, and move it fully out of frame between commands. Use `--show-preview` to see which gesture is being detected in real time.
- **`VLC HTTP not responding`** → Make sure VLC is running and the web interface is enabled: Tools → Preferences → Show all settings → Interface → Main interfaces → Web. Check the port and password in `config.json`.
- **`MPC-HC HTTP not responding`** → Enable the web interface under View → Options → Player → Web Interface and tick "Listen on port" (13579).
- **`Permission denied` on keyboard** → Global key simulation needs elevated rights on Windows. Run the terminal as Administrator, or rely on HTTP control.
- **Tray mode doesn't respond to voice** → Work through these steps in order:
  1. Check the tray icon color: **green** means listening is on, **yellow** means the TTS response is still
     being spoken (wait a moment), **red** means it is paused. If it is red,
     right-click the tray menu and choose **Start Listening**, or press the global hotkey (`Ctrl+Shift+L` by default).
  2. Verify the listener is actually running with `--test-tray`: if the tray menu works there but not in
     `--tray`, the problem is the voice listener, not the tray.
  3. Run `python src/main.py --tray --debug` and watch the console for `Raw recognized text` / `Wake phrase
     detected` / `Command: ...` lines — they confirm the mic is picking up your voice and commands are firing.
  4. Confirm the wake phrase is being recognized — run `python src/main.py --wake-debug --single`, say "hey player",
     and check that the app reports "Wake phrase detected". If the raw text is empty, see "Nothing heard /
     no audio device" below; if the raw text exists but no wake phrase is detected, switch to `--recognizer vosk`.
  5. If the wake word is still unreliable, disable it entirely with `--no-wake`, or add your exact phrasing to
     `wake.phrases` in `config.json` (matching now allows the phrase anywhere in the sentence).
- **Nothing heard / no audio device** → Check that your microphone is the default input device and not muted.
- **`Could not understand the audio`** → The microphone picked up speech but recognition failed. Raise the
  `voice.timeout_seconds` value, move closer to the mic, reduce background noise, and run with `--debug` to see the
  raw recognized text and audio energy levels — this shows whether your speech was even received.
- **`Vosk model download` fails** → The ~50 MB model is fetched from `alphacephei.com` on first `vosk` use. If the
  download fails or you are offline, download `vosk-model-small-en-us-0.15.zip` yourself, unzip it, and set
  `vosk_model_path` in the `recognizer` config section to the unzipped folder.
- **The app reacts to movie/TV dialogue** → Movie soundtracks are full of commands like "pause" or "stop". Use a
  wake phrase (turn on `wake` in config), or literally pause listening while watching: say "stop listening" (or use
  the tray menu **Stop Listening**), then "start listening" when you want commands again. TTS feedback ("Listening
  paused") may itself be picked up by the mic through the speakers — use a **headset** or move the mic away from
  the speakers.
- **The app hears itself (TTS feedback loop)** → TTS feedback is spoken by your speakers and re-recorded by the
  mic. Put on a headset so the mic doesn't hear the speakers, lower TTS with `--no-tts`, or pause listening right
  after issuing a command. The `tts.cooldown_seconds` setting already makes the app ignore audio for a short
  window after it speaks, which helps with quicker responses.
- **My voice isn't being recognized** → Work through these steps in order:
  1. Confirm the mic captures audio: `python src/main.py --record-test`. Play back `test_audio.wav` — if it is
     silent, the mic is muted, not the default input device, or too quiet.
  2. Check the energy levels: `python src/main.py --energy-test`. Speak during the test and confirm your voice
     produces noticeably higher energy than the background noise.
  3. Apply the suggested threshold: `python src/main.py --energy-test` prints a recommended value — use it with
     `python src/main.py --set-energy <value>` to test, or set `voice.energy_threshold` permanently in
     `config.json`. Lower the value if your speech barely exceeds the noise floor.
  4. Run with `--debug` and watch for `rms`/`peak` in the audio stats — they confirm your voice is actually reaching
     the recognizer. If the waveform looks healthy but recognition still fails, see "`Could not understand the audio`"
     above.