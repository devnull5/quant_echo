"""Deterministic intraday signal detection on the focus symbol.

Feed it the latest price + TA after each update; it emits human-readable events only on
*transitions* (e.g. price crosses VWAP), never repeats while the condition persists.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional

from .ta import TA


@dataclass
class Event:
    ts: float
    kind: str      # "bull" | "bear" | "info"
    text: str

    def line(self) -> str:
        return f"{time.strftime('%H:%M', time.localtime(self.ts))} {self.text}"


class SignalTracker:
    def __init__(self, symbol: str, rsi_hi: float = 70.0, rsi_lo: float = 30.0, keep: int = 30):
        self.symbol = symbol.upper()
        self.rsi_hi, self.rsi_lo = rsi_hi, rsi_lo
        self.state: Dict[str, Optional[str]] = {}
        self.day_hi: Optional[float] = None
        self.day_lo: Optional[float] = None
        self.events: Deque[Event] = deque(maxlen=keep)

    def _flip(self, key: str, new: Optional[str], now: float, msgs: Dict[str, tuple]) -> List[Event]:
        """Emit when `key` changes from one known state to another."""
        if new is None:
            return []
        old = self.state.get(key)
        self.state[key] = new
        if old is None or old == new:
            return []
        kind, text = msgs[new]
        return [Event(now, kind, text)]

    def update(self, last: Optional[float], ta: TA, session_hi: Optional[float] = None,
               session_lo: Optional[float] = None, now: Optional[float] = None) -> List[Event]:
        if last is None:
            return []
        now = time.time() if now is None else now
        s = self.symbol
        out: List[Event] = []

        if ta.vwap:
            out += self._flip("vwap", "above" if last >= ta.vwap else "below", now, {
                "above": ("bull", f"{s} crossed ABOVE VWAP {ta.vwap:.2f} (last {last:.2f})"),
                "below": ("bear", f"{s} crossed BELOW VWAP {ta.vwap:.2f} (last {last:.2f})"),
            })
        if ta.ema9 and ta.ema20:
            out += self._flip("ema", "bull" if ta.ema9 >= ta.ema20 else "bear", now, {
                "bull": ("bull", f"{s} EMA9 crossed above EMA20 ({ta.ema9:.2f} > {ta.ema20:.2f})"),
                "bear": ("bear", f"{s} EMA9 crossed below EMA20 ({ta.ema9:.2f} < {ta.ema20:.2f})"),
            })
        if ta.rsi14 is not None:
            zone = "hot" if ta.rsi14 >= self.rsi_hi else "cold" if ta.rsi14 <= self.rsi_lo else "mid"
            out += self._flip("rsi", zone, now, {
                "hot": ("bear", f"{s} RSI overbought {ta.rsi14:.0f}"),
                "cold": ("bull", f"{s} RSI oversold {ta.rsi14:.0f}"),
                "mid": ("info", f"{s} RSI back to neutral {ta.rsi14:.0f}"),
            })
        if ta.macd_hist is not None:
            out += self._flip("macd", "pos" if ta.macd_hist >= 0 else "neg", now, {
                "pos": ("bull", f"{s} MACD histogram turned positive"),
                "neg": ("bear", f"{s} MACD histogram turned negative"),
            })

        # new session extremes (only after we've seen the range once)
        if session_hi is not None and session_lo is not None:
            if self.day_hi is not None and session_hi > self.day_hi:
                out.append(Event(now, "bull", f"{s} new session HIGH {session_hi:.2f}"))
            if self.day_lo is not None and session_lo < self.day_lo:
                out.append(Event(now, "bear", f"{s} new session LOW {session_lo:.2f}"))
            # ratchet so a later, narrower range (e.g. bar refresh) can't re-trigger old extremes
            self.day_hi = session_hi if self.day_hi is None else max(self.day_hi, session_hi)
            self.day_lo = session_lo if self.day_lo is None else min(self.day_lo, session_lo)

        self.events.extend(out)
        return out

    def recent(self, n: int = 8) -> List[Event]:
        return list(self.events)[-n:]
