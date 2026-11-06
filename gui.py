# GUI solution for Coqui TTS made by AceOfSpadesProduc100.
# WARNING: DO NOT run this from IDLE or double-clicking, it will be stuck loading. To be safe, you should enter "python mozilla-tts-gui.py" in your terminal such as command prompt or Powershell.
# Make sure to edit this script to add TTS models and vocoders on the ['values'] of ttsmodelbox and vocodermodelbox.
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext
from tkinter import filedialog
from tkinter import messagebox
from TTS.utils.synthesizer import Synthesizer
from TTS.utils.manage import ModelManager
import os
import simpleaudio as sa

# ---- background synthesis with download progress ----
import threading
from pathlib import Path
import TTS.utils.manage as ttsmanage

_state = {"status": "", "done": 0, "total": 0, "finished": False, "error": None}

class _GuiTqdm:
    """Replaces tqdm inside TTS.utils.manage so downloads report to the GUI progress bar."""
    def __init__(self, *args, total=0, **kwargs):
        _state["done"], _state["total"] = 0, total or 0
    def update(self, n=1):
        _state["done"] += n
    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def write(self, *a, **k):
        pass

ttsmanage.tqdm = _GuiTqdm

_tos = {"event": threading.Event(), "answer": False, "pending": False}

def _gui_ask_tos(model_full_path):
    """Replaces ModelManager.ask_tos: asks in a GUI dialog instead of the terminal.
    Runs in the worker thread; the dialog itself is shown by _poll in the main thread."""
    _tos["event"].clear()
    _tos["pending"] = True
    _tos["event"].wait()
    if _tos["answer"]:
        with open(Path(model_full_path) / "tos_agreed.txt", "w", encoding="utf-8") as f:
            f.write("I have read, understood and agreed to the Terms and Conditions.")
    return _tos["answer"]

ModelManager.ask_tos = staticmethod(_gui_ask_tos)

def _poll():
    statuslabel.config(text=_state["status"])
    if _tos["pending"]:
        _tos["pending"] = False
        _tos["answer"] = messagebox.askyesno(
            title="Terms of service",
            message="You must confirm the following:\n\n"
                    "\u2022 I have purchased a commercial license from Coqui: licensing@coqui.ai\n"
                    "\u2022 Otherwise, I agree to the terms of the non-commercial CPML:\n"
                    "   https://tts-hub.github.io/cpml\n\n"
                    "Do you agree?")
        _tos["event"].set()
    if _state["total"]:
        progressbar.config(mode="determinate", maximum=_state["total"], value=_state["done"])
    elif _state["status"].startswith("Downloading"):
        progressbar.config(mode="indeterminate")
        progressbar.step(3)
    else:
        progressbar.config(mode="determinate", value=0)
    if _state["finished"]:
        progressbar.config(mode="determinate", value=0)
        generatebutton.config(state="normal")
        exportbutton.config(state="normal")
        if _state["error"]:
            messagebox.showerror(message=f"Error: {_state['error']}")
            statuslabel.config(text="Error")
        else:
            statuslabel.config(text="Done")
        return
    window.after(100, _poll)

def _download(manager, kind, name):
    full = f"{kind}/{name}"
    _state.update(status=f"Downloading {full} (if not cached)...", done=0, total=0)
    model_path, config_path, _ = manager.download_model(full)
    _state["total"] = 0
    return model_path, config_path

def _worker(text, cuda, outfile, play):
    try:
        manager = ModelManager(progress_bar=True)
        model_path, config_path = _download(manager, "tts_models", tts_name)
        vocoder_path, vocoder_config_path = _download(manager, "vocoder_models", vocoder_name)
        _state["status"] = "Loading models..."
        synthesizer = Synthesizer(tts_checkpoint=str(model_path), tts_config_path=str(config_path), vocoder_checkpoint=str(vocoder_path), vocoder_config=str(vocoder_config_path), use_cuda=cuda)
        _state["status"] = "Synthesizing..."
        wav = synthesizer.tts(text)
        synthesizer.save_wav(wav, outfile)
        if play:
            _state["status"] = "Playing..."
            sa.WaveObject.from_wave_file(outfile).play().wait_done()
            os.remove(outfile)
    except Exception as e:
        import traceback; traceback.print_exc()
        _state["error"] = str(e)
    _state["finished"] = True

