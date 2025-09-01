# ollama_voice_gui.py
# Minimal Tkinter GUI wrapper for your existing `ollama_voice.py`.

import io
import os
import sys
import json
import queue
import threading
import time
import inspect
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from typing import Any, TextIO, cast
from contextlib import suppress
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# --------------------- import your core module ---------------------
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import ollama_voice as core  # the core module you posted
    core = cast(Any, core)       # allow dynamic attrs w/ type checkers
except Exception as err:
    raise SystemExit(
        "Could not import ollama_voice.py. Make sure this GUI file is next to it.\n" + str(err)
    )

APP_NAME = "Ollama Voice"
CONF_DIR = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "OllamaVoice"
CONF_PATH = CONF_DIR / "config.json"

DEFAULTS = {
    "api_key": "",
    "app_secret": "",
    "callback_url": "https://127.0.0.1:8182",
    "token_path": str(Path.home() / "tokens.json"),
    "symbols": "SPY NVDA QQQ XLF",
    "focus": "SPY",
    "interval": 20,
    "history": 180,
    "window": 5,
    "min_print": 0.001,
    "always_print": False,
    "vwap_buf": 0.10,
    "gate_consecutive": 2,
    "print_gates": True,
    "emit_json": False,
    "csv_path": "",
    "ollama_model": os.getenv("OLLAMA_MODEL", "llama3"),
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_timeout": 45,
    "qa_max_words": 120,
    "question": "What is the current bias?",
}

# --------------------- util: stream redirection ---------------------
class StreamToQueue(io.TextIOBase):
    """File-like text stream that forwards writes to a Queue[str]."""
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q

    def writable(self) -> bool:  # type: ignore[override]
        return True

    def readable(self) -> bool:  # type: ignore[override]
        return False

    def seekable(self) -> bool:  # type: ignore[override]
        return False

    def write(self, s: str) -> int:  # type: ignore[override]
        if s:
            self.q.put(s)
        return len(s)

    def flush(self) -> None:  # type: ignore[override]
        pass

# --------------------- Spinbox helper (ttk or tk) ---------------------
def make_spinbox(parent, var, from_, to):
    # ttk.Spinbox exists in 3.11+, but not always earlier
    sb_cls = getattr(ttk, "Spinbox", None)
    if sb_cls is None:
        return tk.Spinbox(parent, from_=from_, to=to, textvariable=var)
    return sb_cls(parent, from_=from_, to=to, textvariable=var)

# --------------------- workers ---------------------
class LoopWorker(threading.Thread):
    def __init__(self, out_q: "queue.Queue[str]", params: dict):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.params = params
        self._stop_evt = threading.Event()
        self.loop: Any | None = None

    def stop(self) -> None:
        self._stop_evt.set()
        loop = self.loop
        if loop is None:
            return
        # TickerLoop.stop() should be idempotent; guard for attr/state races only.
        with suppress(AttributeError, RuntimeError):
            loop.stop()

    def run(self):
        # runtime overrides (avoid editing source)
        if self.params.get("api_key"):
            core.API_KEY = self.params["api_key"]
        if self.params.get("app_secret"):
            core.APP_SECRET = self.params["app_secret"]
        if self.params.get("callback_url"):
            core.CALLBACK_URL = self.params["callback_url"]
        if self.params.get("token_path"):
            core.TOKEN_PATH = self.params["token_path"]

        try:
            provider = core.SchwabQuoteProvider()

            # Candidate kwargs -> filter to TickerLoop.__init__
            maybe_kwargs = {
                "symbols": self.params.get("symbols", "SPY NVDA QQQ XLF").split(),
                "interval": int(self.params.get("interval", 20)),
                "focus": self.params.get("focus", "SPY"),
                "ollama_model": self.params.get("ollama_model", "llama3"),
                "ollama_url": self.params.get("ollama_url", "http://127.0.0.1:11434"),
                "ollama_timeout": int(self.params.get("ollama_timeout", 45)),
                "emit_json": bool(self.params.get("emit_json", False)),
                "csv_path": self.params.get("csv_path") or None,
                "once": False,
                "verbose": True,
                "vwap_buf": float(self.params.get("vwap_buf", 0.10)),
                "gate_consecutive": int(self.params.get("gate_consecutive", 2)),
                "print_gates": bool(self.params.get("print_gates", True)),
            }
            init_sig = inspect.signature(core.TickerLoop.__init__)
            allowed = {k: v for k, v in maybe_kwargs.items() if k in init_sig.parameters}

            self.loop = core.TickerLoop(provider, **allowed)

            # extra tuning if attrs exist
            loop_obj = self.loop
            if loop_obj is not None:
                try:
                    setattr(loop_obj, "window_points", int(self.params.get("window", 5)))
                except Exception:
                    pass
                try:
                    setattr(loop_obj, "min_print", float(self.params.get("min_print", 0.001)))
                except Exception:
                    pass
                try:
                    setattr(loop_obj, "always_print", bool(self.params.get("always_print", False)))
                except Exception:
                    pass
                brain = getattr(loop_obj, "brain", None)
                if brain is not None:
                    try:
                        setattr(brain, "_max_trail", int(self.params.get("history", 180)))
                    except Exception:
                        pass

            # Redirect stdout/stderr -> GUI
            stream_io: TextIO = cast(TextIO, StreamToQueue(self.out_q))
            with redirect_stdout(stream_io), redirect_stderr(stream_io):
                assert self.loop is not None
                self.loop.run()

        except Exception as err:
            self.out_q.put(f"\n[GUI] Loop stopped: {type(err).__name__}: {err}\n")

