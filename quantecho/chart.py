"""Live candlestick chart with VWAP / EMA overlays and rule-based buy/sell markers.

Pure functions (series + signals) are separate from the Tk widget so the rules are unit-testable.
Markers are TECHNICAL SIGNALS from fixed rules, not advice:

  ▲ BUY  (trend)     EMA9 crosses above EMA20 while price is above VWAP
  ▼ SELL (trend)     EMA9 crosses below EMA20 while price is below VWAP
  ▲ BUY  (reversal)  RSI14 climbs back above 30 (leaving oversold)
  ▼ SELL (reversal)  RSI14 drops back below 70 (leaving overbought)

A marker is only placed on a CLOSED bar, and the same direction is suppressed for COOLDOWN bars.
"""
from __future__ import annotations

import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

COOLDOWN = 5      # bars
WARMUP = 26       # bars before indicators are trusted


@dataclass
class Signal:
    i: int            # bar index
    side: str         # "buy" | "sell"
    kind: str         # "trend" | "reversal"
    reason: str


# ================================================================ series

def ema_series(values: Sequence[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    k, cur = 2 / (period + 1), None
    for v in values:
        cur = v if cur is None else (v - cur) * k + cur
        out.append(cur)
    return out


def vwap_series(bars: Sequence[Dict[str, Any]], in_session: Sequence[bool]) -> List[Optional[float]]:
    """Cumulative VWAP that starts at the first in-session bar; None before it."""
    out: List[Optional[float]] = []
    pv = vv = 0.0
    for b, ok in zip(bars, in_session, strict=True):
        if ok:
            c, v = b.get("close"), b.get("volume") or 0
            h, lo = b.get("high") or c, b.get("low") or c
            if c is not None and v > 0:
                pv += (h + lo + c) / 3 * v
                vv += v
        out.append(pv / vv if vv > 0 else None)
    return out


def rsi_series(closes: Sequence[float], period: int = 14) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    ag, al = sum(gains[:period]) / period, sum(losses[:period]) / period

    def rsi(g, lo):
        return 100.0 if lo == 0 and g > 0 else 50.0 if lo == 0 else 100 - 100 / (1 + g / lo)
    out[period] = rsi(ag, al)
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = rsi(ag, al)
    return out


@dataclass
class Series:
    ema9: List[Optional[float]]
    ema20: List[Optional[float]]
    vwap: List[Optional[float]]
    rsi: List[Optional[float]]


def compute_series(bars: Sequence[Dict[str, Any]], in_session: Optional[Sequence[bool]] = None) -> Series:
    closes = [float(b["close"]) for b in bars]
    sess = list(in_session) if in_session is not None else [True] * len(bars)
    return Series(ema_series(closes, 9), ema_series(closes, 20), vwap_series(bars, sess), rsi_series(closes))


# ================================================================ signals

def find_signals(bars: Sequence[Dict[str, Any]], s: Series, closed: Optional[int] = None) -> List[Signal]:
    """Rule-based markers on closed bars [WARMUP, closed). `closed` defaults to len(bars) - 1, i.e. the
    newest bar is treated as still forming and never gets a marker until it closes."""
    n = len(bars) if closed is None else closed
    n = min(n, len(bars) - 1) if closed is None else n
    out: List[Signal] = []
    last = {"buy": -10**9, "sell": -10**9}

    def add(i, side, kind, reason):
        if i - last[side] >= COOLDOWN:
            out.append(Signal(i, side, kind, reason))
            last[side] = i

    for i in range(max(WARMUP, 1), n):
        c = bars[i]["close"]
        e9p, e20p, e9, e20 = s.ema9[i - 1], s.ema20[i - 1], s.ema9[i], s.ema20[i]
        vw = s.vwap[i]
        if None not in (e9p, e20p, e9, e20):
            if e9p <= e20p and e9 > e20 and (vw is None or c > vw):
                add(i, "buy", "trend", f"EMA9 crossed above EMA20{' with price above VWAP' if vw else ''}")
            elif e9p >= e20p and e9 < e20 and (vw is None or c < vw):
                add(i, "sell", "trend", f"EMA9 crossed below EMA20{' with price below VWAP' if vw else ''}")
        rp, r = s.rsi[i - 1], s.rsi[i]
        if rp is not None and r is not None:
            if rp < 30 <= r:
                add(i, "buy", "reversal", f"RSI back above 30 ({r:.0f}) after oversold")
            elif rp > 70 >= r:
                add(i, "sell", "reversal", f"RSI back below 70 ({r:.0f}) after overbought")
    return out


def merge_live_quote(bars: List[Dict[str, Any]], last: Optional[float], ts_ms: Optional[int]) -> List[Dict[str, Any]]:
    """Fold the latest quote into the forming 1-min candle (or start a new one) so the chart moves between
    bar refreshes. Returns a new list; input untouched."""
    if not bars or last is None:
        return list(bars)
    out = list(bars)
    now_min = (int(ts_ms if ts_ms else time.time() * 1000) // 60_000) * 60_000
    b = dict(out[-1])
    if now_min > b["t"] and now_min - b["t"] <= 5 * 60_000:
        out.append({"t": now_min, "open": b["close"], "high": max(b["close"], last), "low": min(b["close"], last),
                    "close": last, "volume": 0.0})
    elif now_min == b["t"]:
        b["high"], b["low"], b["close"] = max(b.get("high") or last, last), min(b.get("low") or last, last), last
        out[-1] = b
    return out


# ================================================================ widget

C = {"bg": "#0c1016", "grid": "#1c2430", "text": "#9aa9bf", "up": "#3fb950", "down": "#f85149",
     "vwap": "#d29922", "ema9": "#58a6ff", "ema20": "#bc8cff", "cross": "#5b6b80", "last": "#dfe5ee"}


class ChartCanvas(tk.Canvas):
    """Candles + volume + overlays + markers. Call set_data(...) whenever data changes; it redraws at most
    every `min_redraw_s` and on resize. Hover shows OHLCV and any signal on that bar."""

    def __init__(self, master, bars_shown: int = 120, min_redraw_s: float = 0.5, **kw):
        super().__init__(master, bg=C["bg"], highlightthickness=0, **kw)
        self.bars_shown = bars_shown
        self.min_redraw_s = min_redraw_s
        self.symbol = ""
        self.bars: List[Dict[str, Any]] = []
        self.series: Optional[Series] = None
        self.signals: List[Signal] = []
        self._last_draw = 0.0
        self._pending: Optional[str] = None
        self._geom: Dict[str, Any] = {}
        self.bind("<Configure>", lambda _e: self.redraw(force=True))
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _e: self.delete("hover"))

    # -------------------------------------------------- data
    def set_data(self, symbol: str, bars: List[Dict[str, Any]], in_session: Optional[List[bool]] = None) -> None:
        self.symbol = symbol
        self.bars = [b for b in bars if b.get("close") is not None]
        self.series = compute_series(self.bars, in_session) if self.bars else None
        self.signals = find_signals(self.bars, self.series) if self.series and len(self.bars) > WARMUP else []
        self.redraw()

    def clear(self, message: str = "") -> None:
        self.bars, self.series, self.signals = [], None, []
        self.delete("all")
        if message:
            self.create_text(self.winfo_width() // 2, self.winfo_height() // 2, text=message,
                             fill=C["text"], font=("Segoe UI", 10))

    # -------------------------------------------------- drawing
    def redraw(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_draw < self.min_redraw_s:
            if not self._pending:
                self._pending = self.after(int(self.min_redraw_s * 1000), self._deferred)
            return
        self._last_draw = now
        self._draw()

    def _deferred(self) -> None:
        self._pending = None
        self.redraw(force=True)

    def _draw(self) -> None:
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        if W < 80 or H < 60:
            return
        if not self.bars or not self.series:
            self.create_text(W // 2, H // 2, text="Chart appears after ▶ Start (needs 1-minute bars)",
                             fill=C["text"], font=("Segoe UI", 10))
            return
        n0 = max(0, len(self.bars) - self.bars_shown)
        bars = self.bars[n0:]
        s = self.series
        pad_l, pad_r, pad_t, pad_b = 8, 64, 22, 18
        vol_h = int((H - pad_t - pad_b) * 0.18)
        top, bot = pad_t, H - pad_b - vol_h - 6
        x0, x1 = pad_l, W - pad_r
        overlay = [v for arr in (s.vwap, s.ema9, s.ema20) for v in arr[n0:] if v is not None]
        hi = max([b["high"] or b["close"] for b in bars] + overlay)
        lo = min([b["low"] or b["close"] for b in bars] + overlay)
        if hi <= lo:
            hi, lo = hi + 1, lo - 1
        span = hi - lo
        hi, lo = hi + span * 0.06, lo - span * 0.06
        step = (x1 - x0) / max(len(bars), 1)
        bw = max(1.0, step * 0.65)

        def y(p):
            return top + (hi - p) / (hi - lo) * (bot - top)

        def x(i):
            return x0 + (i - n0 + 0.5) * step

        self._geom = {"n0": n0, "x0": x0, "step": step, "top": top, "bot": bot, "hi": hi, "lo": lo, "W": W}

        # grid + price axis
        for k in range(6):
            p = lo + (hi - lo) * k / 5
            yy = y(p)
            self.create_line(x0, yy, x1, yy, fill=C["grid"])
            self.create_text(x1 + 4, yy, text=f"{p:,.2f}" if p < 10_000 else f"{p:,.0f}", anchor="w",
                             fill=C["text"], font=("Consolas", 8))
        # time axis: a label on each :00 / :30 bar (every 15 min when zoomed in to < 60 bars)
        mod = 15 if len(bars) < 60 else 30
        for i in range(n0, len(self.bars)):
            t = datetime.fromtimestamp(self.bars[i]["t"] / 1000, tz=timezone.utc).astimezone()
            if t.minute % mod == 0:
                self.create_line(x(i), top, x(i), H - pad_b, fill=C["grid"])
                self.create_text(x(i), H - pad_b + 2, text=t.strftime("%H:%M"), anchor="n", fill=C["text"],
                                 font=("Consolas", 8))
        # volume
        vmax = max((b.get("volume") or 0) for b in bars) or 1
        vb = H - pad_b
        for i, b in enumerate(bars, start=n0):
            v = b.get("volume") or 0
            col = C["up"] if b["close"] >= (b.get("open") or b["close"]) else C["down"]
            self.create_rectangle(x(i) - bw / 2, vb - v / vmax * vol_h, x(i) + bw / 2, vb, fill=col, outline="",
                                  stipple="gray50")
        # candles
        for i, b in enumerate(bars, start=n0):
            o, c = b.get("open") or b["close"], b["close"]
            col = C["up"] if c >= o else C["down"]
            self.create_line(x(i), y(b.get("high") or c), x(i), y(b.get("low") or c), fill=col)
            self.create_rectangle(x(i) - bw / 2, y(max(o, c)), x(i) + bw / 2, max(y(min(o, c)), y(max(o, c)) + 1),
                                  fill=col, outline=col)

        # overlays
        def line(arr, color, dash=None):
            pts = []
            for i in range(n0, len(self.bars)):
                v = arr[i]
                if v is None:
                    if len(pts) >= 4:
                        self.create_line(*pts, fill=color, width=1.6, dash=dash)
                    pts = []
                else:
                    pts += [x(i), y(v)]
            if len(pts) >= 4:
                self.create_line(*pts, fill=color, width=1.6, dash=dash, smooth=False)
        line(s.vwap, C["vwap"], (4, 2))
        line(s.ema20, C["ema20"])
        line(s.ema9, C["ema9"])

        # markers
        for sg in self.signals:
            if sg.i < n0:
                continue
            b = self.bars[sg.i]
            if sg.side == "buy":
                yy = y(b.get("low") or b["close"]) + 12
                self.create_polygon(x(sg.i), yy - 8, x(sg.i) - 6, yy + 3, x(sg.i) + 6, yy + 3, fill=C["up"], outline=C["bg"], width=1.5)
                self.create_text(x(sg.i), yy + 5, text="B" if sg.kind == "trend" else "b", anchor="n",
                                 fill=C["up"], font=("Segoe UI", 8, "bold"))
            else:
                yy = y(b.get("high") or b["close"]) - 12
                self.create_polygon(x(sg.i), yy + 8, x(sg.i) - 6, yy - 3, x(sg.i) + 6, yy - 3, fill=C["down"], outline=C["bg"], width=1.5)
                self.create_text(x(sg.i), yy - 5, text="S" if sg.kind == "trend" else "s", anchor="s",
                                 fill=C["down"], font=("Segoe UI", 8, "bold"))

        # last price tag
        lc = self.bars[-1]["close"]
        self.create_line(x0, y(lc), x1, y(lc), fill=C["last"], dash=(2, 3))
        self.create_rectangle(x1, y(lc) - 8, W, y(lc) + 8, fill="#2a3443", outline="")
        self.create_text(x1 + 4, y(lc), text=f"{lc:,.2f}" if lc < 10_000 else f"{lc:,.0f}", anchor="w",
                         fill=C["last"], font=("Consolas", 8, "bold"))

        # legend
        legend = [(self.symbol, C["last"]), ("VWAP", C["vwap"]), ("EMA9", C["ema9"]), ("EMA20", C["ema20"]),
                  ("▲B/▼S trend  ▲b/▼s reversal · signals, not advice", C["text"])]
        lx = x0 + 2
        for text, col in legend:
            t = self.create_text(lx, 4, text=text, anchor="nw", fill=col, font=("Segoe UI", 8, "bold"))
            lx = self.bbox(t)[2] + 10

    # -------------------------------------------------- hover
    def _hover(self, e) -> None:
        self.delete("hover")
        g = self._geom
        if not g or not self.bars:
            return
        i = int((e.x - g["x0"]) // g["step"]) + g["n0"]
        if not (g["n0"] <= i < len(self.bars)) or not (g["top"] <= e.y <= g["bot"]):
            return
        b = self.bars[i]
        xx = g["x0"] + (i - g["n0"] + 0.5) * g["step"]
        self.create_line(xx, g["top"], xx, g["bot"], fill=C["cross"], dash=(2, 2), tags="hover")
        self.create_line(g["x0"], e.y, g["W"] - 64, e.y, fill=C["cross"], dash=(2, 2), tags="hover")
        t = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).astimezone()
        txt = (f"{t:%H:%M}  O {b.get('open') or 0:,.2f}  H {b.get('high') or 0:,.2f}  L {b.get('low') or 0:,.2f}  "
               f"C {b['close']:,.2f}  V {int(b.get('volume') or 0):,}")
        sig = next((sg for sg in self.signals if sg.i == i), None)
        if sig:
            txt += f"\n{'▲ BUY' if sig.side == 'buy' else '▼ SELL'} ({sig.kind}): {sig.reason}"
        tid = self.create_text(g["x0"] + 4, g["top"] + 4, text=txt, anchor="nw", fill=C["last"],
                               font=("Consolas", 9), tags="hover")
        bx = self.bbox(tid)
        bg = self.create_rectangle(bx[0] - 3, bx[1] - 2, bx[2] + 3, bx[3] + 2, fill="#141922", outline=C["grid"],
                                   tags="hover")
        self.tag_lower(bg, tid)