def _start(outfile, play):
    global tts_name, vocoder_name
    tts_name, vocoder_name = ttsmodelbox.get(), vocodermodelbox.get()
    generatebutton.config(state="disabled")
    exportbutton.config(state="disabled")
    _state.update(status="Starting...", done=0, total=0, finished=False, error=None)
    threading.Thread(target=_worker, args=(inputbox.get("1.0", "end-1c"), cudacheckbutton.instate(['selected']), outfile, play), daemon=True).start()
    _poll()

def generate():
    if inputbox.get("1.0", "end-1c") == "":
        messagebox.showerror(message="TTS will give a division by zero error if the text field is blank.")
        return
    os.makedirs('coqui-tts-output', exist_ok=True)
    _start("coqui-tts-output/generated.wav", True)

def savetext():
    f = filedialog.asksaveasfile(mode='w', defaultextension=".txt", filetypes=[("Text files", ".txt")])
    if f is None: # asksaveasfile return `None` if dialog closed with "cancel".
        return
    text2save = str(inputbox.get("1.0", "end-1c")) # starts from `1.0`, not `0.0`
    f.write(text2save)
    f.close() # `()` was missing.
    inputbox.edit_modified(False)

def savetextandopen():
    f = filedialog.asksaveasfile(mode='w', defaultextension=".txt", filetypes=[("Text files", ".txt")])
    if f is None: # asksaveasfile return `None` if dialog closed with "cancel".
        return
    text2save = str(inputbox.get("1.0", "end-1c")) # starts from `1.0`, not `0.0`
    f.write(text2save)
    f.close() # `()` was missing.
    inputbox.edit_modified(False)
    opentext()

def savetextandclose():
    f = filedialog.asksaveasfile(mode='w', defaultextension=".txt", filetypes=[("Text files", ".txt")])
    if f is None: # asksaveasfile return `None` if dialog closed with "cancel".
        return
    text2save = str(inputbox.get("1.0", "end-1c")) # starts from `1.0`, not `0.0`
    f.write(text2save)
    f.close() # `()` was missing.
    inputbox.edit_modified(False)
    window.destroy()

def exportaudio():
    if inputbox.get("1.0", "end-1c") == "":
        messagebox.showerror(message="TTS will give a division by zero error if the text field is blank.")
        return
    f = filedialog.asksaveasfile(mode='a', defaultextension=".wav", filetypes=[("Wave files", ".wav")])
    if f is None: # asksaveasfile return `None` if dialog closed with "cancel".
        return
    name = f.name
    f.close()
    _start(name, False)

def opentext():
    file = filedialog.askopenfile(mode='rt', defaultextension=".txt", filetypes=[("Text files", ".txt")])
    if file is None: # asksaveasfile return `None` if dialog closed with "cancel".
        return
    inputbox.delete("1.0", "end")
    contents = file.read()
    inputbox.insert('1.0', contents)
    inputbox.edit_modified(False)
    file.close()

def checkopentext():
    if inputbox.edit_modified():
        response = messagebox.askyesnocancel(message="You have unsaved changes. Do you want to save?")
        if response:
            savetextandopen()
        elif response is None:
            return
        elif not response:
            opentext()
    else:
        opentext()

def on_closing():
    if inputbox.edit_modified():
        response = messagebox.askyesnocancel(message="You have unsaved changes. Do you want to save?")
        if response:
            savetextandclose()
        elif response is None:
            return
        elif not response:
            window.destroy()
    else:
        window.destroy()

