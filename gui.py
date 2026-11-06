# Coqui TTS Studio - a Tkinter GUI for Coqui TTS models.
# Run with: venv/bin/python gui.py
import json
import os
import queue
import subprocess
import threading
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np

# Silence the harmless "[transformers] Model config: bos_token_id ..." warnings from XTTS.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import audio
import TTS.utils.manage as ttsmanage
try:
    import transformers
    transformers.logging.set_verbosity_error()
except Exception:
    pass
from TTS.utils.manage import ModelManager
from TTS.utils.synthesizer import Synthesizer

APP_NAME = "Coqui TTS Studio"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "coqui-tts-output"
LAST_WAV = OUTPUT_DIR / "last.wav"
SETTINGS_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "coqui-tts-gui" / "settings.json"
NO_VOCODER = "(none - model's built-in vocoder)"
ALL_LANGS = "All languages"

PALETTES = {
    "light": dict(bg="#f4f5f8", panel="#ffffff", side="#eaecf1", fg="#1c2230", muted="#6a7384",
                  accent="#5557d9", accent_fg="#ffffff", accent_hover="#4244c0", border="#d3d7e0",
                  field="#ffffff", select="#dfe0fb", danger="#c0392b"),
    "dark": dict(bg="#13151a", panel="#1b1e26", side="#171a20", fg="#e6e9f0", muted="#8b93a5",
                 accent="#7b7dff", accent_fg="#10121a", accent_hover="#9496ff", border="#2b303b",
                 field="#232733", select="#2f3350", danger="#ff6b5b"),
}

# ---------------------------------------------------------------- settings
def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_settings(data):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        traceback.print_exc()

settings = load_settings()

def system_theme():
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=1).stdout
        return "dark" if "dark" in out else "light"
    except Exception:
        return "light"

# ---------------------------------------------------------------- backend
state = {"status": "Ready", "done": 0, "total": 0, "busy": False, "cancel": False, "downloading": False}
ui_queue = queue.Queue()
cache = {"key": None, "synth": None}
play_obj = None

def ui(fn, *args):
    """Run fn(*args) on the Tk main thread (safe to call from workers)."""
    ui_queue.put((fn, args))

class _GuiTqdm:
    """Replaces tqdm inside TTS.utils.manage so downloads report to the GUI progress bar."""
    def __init__(self, *args, total=0, **kwargs):
        state["done"], state["total"] = 0, total or 0
    def update(self, n=1):
        state["done"] += n
    def close(self): pass
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def write(self, *a, **k): pass

ttsmanage.tqdm = _GuiTqdm

def _gui_ask_tos(model_full_path):
    """Replaces ModelManager.ask_tos: asks in a GUI dialog instead of the terminal."""
    result, done = {}, threading.Event()
    def ask():
        result["ok"] = messagebox.askyesno(
            title="Terms of service",
            message="You must confirm the following:\n\n"
                    "• I have purchased a commercial license from Coqui: licensing@coqui.ai\n"
                    "• Otherwise, I agree to the terms of the non-commercial CPML:\n"
                    "   https://tts-hub.github.io/cpml\n\nDo you agree?")
        done.set()
    ui(ask)
    done.wait()
    if result["ok"]:
        with open(Path(model_full_path) / "tos_agreed.txt", "w", encoding="utf-8") as f:
            f.write("I have read, understood and agreed to the Terms and Conditions.")
    return result["ok"]

ModelManager.ask_tos = staticmethod(_gui_ask_tos)

class Cancelled(Exception):
    pass

def check_cancel():
    if state["cancel"]:
        raise Cancelled()

def download(manager, full_name):
    state.update(status=f"Downloading {full_name} (skipped if cached)...", done=0, total=0, downloading=True)
    try:
        model_path, config_path, _ = manager.download_model(full_name)
    finally:
        state.update(total=0, downloading=False)
    return model_path, config_path

def ensure_synth(model, vocoder, cuda):
    """Download (if needed) and load the synthesizer; cached while the selection is unchanged."""
    key = (model, vocoder, cuda)
    if cache["key"] == key:
        return cache["synth"]
    cache.update(key=None, synth=None)  # free the previous model first
    manager = ModelManager(progress_bar=True)
    model_path, config_path = download(manager, f"tts_models/{model}")
    check_cancel()
    kwargs = {}
    if vocoder and vocoder != NO_VOCODER:
        vpath, vconfig = download(manager, f"vocoder_models/{vocoder}")
        kwargs = dict(vocoder_checkpoint=str(vpath), vocoder_config=str(vconfig))
    check_cancel()
    state["status"] = "Loading model..."
    synth = Synthesizer(tts_checkpoint=str(model_path), tts_config_path=str(config_path), use_cuda=cuda, **kwargs)
    cache.update(key=key, synth=synth)
    return synth

