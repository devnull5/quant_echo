"""Twelve Data. Free key: 8 credits/min, 800/day (each symbol in a batch costs 1 credit)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import NY, Field, Http, ProviderError, ProviderSpec, bar, http_error, latest_session, need, parse_local

BASE = "https://api.twelvedata.com"


def _check(p: Any) -> Any:
    if isinstance(p, dict) and p.get("status") == "error":
        code = p.get("code")
        if isinstance(code, int) and code in (401, 403, 429):
            raise http_error(code, f"Twelve Data: {p.get('message', 'error')}", "60" if code == 429 else None)
        raise ProviderError(f"Twelve Data {code or ''}: {p.get('message', 'error')}")
    return p


def _one(q: Dict[str, Any]) -> Optional[Quote]:
    if not isinstance(q, dict) or q.get("status") == "error" or not q.get("symbol"):
        return None
    ts = q.get("last_quote_at") or q.get("timestamp")
    return Quote(
        symbol=q["symbol"].upper(),
        last=coerce_float(q.get("close")),
        bid=None,
        ask=None,
        timestamp_ms=int(ts) * 1000 if isinstance(ts, (int, float)) else None,
        total_volume=coerce_float(q.get("volume")),
        net_change=coerce_float(q.get("change")),
        net_pct=coerce_float(q.get("percent_change")),
    )


def parse_quotes(payload: Any) -> Dict[str, Quote]:
    _check(payload)
    rows = [payload] if isinstance(payload, dict) and "symbol" in payload else list((payload or {}).values())
    return {q.symbol: q for q in (_one(r) for r in rows) if q}


def parse_series(payload: Any) -> List[Dict[str, Any]]:
    _check(payload)
    return [bar(parse_local(v.get("datetime"), NY), v.get("open"), v.get("high"), v.get("low"), v.get("close"),
                v.get("volume"))
            for v in (payload or {}).get("values") or [] if isinstance(v, dict)]


class TwelveDataProvider:
    def __init__(self, key: str, http: Optional[Http] = None):
        self.http = http or Http(BASE, headers={"Authorization": f"apikey {key}"}, secrets=(key,))

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return parse_quotes(self.http.get("quote", {"symbol": ",".join(s.upper() for s in symbols)}))

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        p = self.http.get("time_series", {"symbol": symbol.upper(), "interval": "1min", "outputsize": 1000,
                                          "timezone": "America/New_York", "prepost": "true"})
        return latest_session(parse_series(p))


def _make(c: Mapping[str, str]) -> TwelveDataProvider:
    (k,) = need(c, "twelvedata_key")
    return TwelveDataProvider(k)


SPEC = ProviderSpec(
    id="twelvedata",
    label="Twelve Data",
    factory=_make,
    fields=(Field("twelvedata_key", "API Key", "TWELVEDATA_API_KEY"),),
    notes="Free key: 8 credits/min (1 per symbol). Keep the watchlist small or poll every 60s+.",
    min_interval=60,
    calls_per_symbol=1, max_calls_per_min=8,   # 1 credit per symbol, 8 credits/min free
    min_bars_every=180,
    signup_url="https://twelvedata.com",
)