# Creating tkinter window
window = tk.Tk()
window.geometry('')
window.title("Coqui TTS GUI")
window.resizable(False, False)
window.protocol("WM_DELETE_WINDOW", on_closing)

# Label
ttk.Label(window, text="Enter text here", font=("Tahoma", 10)).grid(column=0, columnspan=3, row=12, padx=10, pady=12)

# Text
inputbox = scrolledtext.ScrolledText(window, height=15, width=70, undo=True)
inputbox.grid(column=0, columnspan=3, row=13, padx=10, pady=12)

# Label
ttk.Label(window, text="Select the tts_model", font=("Tahoma", 10)).grid(column=0, row=14, padx=10, pady=12)
n = tk.StringVar()
ttsmodelbox = ttk.Combobox(window, width=32, textvariable=n, state="readonly")

tts_models_name_list = []
vocoder_models_name_list = []
moddict = ModelManager().models_dict
for lang in moddict["tts_models"]:
    for dataset in moddict["tts_models"][lang]:
        for model in moddict["tts_models"][lang][dataset]:
            model_full_name = f"tts_models--{lang}--{dataset}--{model}"
            output_path = os.path.join(ModelManager().output_prefix, model_full_name)
            tts_models_name_list.append(f'{lang}/{dataset}/{model}')

for lang in moddict["vocoder_models"]:
    for dataset in moddict["vocoder_models"][lang]:
        for model in moddict["vocoder_models"][lang][dataset]:
            model_full_name = f"vocoder_models--{lang}--{dataset}--{model}"
            output_path = os.path.join(ModelManager().output_prefix, model_full_name)
            vocoder_models_name_list.append(f'{lang}/{dataset}/{model}')

# Adding combobox drop down list
ttsmodelbox['values'] = (tts_models_name_list)
ttsmodelbox.grid(column=0, row=15, padx=10, pady=12)
ttsmodelbox.current(0)

# Label
ttk.Label(window, text="Select the vocoder_model", font=("Tahoma", 10)).grid(column=2, row=14, padx=10, pady=12)
r = tk.StringVar()
vocodermodelbox = ttk.Combobox(window, width=32, textvariable=r, state="readonly")

# Checkbutton
# Label
ttk.Label(window, text="Use CUDA (Nvidia GPUs only)", font=("Tahoma", 10)).grid(column=1, columnspan=1, row=14, padx=10, pady=12)
cudacheckbutton = ttk.Checkbutton(window)
cudacheckbutton.grid(column=1, columnspan=1, row=15, padx=10, pady=12)
cudacheckbutton.state(['!alternate'])
cudacheckbutton.state(['!selected'])

# Adding combobox drop down list
vocodermodelbox['values'] = (vocoder_models_name_list)
vocodermodelbox.grid(column=2, row=15, padx=10, pady=12)
vocodermodelbox.current(0)

# Button
opentextbutton = ttk.Button(window, width=20, text="Open text", command=checkopentext)
opentextbutton.grid(column=0, columnspan=1, row=16, padx=10, pady=12)
generatebutton = ttk.Button(window, width=20, text="Generate", command=generate)
generatebutton.grid(column=1, columnspan=1, row=17, padx=10, pady=12)
savetextbutton = ttk.Button(window, width=20, text="Save text", command=savetext)
savetextbutton.grid(column=1, columnspan=1, row=16, padx=10, pady=12)
exportbutton = ttk.Button(window, width=20, text="Export audio", command=exportaudio)
exportbutton.grid(column=2, columnspan=1, row=16, padx=10, pady=12)
# Status + progress bar
statuslabel = ttk.Label(window, text="", font=("Tahoma", 10))
statuslabel.grid(column=0, columnspan=3, row=18, padx=10, pady=(0, 4))
progressbar = ttk.Progressbar(window, length=500, mode="determinate")
progressbar.grid(column=0, columnspan=3, row=19, padx=10, pady=(0, 12))

def loadgui():
    window.mainloop()

if __name__ == "__main__":
    loadgui()
