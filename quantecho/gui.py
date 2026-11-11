"""Tk dashboard: pick a data source, watchlist, focus TA strip, event log, local-AI (Ollama) Q&A."""
from __future__ import annotations

import os
import queue
import re
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Dict, Optional

from . import engine, journal, llm, logsetup
from .chart import ChartCanvas, merge_live_quote
from .logsetup import log as filelog
from .config import Settings, keyring_available, load_settings, normalize_host, save_settings
from .poller import Poller
from .providers import PROVIDERS, all_fields, create, visible_providers
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
    "warn": "#d29922",
}

LABEL_TO_ID = {spec.label: pid for pid, spec in PROVIDERS.items()}
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_HEAD = re.compile(r"^(#{1,4} )", re.M)


def _f(x, nd=2, suffix=""):
    return f"{x:,.{nd}f}{suffix}" if isinstance(x, (int, float)) else "—"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1220x820")
        self.minsize(960, 620)
        self.configure(bg=C["bg"])
        self._set_icon()
        self._style()

        self.settings = load_settings()
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.telemetry = TelemetryStore()
        self.poller: Optional[Poller] = None

        s = self.settings
        self.v_provider = tk.StringVar(value=PROVIDERS[s.provider].label)
        self.v_symbols = tk.StringVar(value=s.symbols)
        self.v_focus = tk.StringVar(value=s.focus)
        self.v_interval = tk.StringVar(value=str(s.interval))
        self.v_model = tk.StringVar(value=s.model)
        self.v_backend = tk.StringVar(value="Built-in" if s.ai_backend == "builtin" else "Ollama")
        self.v_host = tk.StringVar(value=s.ollama_host)
        self.v_remember = tk.BooleanVar(value=s.remember)
        self.v_show_personal = tk.BooleanVar(value=s.show_personal_sources)
        self.v_ask_launch = tk.BooleanVar(value=s.ask_source_on_launch)
        self._market = PROVIDERS[s.provider].market
        self.v_creds: Dict[str, tk.StringVar] = {f.key: tk.StringVar(value=s.creds.get(f.key, f.default))
                                                 for f in all_fields()}
        self.v_question = tk.StringVar(value="What's the current bias for my focus symbol?")
        self.v_with_ctx = tk.BooleanVar(value=True)
        self.v_think = tk.BooleanVar(value=False)
        self.v_auto = tk.BooleanVar(value=False)
        self.v_auto_min = tk.StringVar(value="15")
        self._auto_job: Optional[str] = None
        self.v_status = tk.StringVar(value="Stopped")
        self.v_ai_live = tk.StringVar(value="")
        self.v_ai_status = tk.StringVar(value="")

        self._build()
        self._render_provider_panel()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._pump_job: Optional[str] = self.after(100, self._pump)
        self.after(300, self.refresh_models)
        if s.ask_source_on_launch or not s.first_run_done:
            self.after(400, self.source_picker)

    # ================================================================ layout
    def _set_icon(self) -> None:
        icon = Path(__file__).resolve().parent / "assets" / "icon.png"
        try:
            self._icon_img = tk.PhotoImage(file=str(icon))  # keep a reference or Tk drops it
            self.iconphoto(True, self._icon_img)
        except (tk.TclError, OSError):
            pass  # cosmetic only

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
        st.configure("Warn.TLabel", foreground=C["warn"])
        st.configure("Link.TLabel", foreground=C["accent"], font=("Segoe UI", 9, "underline"))
        st.configure("H.TLabel", foreground=C["text"], font=("Segoe UI", 10, "bold"))
        st.configure("TA.TLabel", font=("Consolas", 11), foreground=C["accent"])
        st.configure("TButton", background="#1f2733", foreground=C["text"], padding=5)
        st.map("TButton", background=[("active", "#2a3443")])
        st.configure("TCheckbutton", background=C["panel"], foreground=C["text"])
        st.configure("TEntry", fieldbackground=C["sunken"], foreground=C["text"], insertcolor=C["text"])
        st.configure("TCombobox", fieldbackground=C["sunken"], foreground=C["text"], background="#1f2733",
                     arrowcolor=C["text"])
        st.map("TCombobox", fieldbackground=[("readonly", C["sunken"])], foreground=[("readonly", C["text"])])
        self.option_add("*TCombobox*Listbox.background", C["sunken"])
        self.option_add("*TCombobox*Listbox.foreground", C["text"])
        st.configure("Treeview", background=C["sunken"], fieldbackground=C["sunken"], foreground=C["text"],
                     rowheight=24, font=("Consolas", 10))
        st.configure("Treeview.Heading", background="#1f2733", foreground=C["muted"])

    def _entry(self, parent, label, var, width, secret=False):
        ttk.Label(parent, text=label, style="Muted.TLabel").pack(side="left", padx=(10, 4))
        e = ttk.Entry(parent, textvariable=var, width=width, show="•" if secret else "")
        e.pack(side="left")
        return e

    def _build(self) -> None:
        # --- toolbar
        self.toolbar = bar = ttk.Frame(self, padding=(0, 6))
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="Source", style="Muted.TLabel").pack(side="left", padx=(10, 4))
        self.cb_provider = cb = ttk.Combobox(bar, textvariable=self.v_provider, state="readonly", width=24)
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", self._on_provider_change)
        self._refresh_provider_list()
        self._entry(bar, "Symbols", self.v_symbols, 26)
        self._entry(bar, "Focus", self.v_focus, 9).bind("<Return>", self._apply_focus)
        self._entry(bar, "Every (s)", self.v_interval, 4)
        ttk.Button(bar, text="▶ Start", command=self.on_start_clicked).pack(side="left", padx=(12, 4))
        ttk.Button(bar, text="■ Stop", command=self.on_stop).pack(side="left")
        ttk.Button(bar, text="⚙ Settings", command=self._toggle_settings).pack(side="left", padx=(12, 0))
        ttk.Label(bar, textvariable=self.v_status, style="Muted.TLabel").pack(side="right", padx=10)

        # --- settings panel (hidden by default)
        self.settings_frame = sf = ttk.Frame(self, padding=(0, 4, 0, 8))
        self.provider_frame = ttk.Frame(sf)
        self.provider_frame.pack(fill="x")
        ai_row = ttk.Frame(sf)
        ai_row.pack(fill="x", pady=(8, 0))
        ttk.Label(ai_row, text="Local AI", style="H.TLabel").pack(side="left", padx=(10, 6))
        cb_be = ttk.Combobox(ai_row, textvariable=self.v_backend, values=["Built-in", "Ollama"],
                             state="readonly", width=9)
        cb_be.pack(side="left")
        cb_be.bind("<<ComboboxSelected>>", lambda _e: (self.on_save(), self.refresh_models()))
        self._entry(ai_row, "Ollama host", self.v_host, 22)
        ttk.Label(ai_row, text="Model", style="Muted.TLabel").pack(side="left", padx=(10, 4))
        self.cb_model = ttk.Combobox(ai_row, textvariable=self.v_model, width=22)
        self.cb_model.pack(side="left")
        ttk.Button(ai_row, text="↻", width=3, command=self.refresh_models).pack(side="left", padx=4)
        ttk.Label(ai_row, textvariable=self.v_ai_status, style="Muted.TLabel").pack(side="left", padx=6)
        ttk.Button(ai_row, text="Save", command=self.on_save).pack(side="right", padx=10)
        ttk.Checkbutton(ai_row, text="Ask for source on launch", variable=self.v_ask_launch,
                        command=self.on_save).pack(side="right", padx=(0, 10))
        ttk.Checkbutton(ai_row, text="Show personal-use-only sources (Yahoo)", variable=self.v_show_personal,
                        command=self._refresh_provider_list).pack(side="right", padx=(0, 10))
        ttk.Checkbutton(ai_row, text="Remember keys (OS keychain)" if keyring_available()
                        else "Remember keys (unavailable — use .env)", variable=self.v_remember).pack(side="right")

        # --- body
        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(4, 0))

        left = ttk.Frame(body)
        cols = ("last", "bid", "ask", "pct", "vol")
        tree_box = ttk.Frame(left)
        tree_box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_box, columns=cols, show="tree headings", height=12)
        self.tree.heading("#0", text="Symbol")
        self.tree.column("#0", width=80)
        for c, label, w in zip(cols, ("Last", "Bid", "Ask", "Day %", "Volume"), (80, 75, 75, 70, 100), strict=True):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="e")
        self.tree.tag_configure("up", foreground=C["up"])
        self.tree.tag_configure("down", foreground=C["down"])
        self.tree.tag_configure("focus", background="#18212d")
        self.tree.bind("<Double-1>", self._on_tree_dbl)
        sb = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Double-click a row to make it the focus", style="Muted.TLabel").pack(anchor="w", pady=4)
        body.add(left, weight=2)

        right = ttk.Frame(body)
        self.ta_label = ttk.Label(right, text="Focus: —", style="TA.TLabel")
        self.ta_label.pack(anchor="w", padx=6, pady=(0, 4))
        # chart on top, log below, draggable divider
        rpane = ttk.PanedWindow(right, orient="vertical")
        rpane.pack(fill="both", expand=True)
        self.chart = ChartCanvas(rpane, height=330)
        log_box = ttk.Frame(rpane)
        rpane.add(self.chart, weight=3)
        rpane.add(log_box, weight=2)
        self._rpane = rpane
        self.after(250, self._place_chart_sash)
        self.text = tk.Text(log_box, wrap="word", bg=C["sunken"], fg=C["text"], insertbackground=C["text"],
                            relief="flat", font=("Consolas", 10), padx=8, pady=6)
        self.text.tag_configure("q", foreground=C["accent"])
        self.text.tag_configure("a", foreground="#e3d7a3")
        self.text.tag_configure("warn", foreground=C["warn"])
        self.text.tag_configure("muted", foreground=C["muted"])
        self.text.tag_configure("bold", font=("Consolas", 10, "bold"), foreground="#f5ecc2")
        self.text.tag_raise("bold")
        self.text.tag_configure("bull", foreground=C["up"])
        self.text.tag_configure("bear", foreground=C["down"])
        self.text.pack(fill="both", expand=True)
        body.add(right, weight=3)

        # --- Q&A
        qa = ttk.Frame(self, padding=(0, 8))
        qa.pack(side="bottom", fill="x")
        auto = ttk.Frame(self)
        auto.pack(side="bottom", fill="x")
        ttk.Checkbutton(auto, text="Auto-analyze every", variable=self.v_auto,
                        command=self._schedule_auto).pack(side="left", padx=(10, 4))
        ttk.Entry(auto, textvariable=self.v_auto_min, width=4).pack(side="left")
        ttk.Label(auto, text="min (while running)", style="Muted.TLabel").pack(side="left", padx=4)
        ttk.Button(auto, text="📄 Log", command=self.open_log).pack(side="right", padx=(0, 10))
        ttk.Button(auto, text="📓 Journal", command=self.open_journal).pack(side="right", padx=4)
        q_entry = ttk.Entry(qa, textvariable=self.v_question)
        q_entry.pack(side="left", fill="x", expand=True, padx=(10, 6))
        q_entry.bind("<Return>", lambda _e: self.on_ask())
        ttk.Checkbutton(qa, text="Market context", variable=self.v_with_ctx).pack(side="left", padx=6)
        ttk.Checkbutton(qa, text="Deep think (slower)", variable=self.v_think).pack(side="left", padx=6)
        self.btn_ask = ttk.Button(qa, text="Ask", command=self.on_ask)
        self.btn_ask.pack(side="left", padx=4)
        self.btn_analyze = ttk.Button(qa, text="Analyze focus", command=self.on_analyze)
        self.btn_analyze.pack(side="left", padx=(4, 10))
        ttk.Label(qa, textvariable=self.v_ai_live, style="Muted.TLabel", width=22).pack(side="left")

    def _render_provider_panel(self) -> None:
        for w in self.provider_frame.winfo_children():
            w.destroy()
        spec = PROVIDERS[self._provider_id()]
        head = ttk.Frame(self.provider_frame)
        head.pack(fill="x")
        ttk.Label(head, text=spec.label, style="H.TLabel").pack(side="left", padx=(10, 8))
        ttk.Label(head, text=spec.notes, style="Muted.TLabel", wraplength=820).pack(side="left")
        if spec.signup_url:
            link = ttk.Label(head, text="get a key ↗", style="Link.TLabel", cursor="hand2")
            link.pack(side="left", padx=8)
            link.bind("<Button-1>", lambda _e, u=spec.signup_url: webbrowser.open(u))
        if spec.fields:
            row = ttk.Frame(self.provider_frame)
            row.pack(fill="x", pady=(4, 0))
            for f in spec.fields:
                self._entry(row, f.label, self.v_creds[f.key], 22 if f.secret else 26, secret=f.secret)
        if spec.id == "schwab":
            ttk.Label(self.provider_frame, text="First time / every 7 days: run  python -m quantecho.auth  "
                      "in a terminal to log in.", style="Warn.TLabel").pack(anchor="w", padx=10, pady=(4, 0))

    def _toggle_settings(self, show: Optional[bool] = None) -> None:
        visible = self.settings_frame.winfo_ismapped()
        if show is None:
            show = not visible
        if show and not visible:
            self.settings_frame.pack(side="top", fill="x", after=self.toolbar)
        elif not show and visible:
            self.settings_frame.pack_forget()

    # ================================================================ helpers
    def _provider_id(self) -> str:
        return LABEL_TO_ID.get(self.v_provider.get(), "yahoo")

    def log(self, s: str, tag: Optional[str] = None) -> None:
        self.text.insert("end", s.rstrip("\n") + "\n", tag or ())
        self.text.see("end")

    def _ts(self) -> str:
        return time.strftime("%H:%M:%S")

    def _collect(self) -> Settings:
        try:
            interval = max(1, int(self.v_interval.get()))
        except ValueError:
            interval = 20
        return Settings(
            provider=self._provider_id(),
            symbols=self.v_symbols.get().strip(),
            focus=(self.v_focus.get().strip() or "SPY").upper(),
            interval=interval,
            model=self.v_model.get().strip() or llm.DEFAULT_MODEL,
            ollama_host=normalize_host(self.v_host.get()),
            ai_backend="builtin" if self.v_backend.get() == "Built-in" else "ollama",
            remember=bool(self.v_remember.get()),
            show_personal_sources=bool(self.v_show_personal.get()),
            first_run_done=True,
            ask_source_on_launch=bool(self.v_ask_launch.get()),
            creds={k: v.get().strip() for k, v in self.v_creds.items()},
        )

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload, "warn" if "error" in payload.lower() else None)
                elif kind == "ai_start":
                    self.text.insert("end", payload, "q")
                    self.text.insert("end", "\n")
                    self.text.mark_set("ai", "end-2c")
                    self.text.mark_gravity("ai", "right")
                    self.text.mark_set("ai_begin", "end-2c")
                    self.text.mark_gravity("ai_begin", "left")
                    self.text.see("end")
                elif kind == "ai_tok":
                    self.text.insert("ai", payload, "a")
                    self.text.see("ai")
                elif kind == "ai_status":
                    self.v_ai_live.set(payload)
                elif kind == "ai_think":
                    self.v_ai_live.set(f"thinking… ~{payload // 4} tok")
                elif kind == "ai_done":
                    self._render_markdown("ai_begin", "ai")
                    self.text.insert("ai", payload, "muted")
                    self.text.see("end")
                    self.v_ai_live.set("")
                    self._ai_busy(False)
                elif kind == "signal":
                    self.log(f"[{self._ts()}] ⚡ {payload.text}", payload.kind if payload.kind != "info" else "muted")
                elif kind == "quotes":
                    self._refresh_table()
                    if self.poller:
                        self.v_status.set(f"{self._status_base} · updated {time.strftime('%H:%M:%S')}")
                elif kind == "models":
                    self._apply_models(payload)
        except queue.Empty:
            pass
        self._pump_job = self.after(100, self._pump)

    def _render_markdown(self, start: str, end: str) -> None:
        """Turn **bold** into a bold tag and '### Heading' into a heading, removing the markers."""
        body = self.text.get(start, end)
        spans = [(m.start(), m.end(), 2, "bold") for m in _BOLD.finditer(body)]
        spans += [(m.start(), m.end(), len(m.group(1)), "head") for m in _HEAD.finditer(body)]
        for s, e, n, tag in sorted(spans, reverse=True):  # back to front keeps offsets valid
            a, b = f"{start}+{s}c", f"{start}+{e}c"
            if tag == "bold":
                self.text.delete(f"{start}+{e - 2}c", b)
                self.text.delete(a, f"{start}+{s + 2}c")
                self.text.tag_add("bold", a, f"{start}+{e - 4}c")
            else:
                self.text.delete(a, f"{start}+{s + n}c")
                self.text.tag_add("bold", a, f"{a} lineend")

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
        vs = f" ({(q.last - ta.vwap) / ta.vwap:+.2%})" if q and q.last and ta.vwap else ""
        self.ta_label.configure(
            text=f"{focus}  VWAP {_f(ta.vwap)}{vs}   EMA9 {_f(ta.ema9)}  EMA20 {_f(ta.ema20)}   "
                 f"RSI {_f(ta.rsi14, 1)}   MACD {_f(ta.macd, 3)} / {_f(ta.macd_signal, 3)}  hist {_f(ta.macd_hist, 3)}"
        )
        self._refresh_chart(focus)

    def _place_chart_sash(self) -> None:
        h = self._rpane.winfo_height()
        if h > 200:
            self._rpane.sashpos(0, int(h * 0.66))  # chart gets ~2/3 of the right side by default

    def _refresh_chart(self, focus: str) -> None:
        bars, sess, q = self.telemetry.chart_data(focus)
        if not bars:
            return
        live = merge_live_quote(bars, q.last if q else None, q.timestamp_ms if q else None)
        sess = sess + [sess[-1]] * (len(live) - len(sess))  # a freshly-started candle inherits its session
        self.chart.set_data(focus, live, sess)

    # ================================================================ data actions
    def _refresh_provider_list(self) -> None:
        specs = visible_providers(bool(self.v_show_personal.get()), self._provider_id())
        self.cb_provider.configure(values=[sp.label for sp in specs])

    def _on_provider_change(self, _evt=None) -> None:
        self._render_provider_panel()
        spec = PROVIDERS[self._provider_id()]
        if spec.market != self._market:  # stocks <-> crypto: swap in sensible symbols
            self.v_symbols.set(spec.default_symbols)
            self.v_focus.set(spec.default_symbols.split()[0])
            self._market = spec.market
        self._refresh_provider_list()
        if spec.fields and not all(self.v_creds[f.key].get().strip() for f in spec.fields if f.secret):
            self._toggle_settings(True)
        if self.poller:
            self.on_stop()
            self.after(200, self.on_start)

    def on_save(self) -> None:
        self.settings = self._collect()
        try:
            failed = save_settings(self.settings)
        except OSError as e:
            messagebox.showerror("Save failed", str(e))
            return
        if failed:
            self.log(f"[{self._ts()}] Settings saved, but no secure keychain is available, so these keys were "
                     f"NOT stored: {', '.join(failed)}. Put them in .env instead.", "warn")
        else:
            where = "OS keychain" if self.settings.remember else "not stored (Remember keys is off)"
            self.log(f"[{self._ts()}] Settings saved. Keys: {where}.")

    def on_start_clicked(self) -> None:
        """Toolbar button: start, or restart with the current settings if already running."""
        if self.poller and self.poller.is_alive():
            self.log(f"[{self._ts()}] Already running — restarting with current settings.", "muted")
            self.on_stop()
        self.on_start()

    def on_start(self) -> None:
        if self.poller and self.poller.is_alive():
            return
        s = self.settings = self._collect()
        spec = PROVIDERS[s.provider]
        try:
            provider = create(s.provider, s.creds)
        except Exception as e:
            filelog.warning(f"Provider {s.provider} failed to start: {e}")
            self._toggle_settings(True)
            messagebox.showerror(spec.label, str(e))
            return
        n_syms = len(dict.fromkeys(s.symbols.replace(",", " ").split())) or 1
        interval = spec.interval_for(n_syms, s.interval)
        if interval != s.interval:
            why = f"{n_syms} symbols on the free tier" if spec.calls_per_symbol else "free-tier limits"
            self.log(f"[{self._ts()}] {spec.label}: {why} → polling every {interval}s "
                     f"instead of {s.interval}s.", "warn")
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.telemetry = TelemetryStore(market=spec.market, clip_volume=spec.clip_volume_spikes)
        self.chart.clear("Loading chart…")
        self.poller = Poller(provider, s.symbols.replace(",", " ").split(), s.focus, interval,
                             self.events, self.telemetry, bars_every=max(spec.min_bars_every, interval))
        self.poller.start()
        filelog.info(f"Start provider={s.provider} focus={s.focus} symbols={s.symbols!r} interval={interval}s")
        self._status_base = f"Live · {spec.label.split(' (')[0]} · {s.focus}"
        self.v_status.set(self._status_base + " · connecting…")

    def on_stop(self) -> None:
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.v_status.set("Stopped")

    def _on_tree_dbl(self, _evt=None) -> None:
        sel = self.tree.focus()
        if sel and (not self.poller or sel != self.poller.focus):
            self.v_focus.set(sel)
            if self.poller:
                self.on_stop()
                self.after(200, self.on_start)

    # ================================================================ AI actions
    def refresh_models(self) -> None:
        if self.v_backend.get() == "Built-in":
            if engine.available():
                self.v_ai_status.set(f"● Built-in AI: {engine.find_model().stem} (Qwen3, Apache-2.0) · "
                                     "starts on your first question")
            else:
                self.v_ai_status.set("⚠ Built-in AI isn't included in this build. Choose Ollama.")
            return
        host = normalize_host(self.v_host.get())
        self.v_ai_status.set("checking…")
        threading.Thread(target=lambda: self.events.put(("models", llm.list_models(host))), daemon=True).start()

    def _apply_models(self, models) -> None:
        # recommended (commercially licensed) models first
        models = sorted(models, key=lambda m: (m not in llm.RECOMMENDED, m))
        self.cb_model.configure(values=models)
        if not models:
            self.v_ai_status.set(f"⚠ No models / Ollama not reachable — run: ollama pull {llm.DEFAULT_MODEL}")
            return
        cur = self.v_model.get().strip()
        if cur not in models and f"{cur}:latest" not in models:
            self.v_model.set(llm.DEFAULT_MODEL if llm.DEFAULT_MODEL in models else models[0])
        lic = llm.RECOMMENDED.get(self.v_model.get(), "check the model's license")
        self.v_ai_status.set(f"● {len(models)} installed · {self.v_model.get()}: {lic}")
        model, host = self.v_model.get(), normalize_host(self.v_host.get())
        threading.Thread(target=llm.preload, args=(model, host), daemon=True).start()  # warm the GPU

    def _ai_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.btn_ask.configure(state=state)
        self.btn_analyze.configure(state=state)

    def _run_ai(self, prompt: str, think: Optional[bool] = None, title: str = "Ask", ctx: str = "") -> None:
        s = self._collect()
        think = bool(self.v_think.get()) if think is None else think
        self._ai_busy(True)
        self.v_ai_live.set("loading model…")
        who = "built-in AI" if s.ai_backend == "builtin" else s.model
        self.events.put(("ai_start", f"[{self._ts()}] A ({who}{', deep think' if think else ''})>"))

        def worker():
            t0 = time.monotonic()
            first = [True]

            def tok(t: str) -> None:
                if first[0]:
                    first[0] = False
                    self.events.put(("ai_tok", "\n"))
                self.events.put(("ai_tok", t))

            if s.ai_backend == "builtin":
                text, st = llm.ask_stream_builtin(
                    prompt, tok, think=think, on_thinking=lambda n: self.events.put(("ai_think", n)),
                    on_status=lambda m: self.events.put(("ai_status", m)))
                s.model = st.model
            else:
                text, st = llm.ask_stream(prompt, tok, model=s.model, host=s.ollama_host, think=think,
                                      on_thinking=lambda n: self.events.put(("ai_think", n)))
            if first[0]:  # nothing streamed -> error/empty message
                self.events.put(("ai_tok", " " + text))
            speed = f", {st.tok_per_s:.0f} tok/s" if st.tok_per_s else ""
            failed = text.startswith(("[Ollama", "[Built-in AI"))
            if failed:
                filelog.warning(f"AI failed ({s.model}): {text}")
            else:
                filelog.info(f"AI ok {title!r} model={s.model} {time.monotonic() - t0:.0f}s {st.tok_per_s:.0f} tok/s")
            note = ""
            if not failed:
                try:
                    journal.append(f"{title} · {s.model}", text, context=ctx)
                    note = " · journaled"
                except OSError:
                    pass
            # trailing newline -> blank line before whatever gets logged next
            self.events.put(("ai_done", f"\n  ({time.monotonic() - t0:.0f}s{speed}{note})\n"))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_focus(self, _evt=None) -> str:
        """Make the Focus field authoritative: if polling a different symbol, switch to it now."""
        new = (self.v_focus.get().strip() or "SPY").upper()
        self.v_focus.set(new)
        if self.poller and self.poller.focus != new:
            self.on_stop()
            self.on_start()
        return new

    def _when_ready(self, focus: str, callback, tries: int = 30) -> None:
        """Call callback(ctx) once telemetry for `focus` exists (polls up to ~15 s while running)."""
        ctx = self.telemetry.context(focus)
        if ctx or tries <= 0 or not self.poller:
            callback(ctx or "")
            return
        if tries == 30:
            self.v_ai_live.set(f"waiting for {focus} data…")
        self.after(500, lambda: self._when_ready(focus, callback, tries - 1))

    def on_ask(self) -> None:
        q = self.v_question.get().strip()
        if not q or str(self.btn_ask.cget("state")) == "disabled":
            return
        focus = self._apply_focus()
        self.log(f"[{self._ts()}] Q> {q}", "q")
        self._ai_busy(True)

        def go(ctx: str) -> None:
            prompt = f"Focus symbol: {focus}\n\n{q}"
            if ctx:
                prompt += f"\n\nTelemetry:\n{ctx}"
            elif self.v_with_ctx.get():
                self.log(f"[AI] No {focus} telemetry (press Start for live data); answering without it.")
            self._run_ai(prompt, title=f"Q ({focus}): {q[:80]}", ctx=ctx)

        if self.v_with_ctx.get():
            self._when_ready(focus, go)
        else:
            go("")

    def on_analyze(self, auto: bool = False) -> None:
        if str(self.btn_analyze.cget("state")) == "disabled":
            return
        focus = self._apply_focus()
        if not self.poller:
            if not auto:
                self.log("[AI] Press ▶ Start first — analysis needs live data.")
            return
        self._ai_busy(True)

        def go(ctx: str) -> None:
            if not ctx:
                self._ai_busy(False)
                self.v_ai_live.set("")
                self.log(f"[AI] No {focus} data came back from {PROVIDERS[self._provider_id()].label}.", "warn")
                return
            self.log(f"[{self._ts()}] {'Auto-a' if auto else 'A'}nalyzing {focus} with {self.v_model.get()}…", "q")
            self._run_ai(llm.ANALYZE_TEMPLATE.format(ctx=ctx), think=True,
                         title=f"{'Auto ' if auto else ''}Analysis {focus}", ctx=ctx)

        self._when_ready(focus, go)

    # ---------- source picker (every launch unless turned off)
    FEATURED = ("coinbase", "alpaca", "simulated")

    def source_picker(self) -> None:
        dlg = tk.Toplevel(self)
        dlg.title("QuantEcho — pick a data source")
        dlg.configure(bg=C["panel"])
        dlg.transient(self)
        dlg.resizable(False, False)
        current = self._provider_id()
        first_time = not self.settings.first_run_done
        ttk.Label(dlg, text="Welcome to QuantEcho" if first_time else "Pick a data source",
                  style="H.TLabel").pack(padx=24, pady=(16, 2))
        hint = "Choose where market data comes from. You can switch any time from the Source menu."
        if not first_time:
            hint += "  ★ = your last source (Enter starts it)."
        ttk.Label(dlg, text=hint,
                  style="Muted.TLabel", wraplength=440).pack(padx=24, pady=(0, 10))

        blurbs = {
            "coinbase": "Live crypto (BTC, ETH, SOL…) — no signup",
            "alpaca": "US stocks, real-time — free key (≈2 min signup)",
            "simulated": "Demo mode — fake prices, no internet",
        }
        visible = visible_providers(bool(self.v_show_personal.get()), current)
        ordered = [sp for pid in self.FEATURED for sp in visible if sp.id == pid] +                   [sp for sp in visible if sp.id not in self.FEATURED]

        def needs_keys(sp) -> bool:
            return any(f.secret and not self.v_creds[f.key].get().strip() for f in sp.fields)

        def choose(pid: str) -> None:
            spec = PROVIDERS[pid]
            self.v_provider.set(spec.label)
            self._on_provider_change()
            dlg.destroy()
            self.on_save()
            if needs_keys(spec):
                self._toggle_settings(True)
                self.log(f"[Setup] {spec.label} needs a free API key: paste it in ⚙ Settings ('get a key ↗' opens "
                         "the signup page), click Save, then ▶ Start.", "warn")
            else:
                self.on_start()

        grid = ttk.Frame(dlg, padding=(18, 0))
        grid.pack(fill="x")
        for i, sp in enumerate(ordered):
            if i == len(self.FEATURED) and len(ordered) > i:
                ttk.Label(grid, text="Other sources", style="Muted.TLabel").pack(anchor="w", pady=(10, 2))
            row = ttk.Frame(grid, padding=(0, 3))
            row.pack(fill="x")
            tag = " ★" if sp.id == current and not first_time else ""
            warn = "⚠ " if sp.personal_use_only else ""
            btn = ttk.Button(row, text=f"{warn}{sp.label}{tag}", width=40, command=lambda p=sp.id: choose(p))
            btn.pack(side="left")
            desc = blurbs.get(sp.id) or sp.notes.split(". ")[0]
            if needs_keys(sp):
                desc += "  · key needed"
            ttk.Label(row, text=desc, style="Muted.TLabel", wraplength=300).pack(side="left", padx=10)
            if sp.id == current:
                btn.focus_set()
                dlg.bind("<Return>", lambda _e, p=sp.id: choose(p))

        foot = ttk.Frame(dlg, padding=(18, 12))
        foot.pack(fill="x")
        ttk.Checkbutton(foot, text="Don't ask again (turn back on in ⚙ Settings)",
                        variable=self.v_ask_launch, onvalue=False, offvalue=True).pack(side="left")
        if not self.v_show_personal.get():
            ttk.Label(dlg, text="Yahoo Finance (personal use only) can be enabled in ⚙ Settings.",
                      style="Muted.TLabel").pack(padx=24, pady=(0, 12))

        def dismiss(_e=None) -> None:  # keep current source, don't auto-start
            dlg.destroy()
            self.on_save()

        dlg.protocol("WM_DELETE_WINDOW", dismiss)
        dlg.bind("<Escape>", dismiss)
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + 100
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

    # ---------- auto-analyze / journal
    def _schedule_auto(self) -> None:
        if self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None
        if not self.v_auto.get():
            return
        try:
            minutes = max(1.0, float(self.v_auto_min.get()))
        except ValueError:
            minutes = 15.0
        self._auto_job = self.after(int(minutes * 60_000), self._auto_tick)

    def _auto_tick(self) -> None:
        self._auto_job = None
        if self.poller and self.poller.is_alive():
            self.on_analyze(auto=True)
        self._schedule_auto()

    def open_journal(self) -> None:
        path = journal.journal_path()
        if not path.exists():
            self.log(f"[Journal] Nothing yet today — analyses are saved to {path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows
        except (AttributeError, OSError):
            webbrowser.open(path.as_uri())

    def report_callback_exception(self, exc, val, tb) -> None:  # Tk hook
        filelog.error("UI callback error", exc_info=(exc, val, tb))
        try:
            self.log(f"[{self._ts()}] Internal error: {exc.__name__}: {val} (details in log file)", "warn")
        except tk.TclError:
            pass

    def open_log(self) -> None:
        path = logsetup.LOG_PATH
        if not path.exists():
            self.log(f"[Log] No log yet at {path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            webbrowser.open(path.as_uri())

    def _on_close(self) -> None:
        self.on_stop()
        engine.ENGINE.stop()  # shut down the bundled AI server, if it was started
        for job in (getattr(self, "_pump_job", None), getattr(self, "_auto_job", None)):
            if job:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self.destroy()


def main() -> None:
    import sys
    if "--selftest" in sys.argv:
        from . import selftest
        sys.exit(selftest.run(sys.argv))
    path = logsetup.setup()
    filelog.info(f"QuantEcho starting (log: {path})")
    App().mainloop()
    filelog.info("QuantEcho closed")