def model_options(synth):
    """Languages, speakers and voice-cloning support of a loaded model."""
    tm = synth.tts_model
    langs = list(getattr(getattr(tm, "language_manager", None), "language_names", None) or [])
    spk_mgr = getattr(tm, "speaker_manager", None)
    speakers = list(getattr(spk_mgr, "speaker_names", None) or []) if spk_mgr else []
    cloning = hasattr(tm, "clone_voice") or bool(getattr(synth, "encoder_checkpoint", None))
    return langs, speakers, cloning

def load_worker(model, vocoder, cuda):
    try:
        synth = ensure_synth(model, vocoder, cuda)
        langs, speakers, cloning = model_options(synth)
        ui(populate_voice_options, langs, speakers, cloning)
        state["status"] = "Model ready"
    except Cancelled:
        state["status"] = "Cancelled"
    except Exception as e:
        traceback.print_exc()
        state["status"] = "Error"
        ui(messagebox.showerror, "Error", str(e))
    finally:
        state["busy"] = False
        ui(refresh_controls)

def synth_worker(text, model, vocoder, cuda, lang, speaker, ref_wav, outfile, play):
    try:
        synth = ensure_synth(model, vocoder, cuda)
        langs, speakers, cloning = model_options(synth)
        ui(populate_voice_options, langs, speakers, cloning)
        lang = (lang or None) if langs else None
        if lang is None and langs:
            lang = "en" if "en" in langs else langs[0]
        ref = ref_wav if (ref_wav and cloning and os.path.exists(ref_wav)) else None
        spk = None
        if ref is None and speakers:
            spk = speaker if speaker in speakers else speakers[0]
        try:
            sentences = synth.split_into_sentences(text, lang) or [text]
        except Exception:
            sentences = [text]
        parts = []
        for i, sen in enumerate(sentences, 1):
            check_cancel()
            state["status"] = f"Synthesizing {i}/{len(sentences)}..."
            wav = synth.tts(sen, speaker_name=spk, language_name=lang, speaker_wav=ref, split_sentences=False)
            parts.append(np.asarray(wav, dtype=np.float32))
        check_cancel()
        synth.save_wav(np.concatenate(parts), str(outfile))
        state["status"] = "Done"
        if play:
            ui(play_file, outfile)
    except Cancelled:
        state["status"] = "Cancelled"
    except Exception as e:
        traceback.print_exc()
        state["status"] = "Error"
        ui(messagebox.showerror, "Error", str(e))
    finally:
        state["busy"] = False
        ui(refresh_controls)

# ---------------------------------------------------------------- audio
# Playback goes through the system sound server (PulseAudio/PipeWire, which WSLg provides)
# instead of ALSA directly, because WSL has no ALSA sound card ("Unknown PCM default").
audio_cfg = {"backend": settings.get("audio_backend", "auto"),
             "output": settings.get("output_device", ""), "input": settings.get("input_device", "")}
audio.set_pulse_server(settings.get("pulse_server", ""))

def play_file(path):
    global play_obj
    stop_audio()
    try:
        play_obj = audio.start_playback(path, audio_cfg["backend"], audio_cfg["output"])
    except Exception as e:
        messagebox.showerror(APP_NAME, f"Could not play audio:\n{e}")
    refresh_controls()

def stop_audio():
    global play_obj
    if play_obj is not None and play_obj.poll() is None:
        play_obj.terminate()
    play_obj = None
    refresh_controls()

def is_playing():
    return play_obj is not None and play_obj.poll() is None

# ---------------------------------------------------------------- model catalogue
manager0 = ModelManager()
models_dict = manager0.models_dict
all_models = []      # (name, lang, downloaded)
all_vocoders = []
for _lang, _ds in models_dict["tts_models"].items():
    for _dataset, _ms in _ds.items():
        for _m in _ms:
            all_models.append((f"{_lang}/{_dataset}/{_m}", _lang))
for _lang, _ds in models_dict["vocoder_models"].items():
    for _dataset, _ms in _ds.items():
        for _m in _ms:
            all_vocoders.append(f"{_lang}/{_dataset}/{_m}")

def is_downloaded(name, kind="tts_models"):
    lang, dataset, model = name.split("/")
    return (Path(manager0.output_prefix) / f"{kind}--{lang}--{dataset}--{model}").is_dir()

def model_item(name):
    lang, dataset, model = name.split("/")
    return models_dict["tts_models"][lang][dataset][model]

# ---------------------------------------------------------------- window
window = tk.Tk()
window.title(APP_NAME)
window.geometry(settings.get("geometry", "1100x720"))
window.minsize(960, 620)

theme_name = settings.get("theme") or system_theme()
style = ttk.Style(window)
style.theme_use("clam")

