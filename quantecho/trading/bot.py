"""The trading engine: signals -> risk checks -> (confirm) -> orders -> position management -> journal.

`step()` is deterministic given its inputs (clock injectable), so the whole loop is unit-testable with the
SIM broker; `run()` wraps it in a background thread.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from ..chart import Signal, compute_series, find_signals
from ..confluence import ConfluenceRule, evaluate
from ..logsetup import log
from ..telemetry import is_rth
from .broker import Broker
from .occ import OptionContract
from .risk import DayState, RiskLimits, check_entry, in_entry_window, market_open, size_position
from .strategy import (StrategyParams, entry_limit, entry_side, exit_limit, exit_reason, fade_reason, pick_contract,
                       trail_armed, trail_reason)
from .tasty import new_external_id

NY = ZoneInfo("America/New_York")
JOURNAL_DIR = Path.home() / ".quanteco_gpt" / "trades"
ENTRY_TIMEOUT_S, EXIT_TIMEOUT_S = 30, 20
EXIT_BACKOFF_S = 30        # wait after a rejected/errored exit before trying again
CANCEL_RESEND_S = 15       # re-send a cancel the broker hasn't confirmed yet


@dataclass
class Position:
    contract: OptionContract
    qty: int
    entry_price: float
    opened: datetime
    modeled: bool
    mark: float = 0.0
    closing: bool = False
    urgent_exit: bool = False
    exit_why: str = ""                # why we first decided to sell (kept across retries)
    retry_at: float = 0.0             # monotonic time before which no new exit order is sent (backoff)
    peak: float = 0.0                 # best mark seen (for the trailing stop)
    trail_hits: int = 0               # consecutive checks past the trail (2 needed: one bad quote isn't enough)

    @property
    def pnl(self) -> float:
        return (self.mark - self.entry_price) * self.qty * 100

    def row(self) -> Dict[str, Any]:
        return {"symbol": self.contract.symbol, "label": self.contract.label(), "qty": self.qty,
                "entry": self.entry_price, "mark": self.mark, "pnl": round(self.pnl, 2), "modeled": self.modeled}


@dataclass
class Pending:
    kind: str                 # "entry" | "exit"
    contract: OptionContract
    qty: int
    limit: float
    order_id: str
    ext_id: str
    placed: float
    position: Optional[Position] = None
    cancel_sent: float = 0.0  # monotonic time we asked the broker to cancel (0 = not yet)


@dataclass
class BotStatus:
    mode: str = "off"
    running: bool = False
    halted: bool = False
    halt_reason: str = ""
    realized_today: float = 0.0
    open_pnl: float = 0.0
    entries_today: int = 0
    positions: List[Dict[str, Any]] = field(default_factory=list)
    last_event: str = ""


class TradingBot:
    def __init__(self, broker: Broker, data: Any, limits: RiskLimits, params: StrategyParams,
                 events: "queue.Queue[tuple]", *, mode: str = "sim",
                 confirm: Optional[Callable[[str], bool]] = None, journal_dir: Optional[Path] = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(NY), bars_every_s: float = 30.0):
        if broker.real_money and confirm is None:
            raise ValueError("LIVE trading requires per-order confirmation (confirm callback)")
        self.broker, self.data, self.limits, self.params = broker, data, limits, params
        self.events, self.mode, self.confirm, self.clock = events, mode, confirm, clock
        self.journal_dir = journal_dir if journal_dir is not None else JOURNAL_DIR   # resolved at runtime
        self.bars_every_s = bars_every_s
        self.state = DayState()
        self.positions: Dict[str, Position] = {}       # by underlying
        self.pending: Dict[str, Pending] = {}          # by ext_id
        self._bars: Dict[str, List[Dict[str, Any]]] = {}
        self._bars_at: Dict[str, float] = {}
        self._seen_signal_t: Dict[str, int] = {}
        self._joined: set = set()             # symbols already checked for 'join the trend at start'
        self._closed_note = None              # date we last journaled 'market closed'
        self._last_sig: Dict[str, Optional[Signal]] = {}
        self.rule = ConfluenceRule()
        self._confl_seen_t: Optional[int] = None
        self._stop = threading.Event()
        self._kill = threading.Event()
        self._lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------------- journal / events
    def journal(self, event: str, **kw: Any) -> None:
        now = self.clock()
        rec = {"ts": now.isoformat(timespec="seconds"), "mode": self.mode, "event": event, **kw}
        try:
            self.journal_dir.mkdir(parents=True, exist_ok=True)
            with (self.journal_dir / f"{now:%Y-%m-%d}.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except OSError as e:
            log.warning(f"trade journal write failed: {e}")
        shown = {k: v for k, v in kw.items() if k not in ("details", "limits", "params")}  # full detail stays in the file
        msg = f"[{now:%H:%M:%S}] 🤖 {event}: " + ", ".join(f"{k}={v}" for k, v in shown.items())
        log.info(msg)
        self.events.put(("bot", msg))

    def status(self) -> BotStatus:
        with self._lock:
            return BotStatus(self.mode, bool(self.thread and self.thread.is_alive()), self.state.halted,
                             self.state.halt_reason, round(self.state.realized_pnl, 2),
                             round(sum(p.pnl for p in self.positions.values()), 2), self.state.entries,
                             [p.row() for p in self.positions.values()])

    # ---------------------------------------------------------------- data
    def _refresh(self, sym: str) -> List[Dict[str, Any]]:
        if time.monotonic() - self._bars_at.get(sym, 0) >= self.bars_every_s or sym not in self._bars:
            self._bars[sym] = self.data.get_session_minute_bars(sym)
            self._bars_at[sym] = time.monotonic()
        return self._bars[sym]

    def _new_signal(self, sym: str, bars: List[Dict[str, Any]]) -> Optional[Signal]:
        """A signal on the bar that JUST closed (index len-2), reported once."""
        if len(bars) < 30:
            return None
        sess = [is_rth(b["t"]) for b in bars]
        sigs = find_signals(bars, compute_series(bars, sess))
        just_closed = len(bars) - 2
        for s in reversed(sigs):
            if s.i == just_closed:
                if self._seen_signal_t.get(sym) == bars[s.i]["t"]:
                    return None
                self._seen_signal_t[sym] = bars[s.i]["t"]
                return s
            if s.i < just_closed:
                break
        return None

    def _trend_in_place(self, sym: str, bars: List[Dict[str, Any]]) -> Optional[Signal]:
        """Once per symbol per run: if no crossover has fired yet but a trend is already established on the
        bar that just closed (EMA9 vs EMA20 and price vs VWAP agree), return a trend signal to join it."""
        if len(bars) < 30:
            return None
        self._joined.add(sym)
        i = len(bars) - 2
        s = compute_series(bars, [is_rth(b["t"]) for b in bars])
        e9, e20, vw, c = s.ema9[i], s.ema20[i], s.vwap[i], bars[i]["close"]
        if None in (e9, e20):
            return None
        if e9 > e20 and (vw is None or c > vw):
            return Signal(i, "buy", "trend", "already in an uptrend at start (EMA9 > EMA20, price above VWAP)")
        if e9 < e20 and (vw is None or c < vw):
            return Signal(i, "sell", "trend", "already in a downtrend at start (EMA9 < EMA20, price below VWAP)")
        return None

    def _new_confluence_signal(self) -> Optional[Signal]:
        """Confluence signal on the minute that JUST closed (all legs aligned), reported once."""
        series = {}
        for s in self.rule.symbols:
            try:
                series[s] = self._refresh(s)
            except Exception as e:
                self.journal("data_error", symbol=s, error=f"{type(e).__name__}: {e}")
                return None
        st = evaluate(series, self.rule)
        if not st.signals or st.signals[-1].i != len(st.timeline) - 1:
            return None
        t = st.timeline[-1]
        if t == self._confl_seen_t:
            return None
        self._confl_seen_t = t
        return st.signals[-1]

    # ---------------------------------------------------------------- main step
    def step(self) -> None:
        with self._lock:
            now = self.clock()
            self.state.roll(now.date().isoformat())
            if self._kill.is_set():
                self._kill_all()
                return
            self._manage_pending(now)
            confl = self.params.signal_source == "confluence"
            confl_sig = self._new_confluence_signal() if confl else None
            for sym in self.limits.allowed_symbols:
                try:
                    bars = self._refresh(sym)
                except Exception as e:
                    self.journal("data_error", symbol=sym, error=f"{type(e).__name__}: {e}")
                    continue
                if not bars:
                    continue
                spot = bars[-1]["close"]
                if confl:
                    sig = confl_sig if sym == self.rule.target else None   # only the target trades
                else:
                    sig = self._new_signal(sym, bars)
                    if sig:
                        self._joined.add(sym)          # a real crossover came first; don't also 'join'
                    elif (self.params.join_trend_on_start and sym not in self._joined
                          and in_entry_window(self.limits, now)):
                        sig = self._trend_in_place(sym, bars)
                if sig:
                    self._last_sig[sym] = sig
                    self.journal("signal", symbol=sym, side=sig.side, kind=sig.kind, reason=sig.reason, spot=spot)
                self._manage_position(sym, spot, now, sig)
                if sig and sym not in self.positions and not any(p.contract.root == sym for p in self.pending.values()):
                    self._try_entry(sym, spot, sig, now)
            open_pnl = sum(p.pnl for p in self.positions.values())
            if not self.state.halted and self.state.realized_pnl + open_pnl <= -abs(self.limits.daily_loss_limit):
                self.state.halted, self.state.halt_reason = True, f"daily loss limit ${self.limits.daily_loss_limit:,.0f}"
                self.journal("halt", reason=self.state.halt_reason, realized=self.state.realized_pnl, open=open_pnl)

    # ---------------------------------------------------------------- entries
    def _try_entry(self, sym: str, spot: float, sig: Signal, now: datetime) -> None:
        right = entry_side(sig, self.params)
        if right is None:
            self.journal("skip", symbol=sym, reason=f"{sig.kind} signals are off "
                         "(tick '+ reversals' to trade them)")
            return
        c = pick_contract(sym, self.broker.chain(sym, spot), spot, right, now.date(), self.params)
        if c is None:
            self.journal("skip", symbol=sym, reason="no expiry/strike in DTE window")
            return
        q = self.broker.quote(c, spot)
        price = self.broker.round_price(c, entry_limit(q.bid, q.ask), buy=True)
        open_syms = list(self.positions) + [p.contract.root for p in self.pending.values()]
        ok, why = check_entry(self.limits, self.state, sym, open_syms, price, q.spread_pct,
                              sum(p.pnl for p in self.positions.values()), now)
        if not ok:
            self.journal("blocked", symbol=sym, contract=c.label(), reasons="; ".join(why))
            return
        qty = size_position(self.limits, price)
        text = (f"BUY TO OPEN {qty}x {c.label()} limit {price:.2f} (${qty * price * 100:,.0f} max risk)"
                f"{' [modeled price]' if q.modeled else ''} on {sig.side.upper()} signal: {sig.reason}")
        if self.confirm and not self.confirm(text):
            self.journal("declined", symbol=sym, order=text)
            return
        ext = new_external_id()
        try:
            r = self.broker.submit(c, "Buy to Open", qty, price, ext)
        except Exception as e:
            self.journal("order_error", symbol=sym, order=text, error=f"{type(e).__name__}: {e}")
            return
        self.state.entries += 1
        self.journal("entry_order", symbol=sym, order=text, ext_id=ext, status=r.status, order_id=r.order_id,
                     message=r.message)
        if r.status == "filled":
            self._opened(c, qty, r.fill_price or price, q.modeled, now)
        elif r.status == "working":
            self.pending[ext] = Pending("entry", c, qty, price, r.order_id, ext, time.monotonic())

    def _opened(self, c: OptionContract, qty: int, fill: float, modeled: bool, now: datetime) -> None:
        self.positions[c.root] = Position(c, qty, fill, now, modeled, mark=fill)
        self.journal("filled_entry", contract=c.label(), qty=qty, price=fill, cost=round(qty * fill * 100, 2))

    # ---------------------------------------------------------------- exits / management
    def _manage_position(self, sym: str, spot: float, now: datetime, sig: Optional[Signal]) -> None:
        p = self.positions.get(sym)
        if not p or p.closing:
            return
        q = self.broker.quote(p.contract, spot)
        p.mark = (q.bid + q.ask) / 2
        p.peak = max(p.peak, p.mark)
        if not market_open(now):
            if self._closed_note != now.date():
                self._closed_note = now.date()
                self.journal("market_closed", note="no orders outside 09:30-16:00 ET; holding until the next open",
                             holding=[x.contract.label() for x in self.positions.values()])
            return
        if time.monotonic() < p.retry_at:
            return
        if p.urgent_exit:
            why = f"retry at bid: {p.exit_why or 'exit'}"
        else:
            why = exit_reason(p.entry_price, p.mark, p.contract, p.opened, now, sig, self.params) \
                or self._dynamic_exit(p, sym)
        if why:
            self._exit(p, q, why, urgent=p.urgent_exit or why.startswith(("stop", "flatten", "kill")))

    def _dynamic_exit(self, p: Position, sym: str) -> Optional[str]:
        """Trailing stop on the premium (confirmed on 2 consecutive checks) and, once it's armed,
        a momentum-fade exit on the underlying's last closed bar."""
        trail = trail_reason(p.entry_price, p.mark, p.peak, self.params)
        p.trail_hits = p.trail_hits + 1 if trail else 0
        if trail and p.trail_hits >= 2:
            return trail
        if self.params.fade_exit and trail_armed(p.entry_price, p.peak, self.params):
            bars = self._bars.get(sym) or []
            if len(bars) >= 30:
                i = len(bars) - 2                                   # last CLOSED bar
                s = compute_series(bars, [is_rth(b["t"]) for b in bars])
                return fade_reason(p.contract, bars[i]["close"], s.ema9[i])
        return None

    def _exit(self, p: Position, q, why: str, urgent: bool) -> None:
        price = self.broker.round_price(p.contract, exit_limit(q.bid, q.ask, urgent), buy=False)
        p.exit_why = p.exit_why or why
        ext = new_external_id()
        try:
            r = self.broker.submit(p.contract, "Sell to Close", p.qty, price, ext)
        except Exception as e:
            p.retry_at = time.monotonic() + EXIT_BACKOFF_S          # don't hammer the broker
            self.journal("order_error", contract=p.contract.label(), action="exit", error=f"{type(e).__name__}: {e}",
                         retry_in_s=EXIT_BACKOFF_S)
            return
        p.closing = True
        self.journal("exit_order", contract=p.contract.label(), reason=why, limit=price, status=r.status, ext_id=ext)
        if r.status == "filled":
            self._closed(p, r.fill_price or price, why)
        elif r.status == "working":
            self.pending[ext] = Pending("exit", p.contract, p.qty, price, r.order_id, ext, time.monotonic(), p)
        else:
            p.closing = False

    def _closed(self, p: Position, fill: float, why: str) -> None:
        pnl = round((fill - p.entry_price) * p.qty * 100, 2)
        self.state.realized_pnl += pnl
        self.positions.pop(p.contract.root, None)
        self.journal("filled_exit", contract=p.contract.label(), qty=p.qty, entry=p.entry_price, exit=fill, pnl=pnl,
                     reason=why)

    def _manage_pending(self, now: datetime) -> None:
        for ext, pd in list(self.pending.items()):
            spot = (self._bars.get(pd.contract.root) or [{"close": 0}])[-1]["close"]
            r = self.broker.status(pd.order_id, pd.contract, spot)
            if r.status == "filled":
                self.pending.pop(ext)
                if pd.kind == "entry":
                    self._opened(pd.contract, pd.qty, r.fill_price or pd.limit, True, now)
                else:
                    self._closed(pd.position, r.fill_price or pd.limit, "exit filled")
            elif r.status in ("rejected", "cancelled"):
                self.pending.pop(ext)
                self.journal(f"{pd.kind}_{r.status}", contract=pd.contract.label(), message=r.message)
                if pd.position:
                    pd.position.closing = False
                    if pd.cancel_sent:                       # our timeout: re-price at the bid next step
                        pd.position.urgent_exit = True
                    else:                                    # broker rejected it: back off before retrying
                        pd.position.retry_at = time.monotonic() + EXIT_BACKOFF_S
            elif pd.cancel_sent:
                # Cancel is asynchronous at the broker: wait until it's confirmed (or filled) before re-pricing,
                # otherwise the new order is rejected ("already have a closing order") or we double up.
                if time.monotonic() - pd.cancel_sent > CANCEL_RESEND_S:
                    self.broker.cancel(pd.order_id)
                    pd.cancel_sent = time.monotonic()
            elif (time.monotonic() - pd.placed > (ENTRY_TIMEOUT_S if pd.kind == "entry" else EXIT_TIMEOUT_S)
                  or not market_open(now)):
                self.broker.cancel(pd.order_id)
                pd.cancel_sent = time.monotonic()
                self.journal(f"{pd.kind}_timeout_cancel_requested", contract=pd.contract.label(), limit=pd.limit)

    # ---------------------------------------------------------------- kill switch
    def kill(self) -> None:
        """Cancel working orders, close all positions at the bid, halt new entries for the day."""
        self._kill.set()
        self.step()

    def _kill_all(self) -> None:
        for ext, pd in list(self.pending.items()):
            self.broker.cancel(pd.order_id)
            self.pending.pop(ext)
        self.state.halted, self.state.halt_reason = True, "kill switch"
        for sym, p in list(self.positions.items()):
            if not p.closing:
                spot = (self._bars.get(sym) or [{"close": 0}])[-1]["close"]
                p.urgent_exit = True
                self._exit(p, self.broker.quote(p.contract, spot), "kill switch", urgent=True)
        self.journal("kill_switch", open_positions=len(self.positions))
        self._kill.clear()

    # ---------------------------------------------------------------- thread
    def start(self, every_s: float = 5.0) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        L, P = self.limits, self.params
        self._adopt_positions()
        self.journal("bot_started", broker=self.broker.name, symbols=" ".join(L.allowed_symbols),
                     rules=(f"${L.max_premium_per_trade:,.0f}/trade, max {L.max_contracts_per_trade} contracts, "
                            f"{L.max_open_positions} open, daily loss ${L.daily_loss_limit:,.0f}; {P.target_dte} DTE "
                            f"({P.min_dte}-{P.max_dte}), TP +{P.take_profit_pct:.0%} / SL -{P.stop_loss_pct:.0%}"),
                     limits=L.to_dict(), params=P.to_dict())

        def run():
            while not self._stop.is_set():
                try:
                    self.step()
                except Exception as e:  # never die silently
                    log.exception("trading bot step failed")
                    self.journal("step_error", error=f"{type(e).__name__}: {e}")
                self.events.put(("bot_status", self.status()))
                self._stop.wait(every_s)
        self.thread = threading.Thread(target=run, daemon=True, name="quantecho-bot")
        self.thread.start()

    def _adopt_positions(self) -> None:
        """Pick up options already held at the broker (e.g. after a restart) so exits and the end-of-day
        flatten still manage them. Entry time is unknown, so it's treated as now."""
        try:
            held = self.broker.open_positions(self.limits.allowed_symbols)
        except Exception as e:
            self.journal("adopt_error", error=f"{type(e).__name__}: {e}")
            return
        now = self.clock()
        with self._lock:
            for c, qty, avg in held:
                if c.root in self.positions:
                    if self.positions[c.root].contract.symbol != c.symbol:
                        self.journal("adopt_skipped", contract=c.label(), reason=f"already managing a {c.root} position")
                    continue
                self.positions[c.root] = Position(c, qty, avg, now, False, mark=avg)
                self.journal("adopted", contract=c.label(), qty=qty, entry=avg)

    def stop(self) -> None:
        self._stop.set()
        self.journal("bot_stopped", open_positions=len(self.positions), pending=len(self.pending))

    def snapshot(self) -> Dict[str, Any]:
        return asdict(self.status())
