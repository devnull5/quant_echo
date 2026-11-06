"""Tiingo IEX endpoint. Free key: real-time IEX top-of-book + intraday minute bars."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import NY, Field, Http, ProviderSpec, bar, latest_session, need, parse_iso, pct

BASE = "https://api.tiingo.com"


def parse_iex(payload: Any) -> Dict[str, Quote]:
    out: Dict[str, Quote] = {}
    for r in payload if isinstance(payload, list) else []:
        if not isinstance(r, dict) or not r.get("ticker"):
            continue
        sym = r["ticker"].upper()
        last = coerce_float(r.get("last")) or coerce_float(r.get("tngoLast"))
        prev = coerce_float(r.get("prevClose"))
        out[sym] = Quote(
            symbol=sym,
            last=last,
            bid=coerce_float(r.get("bidPrice")),
            ask=coerce_float(r.get("askPrice")),
            timestamp_ms=parse_iso(r.get("timestamp") or r.get("lastSaleTimestamp") or ""),
            total_volume=coerce_float(r.get("volume")),
            net_change=(last - prev) if last is not None and prev else None,
            net_pct=pct(last, prev),
        )
    return out


def parse_prices(payload: Any) -> List[Dict[str, Any]]:
    return [bar(parse_iso(r.get("date")), r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                r.get("volume"))
            for r in (payload if isinstance(payload, list) else []) if isinstance(r, dict)]


class TiingoProvider:
    def __init__(self, token: str, http: Optional[Http] = None):
        self.http = http or Http(BASE, headers={"Authorization": f"Token {token}"}, secrets=(token,))

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        tmap = {s.upper().replace(".", "-"): s.upper() for s in symbols}   # BRK.B -> BRK-B (Tiingo spelling)
        raw = parse_iex(self.http.get("iex/", {"tickers": ",".join(t.lower() for t in tmap)}))
        out: Dict[str, Quote] = {}
        for t, q in raw.items():
            q.symbol = tmap.get(t, t)
            out[q.symbol] = q
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        start = (datetime.now(NY).date() - timedelta(days=5)).isoformat()
        p = self.http.get(f"iex/{symbol.lower().replace('.', '-')}/prices", {
            "startDate": start, "resampleFreq": "1min", "afterHours": "true",
            "columns": "open,high,low,close,volume",
        })
        return latest_session(parse_prices(p))


def _make(c: Mapping[str, str]) -> TiingoProvider:
    (k,) = need(c, "tiingo_token")
    return TiingoProvider(k)


SPEC = ProviderSpec(
    id="tiingo",
    label="Tiingo (IEX)",
    factory=_make,
    fields=(Field("tiingo_token", "API Token", "TIINGO_API_KEY"),),
    notes="Free key: real-time IEX quotes + 1-min bars. Free limits ~50 req/hour, 1000/day — poll slowly.",
    min_interval=60,
    min_bars_every=300,
    signup_url="https://www.tiingo.com",
)
