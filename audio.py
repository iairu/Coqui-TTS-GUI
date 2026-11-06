"""Audio playback/recording for Coqui TTS Studio on Linux, including WSL.

Supported backends:
  pulse    PulseAudio or PipeWire (via pactl/paplay/pw-play/parecord/pw-record). Includes WSLg's
           PulseServer socket, which is picked up automatically when present.
  alsa     Plain ALSA (aplay/arecord) on machines with real sound cards.
  windows  WSL only: plays/records through the Windows host with PowerShell. Needs no Linux audio
           stack at all, so it works on Ubuntu/Debian/Arch WSL even when WSLg audio is missing.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

BACKENDS = {
    "auto": "Automatic",
    "pulse": "PulseAudio / PipeWire",
    "alsa": "ALSA",
    "windows": "Windows host (WSL)",
}
WINDOWS_DEFAULT = "Windows default device"
WSLG_PULSE_SOCKET = Path("/mnt/wslg/PulseServer")
_NULL_SINKS = ("auto_null", "null")

_RECORD_PS1 = r"""param($seconds, $out)
Add-Type -TypeDefinition 'using System.Runtime.InteropServices; using System.Text; public class MciRec { [DllImport("winmm.dll", CharSet=CharSet.Auto)] public static extern int mciSendString(string c, StringBuilder r, int n, System.IntPtr h); }'
function M($c){ $sb = New-Object System.Text.StringBuilder 256; $e=[MciRec]::mciSendString($c,$sb,256,[IntPtr]::Zero); if($e -ne 0){ throw "MCI error $e on: $c" } }
M 'open new type waveaudio alias rec'
M 'set rec time format ms bitspersample 16 channels 1 samplespersec 22050 alignment 2 bytespersec 44100'
M 'record rec'
Start-Sleep -Seconds $seconds
M 'stop rec'
M "save rec `"$out`""
M 'close rec'
"""

# ------------------------------------------------------------------ environment
def is_wsl():
    try:
        return "microsoft" in Path("/proc/version").read_text().lower() or "WSL_DISTRO_NAME" in os.environ
    except OSError:
        return "WSL_DISTRO_NAME" in os.environ

def set_pulse_server(server):
    """Use `server` as PULSE_SERVER; with an empty value fall back to WSLg's socket if it exists."""
    server = (server or "").strip()
    if server:
        os.environ["PULSE_SERVER"] = server
    elif "PULSE_SERVER" not in os.environ and WSLG_PULSE_SOCKET.exists():
        os.environ["PULSE_SERVER"] = f"unix:{WSLG_PULSE_SOCKET}"

def _run(cmd, timeout=5, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)

# ------------------------------------------------------------------ device lists
def _pulse_devices(direction):
    if not shutil.which("pactl"):
        return []
    kind = "sinks" if direction == "output" else "sources"
    try:
        items = json.loads(_run(["pactl", "--format=json", "list", kind]).stdout)
    except Exception:
        try:  # old pactl without JSON support
            lines = _run(["pactl", "list", "short", kind]).stdout.splitlines()
            items = [{"name": l.split("\t")[1], "description": l.split("\t")[1]} for l in lines if "\t" in l]
        except Exception:
            return []
    res = []
    for it in items:
        name = it.get("name", "")
        if direction == "input" and name.endswith(".monitor"):
            continue  # loopback of an output, not a microphone
        res.append((name, it.get("description") or name))
    return res

def _alsa_devices(direction):
    tool = "aplay" if direction == "output" else "arecord"
    cards = []
    try:
        for line in Path("/proc/asound/cards").read_text().splitlines():
            m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)", line)
            if m:
                cards.append((f"plughw:{m.group(1)},0", f"{m.group(3).strip()} (card {m.group(1)})"))
    except OSError:
        pass
    if not cards or not shutil.which(tool):
        return []
    return [("default", "ALSA default")] + cards

def list_devices(backend, direction):
    """[(device id, label)] for a concrete backend. Empty means the backend is not usable."""
    if backend == "pulse":
        return _pulse_devices(direction)
    if backend == "alsa":
        return _alsa_devices(direction)
    if backend == "windows":
        ok = is_wsl() and shutil.which("powershell.exe")
        return [("", WINDOWS_DEFAULT)] if ok else []
    return []

def _real(backend, direction):
    devs = list_devices(backend, direction)
    if backend == "pulse":
        devs = [d for d in devs if not d[0].startswith(_NULL_SINKS)]
    return devs

def usable(backend, direction):
    if not _real(backend, direction):
        return False
    tools = {
        ("pulse", "output"): ("paplay", "pw-play"), ("pulse", "input"): ("parecord", "pw-record"),
        ("alsa", "output"): ("aplay",), ("alsa", "input"): ("arecord",),
        ("windows", "output"): ("powershell.exe",), ("windows", "input"): ("powershell.exe",),
    }[(backend, direction)]
    return any(shutil.which(t) for t in tools)

def resolve(preference, direction):
    """Concrete backend to use for 'output'/'input' ('pulse'/'alsa'/'windows'), or None."""
    if preference in ("pulse", "alsa", "windows"):
        return preference if usable(preference, direction) else None
    for b in ("pulse", "alsa", "windows"):
        if usable(b, direction):
            return b
    return None

def diagnose(preference):
    """Human-readable status lines for the settings page."""
    lines = []
    for direction in ("output", "input"):
        b = resolve(preference, direction)
        if b:
            lines.append(f"✓ {direction.capitalize()}: using {BACKENDS[b]}")
        else:
            lines.append(f"⚠ {direction.capitalize()}: no working audio path"
                         + (f" for {BACKENDS[preference]}" if preference != "auto" else ""))
    sinks = _pulse_devices("output")
    if sinks and all(n.startswith(_NULL_SINKS) for n, _ in sinks):
        lines.append("ℹ PulseAudio/PipeWire only has a dummy output (common on WSL without WSLg audio), so it is skipped.")
    if is_wsl():
        if not shutil.which("powershell.exe"):
            lines.append("ℹ WSL detected but Windows interop is disabled, so the Windows host backend is unavailable.")
        elif resolve(preference, "output") != "windows":
            lines.append("ℹ On WSL you can pick “Windows host (WSL)” to play through the Windows default device.")
    if not any(resolve(preference, d) for d in ("output", "input")):
        lines.append("Install pulseaudio-utils/pipewire-pulse or alsa-utils, or run on WSL with interop enabled.")
    return "\n".join(lines)

# ------------------------------------------------------------------ Windows helpers (WSL)
_win_temp_cache = {}

def _windows_temp():
    """(linux path, windows path) of the Windows temp dir."""
    if not _win_temp_cache:
        win = _run(["cmd.exe", "/c", "echo %TEMP%"], cwd="/mnt/c").stdout.strip()
        unix = _run(["wslpath", "-u", win]).stdout.strip()
        _win_temp_cache.update(unix=Path(unix), win=win)
    return _win_temp_cache["unix"], _win_temp_cache["win"]

def _ps(command):
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] + command

# ------------------------------------------------------------------ playback
def start_playback(path, preference="auto", device=""):
    """Start playing a wav file; returns the running subprocess.Popen. Raises RuntimeError if impossible."""
    backend = resolve(preference, "output")
    out = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if backend == "pulse":
        if shutil.which("paplay"):
            cmd = ["paplay"] + ([f"--device={device}"] if device else []) + [str(path)]
        else:
            cmd = ["pw-play"] + ([f"--target={device}"] if device else []) + [str(path)]
        return subprocess.Popen(cmd, **out)
    if backend == "alsa":
        return subprocess.Popen(["aplay", "-q"] + (["-D", device] if device else []) + [str(path)], **out)
    if backend == "windows":
        unix, win = _windows_temp()
        shutil.copyfile(path, unix / "coqui-tts-gui-play.wav")
        script = f"(New-Object Media.SoundPlayer '{win}\\coqui-tts-gui-play.wav').PlaySync()"
        return subprocess.Popen(_ps(["-Command", script]), cwd="/mnt/c", **out)
    raise RuntimeError("No working audio output.\n\n" + diagnose(preference))

# ------------------------------------------------------------------ recording
def record(seconds, dest, preference="auto", device=""):
    """Blocking: record `seconds` of mono 22.05 kHz audio into `dest` (wav). Returns (ok, error text)."""
    dest = Path(dest)
    dest.unlink(missing_ok=True)
    backend = resolve(preference, "input")
    try:
        if backend == "pulse":
            if shutil.which("parecord"):
                cmd = ["parecord", "--file-format=wav", "--channels=1", "--rate=22050"] + \
                      ([f"--device={device}"] if device else []) + [str(dest)]
            else:
                cmd = ["pw-record", "--channels=1", "--rate=22050"] + ([f"--target={device}"] if device else []) + [str(dest)]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            time.sleep(seconds)
            proc.send_signal(signal.SIGINT)  # lets the recorder finalize the wav header
            try:
                _, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                err = b""
            err = err.decode(errors="replace").strip()
        elif backend == "alsa":
            r = _run(["arecord", "-q", "-f", "S16_LE", "-r", "22050", "-c", "1", "-d", str(int(seconds))]
                     + (["-D", device] if device else []) + [str(dest)], timeout=seconds + 10)
            err = r.stderr.strip()
        elif backend == "windows":
            unix, win = _windows_temp()
            (unix / "coqui-tts-gui-rec.ps1").write_text(_RECORD_PS1, encoding="utf-8-sig")
            tmp_name = "coqui-tts-gui-rec.wav"
            (unix / tmp_name).unlink(missing_ok=True)
            r = _run(_ps(["-File", f"{win}\\coqui-tts-gui-rec.ps1", str(int(seconds)), f"{win}\\{tmp_name}"]),
                     timeout=seconds + 30, cwd="/mnt/c")
            err = (r.stderr or r.stdout).strip()
            if (unix / tmp_name).exists():
                shutil.copyfile(unix / tmp_name, dest)
        else:
            return False, "No working audio input.\n\n" + diagnose(preference)
    except Exception as e:
        return False, str(e)
    ok = dest.exists() and dest.stat().st_size > 1000
    return ok, ("" if ok else err or "Nothing was recorded.")