class AskWorker(threading.Thread):
    def __init__(self, out_q: "queue.Queue[str]", params: dict):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.params = params

    def run(self):
        if self.params.get("api_key"):
            core.API_KEY = self.params["api_key"]
        if self.params.get("app_secret"):
            core.APP_SECRET = self.params["app_secret"]
        if self.params.get("callback_url"):
            core.CALLBACK_URL = self.params["callback_url"]
        if self.params.get("token_path"):
            core.TOKEN_PATH = self.params["token_path"]

        try:
            provider = core.SchwabQuoteProvider()
            stream_io: TextIO = cast(TextIO, StreamToQueue(self.out_q))
            with redirect_stdout(stream_io), redirect_stderr(stream_io):
                core.qa_once(
                    provider,
                    user_symbols=self.params.get("symbols", "SPY NVDA QQQ XLF").split(),
                    focus=self.params.get("focus", "SPY"),
                    model=self.params.get("ollama_model", "llama3"),
                    url=self.params.get("ollama_url", "http://127.0.0.1:11434"),
                    timeout=int(self.params.get("ollama_timeout", 45)),
                    max_words=int(self.params.get("qa_max_words", 120)),
                    history=int(self.params.get("history", 180)),
                    question=self.params.get("question", "What is the current bias?"),
                )
        except Exception as err:
            self.out_q.put(f"\n[GUI] Ask failed: {type(err).__name__}: {err}\n")

