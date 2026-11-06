"""Yahoo Finance (unofficial, no key). PERSONAL USE ONLY; may change without notice.

Quotes use the batch `spark` endpoint (max 20 symbols per call) so a 21-symbol watchlist costs
2 requests per poll instead of 21. Spark has no volume, so volume is filled in by rotating
2 per-symbol `chart` calls per poll. Bars for the focus symbol come from `chart`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Http, ProviderError, ProviderSpec, bar, latest_session, pct

BASE = "https://query1.finance.yahoo.com/v8/finance"
SPARK_MAX = 20          # Yahoo rejects larger batches with HTTP 400
VOLUME_PER_POLL = 2     # extra chart calls per poll to fill in volume


def to_yahoo(sym: str) -> str:
    """Class shares: BRK.B -> BRK-B (Yahoo's spelling)."""
    return sym.upper().replace(".", "-").replace("/", "-")


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


def parse_spark(payload: Any) -> Dict[str, Quote]:
    """{YAHOO_SYM: Quote} from a spark response (unknown symbols are simply absent)."""
    out: Dict[str, Quote] = {}
    for ysym, row in (payload or {}).items():
        if not isinstance(row, dict):
            continue
        closes = [c for c in (row.get("close") or []) if c is not None]
        stamps = row.get("timestamp") or []
        last = coerce_float(row.get("fulldayPrice")) or (coerce_float(closes[-1]) if closes else None)
        prev = coerce_float(row.get("previousClose") or row.get("chartPreviousClose"))
        chg = coerce_float(row.get("fulldayChangePercent"))
        out[ysym.upper()] = Quote(
            symbol=ysym.upper(),
            last=last,
            bid=None,
            ask=None,
            timestamp_ms=int(stamps[-1]) * 1000 if stamps else None,
            total_volume=None,
            net_change=(last - prev) if last is not None and prev else None,
            net_pct=chg if chg is not None else pct(last, prev),
        )
    return out


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
        self._volume: Dict[str, float] = {}   # yahoo symbol -> latest daily volume
        self._rot = 0                          # rotation cursor for volume refresh

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        ymap = {to_yahoo(s): s.upper() for s in symbols}   # yahoo spelling -> user's spelling
        ysyms = list(ymap)
        quotes: Dict[str, Quote] = {}
        errors = []
        for i in range(0, len(ysyms), SPARK_MAX):
            chunk = ysyms[i:i + SPARK_MAX]
            try:
                quotes.update(parse_spark(self.http.get("spark", {"symbols": ",".join(chunk), "range": "1d",
                                                                  "interval": "5m"})))
            except ProviderError as e:
                errors.append(str(e))
        if not quotes:
            raise ProviderError("; ".join(errors) or "Yahoo: no quotes returned (unknown symbols?)")

        # volume: refresh a couple of symbols per poll, preferring ones we've never filled
        missing = [y for y in ysyms if y in quotes and y not in self._volume]
        pool = missing or [y for y in ysyms if y in quotes]
        budget = VOLUME_PER_POLL * (2 if missing else 1)  # fill a new watchlist faster, then trickle
        for _ in range(min(budget, len(pool))):
            y = pool[self._rot % len(pool)] if not missing else pool.pop(0)
            self._rot += 1
            try:
                q = parse_quote(self.http.get(f"chart/{y}", {"interval": "1d", "range": "1d"}))
                if q.total_volume is not None:
                    self._volume[y] = q.total_volume
            except ProviderError:
                pass  # volume is best-effort

        out: Dict[str, Quote] = {}
        for y, q in quotes.items():
            user = ymap.get(y, y)
            q.symbol = user
            q.total_volume = self._volume.get(y)
            out[user] = q
        return out

    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]:
        return parse_bars(self.http.get(f"chart/{to_yahoo(symbol)}",
                                        {"interval": "1m", "range": "5d", "includePrePost": "true"}))


SPEC = ProviderSpec(
    id="yahoo",
    label="Yahoo Finance (no key)",
    factory=lambda c: YahooProvider(),
    notes="PERSONAL USE ONLY: unofficial endpoint whose terms don't allow redistribution. No signup; may "
          "rate-limit or change without notice.",
    min_interval=10,
    personal_use_only=True,
    clip_volume_spikes=True,  # Yahoo 1-min data has occasional 50-100x phantom volume bars
)
