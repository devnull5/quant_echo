"""Read-only Schwab market-data provider (quotes + 1-minute candles).

schwab-py client methods return ``httpx.Response`` objects, not dicts, and the
quote fields live under ``payload[SYM]["quote"]``. The parsers below are pure
functions so they can be tested without network access.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .models import Quote
from .ta import coerce_float

NY = ZoneInfo("America/New_York")


def _json(resp: Any) -> Any:
    """Accept an httpx.Response (raise on HTTP error) or an already-decoded payload."""
    if hasattr(resp, "raise_for_status") and hasattr(resp, "json"):
        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            raise RuntimeError(f"HTTP {resp.status_code}: {body}")
        return resp.json()
    return resp


def parse_quotes(payload: Any) -> Dict[str, Quote]:
    out: Dict[str, Quote] = {}
    if not isinstance(payload, dict):
        return out
    for sym, row in payload.items():
        if not isinstance(row, dict) or sym == "errors":
            continue
        q = row.get("quote") if isinstance(row.get("quote"), dict) else row

        def g(*keys: str, q: dict = q) -> Any:
            for k in keys:
                if q.get(k) is not None:
                    return q[k]
            return None

        ts = g("quoteTime", "tradeTime", "quoteTimeInLong", "tradeTimeInLong")
        u = str(row.get("symbol") or sym).upper()
        out[u] = Quote(
            symbol=u,
            last=coerce_float(g("lastPrice", "mark", "closePrice")),
            bid=coerce_float(g("bidPrice")),
            ask=coerce_float(g("askPrice")),
            timestamp_ms=int(ts) if isinstance(ts, (int, float)) else None,
            total_volume=coerce_float(g("totalVolume")),
            net_change=coerce_float(g("netChange")),
            net_pct=coerce_float(g("netPercentChange")),
        )
    return out


def parse_candles(payload: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    candles = payload.get("candles") if isinstance(payload, dict) else None
    for c in candles or []:
        if not isinstance(c, dict) or not isinstance(c.get("datetime"), (int, float)):
            continue
        rows.append({
            "t": int(c["datetime"]),
            "open": c.get("open"),
            "high": c.get("high"),
            "low": c.get("low"),
            "close": c.get("close"),
            "volume": c.get("volume"),
        })
    rows.sort(key=lambda r: r["t"])
    return rows


def latest_session(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only bars from the most recent US/Eastern trading date."""
    if not rows:
        return rows
    day = lambda r: datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).astimezone(NY).date()  # noqa: E731
    last = day(rows[-1])
    return [r for r in rows if day(r) == last]


class SchwabProvider:
    """Wraps a schwab-py client built from an existing token file.

    Create the token once with ``python -m quantecho.auth`` (opens a browser).
    The GUI never runs the interactive login flow itself.
    """

    def __init__(self, api_key: str, app_secret: str, token_path: str, *, client: Optional[Any] = None):
        if client is not None:
            self.client = client
            return
        from schwab.auth import client_from_token_file

        try:
            self.client = client_from_token_file(
                token_path=token_path.strip(),
                api_key=api_key.strip(),
                app_secret=app_secret.strip(),
            )
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"Schwab token not usable ({type(e).__name__}: {e}).\n"
                "Run `python -m quantecho.auth` to log in and create it."
            ) from e

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        return parse_quotes(_json(self.client.get_quotes(symbols)))

    def get_session_minute_bars(self, symbol: str, lookback_days: int = 5) -> List[Dict[str, Any]]:
        """1-minute candles (incl. extended hours) for the latest session.

        Looks back a few days so weekends/holidays still yield the prior session.
        """
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        resp = self.client.get_price_history_every_minute(
            symbol.upper(), start_datetime=start, need_extended_hours_data=True
        )
        return latest_session(parse_candles(_json(resp)))
