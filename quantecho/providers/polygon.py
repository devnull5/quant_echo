"""Polygon.io (now Massive). Live snapshots need a paid Stocks plan; on a free key this
falls back to previous-day close for quotes, and minute bars may lag (free = delayed/EOD)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import NY, Field, Http, ProviderError, ProviderSpec, bar, latest_session, need, pct

BASE = "https://api.polygon.io"


def parse_snapshot(payload: Any) -> Dict[str, Quote]:
    out: Dict[str, Quote] = {}
    for t in (payload or {}).get("tickers") or []:
        if not isinstance(t, dict) or not t.get("ticker"):
            continue
        lt, lq = t.get("lastTrade") or {}, t.get("lastQuote") or {}
        day, mn, prev = t.get("day") or {}, t.get("min") or {}, t.get("prevDay") or {}
        last = coerce_float(lt.get("p")) or coerce_float(mn.get("c")) or coerce_float(day.get("c"))
        ts_ns = lt.get("t") or t.get("updated")
        sym = t["ticker"].upper()
        out[sym] = Quote(
            symbol=sym,
            last=last,
            bid=coerce_float(lq.get("p")),
            ask=coerce_float(lq.get("P")),
            timestamp_ms=int(ts_ns) // 1_000_000 if isinstance(ts_ns, (int, float)) else None,
            total_volume=coerce_float(day.get("v")),
            net_change=coerce_float(t.get("todaysChange")),
            net_pct=coerce_float(t.get("todaysChangePerc")) if t.get("todaysChangePerc") is not None
            else pct(last, coerce_float(prev.get("c"))),
        )
    return out


def parse_aggs(payload: Any) -> List[Dict[str, Any]]:
    return [bar(r.get("t"), r.get("o"), r.get("h"), r.get("l"), r.get("c"), r.get("v"))
            for r in (payload or {}).get("results") or [] if isinstance(r, dict)]


class PolygonProvider:
    def __init__(self, api_key: str, http: Optional[Http] = None):
        self.key = api_key
        self.http = http or Http(BASE, secrets=(api_key,))
        self._snapshot_ok = True

    def _p(self, **kw: Any) -> Dict[str, Any]:
        return {"apiKey": self.key, **kw}

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        syms = [s.upper() for s in symbols]
        if self._snapshot_ok:
            try:
                return parse_snapshot(self.http.get("v2/snapshot/locale/us/markets/stocks/tickers",
                                                    self._p(tickers=",".join(syms))))
            except ProviderError as e:
                if "HTTP 403" not in str(e):
                    raise
                self._snapshot_ok = False  # free plan: use previous close instead
        out: Dict[str, Quote] = {}
        for s in syms:
            rows = parse_aggs(self.http.get(f"v2/aggs/ticker/{s}/prev", self._p(adjusted="true")))
            if rows:
                r = rows[-1]
                out[s] = Quote(s, r["close"], None, None, r["t"], r["volume"])
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        today = datetime.now(NY).date()
        frm = (today - timedelta(days=5)).isoformat()
        p = self.http.get(f"v2/aggs/ticker/{symbol.upper()}/range/1/minute/{frm}/{today.isoformat()}",
                          self._p(adjusted="true", sort="asc", limit=50000))
        return latest_session(parse_aggs(p))


def _make(c: Mapping[str, str]) -> PolygonProvider:
    (k,) = need(c, "polygon_api_key")
    return PolygonProvider(k)


SPEC = ProviderSpec(
    id="polygon",
    label="Polygon.io / Massive",
    factory=_make,
    fields=(Field("polygon_api_key", "API Key", "POLYGON_API_KEY"),),
    notes="Live snapshots need a paid plan. Free key = 5 req/min, previous-close quotes + delayed minute bars.",
    min_interval=15,
    min_bars_every=120,
    signup_url="https://polygon.io",
)
