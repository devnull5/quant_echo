"""Yahoo Finance chart endpoint (unofficial, no key). Delayed for some exchanges; may change without notice."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Http, ProviderError, ProviderSpec, bar, latest_session, pct

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"


def _result(payload: Any) -> Dict[str, Any]:
    chart = (payload or {}).get("chart") or {}
    if chart.get("error"):
        err = chart["error"]
        raise ProviderError(f"Yahoo: {err.get('description') or err}")
    res = chart.get("result") or []
    if not res:
        raise ProviderError("Yahoo: empty result")
    return res[0]


def parse_quote(payload: Any) -> Quote:
    meta = _result(payload).get("meta") or {}
    last = coerce_float(meta.get("regularMarketPrice"))
    prev = coerce_float(meta.get("chartPreviousClose") or meta.get("previousClose"))
    ts = meta.get("regularMarketTime")
    chg_pct = coerce_float(meta.get("regularMarketChangePercent"))
    return Quote(
        symbol=str(meta.get("symbol", "")).upper(),
        last=last,
        bid=None,
        ask=None,
        timestamp_ms=int(ts) * 1000 if isinstance(ts, (int, float)) else None,
        total_volume=coerce_float(meta.get("regularMarketVolume")),
        net_change=(last - prev) if last is not None and prev else None,
        net_pct=chg_pct if chg_pct is not None else pct(last, prev),
    )


def parse_bars(payload: Any) -> List[Dict[str, Any]]:
    r = _result(payload)
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    rows = []
    for i, t in enumerate(ts):
        vals = [c[i] if i < len(c) else None for c in cols]
        rows.append(bar(int(t) * 1000, *vals))
    return latest_session(rows)


class YahooProvider:
    def __init__(self, http: Optional[Http] = None):
        self.http = http or Http(BASE)

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        errors = []
        for s in symbols:
            try:
                q = parse_quote(self.http.get(s.upper(), {"interval": "1d", "range": "1d"}))
                out[s.upper()] = q
            except ProviderError as e:
                errors.append(f"{s}: {e}")
        if errors and not out:
            raise ProviderError("; ".join(errors))
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        return parse_bars(self.http.get(symbol.upper(), {"interval": "1m", "range": "5d", "includePrePost": "true"}))


SPEC = ProviderSpec(
    id="yahoo",
    label="Yahoo Finance (no key)",
    factory=lambda c: YahooProvider(),
    notes="Free, no signup. Unofficial endpoint — fine for personal use, may rate-limit or change.",
    min_interval=10,
)
