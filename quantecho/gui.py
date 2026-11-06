"""Tk dashboard: watchlist, focus TA strip, event log, AI Q&A."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Optional

from . import ai
from .config import Settings, load_settings, save_settings
from .poller import Poller
from .schwab_provider import SchwabProvider
from .telemetry import TelemetryStore

APP_NAME = "QuantEcho"

C = {
    "bg": "#0f1216",
    "panel": "#141922",
    "sunken": "#0c1016",
    "text": "#dfe5ee",
    "muted": "#9aa9bf",
    "up": "#3fb950",
    "down": "#f85149",
    "accent": "#58a6ff",
}


def _f(x, nd=2, suffix=""):
    return f"{x:,.{nd}f}{suffix}" if isinstance(x, (int, float)) else "—"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x800")
        self.minsize(900, 600)
        self.configure(bg=C["bg"])
        self._style()

        self.settings = load_settings()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.telemetry = TelemetryStore()
        self.poller: Optional[Poller] = None

        s = self.settings
        self.v = {
            "symbols": tk.StringVar(value=s.symbols),
            "focus": tk.StringVar(value=s.focus),
            "interval": tk.StringVar(value=str(s.interval)),
            "model": tk.StringVar(value=s.model),
            "schwab_app_key": tk.StringVar(value=s.schwab_app_key),
            "schwab_app_secret": tk.StringVar(value=s.schwab_app_secret),
            "schwab_callback": tk.StringVar(value=s.schwab_callback),
            "schwab_token_path": tk.StringVar(value=s.schwab_token_path),
            "openai_key": tk.StringVar(value=s.openai_key),
        }
        self.v_remember = tk.BooleanVar(value=s.remember)
        self.v_question = tk.StringVar(value="What's the current bias for my focus symbol?")
        self.v_with_ctx = tk.BooleanVar(value=True)
        self.v_status = tk.StringVar(value="Stopped")

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._pump)
        if not (s.schwab_app_key and s.schwab_app_secret):
            self.creds_frame.pack(side="top", fill="x", after=self.toolbar)

    # ---------- layout ----------
    def _style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=C["panel"], foreground=C["text"], fieldbackground=C["sunken"])
        st.configure("TFrame", background=C["panel"])
        st.configure("TLabel", background=C["panel"], foreground=C["text"])
        st.configure("Muted.TLabel", foreground=C["muted"])
        st.configure("TA.TLabel", font=("Consolas", 11), foreground=C["accent"])
        st.configure("TButton", background="#1f2733", foreground=C["text"], padding=5)
        st.map("TButton", background=[("active", "#2a3443")])
        st.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        st.configure("TEntry", fieldbackground=C["sunken"], foreground=C["text"], insertcolor=C["text"])
        st.configure("Treeview", background=C["sunken"], fieldbackground=C["sunken"], foreground=C["text"],
                     rowheight=24, font=("Consolas", 10))
        st.configure("Treeview.Heading", background="#1f2733", foreground=C["muted"])

    def _entry(self, parent, label, key, width, secret=False):
        ttk.Label(parent, text=label, style="Muted.TLabel").pack(side="left", padx=(10, 4))
        ttk.Entry(parent, textvariable=self.v[key], width=width, show="•" if secret else "").pack(side="left")

    def _build(self) -> None:
        self.toolbar = bar = ttk.Frame(self, padding=(0, 6))
        bar.pack(side="top", fill="x")
        self._entry(bar, "Symbols", "symbols", 28)
        self._entry(bar, "Focus", "focus", 7)
        self._entry(bar, "Every (s)", "interval", 4)
        self.btn_start = ttk.Button(bar, text="▶ Start", command=self.on_start)
        self.btn_start.pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="■ Stop", command=self.on_stop).pack(side="left")
        ttk.Button(bar, text="⚙ Keys", command=self._toggle_creds).pack(side="left", padx=(12, 0))
        ttk.Label(bar, textvariable=self.v_status, style="Muted.TLabel").pack(side="right", padx=10)

        self.creds_frame = cf = ttk.Frame(self, padding=(0, 4))
        r1 = ttk.Frame(cf); r1.pack(fill="x", pady=2)
        self._entry(r1, "Schwab App Key", "schwab_app_key", 24, secret=True)
        self._entry(r1, "App Secret", "schwab_app_secret", 24, secret=True)
        self._entry(r1, "Callback", "schwab_callback", 26)
        r2 = ttk.Frame(cf); r2.pack(fill="x", pady=2)
        self._entry(r2, "Token file", "schwab_token_path", 24)
        self._entry(r2, "OpenAI key", "openai_key", 24, secret=True)
        self._entry(r2, "Model", "model", 12)
        ttk.Checkbutton(r2, text="Remember keys", variable=self.v_remember).pack(side="left", padx=10)
        ttk.Button(r2, text="Save", command=self.on_save).pack(side="left")

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        left = ttk.Frame(body)
        cols = ("last", "bid", "ask", "pct", "vol")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings", height=12)
        self.tree.heading("#0", text="Symbol")
        self.tree.column("#0", width=80)
        for c, label, w in zip(cols, ("Last", "Bid", "Ask", "Day %", "Volume"), (80, 75, 75, 70, 100), strict=True):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="e")
        self.tree.tag_configure("up", foreground=C["up"])
        self.tree.tag_configure("down", foreground=C["down"])
        self.tree.tag_configure("focus", background="#18212d")
        self.tree.bind("<Double-1>", self._on_tree_dbl)
        self.tree.pack(fill="both", expand=True)
        ttk.Label(left, text="Double-click a row to make it the focus", style="Muted.TLabel").pack(anchor="w", pady=4)
        body.add(left, weight=2)

        right = ttk.Frame(body)
        self.ta_label = ttk.Label(right, text="Focus: —", style="TA.TLabel")
        self.ta_label.pack(anchor="w", padx=6, pady=(0, 4))
        self.text = tk.Text(right, wrap="word", bg=C["sunken"], fg=C["text"], insertbackground=C["text"],
                            relief="flat", font=("Consolas", 10), padx=8, pady=6)
        self.text.tag_configure("q", foreground=C["accent"])
        self.text.tag_configure("a", foreground="#e3d7a3")
        self.text.pack(fill="both", expand=True)
        body.add(right, weight=3)

        qa = ttk.Frame(self, padding=(0, 8))
        qa.pack(side="bottom", fill="x")
        q_entry = ttk.Entry(qa, textvariable=self.v_question)
        q_entry.pack(side="left", fill="x", expand=True, padx=(10, 6))
        q_entry.bind("<Return>", lambda _e: self.on_ask())
        ttk.Checkbutton(qa, text="Include market context", variable=self.v_with_ctx).pack(side="left", padx=6)
        ttk.Button(qa, text="Ask", command=self.on_ask).pack(side="left", padx=4)
        ttk.Button(qa, text="Analyze focus", command=self.on_analyze).pack(side="left", padx=(4, 10))

    def _toggle_creds(self) -> None:
        if self.creds_frame.winfo_ismapped():
            self.creds_frame.pack_forget()
        else:
            self.creds_frame.pack(side="top", fill="x", after=self.toolbar)

    # ---------- helpers ----------
    def log(self, s: str, tag: Optional[str] = None) -> None:
        self.text.insert("end", s.rstrip("\n") + "\n", tag or ())
        self.text.see("end")

    def _collect(self) -> Settings:
        s = Settings()
        for k, var in self.v.items():
            val = var.get().strip()
            if k == "interval":
                try:
                    val = max(1, int(val))
                except ValueError:
                    val = 20
            setattr(s, k, val)
        s.focus = (s.focus or "SPY").upper()
        s.remember = bool(self.v_remember.get())
        return s

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "answer":
                    self.log(payload, "a")
                elif kind == "quotes":
                    self._refresh_table()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _refresh_table(self) -> None:
        if not self.poller:
            return
        focus = self.poller.focus
        for sym in self.poller.symbols:
            q = self.telemetry.quote(sym)
            if not q:
                continue
            vals = (_f(q.last), _f(q.bid), _f(q.ask), _f(q.net_pct, 2, "%"), _f(q.total_volume, 0))
            tags = []
            if q.net_pct is not None:
                tags.append("up" if q.net_pct >= 0 else "down")
            if sym == focus:
                tags.append("focus")
            if self.tree.exists(sym):
                self.tree.item(sym, values=vals, tags=tags)
            else:
                self.tree.insert("", "end", iid=sym, text=sym, values=vals, tags=tags)
        ta = self.telemetry.ta(focus)
        q = self.telemetry.quote(focus)
        vs = ""
        if q and q.last and ta.vwap:
            vs = f" ({(q.last - ta.vwap) / ta.vwap:+.2%})"
        self.ta_label.configure(
            text=f"{focus}  VWAP {_f(ta.vwap)}{vs}   RSI {_f(ta.rsi14, 1)}   "
                 f"MACD {_f(ta.macd, 3)} / {_f(ta.macd_signal, 3)}  hist {_f(ta.macd_hist, 3)}"
        )

    # ---------- actions ----------
    def on_save(self) -> None:
        self.settings = self._collect()
        try:
            save_settings(self.settings)
            self.log(f"[{time.strftime('%H:%M:%S')}] Settings saved.")
        except OSError as e:
            messagebox.showerror("Save failed", str(e))

    def on_start(self) -> None:
        if self.poller and self.poller.is_alive():
            return
        s = self.settings = self._collect()
        if not s.schwab_app_key or not s.schwab_app_secret:
            messagebox.showwarning("Schwab", "Enter your Schwab App Key and App Secret (⚙ Keys).")
            if not self.creds_frame.winfo_ismapped():
                self._toggle_creds()
            return
        try:
            provider = SchwabProvider(s.schwab_app_key, s.schwab_app_secret, s.schwab_token_path)
        except Exception as e:
            messagebox.showerror("Schwab auth", str(e))
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.poller = Poller(provider, s.symbols.replace(",", " ").split(), s.focus, s.interval,
                             self.events, self.telemetry, bars_every=max(60, s.interval))
        self.poller.start()
        self.v_status.set(f"Live · {s.focus}")

    def on_stop(self) -> None:
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.v_status.set("Stopped")

    def _on_tree_dbl(self, _evt=None) -> None:
        sel = self.tree.focus()
        if sel and (not self.poller or sel != self.poller.focus):
            self.v["focus"].set(sel)
            if self.poller:
                self.on_stop()
                self.after(200, self.on_start)

    def _run_ai(self, prompt: str) -> None:
        s = self._collect()
        if not s.openai_key:
            self.log("[AI] OpenAI key missing (⚙ Keys).")
            return

        def worker():
            ans = ai.ask(prompt, model=s.model or "gpt-5", api_key=s.openai_key)
            self.events.put(("answer", f"[{time.strftime('%H:%M:%S')}] A> {ans}\n"))

        threading.Thread(target=worker, daemon=True).start()

    def on_ask(self) -> None:
        q = self.v_question.get().strip()
        if not q:
            return
        self.log(f"[{time.strftime('%H:%M:%S')}] Q> {q}", "q")
        prompt = q
        if self.v_with_ctx.get():
            ctx = self.telemetry.context(self._collect().focus)
            if ctx:
                prompt = f"{q}\n\nTelemetry:\n{ctx}"
            else:
                self.log("[AI] No telemetry yet; asking without market context.")
        self._run_ai(prompt)

    def on_analyze(self) -> None:
        focus = self._collect().focus
        ctx = self.telemetry.context(focus)
        if not ctx:
            self.log("[AI] No telemetry yet — press Start first.")
            return
        self.log(f"[{time.strftime('%H:%M:%S')}] Analyzing {focus}…", "q")
        self._run_ai(ai.ANALYZE_TEMPLATE.format(ctx=ctx))

    def _on_close(self) -> None:
        self.on_stop()
        self.destroy()


def main() -> None:
    App().mainloop()
