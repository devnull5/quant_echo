"""Alpha Vantage. Free key is 25 requests/DAY — good for an occasional check, not live polling."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from ..models import Quote
from ..ta import coerce_float
from .base import NY, Field, Http, ProviderError, ProviderSpec, RateLimited, bar, latest_session, need, parse_local

BASE = "https://www.alphavantage.co/query"


def _check(p: Any) -> Dict[str, Any]:
    if not isinstance(p, dict):
        raise ProviderError("Alpha Vantage: bad response")
    if "Error Message" in p:
        raise ProviderError(f"Alpha Vantage: {str(p['Error Message'])[:200]}")
    for k in ("Note", "Information"):  # AV signals quota exhaustion with HTTP 200 + one of these
        if k in p:
            raise RateLimited(f"Alpha Vantage: {str(p[k])[:200]}", retry_after=60.0)
    return p


def parse_global_quote(p: Any) -> Optional[Quote]:
    g = _check(p).get("Global Quote") or {}
    if not g.get("01. symbol"):
        return None
    pct_s = str(g.get("10. change percent", "")).rstrip("%")
    return Quote(
        symbol=g["01. symbol"].upper(),
        last=coerce_float(g.get("05. price")),
        bid=None,
        ask=None,
        timestamp_ms=None,
        total_volume=coerce_float(g.get("06. volume")),
        net_change=coerce_float(g.get("09. change")),
        net_pct=coerce_float(pct_s),
    )


def parse_intraday(p: Any) -> List[Dict[str, Any]]:
    p = _check(p)
    tz_name = (p.get("Meta Data") or {}).get("6. Time Zone") or "US/Eastern"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = NY
    series = p.get("Time Series (1min)") or {}
    return [bar(parse_local(ts, tz), v.get("1. open"), v.get("2. high"), v.get("3. low"), v.get("4. close"),
                v.get("5. volume"))
            for ts, v in series.items() if isinstance(v, dict)]


class AlphaVantageProvider:
    def __init__(self, key: str, http: Optional[Http] = None):
        self.key = key
        self.http = http or Http(BASE, secrets=(key,))

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for s in symbols:
            q = parse_global_quote(self.http.get("", {"function": "GLOBAL_QUOTE", "symbol": s.upper(),
                                                      "apikey": self.key}))
            if q:
                out[q.symbol] = q
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        p = self.http.get("", {"function": "TIME_SERIES_INTRADAY", "symbol": symbol.upper(), "interval": "1min",
                               "outputsize": "full", "extended_hours": "true", "apikey": self.key})
        return latest_session(parse_intraday(p))


def _make(c: Mapping[str, str]) -> AlphaVantageProvider:
    (k,) = need(c, "alphavantage_key")
    return AlphaVantageProvider(k)


SPEC = ProviderSpec(
    id="alphavantage",
    label="Alpha Vantage",
    factory=_make,
    fields=(Field("alphavantage_key", "API Key", "ALPHAVANTAGE_API_KEY"),),
    notes="Free key = 25 requests/day total. Use 1 symbol and a long interval, or a premium key.",
    min_interval=600,
    calls_per_symbol=1, max_calls_per_min=25 / 1440,  # 25 requests per DAY
    min_bars_every=1800,
    signup_url="https://www.alphavantage.co",
)
