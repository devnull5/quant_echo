"""Technical-analysis helpers: EMA, RSI(14, Wilder), MACD(12,26,9), VWAP."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class TA:
    vwap: Optional[float] = None
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    macd_hist: Optional[float] = None


def coerce_float(x: Any) -> Optional[float]:
    """Return a finite float, or None for bools/None/NaN/garbage."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def ema(values: Iterable[float], period: int) -> List[float]:
    vals = [v for v in (coerce_float(x) for x in values) if v is not None]
    if not vals:
        return []
    k = 2 / (period + 1)
    out: List[float] = []
    cur: Optional[float] = None
    for v in vals:
        cur = v if cur is None else (v - cur) * k + cur
        out.append(cur)
    return out


def rsi14(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd_12269(closes: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(closes) < 35:  # 26 + 9
        return None, None, None
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    line = [a - b for a, b in zip(ema12, ema26, strict=True)]
    sig = ema(line, 9)
    if not line or not sig:
        return None, None, None
    return line[-1], sig[-1], line[-1] - sig[-1]


def vwap_from_bars(bars: List[Dict[str, Any]]) -> Optional[float]:
    """Session VWAP using typical price (H+L+C)/3 when available, else close."""
    pv = vv = 0.0
    for b in bars:
        c = coerce_float(b.get("close"))
        v = coerce_float(b.get("volume"))
        if c is None or v is None or v <= 0:
            continue
        h, lo = coerce_float(b.get("high")), coerce_float(b.get("low"))
        px = (h + lo + c) / 3 if h is not None and lo is not None else c
        pv += px * v
        vv += v
    return pv / vv if vv > 0 else None


def compute_ta(bars: List[Dict[str, Any]]) -> TA:
    closes = [c for c in (coerce_float(b.get("close")) for b in bars) if c is not None]
    m, s, h = macd_12269(closes)
    return TA(vwap=vwap_from_bars(bars), rsi14=rsi14(closes), macd=m, macd_signal=s, macd_hist=h)
