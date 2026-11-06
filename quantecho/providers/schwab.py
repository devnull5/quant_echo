"""Charles Schwab Trader API (market data only). Needs a token from `python -m quantecho.auth`.

schwab-py methods return httpx.Response objects and quote fields live under payload[SYM]["quote"].
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from ..models import Quote
from ..ta import coerce_float
from .base import Field, ProviderError, ProviderSpec, bar, http_error, latest_session, need


def _json(resp: Any) -> Any:
    if hasattr(resp, "status_code") and hasattr(resp, "json"):
        if resp.status_code >= 400:
            headers = getattr(resp, "headers", None) or {}
            raise http_error(resp.status_code, resp.text or "", headers.get("Retry-After"))
        return resp.json()
    return resp


def parse_quotes(payload: Any) -> Dict[str, Quote]:
    out: Dict[str, Quote] = {}
    if not isinstance(payload, dict):
        return out
    for sym, row in payload.items():
        if not isinstance(row, dict) or sym == "errors":
            continue
        q = row["quote"] if isinstance(row.get("quote"), dict) else row

        def g(*keys: str, q: dict = q) -> Any:
            return next((q[k] for k in keys if q.get(k) is not None), None)

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
    candles = payload.get("candles") if isinstance(payload, dict) else None
    return [
        bar(int(c["datetime"]), c.get("open"), c.get("high"), c.get("low"), c.get("close"), c.get("volume"))
        for c in candles or []
        if isinstance(c, dict) and isinstance(c.get("datetime"), (int, float))
    ]


class SchwabProvider:
    def __init__(self, api_key: str, app_secret: str, token_path: str, *, client: Optional[Any] = None):
        if client is not None:
            self.client = client
            return
        from schwab.auth import client_from_token_file

        try:
            self.client = client_from_token_file(
                token_path=token_path.strip(), api_key=api_key.strip(), app_secret=app_secret.strip()
            )
        except (OSError, ValueError) as e:
            raise ProviderError(
                f"Schwab token not usable ({type(e).__name__}: {e}).\n"
                "Run `python -m quantecho.auth` to log in and create it."
            ) from e

    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        return parse_quotes(_json(self.client.get_quotes(symbols))) if symbols else {}

    def get_session_minute_bars(self, symbol: str, lookback_days: int = 5) -> List[Dict[str, Any]]:
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        resp = self.client.get_price_history_every_minute(
            symbol.upper(), start_datetime=start, need_extended_hours_data=True
        )
        return latest_session(parse_candles(_json(resp)))


def _make(c: Mapping[str, str]) -> SchwabProvider:
    key, secret = need(c, "schwab_app_key", "schwab_app_secret")
    return SchwabProvider(key, secret, c.get("schwab_token_path") or "tokens.json")


SPEC = ProviderSpec(
    id="schwab",
    label="Charles Schwab",
    factory=_make,
    fields=(
        Field("schwab_app_key", "App Key", "SCHWAB_APP_KEY"),
        Field("schwab_app_secret", "App Secret", "SCHWAB_APP_SECRET"),
        Field("schwab_callback", "Callback URL", "SCHWAB_CALLBACK_URL", secret=False,
              default="https://127.0.0.1:8182"),
        Field("schwab_token_path", "Token file", "SCHWAB_TOKEN_PATH", secret=False, default="tokens.json"),
    ),
    notes="Bring your own Schwab developer app (personal use). Real-time with a Schwab account. "
          "Run `python -m quantecho.auth` first; token lasts 7 days.",
    min_interval=2,
    signup_url="https://developer.schwab.com",
    requires="schwab",  # schwab-py; not bundled in the packaged app (its login needs a terminal)
)
