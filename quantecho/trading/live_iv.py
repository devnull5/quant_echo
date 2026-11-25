"""Implied volatility for MODELED option prices (SIM, and the sandbox, which has no market data).

A fixed IV is badly wrong whenever the market is stressed: on 2026-11-25 the VIX was 30.7 while the model
assumed 16% for SPY, so modeled SPY puts came out at about a quarter of their real price. Instead, IV is read
from the CBOE volatility indices (public, via Yahoo) and refreshed every few minutes:

  SPY  <- max(VIX, VIX9D)            30-day / 9-day S&P 500 implied vol (short-dated stress shows in VIX9D)
  QQQ  <- VXN                        Nasdaq-100 implied vol
  NVDA <- max(45%, 1.8 x VXN)        no listed NVDA vol index; NVDA's IV typically runs ~1.6-2x VXN

These are index-level, at-the-money, ~30-day numbers: real quotes still differ (skew, 0-DTE, events).
For real prices, use the "LIVE credentials (read-only) for real option quotes" option.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from ..logsetup import log
from .pricing import DEFAULT_IV

INDEXES = ("^VIX", "^VIX9D", "^VXN")
NVDA_VXN_MULT = 1.8
REFRESH_S = 300.0


def ivs_from_indexes(ix: Dict[str, float]) -> Dict[str, float]:
    """Per-underlying IV (as a fraction) from index levels (in vol points, e.g. 30.7)."""
    out: Dict[str, float] = {}
    spx = [ix[k] for k in ("^VIX", "^VIX9D") if ix.get(k)]
    if spx:
        out["SPY"] = max(spx) / 100
    if ix.get("^VXN"):
        out["QQQ"] = ix["^VXN"] / 100
        out["NVDA"] = max(DEFAULT_IV["NVDA"], NVDA_VXN_MULT * ix["^VXN"] / 100)
    return out


def yahoo_index_levels() -> Dict[str, float]:
    from ..providers.yahoo import YahooProvider
    http = YahooProvider().http
    out = {}
    for sym in INDEXES:
        meta = http.get(f"chart/{sym}", {"interval": "1d", "range": "1d"})["chart"]["result"][0]["meta"]
        px = meta.get("regularMarketPrice")
        if px:
            out[sym] = float(px)
    return out


class LiveIV:
    """Dict-like `.get(root)` used by the brokers' model_quote. Refreshes in the background of the caller
    at most every REFRESH_S; on failure keeps the last good values, else falls back to DEFAULT_IV."""

    def __init__(self, fetch: Optional[Callable[[], Dict[str, float]]] = None,
                 refresh_s: float = REFRESH_S, clock: Callable[[], float] = time.monotonic):
        self.fetch, self.refresh_s, self.clock = fetch, refresh_s, clock   # fetch None -> yahoo_index_levels
        self.values: Dict[str, float] = {}
        self.levels: Dict[str, float] = {}
        self._at: Optional[float] = None
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        with self._lock:
            now = self.clock()
            if self._at is not None and now - self._at < self.refresh_s:
                return
            self._at = now
            try:
                levels = (self.fetch or yahoo_index_levels)()
                vals = ivs_from_indexes(levels)
            except Exception as e:
                log.warning(f"live IV refresh failed ({type(e).__name__}: {e}); "
                            f"using {'last values' if self.values else 'fixed defaults'}")
                return
            if vals:
                self.levels, self.values = levels, vals
                log.info("live IV: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(vals.items())))

    def get(self, root: str, default: Optional[float] = None) -> Optional[float]:
        self._refresh()
        return self.values.get(root.upper(), default)

    def snapshot(self) -> Dict[str, float]:
        self._refresh()
        return {k: round(v, 3) for k, v in self.values.items()}