visible_models = []      # names currently shown in the listbox
current_model = tk.StringVar(value=settings.get("model", "multilingual/multi-dataset/xtts_v2"))
vocoder_var = tk.StringVar(value=settings.get("vocoder", NO_VOCODER))
lang_var = tk.StringVar(value=settings.get("language", ""))
speaker_var = tk.StringVar(value=settings.get("speaker", ""))
ref_var = tk.StringVar(value=settings.get("ref_wav", ""))
search_var = tk.StringVar()
filter_var = tk.StringVar(value=ALL_LANGS)
cuda_var = tk.BooleanVar(value=False)
status_var = tk.StringVar(value="Ready")
count_var = tk.StringVar(value="0 characters")
model_info_var = tk.StringVar()

try:
    import torch
    cuda_ok = torch.cuda.is_available()
except Exception:
    cuda_ok = False
cuda_var.set(bool(settings.get("cuda", True)) and cuda_ok)

themed = {"text": [], "listbox": [], "canvas": []}

def apply_theme(name):
    global theme_name
    theme_name = name
    c = PALETTES[name]
    window.configure(bg=c["bg"])
    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["side"], font=("TkDefaultFont", 10))
    style.configure("TFrame", background=c["bg"])
    style.configure("Panel.TFrame", background=c["panel"])
    style.configure("Side.TFrame", background=c["side"])
    style.configure("Header.TFrame", background=c["panel"])
    style.configure("TLabel", background=c["bg"], foreground=c["fg"])
    style.configure("Side.TLabel", background=c["side"], foreground=c["fg"])
    style.configure("Header.TLabel", background=c["panel"], foreground=c["fg"])
    style.configure("Brand.TLabel", background=c["panel"], foreground=c["fg"], font=("TkDefaultFont", 14, "bold"))
    style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"], font=("TkDefaultFont", 9))
    style.configure("SideMuted.TLabel", background=c["side"], foreground=c["muted"], font=("TkDefaultFont", 9))
    style.configure("HeaderMuted.TLabel", background=c["panel"], foreground=c["muted"], font=("TkDefaultFont", 9))
    style.configure("Section.TLabel", background=c["side"], foreground=c["muted"], font=("TkDefaultFont", 8, "bold"))
    style.configure("TSeparator", background=c["border"])
    style.configure("TButton", background=c["panel"], foreground=c["fg"], bordercolor=c["border"],
                    padding=(12, 6), focuscolor=c["panel"], relief="flat")
    style.map("TButton", background=[("disabled", c["side"]), ("active", c["select"])],
              foreground=[("disabled", c["muted"])])
    style.configure("Side.TButton", background=c["panel"])
    style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_fg"], bordercolor=c["accent"],
                    padding=(16, 7), font=("TkDefaultFont", 10, "bold"))
    style.map("Accent.TButton", background=[("disabled", c["border"]), ("active", c["accent_hover"])],
              foreground=[("disabled", c["muted"])], bordercolor=[("disabled", c["border"])])
    style.configure("Danger.TButton", foreground=c["danger"])
    style.configure("TEntry", fieldbackground=c["field"], foreground=c["fg"], insertcolor=c["fg"], padding=5)
    style.configure("TCombobox", fieldbackground=c["field"], background=c["panel"], foreground=c["fg"],
                    arrowcolor=c["fg"], selectbackground=c["field"], selectforeground=c["fg"], padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["fg"])],
              selectbackground=[("readonly", c["field"])], selectforeground=[("readonly", c["fg"])])
    window.option_add("*TCombobox*Listbox.background", c["field"])
    window.option_add("*TCombobox*Listbox.foreground", c["fg"])
    window.option_add("*TCombobox*Listbox.selectBackground", c["accent"])
    window.option_add("*TCombobox*Listbox.selectForeground", c["accent_fg"])
    style.configure("Side.TCheckbutton", background=c["side"], foreground=c["fg"], focuscolor=c["side"])
    style.map("Side.TCheckbutton", background=[("active", c["side"])], indicatorcolor=[("selected", c["accent"])])
    style.configure("Horizontal.TProgressbar", background=c["accent"], troughcolor=c["side"],
                    bordercolor=c["border"], lightcolor=c["accent"], darkcolor=c["accent"])
    style.configure("Vertical.TScrollbar", background=c["panel"], troughcolor=c["side"], bordercolor=c["side"],
                    arrowcolor=c["muted"])
    for t in themed["text"]:
        t.configure(bg=c["field"], fg=c["fg"], insertbackground=c["fg"], selectbackground=c["accent"],
                    selectforeground=c["accent_fg"], highlightbackground=c["border"], highlightcolor=c["accent"])
    for lb in themed["listbox"]:
        lb.configure(bg=c["field"], fg=c["fg"], selectbackground=c["accent"], selectforeground=c["accent_fg"],
                     highlightbackground=c["border"], highlightcolor=c["accent"])
    for cv in themed["canvas"]:
        draw_logo(cv)
    theme_button.configure(text="☾  Dark" if name == "light" else "☀  Light")

