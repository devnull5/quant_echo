"""Shared provider plumbing: the interface, credential specs, HTTP helper, time utils.

Every provider returns:
  get_quotes(symbols)            -> {SYM: Quote}
  get_session_minute_bars(sym)   -> [{"t": epoch_ms, "open","high","low","close","volume"}] (oldest first,
                                    latest US/Eastern session only; [] if the source has no intraday bars)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

from ..models import Quote

NY = ZoneInfo("America/New_York")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) QuantEcho/1.0"


class Provider(Protocol):
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]: ...
    def get_session_minute_bars(self, symbol: str) -> List[Dict[str, Any]]: ...


@dataclass(frozen=True)
class Field:
    key: str        # settings key, e.g. "alpaca_key_id"
    label: str      # GUI label
    env: str        # environment variable fallback
    secret: bool = True
    default: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    factory: Callable[[Mapping[str, str]], Provider]
    fields: Sequence[Field] = field(default_factory=tuple)
    notes: str = ""
    min_interval: int = 1      # seconds between quote polls the free tier tolerates
    min_bars_every: int = 60   # seconds between bar refreshes
    signup_url: str = ""


class ProviderError(RuntimeError):
    pass


class RateLimited(ProviderError):
    """The source said 'slow down' (HTTP 429 or an in-body quota message)."""

    def __init__(self, msg: str, retry_after: Optional[float] = None):
        super().__init__(msg)
        self.retry_after = retry_after


class AuthError(ProviderError):
    """Bad/expired credentials or plan doesn't include this endpoint (HTTP 401/403)."""


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None


def _clean_body(body: str) -> str:
    """Collapse HTML error pages to their <title>/<h1> and squash whitespace."""
    b = (body or "").strip()
    if b.startswith("<"):
        m = re.search(r"<(?:title|h1)>(.*?)</(?:title|h1)>", b, re.I | re.S)
        b = m.group(1) if m else re.sub(r"<[^>]+>", " ", b)
    return re.sub(r"\s+", " ", b).strip()


def http_error(status: int, body: str, retry_after: Optional[str] = None) -> ProviderError:
    msg = f"HTTP {status}: {_clean_body(body)[:250]}"
    if status in (401, 403):
        msg += " — check the API key / plan in ⚙ Settings"
    if status == 429:
        return RateLimited(msg, _retry_after(retry_after))
    if status in (401, 403):
        return AuthError(msg)
    return ProviderError(msg)


# ---------------------------------------------------------------- HTTP

class Http:
    """Thin httpx wrapper that never leaks API keys into error messages."""

    def __init__(self, base: str = "", headers: Optional[Dict[str, str]] = None,
                 secrets: Sequence[str] = (), timeout: float = 15.0, client: Any = None):
        import httpx

        self.base = base.rstrip("/")
        self.secrets = [s for s in secrets if s]
        self.client = client or httpx.Client(timeout=timeout, headers={"User-Agent": UA, **(headers or {})},
                                             follow_redirects=True)

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return re.sub(r"(api[_-]?key|apikey|token)=[^&\s]+", r"\1=***", text, flags=re.I)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        import httpx

        url = path if path.startswith("http") else f"{self.base}/{path.lstrip('/')}"
        try:
            r = self.client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ProviderError(self.redact(f"{type(e).__name__}: {e}")) from None
        if r.status_code >= 400:
            err = http_error(r.status_code, self.redact(r.text), r.headers.get("Retry-After"))
            raise err
        try:
            return r.json()
        except ValueError:
            raise ProviderError(self.redact(f"Non-JSON response: {r.text[:200]}")) from None


# ---------------------------------------------------------------- time / bars

def parse_iso(s: str) -> Optional[int]:
    """ISO-8601 (with Z / offset / nanoseconds) -> epoch ms."""
    if not isinstance(s, str) or not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*?)\.(\d+)(.*)$", s)  # normalise fraction to exactly 6 digits (py3.10 needs 3 or 6)
    if m:
        s = f"{m.group(1)}.{m.group(2)[:6].ljust(6, '0')}{m.group(3)}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_local(s: str, tz: ZoneInfo = NY) -> Optional[int]:
    """'YYYY-MM-DD HH:MM:SS' in exchange-local time -> epoch ms."""
    try:
        return int(datetime.fromisoformat(s).replace(tzinfo=tz).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def bar(t: Optional[int], o: Any, h: Any, lo: Any, c: Any, v: Any) -> Optional[Dict[str, Any]]:
    from ..ta import coerce_float

    if t is None:
        return None
    row = {"t": int(t), "open": coerce_float(o), "high": coerce_float(h), "low": coerce_float(lo),
           "close": coerce_float(c), "volume": coerce_float(v)}
    return row if row["close"] is not None else None


def latest_session(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort, drop Nones, keep only bars from the most recent US/Eastern trading date."""
    rows = sorted((r for r in rows if r), key=lambda r: r["t"])
    if not rows:
        return rows

    def day(r):
        return datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc).astimezone(NY).date()

    last = day(rows[-1])
    return [r for r in rows if day(r) == last]


def pct(last: Optional[float], prev: Optional[float]) -> Optional[float]:
    if last is None or not prev:
        return None
    return (last - prev) / prev * 100.0


def need(creds: Mapping[str, str], *keys: str) -> List[str]:
    vals = [str(creds.get(k) or "").strip() for k in keys]
    missing = [k for k, v in zip(keys, vals, strict=True) if not v]
    if missing:
        raise ProviderError(f"Missing credentials: {', '.join(missing)}")
    return vals
