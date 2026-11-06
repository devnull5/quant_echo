# final_llama.py — Dark dashboard UI with NEWS panel (quotes grid removed)
from __future__ import annotations

import io
import os
import re
import sys
import json
import time
import queue
import inspect
import threading
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr, suppress
from typing import Any, TextIO, cast, Dict, List
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter import simpledialog


RSI_RE  = re.compile(r"\bRSI(?:\s*\(\s*\d+\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)
MACD_RE = re.compile(r"\bMACD(?:\s*\(\s*\d+\\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)
VWAP_RE = re.compile(r"\bVWAP(?:\s*\(\s*\d+\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)
def _coerce_int(x, default: int = 0) -> int:
    """Best-effort int; returns default on bad/NaN."""
    if isinstance(x, bool):
        return default
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x) if math.isfinite(x) else default
    if isinstance(x, str):
        s = x.strip()
        try:
            v = float(s)
            return int(v) if math.isfinite(v) else default
        except ValueError:
            return default
    return default

def _coerce_float(x) -> float | None:
    """Best-effort float; returns None on bad/NaN."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, str):
        s = x.strip()
        try:
            v = float(s)
            return v if math.isfinite(v) else None
        except ValueError:
            return None
    return None
# --------------------- import your core module ---------------------
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import quantechov01_core as core  # your backend module
    core = cast(Any, core)
except Exception as err:
    raise SystemExit(
        "Could not import llama_drama.py. Make sure this GUI file is next to it.\n" + str(err)
    )

APP_NAME = "QuantEcho"
CONF_DIR = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "OllamaVoice"
CONF_PATH = CONF_DIR / "config.json"

DEFAULTS = {
    "api_key": "",
    "app_secret": "",
    "callback_url": "https://127.0.0.1:8182",
    "token_path": str(Path.home() / "tokens.json"),
    "symbols": "SPY NVDA QQQ XLF IEF/TLT",
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
    "ollama_model": os.getenv("OLLAMA_MODEL", "gpt-oss:20b"),
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_timeout": 45,
    "qa_max_words": 120,
    "question": "Current market trend?",

    # --- NEWS options (GUI + backend passthrough) ---
    "news": bool(os.getenv("NEWSAPI_KEY", "")),  # enable by default when a key exists
    "news_since_h": 6,      # lookback window (hours)
    "news_limit": 12,       # max items
    "news_weight": 0.30,    # bias nudge weight (0..1)
    "print_news": False,    # append a one-liner to prints
    "news_api_key": os.getenv("NEWS_API_KEY", ""),  # optional
}

# --------------------- stream bridge ---------------------
class StreamToQueue(io.TextIOBase):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
    def writable(self) -> bool:  # type: ignore[override]
        return True
    def write(self, s: str) -> int:  # type: ignore[override]
        if s:
            self.q.put(s)
        return len(s)
    def flush(self) -> None:  # type: ignore[override]
        pass

# --------------------- background workers ---------------------
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
        with suppress(AttributeError, RuntimeError):
            loop.stop()

    def run(self):
        # runtime overrides (avoid editing source)
        for k in ("api_key", "app_secret", "callback_url", "token_path"):
            if self.params.get(k):
                setattr(core, k.upper() if k != "token_path" else "TOKEN_PATH", self.params[k])

        # --- NEWS API key passthrough (backend expects NEWSAPI_KEY) ---
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]  # let news_client read it
            setattr(core, "NEWSAPI_KEY", self.params["news_api_key"])  # just in case core reads module var too

        try:
            provider = core.SchwabQuoteProvider()

            maybe_kwargs = {
                "symbols": self.params.get("symbols", DEFAULTS["symbols"]).split(),
                "interval": int(self.params.get("interval", 20)),
                "focus": self.params.get("focus", "SPY"),
                "ollama_model": self.params.get("ollama_model", DEFAULTS["ollama_model"]),
                "ollama_url": self.params.get("ollama_url", DEFAULTS["ollama_url"]),
                "ollama_timeout": int(self.params.get("ollama_timeout", 45)),
                "emit_json": bool(self.params.get("emit_json", False)),
                "csv_path": self.params.get("csv_path") or None,
                "once": False,
                "verbose": False,
                "vwap_buf": float(self.params.get("vwap_buf", 0.10)),
                "gate_consecutive": int(self.params.get("gate_consecutive", 2)),
                "print_gates": bool(self.params.get("print_gates", True)),

                # NEWS knobs (used if your core supports them)
                "news_on": bool(self.params.get("news", False)),
                "news_since_h": int(self.params.get("news_since_h", 6)),
                "news_limit": int(self.params.get("news_limit", 12)),
                "news_weight": float(self.params.get("news_weight", 0.30)),
                "print_news": bool(self.params.get("print_news", False)),
            }
            init_sig = inspect.signature(core.TickerLoop.__init__)
            allowed = {k: v for k, v in maybe_kwargs.items() if k in init_sig.parameters}

            self.loop = core.TickerLoop(provider, **allowed)

            loop_obj = self.loop
            if loop_obj is not None:
                if hasattr(loop_obj, "window_points"):
                    loop_obj.window_points = int(self.params.get("window", 5))
                if hasattr(loop_obj, "min_print"):
                    loop_obj.min_print = float(self.params.get("min_print", 0.001))
                if hasattr(loop_obj, "always_print"):
                    loop_obj.always_print = bool(self.params.get("always_print", False))
                brain = getattr(loop_obj, "brain", None)
                if brain is not None and hasattr(brain, "_max_trail"):
                    brain._max_trail = int(self.params.get("history", 180))

            stream_io: TextIO = cast(TextIO, StreamToQueue(self.out_q))
            with redirect_stdout(stream_io), redirect_stderr(stream_io):
                assert self.loop is not None
                self.loop.run()
        except Exception as exc:
            self.out_q.put(f"\n[GUI] Loop stopped: {type(exc).__name__}: {exc}\n")


class AskWorker(threading.Thread):
    def __init__(self, out_q: "queue.Queue[str]", params: dict):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.params = params
    def run(self):
        for k in ("api_key", "app_secret", "callback_url", "token_path"):
            if self.params.get(k):
                setattr(core, k.upper() if k != "token_path" else "TOKEN_PATH", self.params[k])
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
            setattr(core, "NEWSAPI_KEY", self.params["news_api_key"])

        try:
            provider = core.SchwabQuoteProvider()
            stream_io: TextIO = cast(TextIO, StreamToQueue(self.out_q))
            with redirect_stdout(stream_io), redirect_stderr(stream_io):
                core.qa_once(
                    provider,
                    user_symbols=self.params.get("symbols", DEFAULTS["symbols"]).split(),
                    focus=self.params.get("focus", "SPY"),
                    model=self.params.get("ollama_model", DEFAULTS["ollama_model"]),
                    url=self.params.get("ollama_url", DEFAULTS["ollama_url"]),
                    timeout=int(self.params.get("ollama_timeout", 45)),
                    max_words=int(self.params.get("qa_max_words", 120)),
                    history=int(self.params.get("history", 180)),
                    question=self.params.get("question", DEFAULTS["question"]),
                )
        except Exception as exc:
            self.out_q.put(f"\n[GUI] Ask failed: {type(exc).__name__}: {exc}\n")

# --------------------- THEME ---------------------
COLORS = {
    "bg": "#0f1216",
    "panel": "#141922",
    "panel2": "#121720",
    "sunken": "#0c1016",
    "border": "#1e2630",
    "text": "#dfe5ee",
    "muted": "#9aa9bf",
    "teal": "#20c997",
    "green": "#18d18b",
    "blue": "#4ea1ff",
    "amber": "#f7b955",
    "red": "#ff6363",
}

FONT_MONO = ("Consolas", 10)
FONT_UI = ("Segoe UI", 10)
FONT_HUGE = ("Segoe UI", 24, "bold")
FONT_BIG = ("Segoe UI", 15, "bold")
FONT_CAP = ("Segoe UI", 10, "bold")

def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=COLORS["bg"])  # base background

    # General
    style.configure("TFrame", background=COLORS["panel2"])
    style.configure("TPanedwindow", background=COLORS["bg"], borderwidth=0)
    style.configure("TLabel", background=COLORS["panel2"], foreground=COLORS["text"], font=FONT_UI)
    style.configure("TButton", background=COLORS["panel"], foreground=COLORS["text"], padding=6)
    style.map("TButton", background=[("active", COLORS["sunken"])])
    style.configure("TEntry", fieldbackground=COLORS["sunken"], background=COLORS["sunken"],
                    foreground=COLORS["text"], insertcolor=COLORS["text"], bordercolor=COLORS["border"])
    style.configure("Card.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("Toolbar.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("Title.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI Semibold", 11))
    style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=FONT_UI)

    # Treeview style (reused by NEWS list)
    style.configure(
        "Quotes.Treeview",
        background=COLORS["sunken"],
        fieldbackground=COLORS["sunken"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        rowheight=22,
        font=("Consolas", 10),
    )
    style.configure("Quotes.Treeview.Heading", background=COLORS["panel"], foreground=COLORS["muted"], font=("Segoe UI", 10, "bold"))
    return style

# --------------------- UI ---------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(1080, 680)
        self.style = apply_dark_theme(self)

        # state/config
        self.out_q: "queue.Queue[str]" = queue.Queue()
        self.loop_worker: LoopWorker | None = None
        self.params = self._load_settings()
        self._awaiting_answer: bool = False

        # build
        self._build_toolbar()
        self._build_body()

        # log pump
        self.after(60, self._pump_output, ())

    # ---------- Telemetry -> UI ----------
    def _apply_telemetry(self, obj: dict) -> None:
        # indicators
        ta = obj.get("ta") or {}
        vwap = obj.get("vwap") or {}
        focus = obj.get("focus") or self.params.get("focus", "SPY")

        r = ta.get("rsi14")
        if isinstance(r, (int, float)):
            cast(ttk.Label, self.card_rsi["value"]).configure(text=self._fmt2(r))
            state = "Overbought" if r >= 70 else "Oversold" if r <= 30 else "Neutral"
            color_key = "red" if r >= 70 else "green" if r <= 30 else "muted"
            cast(ttk.Label, self.card_rsi["status"]).configure(text=state, foreground=COLORS[color_key])

        m = ta.get("macd")
        if isinstance(m, (int, float)):
            cast(ttk.Label, self.card_macd["value"]).configure(text=("+" if m >= 0 else "") + self._fmt2(m))
            cast(ttk.Label, self.card_macd["status"]).configure(
                text=("Bullish" if m >= 0 else "Bearish"),
                foreground=COLORS["green" if m >= 0 else "red"],
            )

        vw_f = vwap.get(focus) if isinstance(vwap, dict) else None
        if isinstance(vw_f, (int, float)):
            cast(ttk.Label, self.card_vwap["value"]).configure(text=self._fmt2(vw_f))

        # prices -> ticker mini cards
        prices = obj.get("prices") or {}
        if isinstance(prices, dict):
            for sym, val in prices.items():
                try:
                    p = float(val)
                except (TypeError, ValueError):
                    continue
                if sym in self.ticker_cards:
                    cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

        # news section
        news = obj.get("news")
        if isinstance(news, dict):
            # status summary
            cnt = _coerce_int(news.get("count", 0), 0)
            bias_val = _coerce_float(news.get("bias"))

            if bias_val is not None:
                self.status.configure(text=f"News: {cnt} items, bias {bias_val:+.2f}")
                self.after(2500, self._set_ready, ())

            # items list
            items = news.get("items") or []
            if isinstance(items, list):
                self._update_news_items(items)

    def _append_chat(self, role: str, msg: str) -> None:
        stamp = time.strftime("[%H:%M:%S] ")
        self.prints_text.insert("end", stamp, ("time",))
        self.prints_text.insert("end", f"{role}> {msg.strip()}\n")
        self.prints_text.see("end")

    # ---------- SETTINGS ----------
    @staticmethod
    def _fmt2(x: float | int | str | None) -> str:
        if x is None:
            return "—"
        try:
            d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return "—"
        return f"{d:.2f}"

    @staticmethod
    def _load_settings() -> dict[str, Any]:
        data = DEFAULTS.copy()
        try:
            if CONF_PATH.exists():
                with open(CONF_PATH, "r", encoding="utf-8") as f:
                    data.update(json.load(f))
        except Exception as exc:
            messagebox.showwarning("Settings", f"Could not load settings: {exc}")
        return data

    def _save_settings(self) -> None:
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONF_PATH, "w", encoding="utf-8") as f:
                json.dump(self.params, f, indent=2)
            self._toast("Settings saved")
        except Exception as exc:
            messagebox.showerror("Settings", f"Could not save: {exc}")

    def _open_settings(self) -> None:
        # Modal with fields
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=COLORS["panel2"])
        win.transient(self)
        win.grab_set()

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        def add_row(r, label, widget):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            widget.grid(row=r, column=1, sticky=tk.EW, pady=4)

        # Tk variables
        self.var_api_key = tk.StringVar(value=self.params.get("api_key"))
        self.var_app_secret = tk.StringVar(value=self.params.get("app_secret"))
        self.var_callback = tk.StringVar(value=self.params.get("callback_url"))
        self.var_token = tk.StringVar(value=self.params.get("token_path"))
        self.var_symbols = tk.StringVar(value=self.params.get("symbols"))
        self.var_focus = tk.StringVar(value=self.params.get("focus"))
        self.var_interval = tk.IntVar(value=int(self.params.get("interval", 20)))
        self.var_history = tk.IntVar(value=int(self.params.get("history", 180)))
        self.var_window = tk.IntVar(value=int(self.params.get("window", 5)))
        self.var_min_print = tk.DoubleVar(value=float(self.params.get("min_print", 0.001)))
        self.var_always_print = tk.BooleanVar(value=bool(self.params.get("always_print", False)))
        self.var_vwap_buf = tk.DoubleVar(value=float(self.params.get("vwap_buf", 0.10)))
        self.var_gate_cons = tk.IntVar(value=int(self.params.get("gate_consecutive", 2)))
        self.var_print_gates = tk.BooleanVar(value=bool(self.params.get("print_gates", True)))
        self.var_emit_json = tk.BooleanVar(value=bool(self.params.get("emit_json", False)))
        self.var_csv = tk.StringVar(value=self.params.get("csv_path", ""))
        self.var_model = tk.StringVar(value=self.params.get("ollama_model", DEFAULTS["ollama_model"]))
        self.var_ollama_url = tk.StringVar(value=self.params.get("ollama_url", DEFAULTS["ollama_url"]))
        self.var_ollama_timeout = tk.IntVar(value=int(self.params.get("ollama_timeout", 45)))
        self.var_qa_words = tk.IntVar(value=int(self.params.get("qa_max_words", 120)))
        self.var_question = tk.StringVar(value=self.params.get("question", DEFAULTS["question"]))

        # NEWS vars
        self.var_news = tk.BooleanVar(value=bool(self.params.get("news", False)))
        self.var_news_since_h = tk.IntVar(value=int(self.params.get("news_since_h", 6)))
        self.var_news_limit = tk.IntVar(value=int(self.params.get("news_limit", 12)))
        self.var_news_weight = tk.DoubleVar(value=float(self.params.get("news_weight", 0.30)))
        self.var_print_news = tk.BooleanVar(value=bool(self.params.get("print_news", False)))
        self.var_news_api_key = tk.StringVar(value=self.params.get("news_api_key", ""))

        # entries
        add_row(0, "API Key", ttk.Entry(frm, textvariable=self.var_api_key))
        add_row(1, "App Secret", ttk.Entry(frm, textvariable=self.var_app_secret, show="•"))
        add_row(2, "Callback URL", ttk.Entry(frm, textvariable=self.var_callback))

        tok_row = ttk.Frame(frm)
        ttk.Entry(tok_row, textvariable=self.var_token).pack(side="left", fill="x", expand=True)
        ttk.Button(tok_row, text="Browse", command=lambda: self._pick_path(self.var_token, save=False)).pack(side="left", padx=6)
        add_row(3, "Token Path", tok_row)

        add_row(4, "Symbols", ttk.Entry(frm, textvariable=self.var_symbols))
        add_row(5, "Focus", ttk.Entry(frm, textvariable=self.var_focus))

        def spin(var, a, b):
            sb_cls = getattr(ttk, "Spinbox", None) or tk.Spinbox
            return sb_cls(frm, from_=a, to=b, textvariable=var)

        add_row(6, "Interval (s)", spin(self.var_interval, 5, 300))
        add_row(7, "History pts", spin(self.var_history, 60, 10000))
        add_row(8, "Delta window", spin(self.var_window, 1, 50))
        add_row(9, "Min print Δ", ttk.Entry(frm, textvariable=self.var_min_print))
        add_row(10, "VWAP buffer", ttk.Entry(frm, textvariable=self.var_vwap_buf))
        add_row(11, "Gate consecutive", spin(self.var_gate_cons, 1, 10))

        flags = ttk.Frame(frm)
        ttk.Checkbutton(flags, text="Always print", variable=self.var_always_print).pack(side="left")
        ttk.Checkbutton(flags, text="Print gates", variable=self.var_print_gates).pack(side="left", padx=12)
        ttk.Checkbutton(flags, text="Emit JSON", variable=self.var_emit_json).pack(side="left", padx=12)
        add_row(12, "Flags", flags)

        csv_row = ttk.Frame(frm)
        ttk.Entry(csv_row, textvariable=self.var_csv).pack(side="left", fill="x", expand=True)
        ttk.Button(
            csv_row,
            text="Browse",
            command=lambda: self._pick_path(self.var_csv, save=True, default_extension=".csv")
        ).pack(side="left", padx=6)
        add_row(13, "CSV path", csv_row)

        add_row(14, "Ollama model", ttk.Entry(frm, textvariable=self.var_model))
        add_row(15, "Ollama URL", ttk.Entry(frm, textvariable=self.var_ollama_url))
        add_row(16, "Ollama timeout", spin(self.var_ollama_timeout, 5, 120))
        add_row(17, "Q&A max words", spin(self.var_qa_words, 30, 400))
        add_row(18, "Default question", ttk.Entry(frm, textvariable=self.var_question))

        # --- NEWS controls ---
        news_flags = ttk.Frame(frm)
        ttk.Checkbutton(news_flags, text="Enable news", variable=self.var_news).pack(side="left")
        ttk.Checkbutton(news_flags, text="Print news line", variable=self.var_print_news).pack(side="left", padx=12)
        add_row(19, "News", news_flags)

        def spin_news(var, a, b, step=1):
            sb_cls = getattr(ttk, "Spinbox", None) or tk.Spinbox
            return sb_cls(frm, from_=a, to=b, increment=step, textvariable=var, width=8)

        add_row(20, "News since (h)", spin_news(self.var_news_since_h, 1, 48))
        add_row(21, "News limit",     spin_news(self.var_news_limit,   1, 50))
        add_row(22, "News weight",    ttk.Entry(frm, textvariable=self.var_news_weight))
        add_row(23, "News API key",   ttk.Entry(frm, textvariable=self.var_news_api_key, show="•"))

        buttons = ttk.Frame(frm)
        buttons.grid(row=24, column=0, columnspan=2, sticky=tk.E, pady=(8, 2))
        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(
            buttons,
            text="Save",
            command=lambda: (self._sync_vars_to_params(), self._save_settings(), win.destroy())
        ).pack(side="right", padx=8)

    def _sync_vars_to_params(self) -> None:
        if not hasattr(self, "var_api_key"):
            return
        self.params.update({
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
            "question": self.var_question.get().strip() or DEFAULTS["question"],

            # news
            "news": bool(self.var_news.get()),
            "news_since_h": int(self.var_news_since_h.get()),
            "news_limit": int(self.var_news_limit.get()),
            "news_weight": float(self.var_news_weight.get()),
            "print_news": bool(self.var_print_news.get()),
            "news_api_key": self.var_news_api_key.get().strip(),
        })

    @staticmethod
    def _pick_path(var: tk.Variable, save: bool, default_extension: str = ".json") -> None:
        if save:
            path = filedialog.asksaveasfilename(title="Select file", defaultextension=default_extension)
        else:
            path = filedialog.askopenfilename(title="Select file")
        if path:
            var.set(path)

    # ---------- LAYOUT ----------
    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame")
        bar.pack(side="top", fill="x")

        left = ttk.Frame(bar, style="Toolbar.TFrame")
        left.pack(side="left", padx=10, pady=8)
        ttk.Button(left, text="Start", command=self._on_start).pack(side="left")
        ttk.Button(left, text="Stop", command=self._on_stop).pack(side="left", padx=(8, 0))

        right = ttk.Frame(bar, style="Toolbar.TFrame")
        right.pack(side="right", padx=10, pady=8)
        ttk.Button(right, text="Settings", command=self._open_settings).pack(side="right")
        ttk.Button(right, text="Save history…", command=self._on_export_history).pack(side="right", padx=(0, 8))

    def _build_body(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        paned = ttk.Panedwindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # LEFT column -----
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        self._section(left, "PRINTS")
        self.prints_text = tk.Text(
            left, height=18, wrap="word",
            bg=COLORS["sunken"], fg=COLORS["text"],
            relief="flat", insertbackground=COLORS["text"]
        )
        self.prints_text.configure(font=FONT_MONO)
        self.prints_text.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        self._init_text_tags(self.prints_text)

        # Ask row (bottom of left column)
        ask_row = ttk.Frame(left)
        ask_row.pack(fill="x", padx=12, pady=(6, 12))
        self.ask_entry = ttk.Entry(ask_row)
        self.ask_entry.insert(0, self.params.get("question", DEFAULTS["question"]))
        self.ask_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(ask_row, text="Ask", command=self._on_ask).pack(side="left", padx=8)
        self.ask_entry.bind("<Return>", lambda _e: self._on_ask())

        # RIGHT column -----
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        # Indicators row
        self._section(right, "INDICATORS")
        cards = ttk.Frame(right)
        cards.pack(fill="x", padx=12)

        self.card_rsi = self._indicator_card(cards, "RSI", "—", "Neutral", "amber")
        self.card_macd = self._indicator_card(cards, "MACD", "—", "—", "teal")
        self.card_vwap = self._indicator_card(cards, "VWAP", "—", "—", "blue")
        self.card_bb = self._indicator_card(cards, "BB 'B", "—", "—", "amber")

        # Tickers row
        row2 = ttk.Frame(right)
        row2.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(row2, text="TICKERS    ").pack(side="left")
        self.live_dot = self._dot(row2, COLORS["green"])
        ttk.Label(row2, text="LIVE", style="Muted.TLabel").pack(side="left", padx=(6, 0))

        self.ticker_bar = ttk.Frame(right)
        self.ticker_bar.pack(fill="x", padx=12, pady=6)
        self.ticker_cards: dict[str, dict[str, tk.Widget]] = {}
        for sym in self.params.get("symbols", DEFAULTS["symbols"]).split()[:5]:
            self._add_ticker_card(sym)

        # NEWS panel (replaces quotes grid)
        self._section(right, "NEWS")
        self.news_tree = ttk.Treeview(
            right,
            columns=("when", "src", "title", "sent"),
            show="headings",
            style="Quotes.Treeview",
        )
        self.news_tree.heading("when",  text="WHEN")
        self.news_tree.heading("src",   text="SRC")
        self.news_tree.heading("title", text="TITLE")
        self.news_tree.heading("sent",  text="SENT")

        self.news_tree.column("when",  width=110, anchor="w")
        self.news_tree.column("src",   width=100, anchor="w")
        self.news_tree.column("title", width=540, anchor="w")
        self.news_tree.column("sent",  width=70,  anchor="e")

        self.news_tree.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self._news_urls: Dict[str, str] = {}

        def _open_selected(_evt=None):
            sel = self.news_tree.selection()
            if not sel:
                return
            iid = sel[0]
            url = self._news_urls.get(iid)
            if url:
                webbrowser.open_new_tab(url)
        self.news_tree.bind("<Double-1>", _open_selected)

        # status bar
        status = ttk.Frame(self, style="Toolbar.TFrame")
        status.pack(fill="x")
        self.status = ttk.Label(status, text="Ready", style="Title.TLabel")
        self.status.pack(side="left", padx=12, pady=8)

        if self.params.get("news"):
            self.status.configure(text="News on — double-click a headline to open")
            self.after(2500, self._set_ready, ())

    @staticmethod
    def _section(parent: tk.Widget, title: str) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(bar, text=title, style="Section.TLabel").pack(side="left")

    @staticmethod
    def _indicator_card(parent: tk.Widget, title: str, value: str, status: str, accent_key: str) -> dict[str, tk.Widget]:
        card = ttk.Frame(parent, style="Card.TFrame")
        card.pack(side="left", padx=(0, 10), pady=0, ipadx=10, ipady=10)
        ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="w")
        val = ttk.Label(card, text=value, font=FONT_HUGE)
        val.pack(anchor="w")
        stat = ttk.Label(card, text=status, font=FONT_UI)
        stat.pack(anchor="w")
        c = tk.Canvas(card, width=120, height=2, highlightthickness=0, bg=COLORS["panel"])
        c.pack(fill="x", pady=(8, 0))
        c.create_rectangle(0, 0, 120, 2, fill=COLORS[accent_key], width=0)
        return {"frame": card, "value": val, "status": stat}

    def _add_ticker_card(self, sym: str):
        frame = ttk.Frame(self.ticker_bar, style="Card.TFrame")
        frame.pack(side="left", padx=(0,10), ipadx=10, ipady=8)
        top = ttk.Frame(frame, style="Card.TFrame")
        top.pack(fill="x")
        self._dot(top, COLORS["red"])  # per-card dot placeholder
        ttk.Label(top, text=f"  {sym}", font=FONT_BIG).pack(side="left")
        last = ttk.Label(frame, text="—", font=("Consolas", 14))
        last.pack(anchor="w")
        vol = ttk.Label(frame, text="• 0", style="Muted.TLabel")
        vol.pack(anchor="w")
        self.ticker_cards[sym] = {"frame": frame, "last": last, "vol": vol}

    @staticmethod
    def _dot(parent: tk.Widget, color: str) -> tk.Canvas:
        c = tk.Canvas(parent, width=10, height=10, bg=COLORS["panel"], highlightthickness=0)
        c.pack(side="left")
        c.create_oval(2, 2, 8, 8, fill=color, outline=color)
        return c

    @staticmethod
    def _init_text_tags(t: tk.Text) -> None:
        t.tag_configure("time", foreground=COLORS["blue"])  # [21:30:43]
        t.tag_configure("good", foreground=COLORS["green"])
        t.tag_configure("warn", foreground=COLORS["amber"])
        t.tag_configure("bad", foreground=COLORS["red"])
        t.tag_configure("muted", foreground=COLORS["muted"])

    def _set_ready(self, _args: tuple | None = None) -> None:
        self.status.configure(text="Ready")

    # ---------- UX helpers ----------
    def _toast(self, msg: str) -> None:
        self.status.configure(text=msg)
        self.after(2500, self._set_ready, ())

    # ---------- Start/Stop/Ask ----------
    def _on_start(self):
        if self.loop_worker and self.loop_worker.is_alive():
            messagebox.showinfo("Running", "Loop already running.")
            return
        self._sync_vars_to_params()  # in case settings were opened
        # drain queue & clear UI logs
        with suppress(queue.Empty):
            while True:
                self.out_q.get_nowait()
        self.prints_text.delete("1.0", tk.END)
        self.loop_worker = LoopWorker(self.out_q, self.params.copy())
        self.loop_worker.start()
        self._toast("Loop started")

    def _on_stop(self):
        if self.loop_worker:
            self.loop_worker.stop()
            for _ in range(40):  # give it a moment
                if not self.loop_worker.is_alive():
                    break
                time.sleep(0.1)
        self._toast("Loop stopped")

    def _on_ask(self):
        q = (self.ask_entry.get().strip()
             or self.params.get("question", DEFAULTS["question"]))
        self.params["question"] = q
        self.ask_entry.delete(0, tk.END)

        # Show the question in the chat panel right away
        self._append_chat("Q", q)
        self._awaiting_answer = True

        worker = AskWorker(self.out_q, self.params.copy())
        worker.start()
        self._toast("Asked")

    def _on_export_history(self):
        # Which symbol to export: use current Focus (fallback to SPY)
        sym = (self.params.get("focus") or "SPY").strip().upper()

        # Ask how many years (default 15)
        years = simpledialog.askinteger("Export history", "How many years of daily candles?",
                                        initialvalue=15, minvalue=1, maxvalue=30, parent=self)
        if not years:
            return

        # Pick destination CSV
        out_path = filedialog.asksaveasfilename(
            title=f"Save {sym} daily candles",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")]
        )
        if not out_path:
            return

        # Ensure Schwab client exists (reuse provider from your core)
        try:
            provider = core.SchwabQuoteProvider()
            client = getattr(provider, "client", None)
            if client is None:
                messagebox.showerror("Export", "Schwab client not available.")
                return
        except Exception as e:
            messagebox.showerror("Export", f"Could not init Schwab client:\n{e}")
            return

        # Fetch + save using your core helpers
        try:
            # these helper names should match what you implemented in the core:
            #   core.fetch_daily_candles(client, symbol, years)
            #   core.save_daily_candles_csv(rows, out_path)
            rows = core.fetch_daily_candles(client, sym, years)
            if not rows:
                messagebox.showwarning("Export", f"No candles returned for {sym}.")
                return
            core.save_daily_candles_csv(rows, out_path)
            self._toast(f"Saved {sym} ~{len(rows)} rows to {out_path}")
        except Exception as e:
            messagebox.showerror("Export", f"Export failed:\n{type(e).__name__}: {e}")

    # ---------- Output pump & parsing ----------
    TIME_PAT = re.compile(r"(\[\d{2}:\d{2}:\d{2}])")

    def _pump_output(self, _args: tuple | None = None) -> None:
        try:
            while True:
                chunk = self.out_q.get_nowait()
                for raw in chunk.splitlines():
                    line = raw.rstrip("\n")
                    s = line.strip()
                    if not s:
                        continue

                    # Hide backend chatter
                    if s.startswith("[schwab]") or s.startswith("Requesting:"):
                        continue
                    # Hide debug dumps (from core verbose/_debug_quote_choice)
                    if s == "DEBUG" or s.startswith("DEBUG "):
                        continue

                    # Hide bare ticker-list reprs like "['SPY', 'NVDA', ...]"
                    if s.startswith("[") and s.endswith("]"):
                        # crude but effective: looks like a list of quoted all-caps tickers
                        parts = [p.strip() for p in s[1:-1].split(",")]
                        if parts and all(re.fullmatch(r"'[A-Z]{1,5}'", p) for p in parts):
                            continue

                    # Prices: update ticker mini-cards
                    if s.startswith("Prices:"):
                        self._parse_for_prices(s)
                        continue

                    # Telemetry JSON
                    if s.startswith("{") and s.endswith("}"):
                        try:
                            obj = json.loads(s)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            self._apply_telemetry(obj)
                        # suppress raw JSON in prints
                        continue

                    # First clean line after Ask -> treat as the answer
                    if self._awaiting_answer:
                        self._append_chat("A", s)
                        self._awaiting_answer = False
                        # let regex updaters see it
                        self._parse_for_indicators(s)
                        self._parse_for_prices(s)
                        continue

                    # Otherwise show in PRINTS and let parsers sniff it
                    self._append_prints(line + "\n")
                    self._parse_for_indicators(line)
                    self._parse_for_prices(line)

        except queue.Empty:
            pass
        self.after(60, self._pump_output, ())

    def _append_prints(self, text: str):
        t = self.prints_text
        pos = 0
        for m in self.TIME_PAT.finditer(text):
            a, b = m.span()
            if a > pos:
                t.insert("end", text[pos:a])
            t.insert("end", m.group(1), ("time",))
            pos = b
        if pos < len(text):
            rest = text[pos:]
            for word, tag in (("OVERBOUGHT", "bad"), ("OVERSOLD", "good"), ("Gate:", "muted")):
                if word in rest:
                    parts = rest.split(word)
                    for i, p in enumerate(parts):
                        t.insert("end", p)
                        if i < len(parts) - 1:
                            t.insert("end", word, (tag,))
                    rest = ""
                    break
            if rest:
                t.insert("end", rest)
        t.see("end")

    def _parse_for_indicators(self, text: str) -> None:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    obj = json.loads(s)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    ta = obj.get("ta") or obj
                    rsi = ta.get("rsi") or ta.get("RSI")
                    macd = ta.get("macd") or ta.get("MACD")
                    vwap = ta.get("vwap") or ta.get("VWAP")
                    if rsi is not None:
                        r = float(rsi)
                        cast(ttk.Label, self.card_rsi["value"]).configure(text=self._fmt2(r))
                        state = "Overbought" if r >= 70 else "Oversold" if r <= 30 else "Neutral"
                        color_key = "red" if r >= 70 else "green" if r <= 30 else "muted"
                        cast(ttk.Label, self.card_rsi["status"]).configure(text=state, foreground=COLORS[color_key])
                    if macd is not None:
                        m = float(macd)
                        cast(ttk.Label, self.card_macd["value"]).configure(text=("+" if m >= 0 else "") + self._fmt2(m))
                        cast(ttk.Label, self.card_macd["status"]).configure(
                            text=("Bullish" if m >= 0 else "Bearish"),
                            foreground=COLORS["green" if m >= 0 else "red"],
                        )
                    if vwap is not None:
                        v = float(vwap)
                        cast(ttk.Label, self.card_vwap["value"]).configure(text=self._fmt2(v))

        m = RSI_RE.search(text)
        if m:
            rsi = float(m.group(1))
            cast(ttk.Label, self.card_rsi["value"]).configure(text=self._fmt2(rsi))
            state = "Overbought" if rsi >= 70 else "Oversold" if rsi <= 30 else "Neutral"
            color_key = "red" if rsi >= 70 else "green" if rsi <= 30 else "muted"
            cast(ttk.Label, self.card_rsi["status"]).configure(text=state, foreground=COLORS[color_key])

        m = MACD_RE.search(text)
        if m:
            macd = float(m.group(1))
            cast(ttk.Label, self.card_macd["value"]).configure(text=("+" if macd >= 0 else "") + self._fmt2(macd))
            cast(ttk.Label, self.card_macd["status"]).configure(
                text=("Bullish" if macd >= 0 else "Bearish"),
                foreground=COLORS["green" if macd >= 0 else "red"],
            )

        m = VWAP_RE.search(text)
        if m:
            v = float(m.group(1))
            cast(ttk.Label, self.card_vwap["value"]).configure(text=self._fmt2(v))

        if "above vwap" in text.lower():
            cast(ttk.Label, self.card_vwap["status"]).configure(text="Above", foreground=COLORS["blue"])
        elif "below vwap" in text.lower():
            cast(ttk.Label, self.card_vwap["status"]).configure(text="Below", foreground=COLORS["amber"])

        if "upper band" in text.lower() or ("upper" in text and "band" in text):
            cast(ttk.Label, self.card_bb["status"]).configure(text="Near upper band", foreground=COLORS["amber"])

    def _parse_for_prices(self, text: str) -> None:
        # dict-like: {'SPY': 646.77, "NVDA": 181.48, ...}
        for sym, price in re.findall(r"[\"']([A-Z/]{2,6})[\"']\s*:\s*([0-9]+(?:\.[0-9]+)?)", text):
            try:
                p = float(price)
            except ValueError:
                continue
            if sym in self.ticker_cards:
                cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

        # Also accept "SPY 645.96" or "SPY=645.96"
        sym_re = r"([A-Z]{2,5}(?:/[A-Z]{2,4})?)\s*[:=]?\s*(\d{1,5}(?:[.,]\d{1,2})?)"
        for sym, price in re.findall(sym_re, text):
            try:
                p = float(price.replace(",", "."))
            except ValueError:
                continue
            if sym in self.ticker_cards:
                cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

    # ---------- NEWS helpers ----------
    def _update_news_items(self, items: List[dict[str, object]]) -> None:
        """
        Expects a list like:
        [{"title": "...", "source": "Bloomberg", "url": "https://...",
          "published": 1699999999 or "2025-08-28T13:50:00Z",
          "sentiment": 0.23}, ...]
        Unknown fields are ignored gracefully.
        """
        # Clear table and url map
        for iid in self.news_tree.get_children(""):
            self.news_tree.delete(iid)
        self._news_urls.clear()

        def _fmt_when(ts: object) -> str:
            if ts is None:
                return "—"

            # epoch seconds
            if isinstance(ts, (int, float)):
                try:
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    return dt.astimezone().strftime("%H:%M")
                except (ValueError, OSError, TypeError):
                    return "—"

            # ISO8601 string
            if isinstance(ts, str):
                s = ts.strip()
                try:
                    if s.endswith("Z"):
                        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(s)
                    return dt.astimezone().strftime("%H:%M")
                except (ValueError, OSError, TypeError):
                    return s[:5] if len(s) >= 5 else (s or "—")

            return "—"

        def _fmt_sent(x: Any) -> str:
            """Return a +/−0.00 string or '—' if not parseable."""
            if x is None:
                return "—"

            # Fast path for real numbers
            if isinstance(x, (int, float)):
                return f"{float(x):+.2f}"

            # Strings like "0.12", "+0.4%", "  -1.0  "
            if isinstance(x, str):
                s = x.strip().replace("%", "")
                try:
                    return f"{float(s):+.2f}"
                except (ValueError, TypeError):
                    # Show the original string instead of crashing
                    return s if s else "—"

            # Anything else (dict, list, objects) -> not parseable
            return "—"

        for it in items:
            title = str(it.get("title") or "—")
            src   = str(it.get("source") or it.get("src") or "—")
            url   = str(it.get("url") or "")
            when  = _fmt_when(it.get("published") or it.get("time") or it.get("ts"))
            sent  = _fmt_sent(it.get("sentiment") or it.get("bias"))

            iid = self.news_tree.insert(
                "", "end",
                values=(
                    str(when),
                    str(src),
                    str(title),
                    str(sent),
                )
            )
            if url:
                self._news_urls[iid] = url

    # ---------- teardown ----------
    def destroy(self) -> None:
        # save settings and stop worker
        with suppress(Exception):
            self._save_settings()
        if self.loop_worker and self.loop_worker.is_alive():
            with suppress(Exception):
                self.loop_worker.stop()
        super().destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
