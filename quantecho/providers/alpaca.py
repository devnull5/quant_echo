"""Alpaca Market Data v2. Free plan = IEX feed (real-time, ~2-3% of volume); paid = SIP."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Field, Http, ProviderSpec, bar, latest_session, need, parse_iso, pct

BASE = "https://data.alpaca.markets/v2"


def parse_snapshots(payload: Any) -> Dict[str, Quote]:
    data = payload.get("snapshots", payload) if isinstance(payload, dict) else {}
    out: Dict[str, Quote] = {}
    for sym, snap in (data or {}).items():
        if not isinstance(snap, dict):
            continue
        trade = snap.get("latestTrade") or {}
        quote = snap.get("latestQuote") or {}
        day = snap.get("dailyBar") or {}
        prev = snap.get("prevDailyBar") or {}
        last = coerce_float(trade.get("p")) or coerce_float(day.get("c"))
        prev_c = coerce_float(prev.get("c"))
        out[sym.upper()] = Quote(
            symbol=sym.upper(),
            last=last,
            bid=coerce_float(quote.get("bp")),
            ask=coerce_float(quote.get("ap")),
            timestamp_ms=parse_iso(trade.get("t") or quote.get("t") or ""),
            total_volume=coerce_float(day.get("v")),
            net_change=(last - prev_c) if last is not None and prev_c else None,
            net_pct=pct(last, prev_c),
        )
    return out


def parse_bars(payload: Any) -> List[Dict[str, Any]]:
    return [bar(parse_iso(b.get("t")), b.get("o"), b.get("h"), b.get("l"), b.get("c"), b.get("v"))
            for b in (payload or {}).get("bars") or [] if isinstance(b, dict)]


class AlpacaProvider:
    def __init__(self, key_id: str, secret: str, feed: str = "iex", http: Optional[Http] = None):
        self.feed = feed or "iex"
        self.http = http or Http(BASE, headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
                                 secrets=(key_id, secret))

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        p = self.http.get("stocks/snapshots", {"symbols": ",".join(s.upper() for s in symbols), "feed": self.feed})
        return parse_snapshots(p)

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        start = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {"timeframe": "1Min", "start": start, "limit": 10000, "feed": self.feed,
                                  "adjustment": "raw"}
        for _ in range(10):  # pagination guard
            p = self.http.get(f"stocks/{symbol.upper()}/bars", params)
            rows += parse_bars(p)
            tok = p.get("next_page_token") if isinstance(p, dict) else None
            if not tok:
                break
            params["page_token"] = tok
        return latest_session(rows)


def _make(c: Mapping[str, str]) -> AlpacaProvider:
    k, s = need(c, "alpaca_key_id", "alpaca_secret")
    return AlpacaProvider(k, s, (c.get("alpaca_feed") or "iex").strip().lower())


SPEC = ProviderSpec(
    id="alpaca",
    label="Alpaca",
    factory=_make,
    fields=(
        Field("alpaca_key_id", "API Key ID", "ALPACA_API_KEY_ID"),
        Field("alpaca_secret", "Secret Key", "ALPACA_API_SECRET_KEY"),
        Field("alpaca_feed", "Feed (iex/sip)", "ALPACA_FEED", secret=False, default="iex"),
    ),
    notes="Free account works (paper keys are fine). Free = IEX feed; 'sip' needs a paid data plan.",
    min_interval=2,
    signup_url="https://alpaca.markets",
)