# --------------------- the GUI ---------------------
# --------------------- the GUI ---------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x720")
        self.minsize(980, 640)

        self.out_q: "queue.Queue[str]" = queue.Queue()
        self.loop_worker: LoopWorker | None = None

        self._build_widgets()
        self._load_settings()
        # stubs expect a tuple for the args param
        self.after(60, self._pump_output, ())

    def _set_ready(self, _args: tuple | None = None) -> None:
        self.status.configure(text="Ready")

    def _toast(self, msg: str) -> None:
        self.status.configure(text=msg)
        # also pass an explicit empty tuple here
        self.after(2500, self._set_ready, ())



    # ----- settings I/O -----
    def _load_settings(self):
        data = DEFAULTS.copy()
        try:
            if CONF_PATH.exists():
                with open(CONF_PATH, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                    data.update(saved)
        except Exception as err:
            messagebox.showwarning("Settings", f"Could not load settings: {err}")
        self._apply_to_form(data)

    def _save_settings(self):
        data = self._read_from_form()
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONF_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._toast("Settings saved")
        except Exception as err:
            messagebox.showerror("Settings", f"Could not save: {err}")

    # ----- UI construction -----
    def _build_widgets(self):
        root = self

        # top toolbar
        bar = ttk.Frame(root)
        bar.pack(side="top", fill="x", padx=8, pady=6)

        ttk.Button(bar, text="Start", command=self._on_start).pack(side="left")
        ttk.Button(bar, text="Stop", command=self._on_stop).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="Ask", command=self._on_ask).pack(side="left")

        self.q_entry = ttk.Entry(bar, width=60)
        self.q_entry.insert(0, DEFAULTS["question"])
        self.q_entry.pack(side="left", padx=6)

        ttk.Button(bar, text="Save Settings", command=self._save_settings).pack(side="right")

        # split pane
        paned = ttk.PanedWindow(root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # left: settings
        left = ttk.Frame(paned)
        paned.add(left, weight=0)

        form = ttk.Frame(left)
        form.pack(fill="both", expand=True, padx=8, pady=8)

        def add_row(r, label, widget):
            ttk.Label(form, text=label).grid(row=r, column=0, sticky=tk.W, padx=(0, 8), pady=3)
            widget.grid(row=r, column=1, sticky=tk.EW, pady=3)

        form.columnconfigure(1, weight=1)

        self.var_api_key = tk.StringVar()
        self.var_app_secret = tk.StringVar()
        self.var_callback = tk.StringVar()
        self.var_token = tk.StringVar()
        self.var_symbols = tk.StringVar()
        self.var_focus = tk.StringVar()
        self.var_interval = tk.IntVar()
        self.var_history = tk.IntVar()
        self.var_window = tk.IntVar()
        self.var_min_print = tk.DoubleVar()
        self.var_always_print = tk.BooleanVar()
        self.var_vwap_buf = tk.DoubleVar()
        self.var_gate_cons = tk.IntVar()
        self.var_print_gates = tk.BooleanVar()
        self.var_emit_json = tk.BooleanVar()
        self.var_csv = tk.StringVar()
        self.var_model = tk.StringVar()
        self.var_ollama_url = tk.StringVar()
        self.var_ollama_timeout = tk.IntVar()
        self.var_qa_words = tk.IntVar()

        add_row(0, "API Key", ttk.Entry(form, textvariable=self.var_api_key))
        add_row(1, "App Secret", ttk.Entry(form, textvariable=self.var_app_secret, show="•"))
        add_row(2, "Callback URL", ttk.Entry(form, textvariable=self.var_callback))

        token_row = ttk.Frame(form)
        ttk.Entry(token_row, textvariable=self.var_token).pack(side="left", fill="x", expand=True)
        ttk.Button(token_row, text="Browse", command=self._pick_token).pack(side="left", padx=4)
        add_row(3, "Token Path", token_row)

        add_row(4, "Symbols", ttk.Entry(form, textvariable=self.var_symbols))
        add_row(5, "Focus", ttk.Entry(form, textvariable=self.var_focus))
        add_row(6, "Interval (s)", make_spinbox(form, self.var_interval, 5, 300))
        add_row(7, "History pts", make_spinbox(form, self.var_history, 60, 10000))
        add_row(8, "Delta window", make_spinbox(form, self.var_window, 1, 50))
        add_row(9, "Min print Δ", ttk.Entry(form, textvariable=self.var_min_print))
        add_row(10, "VWAP buffer", ttk.Entry(form, textvariable=self.var_vwap_buf))
        add_row(11, "Gate consecutive", make_spinbox(form, self.var_gate_cons, 1, 10))

        flags = ttk.Frame(form)
        ttk.Checkbutton(flags, text="Always print", variable=self.var_always_print).pack(side="left")
        ttk.Checkbutton(flags, text="Print gates", variable=self.var_print_gates).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(flags, text="Emit JSON", variable=self.var_emit_json).pack(side="left", padx=(12, 0))
        add_row(12, "Flags", flags)

        csv_row = ttk.Frame(form)
        ttk.Entry(csv_row, textvariable=self.var_csv).pack(side="left", fill="x", expand=True)
        ttk.Button(csv_row, text="Browse", command=self._pick_csv).pack(side="left", padx=4)
        add_row(13, "CSV path", csv_row)

        add_row(14, "Ollama model", ttk.Entry(form, textvariable=self.var_model))
        add_row(15, "Ollama URL", ttk.Entry(form, textvariable=self.var_ollama_url))
        add_row(16, "Ollama timeout", make_spinbox(form, self.var_ollama_timeout, 5, 120))
        add_row(17, "Q&A max words", make_spinbox(form, self.var_qa_words, 30, 400))

        for child in form.winfo_children():
            if isinstance(child, ttk.Entry):
                child.configure(width=38)

        # right: console
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        self.console = tk.Text(right, wrap="word", state="normal", height=30)
        self.console.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        self.console.tag_configure("mono", font=("Consolas", 10))

        status_bar = ttk.Frame(right)
        status_bar.pack(fill="x", padx=8, pady=6)

        self.status = ttk.Label(status_bar, text="Ready")
        self.status.pack(side="left")

    # ----- helpers -----
    def _apply_to_form(self, d: dict):
        self.var_api_key.set(d.get("api_key", DEFAULTS["api_key"]))
        self.var_app_secret.set(d.get("app_secret", DEFAULTS["app_secret"]))
        self.var_callback.set(d.get("callback_url", DEFAULTS["callback_url"]))
        self.var_token.set(d.get("token_path", DEFAULTS["token_path"]))
        self.var_symbols.set(d.get("symbols", DEFAULTS["symbols"]))
        self.var_focus.set(d.get("focus", DEFAULTS["focus"]))
        self.var_interval.set(int(d.get("interval", DEFAULTS["interval"])))
        self.var_history.set(int(d.get("history", DEFAULTS["history"])))
        self.var_window.set(int(d.get("window", DEFAULTS["window"])))
        self.var_min_print.set(float(d.get("min_print", DEFAULTS["min_print"])))
        self.var_vwap_buf.set(float(d.get("vwap_buf", DEFAULTS["vwap_buf"])))
        self.var_gate_cons.set(int(d.get("gate_consecutive", DEFAULTS["gate_consecutive"])))
        self.var_always_print.set(bool(d.get("always_print", DEFAULTS["always_print"])))
        self.var_print_gates.set(bool(d.get("print_gates", DEFAULTS["print_gates"])))
        self.var_emit_json.set(bool(d.get("emit_json", DEFAULTS["emit_json"])))
        self.var_csv.set(d.get("csv_path", DEFAULTS["csv_path"]))
        self.var_model.set(d.get("ollama_model", DEFAULTS["ollama_model"]))
        self.var_ollama_url.set(d.get("ollama_url", DEFAULTS["ollama_url"]))
        self.var_ollama_timeout.set(int(d.get("ollama_timeout", DEFAULTS["ollama_timeout"])))
        self.var_qa_words.set(int(d.get("qa_max_words", DEFAULTS["qa_max_words"])))
        if hasattr(self, "q_entry"):
            self.q_entry.delete(0, tk.END)
            self.q_entry.insert(0, d.get("question", DEFAULTS["question"]))

    def _read_from_form(self) -> dict:
        return {
            "api_key": self.var_api_key.get().strip(),
            "app_secret": self.var_app_secret.get().strip(),
            "callback_url": self.var_callback.get().strip(),
            "token_path": self.var_token.get().strip(),
            "symbols": self.var_symbols.get().strip(),
            "focus": self.var_focus.get().strip() or "SPY",
            "interval": int(self.var_interval.get()),
            "history": int(self.var_history.get()),
            "window": int(self.var_window.get()),
            "min_print": float(self.var_min_print.get()),
            "always_print": bool(self.var_always_print.get()),
            "vwap_buf": float(self.var_vwap_buf.get()),
            "gate_consecutive": int(self.var_gate_cons.get()),
            "print_gates": bool(self.var_print_gates.get()),
            "emit_json": bool(self.var_emit_json.get()),
            "csv_path": self.var_csv.get().strip(),
            "ollama_model": self.var_model.get().strip(),
            "ollama_url": self.var_ollama_url.get().strip(),
            "ollama_timeout": int(self.var_ollama_timeout.get()),
            "qa_max_words": int(self.var_qa_words.get()),
            "question": self.q_entry.get().strip() if hasattr(self, "q_entry") else DEFAULTS["question"],
        }

    def _pick_token(self):
        path = filedialog.askopenfilename(
            title="Choose Schwab token.json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if path:
            self.var_token.set(path)

    def _pick_csv(self):
        path = filedialog.asksaveasfilename(
            title="Choose CSV output",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"), ("All files", "*.*")),
        )
        if path:
            self.var_csv.set(path)


    # ----- start/stop/ask -----
    def _on_start(self):
        if self.loop_worker and self.loop_worker.is_alive():
            messagebox.showinfo("Running", "Loop already running.")
            return
        params = self._read_from_form()
        # drain any queued output
        try:
            while True:
                self.out_q.get_nowait()
        except queue.Empty:
            pass
        self.console.delete("1.0", tk.END)
        self.loop_worker = LoopWorker(self.out_q, params)
        self.loop_worker.start()
        self._toast("Loop started")

    def _on_stop(self):
        if self.loop_worker:
            self.loop_worker.stop()
            # give run() a moment to exit its wait cycle
            for _ in range(30):
                if not self.loop_worker.is_alive():
                    break
                time.sleep(0.1)
        self._toast("Loop stopped")

    def _on_ask(self):
        if self.loop_worker and self.loop_worker.is_alive():
            if not messagebox.askyesno("Ask while running?", "The live loop is running. Run a one-shot Q&A anyway?"):
                return
        params = self._read_from_form()
        worker = AskWorker(self.out_q, params)
        worker.start()
        self._toast("Asked")

    # ----- output pump -----
    def _pump_output(self, _args: tuple | None = None) -> None:
        try:
            while True:
                chunk = self.out_q.get_nowait()
                self.console.insert("end", chunk, ("mono",))
                self.console.see("end")
        except queue.Empty:
            pass
        self.after(60, self._pump_output, ())

    def destroy(self):
        try:
            self._save_settings()
        except Exception:
            pass
        if self.loop_worker and self.loop_worker.is_alive():
            self.loop_worker.stop()
        super().destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
