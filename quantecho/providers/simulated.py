"""Offline random-walk market for demos and UI testing. No network, no keys."""
from __future__ import annotations

import random
import time
from typing import Any, Dict, List

from ..models import Quote
from .base import ProviderSpec

_SEED_PX = {"SPY": 740.0, "QQQ": 620.0, "NVDA": 190.0, "AAPL": 260.0, "TSLA": 430.0, "MSFT": 510.0}


class SimulatedProvider:
    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.state: Dict[str, Dict[str, Any]] = {}

    def _sym(self, s: str) -> Dict[str, Any]:
        s = s.upper()
        if s not in self.state:
            px = _SEED_PX.get(s, self.rng.uniform(20, 400))
            now = int(time.time() // 60 * 60 * 1000)
            bars, p = [], px * self.rng.uniform(0.98, 1.02)
            for i in range(390, 0, -1):
                o = p
                p *= 1 + self.rng.gauss(0, 0.0007)
                hi, lo = max(o, p) * (1 + abs(self.rng.gauss(0, 0.0003))), min(o, p) * (1 - abs(self.rng.gauss(0, 0.0003)))
                bars.append({"t": now - i * 60_000, "open": o, "high": hi, "low": lo, "close": p,
                             "volume": float(self.rng.randint(20_000, 200_000))})
            self.state[s] = {"prev": px, "last": p, "bars": bars, "vol": sum(b["volume"] for b in bars)}
        return self.state[s]

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out = {}
        for s in symbols:
            st = self._sym(s)
            st["last"] *= 1 + self.rng.gauss(0, 0.0005)
            st["vol"] += self.rng.randint(1_000, 20_000)
            last, spread = st["last"], max(0.01, st["last"] * 0.0001)
            out[s.upper()] = Quote(s.upper(), round(last, 2), round(last - spread, 2), round(last + spread, 2),
                                   int(time.time() * 1000), st["vol"], last - st["prev"],
                                   (last - st["prev"]) / st["prev"] * 100)
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        st = self._sym(symbol)
        bars = st["bars"]
        now_min = int(time.time() // 60 * 60 * 1000)
        if bars[-1]["t"] < now_min:  # roll a new minute ending at the current price
            o = bars[-1]["close"]
            c = st["last"]
            bars.append({"t": now_min, "open": o, "high": max(o, c), "low": min(o, c), "close": c,
                         "volume": float(self.rng.randint(20_000, 200_000))})
        return list(bars)


SPEC = ProviderSpec(
    id="simulated",
    label="Simulated (offline demo)",
    factory=lambda c: SimulatedProvider(),
    notes="Fake random-walk prices for trying the app with no keys or internet. Not real data!",
    min_interval=1,
    min_bars_every=30,
)
