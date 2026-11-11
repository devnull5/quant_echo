"""Thread-safe store of latest quotes, bars and TA; renders the compact LLM context block."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .models import Quote
from .ta import TA, compute_ta

NY = ZoneInfo("America/New_York")
RTH_OPEN, RTH_CLOSE = dtime(9, 30), dtime(16, 0)


@dataclass
class Snapshot:
    symbol: str
    quote: Optional[Quote] = None
    bars: List[Dict[str, Any]] = field(default_factory=list)
    ta: TA = field(default_factory=TA)
    signals: List[str] = field(default_factory=list)  # recent signal lines, oldest first
    clipped: int = 0  # bars whose volume was capped as a feed glitch
    market: str = "equity"  # "equity" | "crypto"
    bars_at_ms: int = 0     # when these bars were fetched (ticks after this are replayed on top)


def _fmt(x: Optional[float], nd: int = 2) -> str:
    return f"{x:,.{nd}f}" if isinstance(x, (int, float)) else "n/a"


def _ny(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(NY)


def is_rth(ms: int) -> bool:
    t = _ny(ms).time()
    return RTH_OPEN <= t < RTH_CLOSE


def session_phase(ms: int) -> str:
    t = _ny(ms).time()
    if t < RTH_OPEN:
        return "pre-market"
    if t < RTH_CLOSE:
        return "regular session"
    return "after-hours"


def session_bars(bars: List[Dict[str, Any]], market: str = "equity") -> List[Dict[str, Any]]:
    """Bars of the current 'session': US regular hours for equities, the UTC day of the latest bar for
    24/7 crypto markets."""
    if market == "crypto":
        if not bars:
            return []
        day = datetime.fromtimestamp(bars[-1]["t"] / 1000, tz=timezone.utc).date()
        return [b for b in bars if datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).date() == day]
    return [b for b in bars if is_rth(b["t"])]


def clip_volume_outliers(bars: List[Dict[str, Any]], factor: float = 15.0, market: str = "equity") -> tuple:
    """Cap any single bar's volume at `factor` x the median non-zero regular-session volume
    (pre-market volume is tiny and would make the normal opening surge look like an outlier).

    Some feeds (notably Yahoo 1-min) occasionally report one bar with 10-20x normal volume at an
    unremarkable price; left alone it drags VWAP and wrecks relative-volume readings.
    Returns (clean_bars, n_clipped). Input is not mutated.
    """
    def positive(rows):
        return sorted(v for v in (b.get("volume") for b in rows) if isinstance(v, (int, float)) and v > 0)

    vols = positive(session_bars(bars, market))
    if len(vols) < 20:
        vols = positive(bars)
    if len(vols) < 20:
        return list(bars), 0
    cap = vols[len(vols) // 2] * factor
    out, n = [], 0
    for b in bars:
        v = b.get("volume")
        if isinstance(v, (int, float)) and v > cap:
            b = {**b, "volume": cap}
            n += 1
        out.append(b)
    return out, n


def resample(bars: List[Dict[str, Any]], minutes: int = 5) -> List[Dict[str, Any]]:
    """Aggregate 1-min bars into N-min bars aligned to the clock."""
    out: List[Dict[str, Any]] = []
    step = minutes * 60_000
    for b in bars:
        if b.get("close") is None:
            continue
        bucket = b["t"] - b["t"] % step
        if out and out[-1]["t"] == bucket:
            o = out[-1]
            o["high"] = max(o["high"], b.get("high") or b["close"])
            o["low"] = min(o["low"], b.get("low") or b["close"])
            o["close"] = b["close"]
            o["volume"] += b.get("volume") or 0
        else:
            out.append({"t": bucket, "open": b.get("open") or b["close"], "high": b.get("high") or b["close"],
                        "low": b.get("low") or b["close"], "close": b["close"], "volume": b.get("volume") or 0})
    return out


def rsi_zone(r: float) -> str:
    if r >= 70:
        return "overbought (>=70)"
    if r <= 30:
        return "oversold (<=30)"
    if r < 45:
        return "weak/bearish-leaning but NOT oversold (30-45)"
    if r <= 55:
        return "neutral (45-55)"
    return "strong/bullish-leaning but NOT overbought (55-70)"


def structure_5m(b5: List[Dict[str, Any]]) -> Optional[str]:
    if len(b5) < 3:
        return None
    a, b, c = b5[-3:]
    hh = a["high"] < b["high"] < c["high"]
    hl = a["low"] < b["low"] < c["low"]
    lh = a["high"] > b["high"] > c["high"]
    ll = a["low"] > b["low"] > c["low"]
    if hh and hl:
        return "last three 5-min bars: higher highs AND higher lows (up-structure)"
    if lh and ll:
        return "last three 5-min bars: lower highs AND lower lows (down-structure)"
    return "last three 5-min bars: mixed / overlapping (no clear structure)"


def compute_facts(last: Optional[float], ta: TA, bars: List[Dict[str, Any]], rel_vol: Optional[float],
                  market: str = "equity") -> List[str]:
    """Deterministic, plain-English readings so the LLM interprets rather than computes."""
    if last is None:
        return []
    out: List[str] = []

    def vs(name: str, level: Optional[float]) -> None:
        if level:
            d = last - level
            out.append(f"Price is {'ABOVE' if d >= 0 else 'BELOW'} {name} by {abs(d):.2f} ({d / level:+.2%})")

    vs("VWAP", ta.vwap)
    vs("EMA9", ta.ema9)
    vs("EMA20", ta.ema20)
    if ta.ema9 and ta.ema20:
        out.append(f"EMA9 is {'above' if ta.ema9 >= ta.ema20 else 'below'} EMA20 → short-term trend "
                   f"{'up' if ta.ema9 >= ta.ema20 else 'down'}")
    if ta.rsi14 is not None:
        out.append(f"RSI14 {ta.rsi14:.1f} = {rsi_zone(ta.rsi14)}")
    if ta.macd is not None and ta.macd_signal is not None:
        out.append(f"MACD is {'above' if ta.macd >= ta.macd_signal else 'below'} its signal line; histogram "
                   f"{'positive' if (ta.macd_hist or 0) >= 0 else 'negative'}")
    rth = session_bars(bars, market)
    if rth:
        hi = max(b.get("high") or b["close"] for b in rth)
        lo = min(b.get("low") or b["close"] for b in rth)
        out.append(f"Distance to {'UTC-day' if market == 'crypto' else 'session'} high {hi:.2f}: {hi - last:+.2f} ({(hi - last) / last:+.2%}); "
                   f"to session low {lo:.2f}: {lo - last:+.2f} ({(lo - last) / last:+.2%})")
    major = {"VWAP": ta.vwap, "EMA9": ta.ema9, "EMA20": ta.ema20}
    if rth:
        major["day high"], major["day low"] = hi, lo
    b5 = resample(bars, 5)[-13:-1]  # last hour of completed 5-min bars
    swing = {}
    if b5:
        h = max(b5, key=lambda b: b["high"])
        lo5 = min(b5, key=lambda b: b["low"])
        swing[f"1h swing high ({_ny(h['t']):%H:%M})"] = h["high"]
        swing[f"1h swing low ({_ny(lo5['t']):%H:%M})"] = lo5["low"]

    def side(pred, reverse):
        cands = sorted(((v, k) for k, v in {**major, **swing}.items() if v and pred(v)), reverse=reverse)
        picked: List[tuple] = []
        for v, k in cands:  # merge levels within 0.05% of each other
            if picked and abs(v - picked[-1][0]) / last < 0.0005:
                picked[-1] = (picked[-1][0], picked[-1][1] + " / " + k)
            else:
                picked.append((v, k))
        return picked[:4]

    below = side(lambda v: v < last, True)
    above = side(lambda v: v > last, False)
    if below:
        out.append("Support candidates (BELOW price, nearest first): "
                   + ", ".join(f"{v:.2f} {k}" for v, k in below))
    if above:
        out.append("Resistance candidates (ABOVE price, nearest first): "
                   + ", ".join(f"{v:.2f} {k}" for v, k in above))
    if rel_vol is not None:
        label = "light" if rel_vol < 0.7 else "heavy" if rel_vol > 1.3 else "normal"
        out.append(f"Recent volume is {label} ({rel_vol:.2f}x)")
    s5 = structure_5m(resample(bars, 5))
    if s5:
        out.append(s5)
    return out


def render_context(snap: Snapshot, n5: int = 12) -> str:
    q, ta, bars = snap.quote, snap.ta, snap.bars
    last = q.last if q and q.last is not None else (bars[-1]["close"] if bars else None)
    now_ms = int(time.time() * 1000)
    crypto = snap.market == "crypto"
    lines = [f"SYMBOL: {snap.symbol}   (times US/Eastern, now {_ny(now_ms):%Y-%m-%d %H:%M})"]
    if crypto:
        lines.append("Market: crypto, trades 24/7. 'Day' = current UTC day; % change is vs 24 hours ago.")

    if q:
        prev = (q.last - q.net_change) if q.last is not None and q.net_change is not None else None
        age = f"{(now_ms - q.timestamp_ms) / 1000:.0f}s old" if q.timestamp_ms else "age n/a"
        ba = f"  Bid {_fmt(q.bid)} / Ask {_fmt(q.ask)}" if q.bid or q.ask else ""
        ref = "price 24h ago" if crypto else "prev close"
        lines.append(f"Last {_fmt(last)}  ({_fmt(q.net_pct)}% vs {ref} {_fmt(prev)}){ba}  [{age}]")
    if bars:
        if not crypto:
            lines.append(f"Phase of latest bar: {session_phase(bars[-1]['t'])} ({_ny(bars[-1]['t']):%Y-%m-%d %H:%M})")
        rth = session_bars(bars, snap.market)
        if rth:
            hi = max(b.get("high") or b["close"] for b in rth)
            lo = min(b.get("low") or b["close"] for b in rth)
            lines.append(f"{'UTC day' if crypto else 'Regular session'}: open {_fmt(rth[0].get('open'))}  "
                         f"high {_fmt(hi)}  low {_fmt(lo)}  ({len(rth)} min)")
            if last is not None and hi > lo:
                lines.append(f"Position in day range: {(last - lo) / (hi - lo):.0%} (0%=low, 100%=high)")

    def rel(level: Optional[float]) -> str:
        return f" ({(last - level) / level:+.2%})" if last is not None and level else ""

    lines.append(f"VWAP({'UTC day' if crypto else 'RTH'}) {_fmt(ta.vwap)}{rel(ta.vwap)}   EMA9 {_fmt(ta.ema9)}{rel(ta.ema9)}"
                 f"   EMA20 {_fmt(ta.ema20)}{rel(ta.ema20)}")
    lines.append(f"RSI14(1m) {_fmt(ta.rsi14, 1)}   MACD(1m) {_fmt(ta.macd, 3)} / signal {_fmt(ta.macd_signal, 3)}"
                 f" / hist {_fmt(ta.macd_hist, 3)}")

    rel_vol: Optional[float] = None
    if len(bars) >= 10:
        recent = [b.get("volume") or 0 for b in bars[-5:]]
        base = [b.get("volume") or 0 for b in bars[-65:-5]] or recent
        avg_base = sum(base) / len(base)
        if avg_base > 0:
            rel_vol = sum(recent) / 5 / avg_base
            lines.append(f"Relative volume (last 5 min vs prior hour avg): {rel_vol:.2f}x")

    facts = compute_facts(last, ta, bars, rel_vol, snap.market)
    if facts:
        lines.append("FACTS (computed by code — treat as correct, do not contradict):")
        lines += [f"  - {f}" for f in facts]
    if snap.clipped:
        lines.append(f"Data note: {snap.clipped} bar(s) with feed-glitch volume (>15x median) were capped "
                     "(data hygiene only, NOT a market signal).")
    if snap.signals:
        lines.append("Recent signals (oldest first):")
        lines += [f"  {x}" for x in snap.signals[-8:]]

    b5 = resample(bars, 5)[-n5:]
    if b5:
        lines.append(f"Last {len(b5)} x 5-min bars (oldest first): time  O / H / L / C  vol")
        for b in b5:
            lines.append(f"  {_ny(b['t']):%H:%M}  {_fmt(b['open'])} / {_fmt(b['high'])} / {_fmt(b['low'])}"
                         f" / {_fmt(b['close'])}  {int(b['volume']):,}")
    return "\n".join(lines)


class TelemetryStore:
    def __init__(self, market: str = "equity", clip_volume: bool = False) -> None:
        self._lock = threading.RLock()
        self._snaps: Dict[str, Snapshot] = {}
        self.market = market
        self.clip_volume = clip_volume

    def _get(self, sym: str) -> Snapshot:
        return self._snaps.setdefault(sym, Snapshot(sym, market=self.market))

    def update_quotes(self, quotes: Dict[str, Quote]) -> None:
        with self._lock:
            for sym, q in quotes.items():
                self._get(sym.upper()).quote = q

    def update_bars(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        bars, clipped = clip_volume_outliers(bars, market=self.market) if self.clip_volume else (list(bars), 0)
        rth = session_bars(bars, self.market)
        ta = compute_ta(bars, vwap_bars=rth or None)
        with self._lock:
            s = self._get(symbol.upper())
            s.bars, s.ta, s.clipped = list(bars), ta, clipped
            s.bars_at_ms = int(time.time() * 1000)

    def add_signals(self, symbol: str, lines: List[str], keep: int = 30) -> None:
        with self._lock:
            s = self._get(symbol.upper())
            s.signals = (s.signals + list(lines))[-keep:]

    def session_range(self, symbol: str) -> tuple:
        """(high, low) of the regular session so far, or (None, None)."""
        with self._lock:
            s = self._snaps.get(symbol.upper())
            rth = session_bars(s.bars if s else [], self.market)
        if not rth:
            return None, None
        return (max(b.get("high") or b["close"] for b in rth), min(b.get("low") or b["close"] for b in rth))

    def chart_data(self, symbol: str) -> tuple:
        """(bars, in_session flags, latest quote, bars_at_ms) for the chart; bars are a copy."""
        with self._lock:
            s = self._snaps.get(symbol.upper())
            bars = list(s.bars) if s else []
            q = s.quote if s else None
            at = s.bars_at_ms if s else 0
        session = {b["t"] for b in session_bars(bars, self.market)}
        return bars, [b["t"] in session for b in bars], q, at

    def update_tick(self, symbol: str, price: float, ts_ms: int, bid=None, ask=None, open_24h=None) -> None:
        """Fold a real-time tick into the symbol's quote (price, bid/ask, change vs reference)."""
        from dataclasses import replace
        with self._lock:
            s = self._get(symbol.upper())
            q = s.quote
            if q is None:
                s.quote = Quote(symbol.upper(), price, bid, ask, ts_ms, None)
                return
            ref = open_24h if open_24h else ((q.last - q.net_change) if q.last is not None and q.net_change is not None
                                             else None)
            s.quote = replace(q, last=price, bid=bid if bid is not None else q.bid,
                              ask=ask if ask is not None else q.ask, timestamp_ms=ts_ms,
                              net_change=(price - ref) if ref else q.net_change,
                              net_pct=((price - ref) / ref * 100) if ref else q.net_pct)

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