# ---- header
header = ttk.Frame(window, style="Header.TFrame", padding=(16, 10))
header.grid(row=0, column=0, columnspan=2, sticky="ew")
header.columnconfigure(2, weight=1)

def draw_logo(cv):
    c = PALETTES[theme_name]
    cv.delete("all")
    cv.configure(bg=c["panel"])
    cv.create_oval(1, 1, 35, 35, fill=c["accent"], outline="")
    for i, h in enumerate((6, 12, 18, 12, 7)):
        x = 8 + i * 5
        cv.create_line(x, 18 - h / 2, x, 18 + h / 2, fill=c["accent_fg"], width=3, capstyle="round")

logo = tk.Canvas(header, width=36, height=36, highlightthickness=0)
logo.grid(row=0, column=0, rowspan=2, padx=(0, 10))
themed["canvas"].append(logo)
ttk.Label(header, text=APP_NAME, style="Brand.TLabel").grid(row=0, column=1, sticky="w")
ttk.Label(header, text="Neural text-to-speech, offline on your machine", style="HeaderMuted.TLabel").grid(row=1, column=1, sticky="w")

def toggle_theme():
    apply_theme("dark" if theme_name == "light" else "light")
theme_button = ttk.Button(header, command=toggle_theme)
theme_button.grid(row=0, column=4, rowspan=2, sticky="e", padx=(8, 0))
settings_button = ttk.Button(header, text="\u2699  Settings")
settings_button.grid(row=0, column=3, rowspan=2, sticky="e")

# ---- sidebar
side = ttk.Frame(window, style="Side.TFrame", padding=14, width=320)
side.grid(row=1, column=0, sticky="ns")
side.grid_propagate(False)
side.columnconfigure(0, weight=1)
side.rowconfigure(4, weight=1)
window.rowconfigure(1, weight=1)
window.columnconfigure(1, weight=1)

