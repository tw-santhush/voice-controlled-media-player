# Voice-Controlled Media Player

A Python project that controls VLC and MPC-HC (Media Player Classic - Home Cinema) using voice commands.

## Setup

### Prerequisites
- Python 3.9+
- A microphone
- VLC and/or MPC-HC installed

### Virtual Environment Setup (Windows)

Create a virtual environment:

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

### pyAudio Note

`pyaudio` is required for microphone input in `SpeechRecognition`. Python 3.10+ has no official wheel for `pyaudio` on some platforms. If the install above fails, install `pipwin` and install the wheel:

```powershell
pip install pipwin
pipwin install pyaudio
```

Alternatively, on Windows, download and install the matching `.whl` from the [PyAudio releases](https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio), then:

```powershell
pip install path/to/PyAudio-*.whl
```

### Running

```powershell
python src/main.py
```

## Project Structure

```
.
+-- src/          # Source code
+-- tests/        # Unit tests
+-- requirements.txt
+-- README.md
```