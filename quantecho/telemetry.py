"""Thread-safe store of latest quotes, bars and TA; renders the LLM context block."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Quote
from .ta import TA, compute_ta


@dataclass
class Snapshot:
    symbol: str
    quote: Optional[Quote] = None
    bars: List[Dict[str, Any]] = field(default_factory=list)
    ta: TA = field(default_factory=TA)


def _fmt(x: Optional[float], nd: int = 2) -> str:
    return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "n/a"


def _hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime("%H:%M")


def render_context(snap: Snapshot, max_bars: int = 30) -> str:
    q, ta = snap.quote, snap.ta
    stamp = "n/a"
    if q and q.timestamp_ms:
        stamp = datetime.fromtimestamp(q.timestamp_ms / 1000, tz=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S %Z"
        )
    last = q.last if q else None
    lines = [
        f"FOCUS: {snap.symbol}",
        f"Last {_fmt(last)}  Bid {_fmt(q.bid if q else None)}  Ask {_fmt(q.ask if q else None)}"
        f"  DayChg {_fmt(q.net_pct if q else None)}%  @ {stamp}",
        f"VWAP {_fmt(ta.vwap)}  RSI14 {_fmt(ta.rsi14)}  MACD {_fmt(ta.macd, 3)}"
        f"  Sig {_fmt(ta.macd_signal, 3)}  Hist {_fmt(ta.macd_hist, 3)}",
    ]
    if last is not None and ta.vwap:
        lines.append(f"Price vs VWAP: {(last - ta.vwap) / ta.vwap:+.2%}")
    if snap.bars:
        lows = [b["low"] for b in snap.bars if isinstance(b.get("low"), (int, float))]
        highs = [b["high"] for b in snap.bars if isinstance(b.get("high"), (int, float))]
        if lows and highs:
            lines.append(f"Session range {_fmt(min(lows))} - {_fmt(max(highs))} ({len(snap.bars)} x 1m bars)")
        lines.append("Recent 1m bars (newest last):")
        for b in snap.bars[-max_bars:]:
            if isinstance(b.get("close"), (int, float)) and isinstance(b.get("volume"), (int, float)):
                lines.append(f"  {_hhmm(b['t'])}  close={b['close']:.2f}  vol={int(b['volume'])}")
    return "\n".join(lines)


class TelemetryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snaps: Dict[str, Snapshot] = {}

    def _get(self, sym: str) -> Snapshot:
        return self._snaps.setdefault(sym, Snapshot(sym))

    def update_quotes(self, quotes: Dict[str, Quote]) -> None:
        with self._lock:
            for sym, q in quotes.items():
                self._get(sym.upper()).quote = q

    def update_bars(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        ta = compute_ta(bars)
        with self._lock:
            s = self._get(symbol.upper())
            s.bars, s.ta = list(bars), ta

    def quote(self, symbol: str) -> Optional[Quote]:
        with self._lock:
            s = self._snaps.get(symbol.upper())
            return s.quote if s else None

    def ta(self, symbol: str) -> TA:
        with self._lock:
            s = self._snaps.get(symbol.upper())
            return s.ta if s else TA()

    def context(self, symbol: str) -> Optional[str]:
        with self._lock:
            s = self._snaps.get(symbol.upper())
            if not s or (s.quote is None and not s.bars):
                return None
            return render_context(s)
