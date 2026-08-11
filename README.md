# Voice-Controlled Media Player

A Python project that controls VLC and MPC-HC (Media Player Classic - Home Cinema) using voice commands.

Control works over each player's HTTP web interface, with keyboard shortcut simulation as a fallback when HTTP is unavailable.

## Prerequisites

- Python 3.9+
- A microphone
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

# Run the app
python src/main.py --player auto --continuous
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
python src/main.py --player auto --continuous
```

### Command-line flags

| Flag           | Description                                                     |
| -------------- | --------------------------------------------------------------- |
| `--player`     | `vlc`, `mpc`, or `auto` (default: `auto`). Auto detects the running player. |
| `--continuous` | Run continuously, listening for voice commands (default: `True`). |
| `--single`     | Listen for a single command and exit (useful for testing).      |
| `--config`     | Path to a custom JSON config file (default: `config.json`).     |
| `--tts`        | Force-enable text-to-speech feedback (overrides config).        |
| `--no-tts`     | Force-disable text-to-speech feedback (overrides config).       |
| `--tts-engine` | TTS backend: `auto` (default), `pyttsx3`, `powershell`, `say`, `espeak`, or `system`. |
| `--no-wake`    | Disable the wake-word requirement (overrides config).           |
| `--debug`      | Enable debug logging (raw recognized text, mic details, audio levels). |
| `--recognizer` | Speech recognizer: `google` (online), `vosk` (offline), or `auto` (default: config or auto). |
| `--mic-index`  | Microphone device index to use (list them with `--list-mics`).  |
| `--list-mics`  | List all available microphones (via pyaudio) and exit.          |
| `--test-tts`   | Speak a test phrase with the configured TTS engine, then exit (no microphone needed). |
| `--train-wake` | Listen to 5 repetitions of your wake phrase and analyze how well it is recognized. |
| `--check-deps` | Print a dependency status report and exit (no microphone needed). |

### Health check

Before running, diagnose your environment:

```powershell
python src/main.py --check-deps
```

It reports which of `speech_recognition`, `pyaudio`, `requests`, `keyboard`, `python-vlc`, `vosk`, and `pyttsx3`
are available, and tests the TTS setup (it speaks a short test phrase if TTS works).

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
   | `voice`            | Listening timeout and phrase time limit (seconds)                        |
   | `recognizer`       | Speech engine: `google`, `vosk` (offline), or `auto`, plus the Vosk model path |
   | `player`           | Default skip seconds and volume step                                     |
   | `keyboard_fallback` | Whether keyboard fallback is allowed, and the shortcut keys              |
   | `commands`         | Spoken phrases → action mappings for voice commands                      |
   | `tts`              | Text-to-speech feedback: `enabled` toggles it, `voice_id` selects a voice (null = default) |
   | `wake`             | Wake-word support: `enabled`, `phrases` (e.g. "hey player"), and `timeout_seconds` |

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
    "fallback_enabled": true
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

Override on the command line: `--tts` forces it on, `--no-tts` forces it off, and `--tts-engine`
selects the backend for this run.

## Wake Word

By default the app only acts on commands spoken after a wake phrase, so it won't trigger on normal
conversation. Configure it under `wake`:

```json
"wake": {
    "enabled": true,
    "phrases": ["hey player", "hello player", "player"],
    "timeout_seconds": 3
}
```

- `enabled`: set to `false` to process every utterance as a command.
- `phrases`: phrases (case-insensitive) that arm the app for a command. Detection uses word boundaries and
  ignores punctuation around the phrase ("hey player - play" works), and the longest matching phrase wins.
- `timeout_seconds`: if you say just the wake phrase, the app waits this long for the follow-up command.

Examples:

```bash
# Wake required (default) — "hey player play" works, bare "play" is ignored
python src/main.py --player vlc --single

# Disable the wake requirement for this run
python src/main.py --player vlc --single --no-wake
```

Enable/disable TTS exactly as above or in `config.json`.

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
+-- src/                    # Source code (config, voice listener, player controllers, main)
+-- tests/                  # Unit tests (mocked microphone)
+-- config.example.json     # Committed template; copy to config.json and edit
+-- requirements.txt
+-- README.md
```

## Troubleshooting

- **`ModuleNotFoundError: No module named 'keyboard'`** → Reactivate the virtual environment (`.venv\Scripts\activate`) and run `pip install -r requirements.txt`, or install it globally. `keyboard` is optional; HTTP control still works without it.
- **`pyaudio not found`** → Install the wheel that matches your Python version (e.g. `PyAudio‑0.2.14‑cp311‑cp311‑win_amd64.whl` for Python 3.11 on 64-bit Windows), or use `pipwin install pyaudio`.
- **`VLC HTTP not responding`** → Make sure VLC is running and the web interface is enabled: Tools → Preferences → Show all settings → Interface → Main interfaces → Web. Check the port and password in `config.json`.
- **`MPC-HC HTTP not responding`** → Enable the web interface under View → Options → Player → Web Interface and tick "Listen on port" (13579).
- **`Permission denied` on keyboard** → Global key simulation needs elevated rights on Windows. Run the terminal as Administrator, or rely on HTTP control.
- **Nothing heard / no audio device** → Check that your microphone is the default input device and not muted.
- **`Could not understand the audio`** → The microphone picked up speech but recognition failed. Raise the
  `voice.timeout_seconds` value, move closer to the mic, reduce background noise, and run with `--debug` to see the
  raw recognized text and audio energy levels — this shows whether your speech was even received.
- **`Vosk model download` fails** → The ~50 MB model is fetched from `alphacephei.com` on first `vosk` use. If the
  download fails or you are offline, download `vosk-model-small-en-us-0.15.zip` yourself, unzip it, and set
  `vosk_model_path` in the `recognizer` config section to the unzipped folder.