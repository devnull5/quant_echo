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


def apply_tick(bars: List[Dict[str, Any]], price: float, ts_ms: int, size: float = 0.0,
               max_gap_min: int = 5) -> List[Dict[str, Any]]:
    """Fold one trade/quote tick into 1-min candles: update the forming candle, or open the next one.
    Ticks older than the last candle are ignored; gaps > max_gap_min don't invent candles. Input untouched."""
    if not bars or price is None:
        return list(bars)
    out = list(bars)
    minute = (int(ts_ms) // 60_000) * 60_000
    last = out[-1]
    if minute == last["t"]:
        b = dict(last)
        b["high"] = max(b.get("high") or price, price)
        b["low"] = min(b.get("low") or price, price)
        b["close"] = price
        b["volume"] = (b.get("volume") or 0.0) + (size or 0.0)
        out[-1] = b
    elif last["t"] < minute <= last["t"] + max_gap_min * 60_000:
        o = last["close"]
        out.append({"t": minute, "open": o, "high": max(o, price), "low": min(o, price), "close": price,
                    "volume": size or 0.0})
    return out


# ================================================================ widget

C = {"bg": "#0c1016", "grid": "#1c2430", "text": "#9aa9bf", "up": "#3fb950", "down": "#f85149",
     "vwap": "#d29922", "ema9": "#58a6ff", "ema20": "#bc8cff", "cross": "#5b6b80", "last": "#dfe5ee"}

PAD_L, PAD_R, PAD_T, PAD_B = 8, 64, 22, 18
DEFAULT_SHOWN = 120


def _fmt_px(p: float) -> str:
    return f"{p:,.2f}" if p < 10_000 else f"{p:,.0f}"


class ChartCanvas(tk.Canvas):
    """Candles + volume + overlays + markers.

    Responsiveness: set_data() takes a cheap INCREMENTAL path when only the forming candle changed
    (moves ~10 canvas items, well under 1 ms) and a throttled FULL redraw otherwise (new bar, price outside
    the visible range, resize, zoom/pan). Mouse: wheel = zoom, drag = pan back in time, double-click = live.
    """

    def __init__(self, master, bars_shown: int = DEFAULT_SHOWN, max_fps: float = 30.0, **kw):
        super().__init__(master, bg=C["bg"], highlightthickness=0, **kw)
        self.bars_shown = bars_shown
        self.offset = 0                     # bars scrolled back from the latest (0 = following live)
        self.min_full_s = 0.2               # full redraws at most 5/s
        self.min_inc_s = 1.0 / max_fps      # incremental updates at most max_fps/s
        self.symbol = ""
        self.bars: List[Dict[str, Any]] = []
        self.series: Optional[Series] = None
        self.signals: List[Signal] = []
        self._in_session: Optional[List[bool]] = None
        self._last_full = self._last_inc = 0.0
        self._pending: Optional[str] = None
        self._geom: Dict[str, Any] = {}
        self._drag: Optional[tuple] = None
        self.stats = {"full": 0, "inc": 0}
        self.bind("<Configure>", lambda _e: self._full(force=True))
        self.bind("<Motion>", self._hover)
        self.bind("<Leave>", lambda _e: self.delete("hover"))
        self.bind("<MouseWheel>", self._wheel)                       # Windows / macOS
        self.bind("<Button-4>", lambda e: self._zoom(1 / 1.15, e))    # Linux wheel up
        self.bind("<Button-5>", lambda e: self._zoom(1.15, e))
        self.bind("<ButtonPress-1>", self._drag_start)
        self.bind("<B1-Motion>", self._drag_move)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag", None))
        self.bind("<Double-Button-1>", lambda _e: self.go_live())

    # -------------------------------------------------- data
    def set_data(self, symbol: str, bars: List[Dict[str, Any]], in_session: Optional[List[bool]] = None) -> None:
        bars = [b for b in bars if b.get("close") is not None]
        same_bars = (symbol == self.symbol and self.bars and len(bars) == len(self.bars)
                     and bars[-1]["t"] == self.bars[-1]["t"]
                     and (len(bars) < 2 or (bars[-2]["t"] == self.bars[-2]["t"]
                                            and bars[-2]["close"] == self.bars[-2]["close"])))
        self.symbol, self.bars, self._in_session = symbol, bars, in_session
        self.series = compute_series(bars, in_session) if bars else None
        if not same_bars:  # signals only change when a bar closes
            self.signals = find_signals(bars, self.series) if self.series and len(bars) > WARMUP else []
            self._full()
        else:
            self._incremental()

    def clear(self, message: str = "") -> None:
        self.bars, self.series, self.signals, self._geom = [], None, [], {}
        self.offset = 0
        self.delete("all")
        if message:
            self.create_text(self.winfo_width() // 2, self.winfo_height() // 2, text=message,
                             fill=C["text"], font=("Segoe UI", 10))

    def go_live(self) -> None:
        self.offset, self.bars_shown = 0, DEFAULT_SHOWN
        self._full(force=True)

    # -------------------------------------------------- scheduling
    def _full(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_full >= self.min_full_s:
            if self._pending:
                self.after_cancel(self._pending)
                self._pending = None
            self._last_full = self._last_inc = now
            self._draw()
            self.stats["full"] += 1
        elif not self._pending:
            delay = int((self.min_full_s - (now - self._last_full)) * 1000) + 1
            self._pending = self.after(delay, self._deferred_full)

    def _deferred_full(self) -> None:
        self._pending = None
        self._full(force=True)

    def redraw(self, force: bool = False) -> None:  # public alias kept for callers/tests
        self._full(force=force)

    def _incremental(self) -> None:
        g = self._geom
        if not g or self.offset or not self.bars or not self.series:
            return self._full()
        b = self.bars[-1]
        hi_b, lo_b = b.get("high") or b["close"], b.get("low") or b["close"]
        tails = [arr[-1] for arr in (self.series.vwap, self.series.ema9, self.series.ema20) if arr[-1] is not None]
        if max([hi_b] + tails) > g["hi"] or min([lo_b] + tails) < g["lo"] or (b.get("volume") or 0) > g["vmax"]:
            return self._full()                          # out of the visible range -> rescale
        now = time.monotonic()
        if now - self._last_inc < self.min_inc_s:
            if not self._pending:
                self._pending = self.after(int(self.min_inc_s * 1000) + 1, self._deferred_inc)
            return
        self._last_inc = now
        self._update_live_items()
        self.stats["inc"] += 1

    def _deferred_inc(self) -> None:
        self._pending = None
        self._last_inc = 0.0
        self._incremental()

    # -------------------------------------------------- geometry helpers
    def _y(self, p: float) -> float:
        g = self._geom
        return g["top"] + (g["hi"] - p) / (g["hi"] - g["lo"]) * (g["bot"] - g["top"])

    def _x(self, i: int) -> float:
        g = self._geom
        return g["x0"] + (i - g["n0"] + 0.5) * g["step"]

    # -------------------------------------------------- incremental update of the forming candle
    def _update_live_items(self) -> None:
        g, i, b = self._geom, len(self.bars) - 1, self.bars[-1]
        x, y, bw = self._x(i), self._y, g["bw"]
        o, c = b.get("open") or b["close"], b["close"]
        col = C["up"] if c >= o else C["down"]
        self.coords("live_wick", x, y(b.get("high") or c), x, y(b.get("low") or c))
        self.itemconfigure("live_wick", fill=col)
        self.coords("live_body", x - bw / 2, y(max(o, c)), x + bw / 2, max(y(min(o, c)), y(max(o, c)) + 1))
        self.itemconfigure("live_body", fill=col, outline=col)
        vb, vh = g["vb"], g["vol_h"]
        self.coords("live_vol", x - bw / 2, vb - (b.get("volume") or 0) / g["vmax"] * vh, x + bw / 2, vb)
        self.itemconfigure("live_vol", fill=col)
        for name, arr in (("vwap", self.series.vwap), ("ema9", self.series.ema9), ("ema20", self.series.ema20)):
            prev, cur = arr[i - 1] if i else None, arr[i]
            if prev is not None and cur is not None:
                self.coords(f"tail_{name}", self._x(i - 1), y(prev), x, y(cur))
        yy = y(c)
        self.coords("last_line", g["x0"], yy, g["x1"], yy)
        self.coords("last_box", g["x1"], yy - 8, g["W"], yy + 8)
        self.coords("last_text", g["x1"] + 4, yy)
        self.itemconfigure("last_text", text=_fmt_px(c))

    # -------------------------------------------------- full draw
    def _draw(self) -> None:
        self.delete("all")
        W, H = self.winfo_width(), self.winfo_height()
        self._geom = {}
        if W < 80 or H < 60:
            return
        if not self.bars or not self.series:
            self.create_text(W // 2, H // 2, text="Chart appears after ▶ Start (needs 1-minute bars)",
                             fill=C["text"], font=("Segoe UI", 10))
            return
        n = len(self.bars)
        self.bars_shown = max(20, min(self.bars_shown, max(20, n)))
        self.offset = max(0, min(self.offset, max(0, n - self.bars_shown)))
        end = n - self.offset
        n0 = max(0, end - self.bars_shown)
        vis = self.bars[n0:end]
        s = self.series
        vol_h = int((H - PAD_T - PAD_B) * 0.18)
        top, bot = PAD_T, H - PAD_B - vol_h - 6
        x0, x1 = PAD_L, W - PAD_R
        overlay = [v for arr in (s.vwap, s.ema9, s.ema20) for v in arr[n0:end] if v is not None]
        hi = max([b.get("high") or b["close"] for b in vis] + overlay)
        lo = min([b.get("low") or b["close"] for b in vis] + overlay)
        if hi <= lo:
            hi, lo = hi + 1, lo - 1
        span = hi - lo
        hi, lo = hi + span * 0.08, lo - span * 0.08          # headroom so live ticks rarely force a rescale
        step = (x1 - x0) / max(len(vis), 1)
        bw = max(1.0, step * 0.65)
        vmax = max((b.get("volume") or 0) for b in vis) or 1
        vmax *= 1.5                                         # headroom for the forming candle's volume
        vb = H - PAD_B
        self._geom = {"n0": n0, "end": end, "x0": x0, "x1": x1, "step": step, "bw": bw, "top": top, "bot": bot,
                      "hi": hi, "lo": lo, "W": W, "vmax": vmax, "vb": vb, "vol_h": vol_h}
        x, y = self._x, self._y
        live = self.offset == 0
        last_i = n - 1

        for k in range(6):                                   # grid + price axis
            p = lo + (hi - lo) * k / 5
            yy = y(p)
            self.create_line(x0, yy, x1, yy, fill=C["grid"])
            self.create_text(x1 + 4, yy, text=_fmt_px(p), anchor="w", fill=C["text"], font=("Consolas", 8))
        mod = 15 if len(vis) < 60 else 30 if len(vis) < 300 else 60
        for i in range(n0, end):                             # time axis
            t = datetime.fromtimestamp(self.bars[i]["t"] / 1000, tz=timezone.utc).astimezone()
            if t.minute % mod == 0:
                self.create_line(x(i), top, x(i), H - PAD_B, fill=C["grid"])
                self.create_text(x(i), H - PAD_B + 2, text=t.strftime("%H:%M"), anchor="n", fill=C["text"],
                                 font=("Consolas", 8))

        for i in range(n0, end):                             # volume + candles
            b = self.bars[i]
            o, c = b.get("open") or b["close"], b["close"]
            col = C["up"] if c >= o else C["down"]
            tag = ("live_vol",) if (live and i == last_i) else ()
            v = b.get("volume") or 0
            self.create_rectangle(x(i) - bw / 2, vb - v / vmax * vol_h, x(i) + bw / 2, vb, fill=col, outline="",
                                  stipple="gray50", tags=tag)
            wt, bt = (("live_wick",), ("live_body",)) if (live and i == last_i) else ((), ())
            self.create_line(x(i), y(b.get("high") or c), x(i), y(b.get("low") or c), fill=col, tags=wt)
            self.create_rectangle(x(i) - bw / 2, y(max(o, c)), x(i) + bw / 2, max(y(min(o, c)), y(max(o, c)) + 1),
                                  fill=col, outline=col, tags=bt)

        def line(arr, name, color, dash=None):
            stop = end - 1 if live else end               # the live tail is its own segment (cheap to move)
            pts = []
            for i in range(n0, stop):
                v = arr[i]
                if v is None:
                    if len(pts) >= 4:
                        self.create_line(*pts, fill=color, width=1.6, dash=dash)
                    pts = []
                else:
                    pts += [x(i), y(v)]
            if len(pts) >= 4:
                self.create_line(*pts, fill=color, width=1.6, dash=dash)
            if live and end - 2 >= n0 and arr[end - 2] is not None and arr[end - 1] is not None:
                self.create_line(x(end - 2), y(arr[end - 2]), x(end - 1), y(arr[end - 1]), fill=color, width=1.6,
                                 dash=dash, tags=(f"tail_{name}",))
        line(s.vwap, "vwap", C["vwap"], (4, 2))
        line(s.ema20, "ema20", C["ema20"])
        line(s.ema9, "ema9", C["ema9"])

        for sg in self.signals:                              # markers
            if not (n0 <= sg.i < end):
                continue
            b = self.bars[sg.i]
            if sg.side == "buy":
                yy = y(b.get("low") or b["close"]) + 12
                self.create_polygon(x(sg.i), yy - 8, x(sg.i) - 6, yy + 3, x(sg.i) + 6, yy + 3, fill=C["up"],
                                    outline=C["bg"], width=1.5)
                self.create_text(x(sg.i), yy + 5, text="B" if sg.kind == "trend" else "b", anchor="n",
                                 fill=C["up"], font=("Segoe UI", 8, "bold"))
            else:
                yy = y(b.get("high") or b["close"]) - 12
                self.create_polygon(x(sg.i), yy + 8, x(sg.i) - 6, yy - 3, x(sg.i) + 6, yy - 3, fill=C["down"],
                                    outline=C["bg"], width=1.5)
                self.create_text(x(sg.i), yy - 5, text="S" if sg.kind == "trend" else "s", anchor="s",
                                 fill=C["down"], font=("Segoe UI", 8, "bold"))

        lc = self.bars[-1]["close"]                          # last price (always the latest, even when panned)
        if lo <= lc <= hi:
            self.create_line(x0, y(lc), x1, y(lc), fill=C["last"], dash=(2, 3), tags=("last_line",))
            self.create_rectangle(x1, y(lc) - 8, W, y(lc) + 8, fill="#2a3443", outline="", tags=("last_box",))
            self.create_text(x1 + 4, y(lc), text=_fmt_px(lc), anchor="w", fill=C["last"],
                             font=("Consolas", 8, "bold"), tags=("last_text",))

        legend = [(self.symbol, C["last"]), ("VWAP", C["vwap"]), ("EMA9", C["ema9"]), ("EMA20", C["ema20"]),
                  ("▲B/▼S trend  ▲b/▼s reversal · signals, not advice", C["text"])]
        lx = x0 + 2
        for text, col in legend:
            t = self.create_text(lx, 4, text=text, anchor="nw", fill=col, font=("Segoe UI", 8, "bold"))
            lx = self.bbox(t)[2] + 10
        if not live:
            self.create_text(x1 - 4, top + 4, text=f"◀ {self.offset} bars back · double-click for LIVE",
                             anchor="ne", fill=C["vwap"], font=("Segoe UI", 8, "bold"))

    # -------------------------------------------------- zoom / pan
    def _wheel(self, e) -> None:
        self._zoom(1 / 1.15 if e.delta > 0 else 1.15, e)

    def _zoom(self, factor: float, _e=None) -> None:
        if not self.bars:
            return
        self.bars_shown = int(max(20, min(len(self.bars), 1440, round(self.bars_shown * factor))))
        self._full(force=True)

    def _drag_start(self, e) -> None:
        self._drag = (e.x, self.offset)

    def _drag_move(self, e) -> None:
        g = self._geom
        if not self._drag or not g:
            return
        x_start, off_start = self._drag
        shift = int(round((e.x - x_start) / max(g["step"], 1e-6)))
        new = max(0, min(off_start + shift, max(0, len(self.bars) - self.bars_shown)))
        if new != self.offset:
            self.offset = new
            self._full(force=True)

    # -------------------------------------------------- hover
    def _hover(self, e) -> None:
        self.delete("hover")
        g = self._geom
        if not g or not self.bars or self._drag:
            return
        i = int((e.x - g["x0"]) // g["step"]) + g["n0"]
        if not (g["n0"] <= i < g["end"]) or not (g["top"] <= e.y <= g["bot"]):
            return
        b = self.bars[i]
        xx = self._x(i)
        self.create_line(xx, g["top"], xx, g["bot"], fill=C["cross"], dash=(2, 2), tags="hover")
        self.create_line(g["x0"], e.y, g["x1"], e.y, fill=C["cross"], dash=(2, 2), tags="hover")
        t = datetime.fromtimestamp(b["t"] / 1000, tz=timezone.utc).astimezone()
        txt = (f"{t:%H:%M}  O {b.get('open') or 0:,.2f}  H {b.get('high') or 0:,.2f}  L {b.get('low') or 0:,.2f}  "
               f"C {b['close']:,.2f}  V {b.get('volume') or 0:,.4g}")
        sig = next((sg for sg in self.signals if sg.i == i), None)
        if sig:
            txt += f"\n{'▲ BUY' if sig.side == 'buy' else '▼ SELL'} ({sig.kind}): {sig.reason}"
        tid = self.create_text(g["x0"] + 4, g["top"] + 4, text=txt, anchor="nw", fill=C["last"],
                               font=("Consolas", 9), tags="hover")
        bx = self.bbox(tid)
        bg = self.create_rectangle(bx[0] - 3, bx[1] - 2, bx[2] + 3, bx[3] + 2, fill="#141922", outline=C["grid"],
                                   tags="hover")
        self.tag_lower(bg, tid)
