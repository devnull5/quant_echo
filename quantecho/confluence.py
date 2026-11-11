"""Intermarket confluence signals for one target (default NVDA).

A BUY on the target fires when, over the same lookback window of closed 1-minute bars:
  * every REQUIRED instrument moved UP          (default: NVDA, SPY, QQQ, YM)
  * every NON-ADVERSE instrument did NOT move UP (default: DXY, TNX; falling or stalling is fine)
SELL is the mirror image. The signal fires on the bar where confluence BEGINS (not on every bar it
persists), with a cooldown.

"Moved" is volatility-normalized per instrument: the N-bar return must exceed k x that instrument's own
typical N-bar move (stdev of 1-bar returns x sqrt(N)), with a small absolute floor, so NVDA's larger
swings and DXY's tiny ones are judged on the same footing.

Futures/indices on free feeds are 10-15 min delayed, so by default the legs use real-time ETF proxies:
YM ~ DIA, DXY ~ UUP, TNX ~ inverse of IEF (bond prices fall when yields rise).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from .chart import Signal

UP, DOWN, FLAT = 1, -1, 0


@dataclass(frozen=True)
class Leg:
    name: str            # what the user means, e.g. "YM"
    symbol: str          # what we actually fetch, e.g. "DIA"
    invert: bool = False  # True if the proxy moves opposite to the named instrument (TNX vs IEF)
    role: str = "required"  # "required" | "non_adverse" | "info"

    @property
    def label(self) -> str:
        if self.symbol == self.name:
            return self.name
        return f"{self.name}≈{'1/' if self.invert else ''}{self.symbol}"


DEFAULT_LEGS = (
    Leg("NVDA", "NVDA"),
    Leg("SPY", "SPY"),
    Leg("QQQ", "QQQ"),
    Leg("YM", "DIA"),
    Leg("DXY", "UUP", role="non_adverse"),
    Leg("TNX", "IEF", invert=True, role="non_adverse"),
)


@dataclass
class ConfluenceRule:
    target: str = "NVDA"
    legs: Sequence[Leg] = field(default_factory=lambda: DEFAULT_LEGS)
    lookback: int = 5            # bars
    k_sigma: float = 1.0         # move must exceed k x typical N-bar move
    min_move: float = 0.0002     # absolute floor (0.02%) so dead-flat tapes don't produce "moves"
    vol_window: int = 120        # bars used to estimate typical move
    cooldown: int = 5
    warmup: int = 30

    @property
    def symbols(self) -> List[str]:
        return [leg.symbol for leg in self.legs]


def _closes_by_t(bars: Sequence[Dict]) -> Dict[int, float]:
    return {b["t"]: float(b["close"]) for b in bars if b.get("close") is not None}


def aligned_timeline(series: Dict[str, Sequence[Dict]], symbols: Sequence[str]) -> List[int]:
    """Minutes (bar timestamps) present for EVERY symbol, ascending."""
    sets = [set(_closes_by_t(series.get(s, []))) for s in symbols]
    if not sets or any(not s for s in sets):
        return []
    return sorted(set.intersection(*sets))


def directions(closes: List[float], rule: ConfluenceRule) -> List[int]:
    """UP / DOWN / FLAT per bar using vol-normalized N-bar returns (FLAT during warm-up)."""
    n, N = len(closes), rule.lookback
    out = [FLAT] * n
    rets = [0.0] + [(closes[i] / closes[i - 1] - 1.0) if closes[i - 1] else 0.0 for i in range(1, n)]
    for i in range(max(N, 2), n):
        lo = max(1, i - rule.vol_window)
        window = rets[lo:i + 1]
        if len(window) < 10:
            continue
        mean = sum(window) / len(window)
        sd = math.sqrt(sum((r - mean) ** 2 for r in window) / (len(window) - 1))
        thr = max(rule.k_sigma * sd * math.sqrt(N), rule.min_move)
        base = closes[i - N]
        r = closes[i] / base - 1.0 if base else 0.0
        out[i] = UP if r >= thr else DOWN if r <= -thr else FLAT
    return out


@dataclass
class ConfluenceState:
    timeline: List[int]
    dirs: Dict[str, List[int]]        # by leg NAME, already inverted where needed
    signals: List[Signal]             # indices refer to `timeline`

    def latest(self) -> Dict[str, int]:
        return {k: (v[-1] if v else FLAT) for k, v in self.dirs.items()}


def _arrow(d: int) -> str:
    return "▲" if d == UP else "▼" if d == DOWN else "–"


def evaluate(series: Dict[str, Sequence[Dict]], rule: ConfluenceRule, closed_only: bool = True) -> ConfluenceState:
    tl = aligned_timeline(series, rule.symbols)
    if closed_only and tl:
        tl = tl[:-1]                              # the newest minute is still forming
    dirs: Dict[str, List[int]] = {}
    for leg in rule.legs:
        cl = _closes_by_t(series.get(leg.symbol, []))
        d = directions([cl[t] for t in tl], rule)
        dirs[leg.name] = [-x for x in d] if leg.invert else d
    req = [lg.name for lg in rule.legs if lg.role == "required"]
    na = [lg.name for lg in rule.legs if lg.role == "non_adverse"]

    def confl(i: int, side: int) -> bool:
        return all(dirs[n][i] == side for n in req) and all(dirs[n][i] != side for n in na)

    signals: List[Signal] = []
    last = {UP: -10**9, DOWN: -10**9}
    for i in range(max(rule.warmup, 1), len(tl)):
        for side, name in ((UP, "buy"), (DOWN, "sell")):
            if confl(i, side) and not confl(i - 1, side) and i - last[side] >= rule.cooldown:
                legs = "  ".join(f"{lg.name}{_arrow(dirs[lg.name][i])}" for lg in rule.legs)
                signals.append(Signal(i, name, "confluence", f"{name.upper()} confluence: {legs}"))
                last[side] = i
    return ConfluenceState(tl, dirs, signals)


def signals_on_target(state: ConfluenceState, target_bars: Sequence[Dict]) -> List[Signal]:
    """Re-index confluence signals onto the target symbol's own bar list (for its chart)."""
    pos = {b["t"]: i for i, b in enumerate(target_bars)}
    out = []
    for s in state.signals:
        t = state.timeline[s.i]
        if t in pos:
            out.append(Signal(pos[t], s.side, s.kind, s.reason))
    return out


def summary(state: ConfluenceState, rule: ConfluenceRule) -> str:
    cur = state.latest()
    return "  ".join(f"{lg.name} {_arrow(cur.get(lg.name, FLAT))}" for lg in rule.legs)
