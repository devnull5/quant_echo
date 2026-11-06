"""Finnhub. Free key: real-time US quotes, 60 req/min. Intraday candles are a paid feature."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Field, Http, ProviderError, ProviderSpec, bar, latest_session, need

BASE = "https://finnhub.io/api/v1"


def parse_quote(sym: str, p: Any) -> Optional[Quote]:
    if not isinstance(p, dict) or not p.get("c"):
        return None  # Finnhub returns zeros for unknown symbols
    t = p.get("t")
    return Quote(
        symbol=sym.upper(),
        last=coerce_float(p.get("c")),
        bid=None,
        ask=None,
        timestamp_ms=int(t) * 1000 if isinstance(t, (int, float)) and t else None,
        total_volume=None,
        net_change=coerce_float(p.get("d")),
        net_pct=coerce_float(p.get("dp")),
    )


def parse_candles(p: Any) -> List[Dict[str, Any]]:
    if not isinstance(p, dict) or p.get("s") != "ok":
        return []
    cols = [p.get(k) or [] for k in ("o", "h", "l", "c", "v")]
    return [bar(int(t) * 1000, *(c[i] if i < len(c) else None for c in cols)) for i, t in enumerate(p.get("t") or [])]


class FinnhubProvider:
    def __init__(self, token: str, http: Optional[Http] = None):
        self.http = http or Http(BASE, headers={"X-Finnhub-Token": token}, secrets=(token,))
        self._candles_ok = True

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            q = parse_quote(s, self.http.get("quote", {"symbol": s.upper()}))
            if q:
                out[q.symbol] = q
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        if not self._candles_ok:
            return []
        now = int(time.time())
        try:
            p = self.http.get("stock/candle", {"symbol": symbol.upper(), "resolution": "1",
                                               "from": now - 5 * 86400, "to": now})
        except ProviderError as e:
            if "HTTP 403" in str(e) or "access" in str(e).lower():
                self._candles_ok = False
                raise ProviderError("Finnhub free plan has no intraday candles — quotes only, TA unavailable") from None
            raise
        return latest_session(parse_candles(p))


def _make(c: Mapping[str, str]) -> FinnhubProvider:
    (k,) = need(c, "finnhub_token")
    return FinnhubProvider(k)


SPEC = ProviderSpec(
    id="finnhub",
    label="Finnhub",
    factory=_make,
    fields=(Field("finnhub_token", "API Key", "FINNHUB_API_KEY"),),
    notes="Free key: real-time quotes (60/min). Minute candles need a paid plan, so TA stays empty on free.",
    min_interval=5,
    calls_per_symbol=1, max_calls_per_min=50,  # 60/min free; keep headroom
    min_bars_every=300,
    signup_url="https://finnhub.io",
)
