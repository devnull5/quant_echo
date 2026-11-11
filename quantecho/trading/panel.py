"""Trading window: mode, tastytrade credentials, risk limits, strategy, start/stop, KILL SWITCH, positions.

All bot work runs on the bot's thread; it talks to this window only through the app's event queue:
  ("bot", text)            log line
  ("bot_status", status)   BotStatus snapshot
  ("bot_confirm", req)     order needs a yes/no (the bot thread waits up to CONFIRM_TIMEOUT_S, then declines)
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from typing import Any, Dict

from ..config import secret_get, secret_set
from ..logsetup import log
from .risk import RiskLimits
from .strategy import StrategyParams

CONFIRM_TIMEOUT_S = 60
LIVE_PHRASE = "REAL MONEY"
SECRETS = {"sandbox": ("tasty_sandbox_secret", "tasty_sandbox_refresh"),
           "live": ("tasty_live_secret", "tasty_live_refresh")}

C = {"bg": "#0f1216", "panel": "#141922", "sunken": "#0c1016", "text": "#dfe5ee", "muted": "#9aa9bf",
     "up": "#3fb950", "down": "#f85149", "warn": "#d29922", "kill": "#b62324"}


class ConfirmRequest:
    def __init__(self, text: str):
        self.text, self.answer, self.done = text, False, threading.Event()


class TradingPanel(tk.Toplevel):
    def __init__(self, app: Any):
        super().__init__(app)
        self.app = app
        self.title("QuantEcho Trading: tastytrade options (long calls/puts)")
        self.configure(bg=C["panel"])
        self.geometry("1040x760")
        self.minsize(860, 640)
        self.bot = None
        t = dict(app.settings.trading or {})
        L, P = RiskLimits(), StrategyParams()
        self.v: Dict[str, tk.Variable] = {
            "mode": tk.StringVar(value=t.get("mode", "sim")),
            "account_sandbox": tk.StringVar(value=t.get("account_sandbox", "")),
            "account_live": tk.StringVar(value=t.get("account_live", "")),
            "prod_quotes": tk.BooleanVar(value=bool(t.get("prod_quotes", False))),
            "confirm_each": tk.BooleanVar(value=bool(t.get("confirm_each", True))),
            "symbols": tk.StringVar(value=t.get("symbols", " ".join(L.allowed_symbols))),
            "max_premium": tk.StringVar(value=str(t.get("max_premium", L.max_premium_per_trade))),
            "max_contracts": tk.StringVar(value=str(t.get("max_contracts", L.max_contracts_per_trade))),
            "max_open": tk.StringVar(value=str(t.get("max_open", L.max_open_positions))),
            "daily_loss": tk.StringVar(value=str(t.get("daily_loss", L.daily_loss_limit))),
            "max_entries": tk.StringVar(value=str(t.get("max_entries", L.max_entries_per_day))),
            "target_dte": tk.StringVar(value=str(t.get("target_dte", P.target_dte))),
            "min_dte": tk.StringVar(value=str(t.get("min_dte", P.min_dte))),
            "max_dte": tk.StringVar(value=str(t.get("max_dte", P.max_dte))),
            "strikes_otm": tk.StringVar(value=str(t.get("strikes_otm", P.strikes_otm))),
            "tp": tk.StringVar(value=str(int(t.get("tp", P.take_profit_pct * 100)))),
            "sl": tk.StringVar(value=str(int(t.get("sl", P.stop_loss_pct * 100)))),
            "opp_exit": tk.BooleanVar(value=bool(t.get("opp_exit", P.exit_on_opposite_signal))),
            "reversals": tk.BooleanVar(value=bool(t.get("reversals", P.use_reversal_signals))),
        }
        self.v_secret = {k: tk.StringVar(value=secret_get(k) or "") for pair in SECRETS.values() for k in pair}
        self.v_status = tk.StringVar(value="Bot stopped")
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.withdraw)  # closing the window keeps the bot running

    # ------------------------------------------------------------------ layout
    def _row(self, parent, label, var, width=10, secret=False):
        ttk.Label(parent, text=label, style="Muted.TLabel").pack(side="left", padx=(8, 3))
        e = ttk.Entry(parent, textvariable=var, width=width, show="•" if secret else "")
        e.pack(side="left")
        return e

    def _build(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Mode", style="H.TLabel").pack(side="left")
        for val, txt in (("sim", "SIM (local, modeled)"), ("sandbox", "tastytrade Sandbox"), ("live", "LIVE (real money)")):
            ttk.Radiobutton(top, text=txt, value=val, variable=self.v["mode"]).pack(side="left", padx=6)
        self.kill_btn = tk.Button(top, text="■ KILL SWITCH", bg=C["kill"], fg="white", activebackground="#d1242f",
                                  font=("Segoe UI", 10, "bold"), relief="flat", padx=12, command=self.on_kill)
        self.kill_btn.pack(side="right")
        ttk.Button(top, text="Stop bot", command=self.on_stop).pack(side="right", padx=6)
        ttk.Button(top, text="▶ Start bot", command=self.on_start).pack(side="right")

        warn = ttk.Label(self, style="Warn.TLabel", wraplength=940, padding=(8, 0),
                         text="Long calls/puts only. Not financial advice: the signals have no proven edge. "
                              "Run SIM and Sandbox for a long time before LIVE. You are responsible for every trade.")
        warn.pack(fill="x")

        creds = ttk.LabelFrame(self, text="tastytrade credentials (saved in Windows Credential Manager)", padding=6)
        creds.pack(fill="x", padx=8, pady=6)
        for env in ("sandbox", "live"):
            r = ttk.Frame(creds)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=env.title(), width=8, style="H.TLabel").pack(side="left")
            sec, ref = SECRETS[env]
            self._row(r, "Client secret", self.v_secret[sec], 22, secret=True)
            self._row(r, "Refresh token", self.v_secret[ref], 30, secret=True)
            self._row(r, "Account # (optional)", self.v[f"account_{env}"], 12)
            ttk.Button(r, text="Test", command=lambda e=env: self.on_test(e)).pack(side="left", padx=8)
        r = ttk.Frame(creds)
        r.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(r, text="Use LIVE credentials (read-only) for real option quotes in SIM/Sandbox",
                        variable=self.v["prod_quotes"]).pack(side="left")
        ttk.Button(r, text="Save settings", command=self.save).pack(side="right")

        cfg = ttk.Frame(self)
        cfg.pack(fill="x", padx=8)
        risk = ttk.LabelFrame(cfg, text="Hard risk limits", padding=6)
        risk.pack(side="left", fill="both", expand=True, padx=(0, 4))
        r1, r2 = ttk.Frame(risk), ttk.Frame(risk)
        r1.pack(fill="x"), r2.pack(fill="x", pady=4)
        self._row(r1, "Symbols", self.v["symbols"], 14)
        self._row(r1, "Max $/trade", self.v["max_premium"], 7)
        self._row(r1, "Max contracts", self.v["max_contracts"], 4)
        self._row(r2, "Max open", self.v["max_open"], 4)
        self._row(r2, "Daily loss $", self.v["daily_loss"], 7)
        self._row(r2, "Entries/day", self.v["max_entries"], 4)
        ttk.Checkbutton(r2, text="Confirm every order", variable=self.v["confirm_each"]).pack(side="left", padx=10)

        strat = ttk.LabelFrame(cfg, text="Strategy (trend B → call, S → put)", padding=6)
        strat.pack(side="left", fill="both", expand=True, padx=(4, 0))
        s1, s2 = ttk.Frame(strat), ttk.Frame(strat)
        s1.pack(fill="x"), s2.pack(fill="x", pady=4)
        self._row(s1, "Target DTE", self.v["target_dte"], 4)
        self._row(s1, "Min", self.v["min_dte"], 3)
        self._row(s1, "Max", self.v["max_dte"], 3)
        self._row(s1, "Strikes OTM", self.v["strikes_otm"], 3)
        self._row(s2, "Take profit %", self.v["tp"], 4)
        self._row(s2, "Stop %", self.v["sl"], 4)
        ttk.Checkbutton(s2, text="Exit on opposite signal", variable=self.v["opp_exit"]).pack(side="left", padx=6)
        ttk.Checkbutton(s2, text="+ reversals", variable=self.v["reversals"]).pack(side="left")

        st = ttk.Frame(self, padding=(8, 6))
        st.pack(fill="x")
        ttk.Label(st, textvariable=self.v_status, style="H.TLabel").pack(side="left")

        cols = ("qty", "entry", "mark", "pnl", "priced")
        self.pos = ttk.Treeview(self, columns=cols, show="tree headings", height=4)
        self.pos.heading("#0", text="Position")
        self.pos.column("#0", width=260)
        for c, lbl, w in zip(cols, ("Qty", "Entry", "Mark", "P&L $", "Prices"), (50, 80, 80, 90, 90), strict=True):
            self.pos.heading(c, text=lbl)
            self.pos.column(c, width=w, anchor="e")
        self.pos.pack(fill="x", padx=8)

        self.text = tk.Text(self, height=14, wrap="word", bg=C["sunken"], fg=C["text"], relief="flat",
                            font=("Consolas", 9), padx=6, pady=4)
        self.text.pack(fill="both", expand=True, padx=8, pady=8)

    # ------------------------------------------------------------------ settings
    def _num(self, key: str, cast=float, lo=None, hi=None):
        try:
            x = cast(str(self.v[key].get()).strip())
        except ValueError:
            raise ValueError(f"'{key}' must be a number") from None
        if lo is not None and x < lo or hi is not None and x > hi:
            raise ValueError(f"'{key}' must be between {lo} and {hi}")
        return x

    def limits_and_params(self):
        syms = [s.upper() for s in str(self.v["symbols"].get()).replace(",", " ").split() if s.strip()]
        if not syms:
            raise ValueError("add at least one symbol")
        L = RiskLimits(allowed_symbols=syms, max_premium_per_trade=self._num("max_premium", float, 1, 1e6),
                       max_contracts_per_trade=self._num("max_contracts", int, 1, 100),
                       max_open_positions=self._num("max_open", int, 1, 20),
                       daily_loss_limit=self._num("daily_loss", float, 1, 1e6),
                       max_entries_per_day=self._num("max_entries", int, 1, 100))
        P = StrategyParams(target_dte=self._num("target_dte", int, 0, 60), min_dte=self._num("min_dte", int, 0, 60),
                           max_dte=self._num("max_dte", int, 0, 90), strikes_otm=self._num("strikes_otm", int, 0, 10),
                           take_profit_pct=self._num("tp", float, 1, 1000) / 100,
                           stop_loss_pct=self._num("sl", float, 1, 100) / 100,
                           exit_on_opposite_signal=bool(self.v["opp_exit"].get()),
                           use_reversal_signals=bool(self.v["reversals"].get()))
        if not P.min_dte <= P.target_dte <= P.max_dte:
            raise ValueError("DTE must satisfy Min <= Target <= Max")
        return L, P

    def save(self) -> None:
        self.app.settings.trading = {k: v.get() for k, v in self.v.items()}
        unsaved = [k for k, var in self.v_secret.items() if not secret_set(k, var.get().strip()) and var.get().strip()]
        try:
            self.app.on_save()
        except Exception as e:
            log.warning(f"saving trading settings failed: {e}")
        self.log("Settings saved." + (f" (No keychain: {', '.join(unsaved)} kept for this session only.)" if unsaved else ""))

    # ------------------------------------------------------------------ brokers
    def _client(self, env: str):
        from .tasty import TastytradeClient
        sec, ref = SECRETS[env]
        return TastytradeClient(env, self.v_secret[sec].get(), self.v_secret[ref].get(),
                                account=str(self.v[f"account_{env}"].get()))

    def _make_broker(self, mode: str):
        from .broker import SimBroker, TastyBroker
        quote_client = None
        if self.v["prod_quotes"].get() and mode in ("sim", "sandbox"):
            quote_client = self._client("live")
        if mode == "sim":
            return SimBroker(quote_client=quote_client)    # local fills; real quotes if configured
        return TastyBroker(self._client(mode), quote_client=quote_client)

    def _data_provider(self):
        from ..providers import PROVIDERS, create
        s = self.app._collect()
        spec = PROVIDERS[s.provider]
        if spec.market != "equity":
            raise ValueError(f"The bot needs a STOCK data source for SPY/NVDA bars; '{spec.label}' is crypto. "
                             "Switch Source (e.g. Yahoo, Alpaca, Simulated) first.")
        return create(s.provider, s.creds), spec.label

    # ------------------------------------------------------------------ actions
    def log(self, msg: str) -> None:
        self.text.insert("end", msg.rstrip() + "\n")
        self.text.see("end")

    def on_test(self, env: str) -> None:
        self.log(f"Testing {env} connection…")

        def work():
            try:
                c = self._client(env)
                acct = c.ensure_account()
                bal = c.balances()
                chain = c.option_chain("SPY")
                first = chain[0] if chain else None
                msg = (f"✔ {env}: account …{acct[-4:]} | buying power "
                       f"{bal.get('derivative-buying-power', bal.get('equity-buying-power', '?'))} | SPY chain: "
                       f"{len(chain)} expirations"
                       + (f", first {first.expiry} with {len(first.strikes)} strikes" if first else
                          " (could not parse; please report)"))
            except Exception as e:
                msg = f"✖ {env}: {type(e).__name__}: {e}"
            self.app.events.put(("bot", msg))
        threading.Thread(target=work, daemon=True).start()

    def on_start(self) -> None:
        from .bot import TradingBot
        if self.bot and self.bot.thread and self.bot.thread.is_alive():
            self.log("Bot already running.")
            return
        mode = self.v["mode"].get()
        try:
            L, P = self.limits_and_params()
            data, data_label = self._data_provider()
            broker = self._make_broker(mode)
        except Exception as e:
            messagebox.showerror("Can't start bot", str(e), parent=self)
            return
        confirm = self._confirm_fn() if (self.v["confirm_each"].get() or mode == "live") else None
        if mode == "live":
            typed = simpledialog.askstring(
                "LIVE trading", f"This will place REAL orders with REAL money on your tastytrade account.\n"
                                f"Limits: ${L.max_premium_per_trade:,.0f}/trade, daily loss ${L.daily_loss_limit:,.0f}.\n"
                                f"Every order will still ask for your confirmation.\n\nType {LIVE_PHRASE} to continue:",
                parent=self)
            if (typed or "").strip().upper() != LIVE_PHRASE:
                self.log("LIVE start cancelled.")
                return
        self.save()
        try:
            self.bot = TradingBot(broker, data, L, P, self.app.events, mode=mode, confirm=confirm)
        except ValueError as e:
            messagebox.showerror("Can't start bot", str(e), parent=self)
            return
        self.bot.start()
        self.log(f"▶ Bot started: {broker.name}, data from {data_label}, symbols {L.allowed_symbols}")

    def on_stop(self) -> None:
        if self.bot:
            self.bot.stop()
            n = len(self.bot.positions)
            self.log("■ Bot stopped." + (f" {n} position(s) still open and NO LONGER MANAGED." if n else ""))

    def on_kill(self) -> None:
        if not self.bot:
            self.log("Kill switch: bot not running.")
            return
        if not messagebox.askyesno("KILL SWITCH", "Cancel all working orders, sell all open positions at the bid, "
                                                  "and halt trading for today?", parent=self, icon="warning"):
            return
        threading.Thread(target=self.bot.kill, daemon=True).start()
        self.log("■ KILL SWITCH engaged")

    def _confirm_fn(self):
        events = self.app.events

        def confirm(text: str) -> bool:     # runs on the BOT thread
            req = ConfirmRequest(text)
            events.put(("bot_confirm", req))
            if not req.done.wait(CONFIRM_TIMEOUT_S):
                return False
            return req.answer
        return confirm

    # ------------------------------------------------------------------ events from the bot (GUI thread)
    def handle(self, kind: str, payload: Any) -> None:
        if kind == "bot":
            self.log(payload)
        elif kind == "bot_status":
            self._show_status(payload)
        elif kind == "bot_confirm":
            self.deiconify()
            self.lift()
            payload.answer = messagebox.askyesno("Confirm order", payload.text + "\n\nSend this order?",
                                                 parent=self)
            payload.done.set()

    def _show_status(self, s) -> None:
        state = "RUNNING" if s.running else "stopped"
        halt = f" · HALTED ({s.halt_reason})" if s.halted else ""
        self.v_status.set(f"{s.mode.upper()} · {state}{halt} · realized ${s.realized_today:,.2f} · "
                          f"open ${s.open_pnl:,.2f} · entries today {s.entries_today}")
        self.pos.delete(*self.pos.get_children())
        for p in s.positions:
            self.pos.insert("", "end", text=p["label"],
                            values=(p["qty"], f"{p['entry']:.2f}", f"{p['mark']:.2f}", f"{p['pnl']:,.2f}",
                                    "modeled" if p["modeled"] else "real"))

    def open_positions(self) -> int:
        return len(self.bot.positions) if self.bot else 0


def trading_available() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("quantecho.trading.bot") is not None
    except (ImportError, ValueError):
        return False
