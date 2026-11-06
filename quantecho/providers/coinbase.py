"""Coinbase Exchange public market data. Keyless, official, real-time crypto (BTC-USD, ETH-USD, ...).

Docs: https://docs.cdp.coinbase.com/exchange/reference/  (public endpoints, ~10 req/s per IP)
Crypto trades 24/7, so a "session" here is the UTC day.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Http, ProviderError, ProviderSpec, bar, parse_iso, pct

BASE = "https://api.exchange.coinbase.com"
PAGE = 300  # max candles per request


def normalize_symbol(s: str) -> str:
    """'btc' -> 'BTC-USD', 'eth-eur' -> 'ETH-EUR', 'BTCUSD' -> 'BTC-USD'."""
    s = s.strip().upper().replace("/", "-")
    if "-" in s:
        return s
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[: -len(quote)]}-{quote}"
    return f"{s}-USD"


def parse_quote(sym: str, ticker: Dict[str, Any], stats: Optional[Dict[str, Any]]) -> Quote:
    last = coerce_float(ticker.get("price"))
    st = stats or {}
    open24 = coerce_float(st.get("open"))
    return Quote(
        symbol=sym,
        last=last,
        bid=coerce_float(ticker.get("bid")),
        ask=coerce_float(ticker.get("ask")),
        timestamp_ms=parse_iso(ticker.get("time") or ""),
        total_volume=coerce_float(st.get("volume") or ticker.get("volume")),
        net_change=(last - open24) if last is not None and open24 else None,
        net_pct=pct(last, open24),  # 24h change (crypto has no "previous close")
    )


def parse_candles(rows: Any) -> List[Dict[str, Any]]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, list) and len(r) >= 6:
            t, lo, hi, o, c, v = r[:6]
            out.append(bar(int(t) * 1000, o, hi, lo, c, v))
    return sorted((b for b in out if b), key=lambda b: b["t"])


class CoinbaseProvider:
    market = "crypto"

    def __init__(self, http: Optional[Http] = None):
        self.http = http or Http(BASE)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        errors = []
        for raw in symbols:
            sym = normalize_symbol(raw)
            try:
                ticker = self.http.get(f"products/{sym}/ticker")
                try:
                    stats = self.http.get(f"products/{sym}/stats")
                except ProviderError:
                    stats = None
                out[raw.upper()] = parse_quote(raw.upper(), ticker, stats)
            except ProviderError as e:
                errors.append(f"{raw}: {'unknown product' if 'NotFound' in str(e) else e}")
        if errors and not out:
            raise ProviderError("; ".join(errors))
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        """1-min candles for the current UTC day (at least the last 5 hours, so TA has history)."""
        sym = normalize_symbol(symbol)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = min(now.replace(hour=0, minute=0), now - timedelta(minutes=PAGE))
        rows: List[Dict[str, Any]] = []
        end = now
        for _ in range(6):  # <= 1800 minutes
            s = max(start, end - timedelta(minutes=PAGE))
            page = parse_candles(self.http.get(f"products/{sym}/candles", {
                "granularity": 60, "start": s.isoformat(), "end": end.isoformat()}))
            rows = page + rows
            if s <= start or not page:
                break
            end = s
        seen, uniq = set(), []
        for b in sorted(rows, key=lambda b: b["t"]):
            if b["t"] not in seen:
                seen.add(b["t"])
                uniq.append(b)
        return uniq


SPEC = ProviderSpec(
    id="coinbase",
    label="Coinbase (crypto, no key)",
    factory=lambda c: CoinbaseProvider(),
    notes="Free, official, real-time crypto prices — no signup. Symbols like BTC-USD, ETH-USD, SOL-USD "
          "(or just BTC). Trades 24/7; 'day' = UTC day, change % = 24h.",
    min_interval=5,
    calls_per_symbol=2, max_calls_per_min=300,  # ticker+stats per symbol; public limit ~10/s
    min_bars_every=60,
    signup_url="https://docs.cdp.coinbase.com/exchange/introduction/welcome",
    market="crypto",
    default_symbols="BTC-USD ETH-USD SOL-USD",
)