ttk.Label(side, text="MODEL", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
search_row = ttk.Frame(side, style="Side.TFrame")
search_row.grid(row=1, column=0, sticky="ew")
search_row.columnconfigure(0, weight=1)
search_entry = ttk.Entry(search_row, textvariable=search_var)
search_entry.grid(row=0, column=0, sticky="ew")
lang_filter = ttk.Combobox(search_row, textvariable=filter_var, state="readonly", width=12,
                           values=[ALL_LANGS] + sorted({l for _, l in all_models}))
lang_filter.grid(row=0, column=1, padx=(6, 0))
ttk.Label(side, text="✓ = already downloaded", style="SideMuted.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 2))

list_frame = ttk.Frame(side, style="Side.TFrame")
list_frame.grid(row=4, column=0, sticky="nsew")
list_frame.rowconfigure(0, weight=1)
list_frame.columnconfigure(0, weight=1)
model_list = tk.Listbox(list_frame, exportselection=False, activestyle="none", borderwidth=0, highlightthickness=1,
                        height=8)
model_list.grid(row=0, column=0, sticky="nsew")
themed["listbox"].append(model_list)
list_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=model_list.yview)
list_scroll.grid(row=0, column=1, sticky="ns")
model_list.configure(yscrollcommand=list_scroll.set)

ttk.Label(side, textvariable=model_info_var, style="SideMuted.TLabel", wraplength=280, justify="left").grid(
    row=5, column=0, sticky="w", pady=(6, 8))

ttk.Label(side, text="VOCODER", style="Section.TLabel").grid(row=6, column=0, sticky="w", pady=(0, 4))
vocoder_box = ttk.Combobox(side, textvariable=vocoder_var, state="readonly", values=[NO_VOCODER] + all_vocoders)
vocoder_box.grid(row=7, column=0, sticky="ew")

cuda_check = ttk.Checkbutton(side, text="Use CUDA GPU" if cuda_ok else "Use CUDA GPU (none detected)",
                             variable=cuda_var, style="Side.TCheckbutton")
cuda_check.grid(row=8, column=0, sticky="w", pady=(10, 0))
if not cuda_ok:
    cuda_check.state(["disabled"])

load_button = ttk.Button(side, text="Load model", style="Side.TButton")
load_button.grid(row=9, column=0, sticky="ew", pady=(10, 0))

ttk.Separator(side).grid(row=10, column=0, sticky="ew", pady=12)

voice_frame = ttk.Frame(side, style="Side.TFrame")
voice_frame.grid(row=11, column=0, sticky="ew")
voice_frame.columnconfigure(0, weight=1)
ttk.Label(voice_frame, text="VOICE", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
voice_hint = ttk.Label(voice_frame, style="SideMuted.TLabel", wraplength=280, justify="left",
                       text="Load the model to see its language, speaker and voice-cloning options.")
voice_hint.grid(row=1, column=0, sticky="w")
lang_label = ttk.Label(voice_frame, text="Language", style="Side.TLabel")
lang_box = ttk.Combobox(voice_frame, textvariable=lang_var, state="readonly")
speaker_label = ttk.Label(voice_frame, text="Speaker", style="Side.TLabel")
speaker_box = ttk.Combobox(voice_frame, textvariable=speaker_var, state="readonly")
ref_label = ttk.Label(voice_frame, text="Clone voice from a recording (>3 s)", style="Side.TLabel")
ref_row = ttk.Frame(voice_frame, style="Side.TFrame")
ref_row.columnconfigure(0, weight=1)
ref_entry = ttk.Entry(ref_row, textvariable=ref_var)
ref_entry.grid(row=0, column=0, sticky="ew")

def browse_ref():
    p = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.flac *.ogg"), ("All files", "*.*")])
    if p:
        ref_var.set(p)
ttk.Button(ref_row, text="…", width=3, command=browse_ref, style="Side.TButton").grid(row=0, column=1, padx=(4, 0))
ttk.Button(ref_row, text="✕", width=3, command=lambda: ref_var.set(""), style="Side.TButton").grid(row=0, column=2, padx=(4, 0))

voice_widgets = (lang_label, lang_box, speaker_label, speaker_box, ref_label, ref_row)

def populate_voice_options(langs, speakers, cloning):
    for w in voice_widgets:
        w.grid_remove()
    row = 1
    voice_hint.grid_remove()
    if langs:
        lang_box.configure(values=langs)
        if lang_var.get() not in langs:
            lang_var.set("en" if "en" in langs else langs[0])
        lang_label.grid(row=row, column=0, sticky="w", pady=(0, 2)); lang_box.grid(row=row + 1, column=0, sticky="ew", pady=(0, 8))
        row += 2
    if speakers:
        speaker_box.configure(values=speakers)
        if speaker_var.get() not in speakers:
            speaker_var.set(speakers[0])
        speaker_label.grid(row=row, column=0, sticky="w", pady=(0, 2)); speaker_box.grid(row=row + 1, column=0, sticky="ew", pady=(0, 8))
        row += 2
    if cloning:
        ref_label.grid(row=row, column=0, sticky="w", pady=(0, 2)); ref_row.grid(row=row + 1, column=0, sticky="ew")
        row += 2
    if row == 1:
        voice_hint.configure(text="This model has no extra voice options.")
        voice_hint.grid(row=1, column=0, sticky="w")

def reset_voice_options():
    for w in voice_widgets:
        w.grid_remove()
    voice_hint.configure(text="Load the model to see its language, speaker and voice-cloning options.")
    voice_hint.grid(row=1, column=0, sticky="w")

# ---- main area
main = ttk.Frame(window, padding=(16, 14, 16, 8))
main.grid(row=1, column=1, sticky="nsew")
main.rowconfigure(1, weight=1)
main.columnconfigure(0, weight=1)

toolbar = ttk.Frame(main)
toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
toolbar.columnconfigure(3, weight=1)
open_button = ttk.Button(toolbar, text="Open text…")
open_button.grid(row=0, column=0)
save_button = ttk.Button(toolbar, text="Save text…")
save_button.grid(row=0, column=1, padx=(6, 0))
ttk.Label(toolbar, textvariable=count_var, style="Muted.TLabel").grid(row=0, column=3, sticky="e")

text_frame = ttk.Frame(main)
text_frame.grid(row=1, column=0, sticky="nsew")
text_frame.rowconfigure(0, weight=1)
text_frame.columnconfigure(0, weight=1)
inputbox = tk.Text(text_frame, wrap="word", undo=True, borderwidth=0, highlightthickness=1, padx=12, pady=10,
                   font=("TkTextFont", 12), spacing3=4)
inputbox.grid(row=0, column=0, sticky="nsew")
themed["text"].append(inputbox)
text_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=inputbox.yview)
text_scroll.grid(row=0, column=1, sticky="ns")
inputbox.configure(yscrollcommand=text_scroll.set)
inputbox.insert("1.0", settings.get("text", ""))
inputbox.edit_modified(False)

actions = ttk.Frame(main)
actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
generate_button = ttk.Button(actions, text="▶  Generate & play", style="Accent.TButton")
generate_button.grid(row=0, column=0)
stop_button = ttk.Button(actions, text="■  Stop")
stop_button.grid(row=0, column=1, padx=(6, 0))
replay_button = ttk.Button(actions, text="Play last")
replay_button.grid(row=0, column=2, padx=(6, 0))
export_button = ttk.Button(actions, text="Export audio…")
export_button.grid(row=0, column=3, padx=(6, 0))
cancel_button = ttk.Button(actions, text="Cancel", style="Danger.TButton")
cancel_button.grid(row=0, column=4, padx=(6, 0))

# ---- footer
footer = ttk.Frame(window, padding=(16, 6, 16, 12))
footer.grid(row=2, column=0, columnspan=2, sticky="ew")
footer.columnconfigure(1, weight=1)
ttk.Label(footer, textvariable=status_var, style="Muted.TLabel", width=34).grid(row=0, column=0, sticky="w")
progressbar = ttk.Progressbar(footer, mode="determinate")
progressbar.grid(row=0, column=1, sticky="ew")

# ---- settings page (covers sidebar + main area)
SYSTEM_DEFAULT = "(system default)"
pulse_var = tk.StringVar(value=settings.get("pulse_server", ""))
backend_var = tk.StringVar(value=audio.BACKENDS.get(audio_cfg["backend"], audio.BACKENDS["auto"]))
out_var = tk.StringVar()
in_var = tk.StringVar()
audio_diag_var = tk.StringVar()
device_map = {"output": {}, "input": {}}

settings_page = ttk.Frame(window, padding=(32, 24))
settings_page.grid(row=1, column=0, columnspan=2, sticky="nsew")
settings_page.columnconfigure(1, weight=1)
ttk.Label(settings_page, text="Settings", font=("TkDefaultFont", 16, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
ttk.Label(settings_page, text="Audio", style="Muted.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 14))

ttk.Label(settings_page, text="Audio system").grid(row=2, column=0, sticky="w", pady=6, padx=(0, 16))
backend_box = ttk.Combobox(settings_page, textvariable=backend_var, state="readonly", values=list(audio.BACKENDS.values()))
backend_box.grid(row=2, column=1, sticky="ew")

ttk.Label(settings_page, text="Output device").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 16))
out_box = ttk.Combobox(settings_page, textvariable=out_var, state="readonly")
out_box.grid(row=3, column=1, sticky="ew")
test_out_button = ttk.Button(settings_page, text="Play test sound")
test_out_button.grid(row=3, column=2, padx=(8, 0))

ttk.Label(settings_page, text="Input device").grid(row=4, column=0, sticky="w", pady=6, padx=(0, 16))
in_box = ttk.Combobox(settings_page, textvariable=in_var, state="readonly")
in_box.grid(row=4, column=1, sticky="ew")
test_in_button = ttk.Button(settings_page, text="Record 3 s test")
test_in_button.grid(row=4, column=2, padx=(8, 0))

ttk.Label(settings_page, text="PulseAudio server").grid(row=5, column=0, sticky="w", pady=6, padx=(0, 16))
ttk.Entry(settings_page, textvariable=pulse_var).grid(row=5, column=1, sticky="ew")
ttk.Label(settings_page, text="Optional (PULSE_SERVER), e.g. unix:/mnt/wslg/PulseServer or tcp:192.168.1.5. Leave empty for the default.",
          style="Muted.TLabel").grid(row=6, column=1, sticky="w")

refresh_dev_button = ttk.Button(settings_page, text="Apply / refresh devices")
refresh_dev_button.grid(row=7, column=0, columnspan=3, sticky="w", pady=(16, 0))
ttk.Label(settings_page, textvariable=audio_diag_var, wraplength=760, justify="left").grid(
    row=8, column=0, columnspan=3, sticky="w", pady=(16, 0))

def selected_backend():
    return next((k for k, v in audio.BACKENDS.items() if v == backend_var.get()), "auto")

def refresh_devices(*_):
    audio_cfg["backend"] = selected_backend()
    audio.set_pulse_server(pulse_var.get())
    for direction, box, var in (("output", out_box, out_var), ("input", in_box, in_var)):
        backend = audio.resolve(audio_cfg["backend"], direction)
        mapping = {SYSTEM_DEFAULT: ""}
        if backend:
            for dev_id, label in audio.list_devices(backend, direction):
                if backend == "windows" or dev_id.startswith(("auto_null",)):
                    continue  # the Windows entry IS the system default; dummy devices are useless
                mapping[label if label not in mapping else f"{label} ({dev_id})"] = dev_id
        device_map[direction] = mapping
        box.configure(values=list(mapping))
        cur = audio_cfg[direction]
        var.set(next((d for d, n in mapping.items() if n == cur), SYSTEM_DEFAULT))
        if cur not in mapping.values():
            audio_cfg[direction] = ""
    audio_diag_var.set(audio.diagnose(audio_cfg["backend"]))

def apply_devices(*_):
    audio_cfg["backend"] = selected_backend()
    audio_cfg["output"] = device_map["output"].get(out_var.get(), "")
    audio_cfg["input"] = device_map["input"].get(in_var.get(), "")

backend_box.bind("<<ComboboxSelected>>", lambda e: (audio_cfg.update(output="", input=""), refresh_devices()))
out_box.bind("<<ComboboxSelected>>", apply_devices)
in_box.bind("<<ComboboxSelected>>", apply_devices)
refresh_dev_button.configure(command=lambda: (apply_devices(), refresh_devices()))

def make_test_tone(path, seconds=1.0, rate=22050):
    import wave
    t = np.arange(int(seconds * rate)) / rate
    env = np.minimum(1, np.minimum(t, seconds - t) * 20)
    data = (0.3 * np.sin(2 * np.pi * 440 * t) * env * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(data.tobytes())

def play_test_sound():
    apply_devices()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUTPUT_DIR / "test-tone.wav"
    make_test_tone(p)
    play_file(p)

def record_sample(seconds, dest, on_done):
    """Record from the selected input in a thread; on_done(ok, error) runs on the Tk thread."""
    apply_devices()
    cfg = dict(audio_cfg)
    state["status"] = f"Recording {seconds} s..."
    def run():
        ok, err = audio.record(seconds, dest, cfg["backend"], cfg["input"])
        ui(on_done, ok, err)
    threading.Thread(target=run, daemon=True).start()

def test_input():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUTPUT_DIR / "mic-test.wav"
    test_in_button.state(["disabled"])
    def done(ok, err):
        import wave
        test_in_button.state(["!disabled"])
        state["status"] = "Ready"
        if not ok:
            messagebox.showerror(APP_NAME, "Recording failed.\n" + err)
            return
        with wave.open(str(p)) as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        peak = int(np.abs(raw).max()) * 100 // 32767 if raw.size else 0
        audio_diag_var.set(f"Recorded 3 s, peak level {peak}%. "
                           + ("Playing it back..." if peak > 0 else "\u26a0 Only silence was captured - check the input device."))
        if peak > 0:
            play_file(p)
    record_sample(3, p, done)

test_out_button.configure(command=play_test_sound)
test_in_button.configure(command=test_input)

def show_settings(show):
    if show:
        refresh_devices()
        settings_page.grid()
        settings_page.tkraise()
        settings_button.configure(text="\u2190  Back")
    else:
        apply_devices()
        settings_page.grid_remove()
        settings_button.configure(text="\u2699  Settings")
settings_button.configure(command=lambda: show_settings(not settings_page.winfo_viewable()))
settings_page.grid_remove()
refresh_devices()

# ---------------------------------------------------------------- behaviour
def refresh_controls():
    busy = state["busy"]
    for b in (generate_button, export_button, load_button):
        b.state(["disabled"] if busy else ["!disabled"])
    cancel_button.state(["!disabled"] if busy else ["disabled"])
    replay_button.state(["!disabled"] if (LAST_WAV.exists() and not busy) else ["disabled"])
    stop_button.state(["!disabled"] if is_playing() else ["disabled"])
    model_list.configure(state="disabled" if busy else "normal")

def update_count(_e=None):
    t = inputbox.get("1.0", "end-1c")
    count_var.set(f"{len(t):,} characters · {len(t.split()):,} words")

def rebuild_list(*_):
    q = search_var.get().strip().lower()
    lf = filter_var.get()
    visible_models.clear()
    model_list.delete(0, "end")
    for name, lang in all_models:
        if lf != ALL_LANGS and lang != lf:
            continue
        if q and q not in name.lower():
            continue
        visible_models.append(name)
        model_list.insert("end", ("✓  " if is_downloaded(name) else "     ") + name)
    sel = current_model.get()
    if sel in visible_models:
        i = visible_models.index(sel)
        model_list.selection_set(i)
        model_list.see(i)

def on_model_select(_e=None):
    sel = model_list.curselection()
    if not sel:
        return
    name = visible_models[sel[0]]
    if name == current_model.get() and model_info_var.get():
        return
    current_model.set(name)
    show_model_info()
    # pick the model's default vocoder (or none)
    dv = (model_item(name).get("default_vocoder") or "")
    dv = dv.replace("vocoder_models/", "")
    vocoder_var.set(dv if dv in all_vocoders else NO_VOCODER)
    if cache["key"] is None or cache["key"][0] != name:
        reset_voice_options()

def show_model_info():
    name = current_model.get()
    try:
        item = model_item(name)
    except KeyError:
        model_info_var.set("")
        return
    desc = item.get("description", "")
    tail = "Downloaded." if is_downloaded(name) else "Will be downloaded on first use."
    if item.get("tos_required"):
        tail += " Requires accepting a licence."
    model_info_var.set(f"{desc}\n{tail}".strip())

def start_job(target, *args):
    if state["busy"]:
        return
    state.update(busy=True, cancel=False, status="Starting...", done=0, total=0)
    refresh_controls()
    threading.Thread(target=target, args=args, daemon=True).start()

def selection():
    return (current_model.get(), vocoder_var.get(), bool(cuda_var.get()) and cuda_ok)

def do_load():
    m, v, c = selection()
    if m not in dict(all_models):
        messagebox.showinfo(APP_NAME, "Select a model first.")
        return
    start_job(load_worker, m, v, c)

def do_generate(outfile=None, play=True):
    text = inputbox.get("1.0", "end-1c").strip()
    if not text:
        messagebox.showinfo(APP_NAME, "Enter some text first.")
        return
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    m, v, c = selection()
    stop_audio()
    start_job(synth_worker, text, m, v, c, lang_var.get(), speaker_var.get(), ref_var.get().strip(),
              outfile or LAST_WAV, play)

def do_export():
    if not inputbox.get("1.0", "end-1c").strip():
        messagebox.showinfo(APP_NAME, "Enter some text first.")
        return
    path = filedialog.asksaveasfilename(defaultextension=".wav", filetypes=[("Wave files", "*.wav")])
    if path:
        do_generate(path, play=False)

def do_cancel():
    if state["busy"]:
        state["cancel"] = True
        state["status"] = "Cancelling (after the current step)..."

def do_replay():
    if LAST_WAV.exists():
        play_file(LAST_WAV)

def save_text():
    path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
    if not path:
        return False
    Path(path).write_text(inputbox.get("1.0", "end-1c"), encoding="utf-8")
    inputbox.edit_modified(False)
    return True

def confirm_discard():
    """True if it is fine to replace/close the current text."""
    if not inputbox.edit_modified() or not inputbox.get("1.0", "end-1c").strip():
        return True
    r = messagebox.askyesnocancel(APP_NAME, "You have unsaved changes. Do you want to save?")
    if r is None:
        return False
    return save_text() if r else True

def open_text():
    if not confirm_discard():
        return
    path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
    if not path:
        return
    inputbox.delete("1.0", "end")
    inputbox.insert("1.0", Path(path).read_text(encoding="utf-8", errors="replace"))
    inputbox.edit_modified(False)
    update_count()

def on_closing():
    if not confirm_discard():
        return
    stop_audio()
    save_settings({
        "theme": theme_name, "model": current_model.get(), "vocoder": vocoder_var.get(),
        "language": lang_var.get(), "speaker": speaker_var.get(), "ref_wav": ref_var.get(),
        "audio_backend": audio_cfg["backend"], "output_device": audio_cfg["output"], "input_device": audio_cfg["input"],
        "pulse_server": pulse_var.get().strip(),
        "cuda": bool(cuda_var.get()), "geometry": window.geometry(), "text": inputbox.get("1.0", "end-1c"),
    })
    window.destroy()

def pump():
    """Main-thread loop: run queued UI calls and update status/progress."""
    try:
        while True:
            fn, args = ui_queue.get_nowait()
            fn(*args)
    except queue.Empty:
        pass
    status_var.set(state["status"])
    if state["total"]:
        progressbar.stop()
        progressbar.configure(mode="determinate", maximum=state["total"], value=state["done"])
    elif state["busy"] and state["downloading"]:
        progressbar.configure(mode="indeterminate")
        progressbar.step(3)
    elif state["busy"]:
        progressbar.configure(mode="indeterminate")
        progressbar.step(2)
    else:
        progressbar.configure(mode="determinate", value=0)
    if play_obj is not None and not is_playing():
        stop_audio()
    window.after(60, pump)

# ---- wiring
generate_button.configure(command=lambda: do_generate())
export_button.configure(command=do_export)
cancel_button.configure(command=do_cancel)
stop_button.configure(command=stop_audio)
replay_button.configure(command=do_replay)
load_button.configure(command=do_load)
open_button.configure(command=open_text)
save_button.configure(command=save_text)
model_list.bind("<<ListboxSelect>>", on_model_select)
search_var.trace_add("write", rebuild_list)
filter_var.trace_add("write", rebuild_list)
for ev in ("<KeyRelease>", "<<Paste>>", "<<Cut>>", "<<Undo>>", "<<Redo>>"):
    inputbox.bind(ev, lambda e: window.after_idle(update_count))
window.bind("<Control-Return>", lambda e: do_generate())
window.bind("<Control-o>", lambda e: open_text())
window.bind("<Control-s>", lambda e: save_text())
window.protocol("WM_DELETE_WINDOW", on_closing)

if current_model.get() not in dict(all_models):
    current_model.set("multilingual/multi-dataset/xtts_v2" if "multilingual/multi-dataset/xtts_v2" in dict(all_models) else all_models[0][0])
apply_theme(theme_name)
rebuild_list()
show_model_info()
reset_voice_options()
update_count()
refresh_controls()
pump()

def loadgui():
    window.mainloop()

if __name__ == "__main__":
    loadgui()
