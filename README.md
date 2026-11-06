# Simple Coqui-TTS GUI

*Graphical user interface for using released Coqui-TTS and vocoder models in the form of a text editor, made using Tkinter. This project is not made nor endorsed by the authors of Coqui-TTS. Forked from older project: https://github.com/smaybius/Coqui-TTS-GUI-solution*

![Preview](./preview.png)

## How to use

First run:
```bash
git clone https://github.com/iairu/Coqui-TTS-GUI.git
cd Coqui-TTS-GUI
python -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python gui.py
```

Subsequent runs:
```bash
cd Coqui-TTS-GUI
venv/bin/python gui.py
```

## Audio

Playback and recording are handled by `audio.py` and can be configured in the in-app **Settings** page:

- **PulseAudio / PipeWire** (`pactl`, `paplay`/`pw-play`, `parecord`/`pw-record`), including WSLg's `/mnt/wslg/PulseServer`.
- **ALSA** (`aplay`/`arecord`) when the machine has real sound cards.
- **Windows host (WSL)**: plays/records through the Windows default device via PowerShell. Works on Ubuntu, Debian and Arch WSL without any Linux audio setup.

"Automatic" picks the first working option in that order and ignores dummy PulseAudio outputs.
