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
| `--check-deps` | Print a dependency status report and exit (no microphone needed). |

### Health check

Before running, diagnose your environment:

```powershell
python src/main.py --check-deps
```

It reports which of `speech_recognition`, `pyaudio`, `requests`, `keyboard`, and `python-vlc` are available.

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
   | `player`           | Default skip seconds and volume step                                     |
   | `keyboard_fallback` | Whether keyboard fallback is allowed, and the shortcut keys              |
   | `commands`         | Spoken phrases → action mappings for voice commands                      |

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
- **`VLC HTTP not responding`** → Make sure VLC is running and the web interface is enabled: Tools → Preferences → Show all settings → Interface → Main interfaces → Web. Check the port and password in `src/config.py`.
- **`MPC-HC HTTP not responding`** → Enable the web interface under View → Options → Player → Web Interface and tick "Listen on port" (13579).
- **`Permission denied` on keyboard** → Global key simulation needs elevated rights on Windows. Run the terminal as Administrator, or rely on HTTP control.
- **Nothing heard / no audio device** → Check that your microphone is the default input device and not muted.