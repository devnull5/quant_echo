# QuantEcho_core.py — Polygon-only data backend, OpenAI Q&A, VWAP/TA, optional News
# ---------------------------------------------------------------------------------
# Highlights:
# - Replaced Schwab provider with Polygon REST (batch snapshots + minute aggregates).
# - No OAuth: single POLYGON_API_KEY via env; optional POLYGON_BASE_URL override.
# - Keeps deterministic bias, VWAP/TA, Q&A via OpenAI Responses API, News hooks.
# - Macro proxies (DXY≈UUP, TNX≈IEF/TLT inverse) preserved.
# - CLI stays compatible; --symbols/--focus/--ask/--chat flow unchanged.
# ---------------------------------------------------------------------------------

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Event, RLock
from typing import Any, List, Optional, Tuple, Dict

import requests
from requests.exceptions import RequestException, Timeout, ConnectionError as ReqConnectionError
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ===== OpenAI helper =====
try:
    from openai import OpenAI  # openai>=1.0
except ImportError:
    OpenAI = None  # graceful degrade if library not installed
try:
    from openai import (
        AuthenticationError,
        APIConnectionError,
        RateLimitError,
        BadRequestError,
        InternalServerError,
        APIError,
    )
except ImportError:
    AuthenticationError = APIConnectionError = RateLimitError = BadRequestError = InternalServerError = APIError = Exception  # type: ignore[misc,assignment]


def _get_openai_client():
    """Returns an OpenAI client using env OPENAI_API_KEY; returns None if not available."""
    if OpenAI is None:
        return None
    try:
        base_url = os.getenv("OPENAI_BASE_URL") or None
        return OpenAI(base_url=base_url) if base_url else OpenAI()
    except (AuthenticationError, APIConnectionError, RateLimitError, BadRequestError, InternalServerError, APIError):
        return None


def _responses_output_text(obj) -> str:
    """Extract text from OpenAI Responses API SDK object."""
    if not obj:
        return ""
    txt = getattr(obj, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    out = getattr(obj, "output", None)
    parts: list[str] = []
    if isinstance(out, (list, tuple)):
        for block in out:
            content = getattr(block, "content", None)
            if isinstance(content, (list, tuple)):
                for item in content:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        t = text.strip()
                        if t:
                            parts.append(t)
    return " ".join(parts).strip()


def _responses_output_text_from_json(obj: dict) -> str:
    """Extract text from OpenAI Responses API JSON dict."""
    if not isinstance(obj, dict):
        return ""
    txt = obj.get("output_text")
    if isinstance(txt, str) and txt.strip():
        return txt.strip()
    out = obj.get("output")
    parts = []
    if isinstance(out, list):
        for block in out:
            if not isinstance(block, dict):
                continue
            content = block.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        t = item.get("text")
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
    return " ".join(parts).strip() if parts else ""


def _openai_responses_answer(
    prompt: str,
    *,
    model: str = "gpt-5-nano",
    timeout: int = 45,
) -> Optional[str]:
    """
    Call OpenAI Responses API (for GPT-5 family). Returns text or None.
    Uses SDK if available; falls back to plain HTTP via requests.
    """
    client = _get_openai_client()

    # SDK path
    if client is not None:
        client_call = client.with_options(timeout=timeout) if timeout else client
        try:
            resp = client_call.responses.create(
                model=model,
                input=("You are a concise market assistant. Keep answers tight and factual.\n\n" + prompt),
                max_output_tokens=1024,
            )
            txt = _responses_output_text(resp)
            return txt if txt else None
        except (AuthenticationError, RateLimitError, APIConnectionError, BadRequestError,
                InternalServerError, APIError, TimeoutError):
            _log = globals().get("log")
            if callable(_log):
                _log("(OpenAI Responses error via SDK)")
            # fall through

    # HTTP fallback
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    endpoint_url = f"{base}/responses"
    body = {
        "model": model or "gpt-5-nano",
        "input": "You are a concise market assistant. Keep answers tight and factual.\n\n" + prompt,
        "max_output_tokens": 1024,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        resp = requests.post(endpoint_url, headers=headers, json=body, timeout=timeout or 45)
        resp.raise_for_status()
        data = resp.json()
        txt = _responses_output_text_from_json(data)
        return txt if txt else None
    except requests.RequestException as e:
        _log = globals().get("log")
        if callable(_log):
            _log("(OpenAI Responses error via HTTP)", e)
        return None


DEBUG_QUOTE_FIELDS = os.getenv("DEBUG_QUOTE_FIELDS", "0") == "1"

# ---- optional News context (safe fallbacks) ----
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")
SYMBOL_RE = re.compile(r"\b[A-Z]{1,5}(?:/[A-Z]{2,4})?\b")

# ====== POLYGON CREDS ======
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
POLYGON_BASE_URL = (os.getenv("POLYGON_BASE_URL") or "https://api.polygon.io").rstrip("/")
# ===========================

AI_DISABLED = {"off", "none", "", "0", "false", "disabled"}

def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)

# ---------- Macro aliases (DXY/TNX) ----------
DXY_ALIASES = ["$DXY", "DXY", "UUP"]     # Use UUP ETF as liquid proxy
TNX_ALIASES = ["$TNX", "TNX", "IEF", "TLT"]  # IEF/TLT inverse for yields
INVERSE_PROXIES = {"IEF", "TLT"}
REQUEST_FILTER_DROP = {"DXY", "TNX", "$DXY", "$TNX"}

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ModuleNotFoundError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception  # fallback


def virginia_now_str(fmt: str = "%Y-%m-%d %I:%M:%S %p %Z") -> str:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York")).strftime(fmt)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now().astimezone().strftime(fmt)


def expand_symbols(user_syms: List[str]) -> List[str]:
    base = list(user_syms)
    for s in DXY_ALIASES + TNX_ALIASES:
        if s not in base:
            base.append(s)
    return base


def normalize_provider_symbols(user_syms: List[str]) -> List[str]:
    out: List[str] = []
    for s in user_syms:
        if s in REQUEST_FILTER_DROP or s.startswith("$"):
            continue
        if s.startswith("/"):
            log(f"Skipping futures-like symbol '{s}' (Polygon stocks API expects equities/ETFs here).")
            continue
        if s not in out:
            out.append(s)
    return out


def coerce_float(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            v = float(s)
        except ValueError:
            return None
        return None if (math.isnan(v) or math.isinf(v)) else v
    return None


def coerce_ts(x) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        t = float(x)
        return t / 1000.0 if t > 1e12 else t
    if isinstance(x, datetime):
        return x.timestamp()
    if isinstance(x, str):
        s = x.strip().replace(",", "")
        if not s:
            return None
        try:
            t = float(s)
        except (TypeError, ValueError):
            return None
        return t / 1000.0 if t > 1e12 else t
    return None


def _is_rth(ts: float | None = None) -> bool:
    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            tz = None
    if tz is None:
        tz = datetime.now().astimezone().tzinfo
    if ts is not None:
        try:
            ts_float = float(ts)
            dt = datetime.fromtimestamp(ts_float, tz)
        except (TypeError, ValueError, OverflowError, OSError):
            dt = datetime.now(tz)
    else:
        dt = datetime.now(tz)
    if dt.weekday() >= 5:
        return False
    hhmm = dt.hour * 60 + dt.minute
    return (9 * 60 + 30) <= hhmm < (16 * 60)

# =========================== Quote Provider (Polygon) ===========================

@dataclass
class Quote:
    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    timestamp: Optional[float] = None
    total_volume: Optional[float] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def px(self) -> Optional[float]:
        return self.last if self.last is not None else (self.mid if self.mid is not None else (self.bid if self.bid is not None else self.ask))


class BaseQuoteProvider:
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        raise NotImplementedError


class PolygonQuoteProvider(BaseQuoteProvider):
    """
    Efficient stock/ETF snapshots via Polygon:
      - Batch: /v2/snapshot/locale/us/markets/stocks/tickers?tickers=SPY,QQQ,...
        fields: lastTrade.p, lastTrade.t, lastQuote.bp/ap, day.v
      - Fallback per-ticker (if batch fails): last trade + latest NBBO.
    """
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, verbose: bool = False):
        self.verbose = bool(verbose)
        self.api_key = (api_key or POLYGON_API_KEY or "").strip()
        self.base = (base_url or POLYGON_BASE_URL).rstrip("/")
        if not self.api_key:
            log("⚠️ POLYGON_API_KEY missing — provider will return empty data.")

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ---------- REST helpers ----------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 10) -> Optional[dict]:
        if not self.api_key:
            return None
        url = f"{self.base}{path}"
        q = dict(params or {})
        q["apiKey"] = self.api_key
        try:
            r = self._session.get(url, params=q, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (ReqConnectionError, Timeout, RequestException) as e:
            if self.verbose:
                log(f"[polygon] GET {path} error:", e)
            return None

    def _snapshot_batch(self, symbols: List[str]) -> Optional[dict]:
        # Up to ~50 tickers per call is safe; call site handles chunking
        path = "/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {"tickers": ",".join(symbols)}
        return self._get(path, params, timeout=10)

    def _snapshot_single(self, sym: str) -> Optional[dict]:
        return self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{sym}", timeout=8)

    def _latest_nbbo(self, sym: str) -> Optional[dict]:
        # /v3/quotes/{ticker}/nbbo/latest
        return self._get(f"/v3/quotes/{sym}/nbbo/latest", timeout=6)

    def _last_trade(self, sym: str) -> Optional[dict]:
        # /v2/last/trade/{ticker}
        return self._get(f"/v2/last/trade/{sym}", timeout=6)

    # ---------- Public ----------
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """
        Batch-queries Polygon for quotes/trades and returns a {symbol: Quote} map.
        Requires: self.api_key, self._snapshot_batch, self._last_trade, self._latest_nbbo
        """
        res: Dict[str, Quote] = {}
        to_query = normalize_provider_symbols(symbols)
        if not to_query or not getattr(self, "api_key", None):
            return res

        # Do batch in chunks for efficiency
        chunk_size = 40
        for i in range(0, len(to_query), chunk_size):
            chunk = to_query[i:i + chunk_size]
            data = self._snapshot_batch(chunk)  # expected shape: {"tickers": [ ... ]}
            if isinstance(data, dict) and isinstance(data.get("tickers"), list):
                for item in data["tickers"]:
                    sym = str(item.get("ticker") or "").upper().strip()
                    if not sym:
                        continue
                    # Primary fields (Polygon snapshot v2 schema)
                    last = coerce_float((((item.get("lastTrade") or {}) or {}).get("p")))
                    ts = coerce_ts((((item.get("lastTrade") or {}) or {}).get("t")))
                    bid = coerce_float((((item.get("lastQuote") or {}) or {}).get("bp")))
                    ask = coerce_float((((item.get("lastQuote") or {}) or {}).get("ap")))
                    # Day volume (RTH cumulative)
                    tv = coerce_float((((item.get("day") or {}) or {}).get("v")))
                    if bid is not None and ask is not None and bid > ask:
                        # Rare NBBO inversion -> discard
                        bid, ask = None, None
                    res[sym] = Quote(symbol=sym, last=last, bid=bid, ask=ask, timestamp=ts, total_volume=tv)

            # Fallbacks for any missing in this chunk
            for sym in chunk:
                u = sym.upper()
                if u in res:
                    continue
                lt = self._last_trade(u) or {}
                nbbo = self._latest_nbbo(u) or {}
                last = coerce_float(((lt.get("results") or {}) or {}).get("p") or lt.get("price"))
                ts = coerce_ts(((lt.get("results") or {}) or {}).get("t") or lt.get("sip_timestamp"))
                nbbo_res = (nbbo.get("results") or {}) or {}
                # Be lenient about key aliases across Polygon variants
                bid = coerce_float(nbbo_res.get("bidPrice") or nbbo_res.get("bp") or nbbo_res.get("P"))
                ask = coerce_float(nbbo_res.get("askPrice") or nbbo_res.get("ap") or nbbo_res.get("p"))
                if bid is not None and ask is not None and bid > ask:
                    bid, ask = None, None
                res[u] = Quote(symbol=u, last=last, bid=bid, ask=ask, timestamp=ts, total_volume=None)

        return res

    # ---------- Aggregates ----------
    def get_today_minute_aggregates(self, symbol: str) -> List[dict]:
        """
        Returns today's 1-min aggregates (up to now) for `symbol` in America/New_York.
        Uses endpoint `/v2/aggs/ticker/{ticker}/range/1/min/{from}/{to}`.
        """
        if not getattr(self, "api_key", None):
            return []

        # Compute trading day window (midnight to now NY time; Polygon returns all mins)
        if ZoneInfo is not None:
            tz = ZoneInfo("America/New_York")
        else:
            tz = datetime.now().astimezone().tzinfo
        now = datetime.now(tz)
        start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
        # Pad by one day to be safe if called pre-market
        date_from = start.strftime("%Y-%m-%d")
        date_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        path = f"/v2/aggs/ticker/{symbol.upper()}/range/1/min/{date_from}/{date_to}"
        params = {"adjusted": "true", "sort": "asc", "limit": 50000}
        data = self._get(path, params=params, timeout=12)

        results: List[dict] = []
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            for r in data["results"]:
                # Polygon 2.x aggregates: o,h,l,c,v,t
                results.append({
                    "datetime": r.get("t"),  # ms epoch
                    "open": r.get("o"),
                    "high": r.get("h"),
                    "low": r.get("l"),
                    "close": r.get("c"),
                    "volume": r.get("v"),
                })
        return results


# =========================== Macro synth + VWAP/TA brain ===========================

def enrich_with_macros(quotes: Dict[str, Quote]) -> None:
    def px(sym: str) -> Optional[float]:
        q = quotes.get(sym)
        return q.px if q else None

    dxy_val = None
    for cand in DXY_ALIASES:
        v = px(cand)
        if v is not None:
            dxy_val = v
            break
    if dxy_val is not None:
        quotes["DXY"] = Quote("DXY", last=float(dxy_val), bid=None, ask=None)

    tnx_val = None
    for cand in TNX_ALIASES:
        v = px(cand)
        if v is not None:
            tnx_val = -float(v) if cand in INVERSE_PROXIES else float(v)
            break
    if tnx_val is not None:
        quotes["TNX"] = Quote("TNX", last=float(tnx_val), bid=None, ask=None)


@dataclass
class Snapshot:
    t: float
    prices: Dict[str, float]
    vwap: Dict[str, float]


class VWAPCalc:
    def __init__(self, tz=None):
        self.tz = tz or (ZoneInfo("America/New_York") if ZoneInfo else datetime.now().astimezone().tzinfo)
        self.day_key: str = ""
        self.cum_vol: float = 0.0
        self.cum_dollar: float = 0.0

    def _key(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self.tz).strftime("%Y-%m-%d")

    def reset_for(self, ts: float):
        self.day_key = self._key(ts)
        self.cum_vol = 0.0
        self.cum_dollar = 0.0

    def seed_from_candles(self, candles: List[dict]):
        for c in candles:
            ts = coerce_ts(c.get("datetime") or c.get("time") or c.get("timestamp") or time.time())
            if ts is None:
                continue
            key = self._key(ts)
            if not self.day_key:
                self.day_key = key
            if key != self.day_key:
                continue
            vol = coerce_float(c.get("volume"))
            o = coerce_float(c.get("open"))
            h = coerce_float(c.get("high"))
            l = coerce_float(c.get("low"))
            cl = coerce_float(c.get("close") or c.get("closePrice"))
            if vol is None or vol <= 0 or cl is None:
                continue
            typical = cl if (o is None or h is None or l is None) else (h + l + cl) / 3.0
            self.cum_vol += vol
            self.cum_dollar += typical * vol

    def update_live(self, ts: Optional[float], px: Optional[float], cum_volume: Optional[float]):
        if ts is None or px is None or cum_volume is None:
            return
        key = self._key(ts)
        if not self.day_key or self.day_key != key:
            self.reset_for(ts)
        if cum_volume < self.cum_vol:
            self.reset_for(ts)
        delta = cum_volume - self.cum_vol
        if delta > 0:
            self.cum_dollar += px * delta
            self.cum_vol = cum_volume

    def value(self) -> Optional[float]:
        return (self.cum_dollar / self.cum_vol) if self.cum_vol > 0 else None


class MarketBrain:
    def __init__(self, focus: str = "SPY", max_trail: int = 180):
        self.focus = focus
        self._trail: List[Snapshot] = []
        self._max_trail = int(max_trail)
        self._lock = RLock()
        self._tz = ZoneInfo("America/New_York") if ZoneInfo else datetime.now().astimezone().tzinfo
        self._vwap: Dict[str, VWAPCalc] = {}
        self.vwap_buf: float = 0.10
        self.gate_consecutive: int = 2

    def _get_vwap_calc(self, sym: str) -> VWAPCalc:
        if sym not in self._vwap:
            self._vwap[sym] = VWAPCalc(self._tz)
        return self._vwap[sym]

    def latest_prices(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._trail[-1].prices) if self._trail else {}

    def latest_vwap(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._trail[-1].vwap) if self._trail else {}

    def series_len(self, sym: Optional[str] = None, max_points: Optional[int] = None) -> int:
        sym = sym or self.focus
        count = sum(1 for snap in self._trail if snap.prices.get(sym) is not None)
        return min(count, max_points) if isinstance(max_points, int) else count

    def seed_vwap_from_candles(self, symbol: str, candles: List[dict]) -> None:
        self._get_vwap_calc(symbol).seed_from_candles(candles)

    def _series(self, sym: str, max_points: int = 400) -> List[float]:
        with self._lock:
            out: List[float] = []
            for snap in self._trail:
                px = snap.prices.get(sym)
                if px is not None:
                    out.append(px)
            if max_points and len(out) > max_points:
                out = out[-max_points:]
            return out

    @staticmethod
    def _ema_series(vals: List[float], n: int) -> List[float]:
        if n <= 0 or len(vals) < n:
            return []
        k = 2.0 / (n + 1.0)
        ema = sum(vals[:n]) / n
        out = [ema]
        for x in vals[n:]:
            ema = (x - ema) * k + ema
            out.append(ema)
        return out

    def _macd_pack(self, vals: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if len(vals) < max(slow, fast) + signal + 2:
            return None, None, None
        ema_fast = self._ema_series(vals, fast)
        ema_slow = self._ema_series(vals, slow)
        common = min(len(ema_fast), len(ema_slow))
        if common == 0:
            return None, None, None
        macd_series = [ema_fast[-common + i] - ema_slow[-common + i] for i in range(common)]
        if len(macd_series) < signal:
            return None, None, None
        k = 2.0 / (signal + 1.0)
        sig = sum(macd_series[:signal]) / signal
        for m in macd_series[signal:]:
            sig = (m - sig) * k + sig
        macd_val = macd_series[-1]
        hist = macd_val - sig
        return macd_val, sig, hist

    @staticmethod
    def _rsi(vals: List[float], n: int = 14) -> Optional[float]:
        if len(vals) < n + 1:
            return None
        gains = 0.0
        losses = 0.0
        for i in range(1, n + 1):
            ch = vals[i] - vals[i - 1]
            if ch >= 0:
                gains += ch
            else:
                losses += -ch
        avg_gain = gains / n
        avg_loss = losses / n
        for i in range(n + 1, len(vals)):
            ch = vals[i] - vals[i - 1]
            gain = ch if ch > 0 else 0.0
            loss = -ch if ch < 0 else 0.0
            avg_gain = (avg_gain * (n - 1) + gain) / n
            avg_loss = (avg_loss * (n - 1) + loss) / n
        if avg_loss == 0 and avg_gain == 0:
            return 50.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def _bollinger_bands(vals: List[float], n: int = 20, k: float = 2.0) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        if len(vals) < n:
            return None, None, None, None
        import statistics as stats
        window = vals[-n:]
        mid = sum(window) / n
        if len(window) < 2:
            return None, None, None, None
        sd = float(stats.pstdev(window))
        upper = mid + k * sd
        lower = mid - k * sd
        last_px = vals[-1]
        pos = None
        if upper != lower:
            pos = max(-1.0, min(1.0, (last_px - mid) / (upper - mid)))
        return mid, upper, lower, pos

    def ta_pack(self) -> Dict[str, Optional[float]]:
        sym = self.focus
        vs = self._series(sym, 400)
        out: Dict[str, Optional[float]] = {
            "rsi14": None, "macd": None, "macd_signal": None, "macd_hist": None,
            "bb_pos": None, "bb_mid": None, "bb_up": None, "bb_lo": None,
            "sma5": None, "sma20": None,
        }
        if len(vs) >= 5:
            out["sma5"] = sum(vs[-5:]) / 5.0
        if len(vs) >= 20:
            out["sma20"] = sum(vs[-20:]) / 20.0
        rsi = self._rsi(vs, 14)
        macd_val, macd_sig, macd_hist = self._macd_pack(vs)
        bb_mid, bb_up, bb_lo, bb_pos = self._bollinger_bands(vs, 20, 2.0)
        out["rsi14"] = rsi
        out["macd"] = macd_val
        out["macd_signal"] = macd_sig
        out["macd_hist"] = macd_hist
        out["bb_pos"] = bb_pos
        out["bb_mid"] = bb_mid
        out["bb_up"] = bb_up
        out["bb_lo"] = bb_lo
        return out

    def vwap(self, sym: str) -> Optional[float]:
        return self._get_vwap_calc(sym).value()

    def vwap_spread(self, sym: str) -> Optional[float]:
        px = self.latest_prices().get(sym)
        vw = self.vwap(sym)
        if px is None or vw is None:
            return None
        return px - vw

    def _consecutive_vs_vwap(self, sym: str, above: bool, buf: float, need: int) -> int:
        cnt = 0
        for snap in reversed(self._trail):
            px = snap.prices.get(sym)
            vw = snap.vwap.get(sym)
            if px is None or vw is None:
                break
            diff = px - vw
            ok = diff > buf if above else diff < -buf
            if ok:
                cnt += 1
                if cnt >= need:
                    break
            else:
                break
        return cnt

    def ingest(self, quotes: Dict[str, Quote]):
        def _pick_ts() -> float:
            fq = quotes.get(self.focus)
            if fq and fq.timestamp:
                return float(fq.timestamp)
            ts_list = [q.timestamp for q in quotes.values() if q.timestamp is not None]
            if ts_list:
                return float(max(ts_list))
            return time.time()

        ts = _pick_ts()
        prices: Dict[str, float] = {}
        vmap: Dict[str, float] = {}

        for s, qt in quotes.items():
            px = qt.px
            if px is not None:
                prices[s] = px
            self._get_vwap_calc(s).update_live(qt.timestamp or ts, px, qt.total_volume)
            vw = self._get_vwap_calc(s).value()
            if vw is not None:
                vmap[s] = vw

        self._trail.append(Snapshot(t=ts, prices=prices, vwap=vmap))
        if len(self._trail) > self._max_trail:
            self._trail.pop(0)

    def _pct_delta(self, sym: str, window: int = 5) -> Optional[float]:
        import statistics as stats
        vals = [snap.prices.get(sym) for snap in self._trail[-window:] if sym in snap.prices]
        if len(vals) < 2:
            return None
        base = stats.median(vals[:-1]) if len(vals) > 2 else vals[0]
        last = vals[-1]
        if base in (None, 0) or last is None:
            return None
        return (last - base) / abs(base)

    def pct_delta(self, sym: str, window: int = 5) -> Optional[float]:
        return self._pct_delta(sym, window)

    def bias(self) -> str:
        d = self.pct_delta(self.focus)
        if d is None:
            return "neutral"
        up, dn = 0.0018, -0.0018
        if d > up:
            return "bullish"
        if d < dn:
            return "bearish"
        return "neutral"

    def pre_order_check(self, side: str) -> Tuple[bool, str, Dict[str, bool]]:
        side = side.lower().strip()
        vw = self.latest_vwap()
        need = self.gate_consecutive
        buf = self.vwap_buf

        def abv(sym): return self._consecutive_vs_vwap(sym, True, buf, need) >= need
        def bel(sym): return self._consecutive_vs_vwap(sym, False, buf, need) >= need

        spy_up = abv("SPY")
        qqq_up = abv("QQQ")
        nvda_up = abv("NVDA")
        spy_dn = bel("SPY")
        qqq_dn = bel("QQQ")
        nvda_dn = bel("NVDA")

        dxy_rising = (self.pct_delta("DXY") or 0) > 0.001
        tnx_rising = (self.pct_delta("TNX") or 0) > 0.001
        xlf_weak = bel("XLF") or ((self.pct_delta("XLF") or 0) < -0.001)

        triggers = {
            "spy>vwap": spy_up, "qqq>vwap": qqq_up, "nvda>vwap": nvda_up,
            "spy<vwap": spy_dn, "qqq<vwap": qqq_dn, "nvda<vwap": nvda_dn,
            "dxy_rising": dxy_rising, "tnx_rising": tnx_rising, "xlf_weak": xlf_weak,
            "have_vwap": ("SPY" in vw and "QQQ" in vw and "NVDA" in vw),
        }

        if side in {"call", "calls", "c"}:
            ok = triggers["spy>vwap"] and triggers["qqq>vwap"] and triggers["nvda>vwap"] and not (dxy_rising or tnx_rising)
            reason = "OK" if ok else "Needs SPY/QQQ/NVDA above VWAP and DXY/TNX not rising"
            return ok, reason, triggers

        if side in {"put", "puts", "p"}:
            ok = triggers["spy<vwap"] and triggers["qqq<vwap"] and (xlf_weak or dxy_rising or tnx_rising)
            reason = "OK" if ok else "Needs SPY/QQQ below VWAP and (XLF weak or DXY/TNX rising)"
            return ok, reason, triggers

        return False, "Unknown side; use calls/puts", triggers

    from typing import List

    def narrative(
            self,
            symbols: List[str],
            *,
            structured: bool = True,
            include_extras: bool = False,
    ) -> str:
        px = self.latest_prices()
        vws = self.latest_vwap()
        f: str = str(self.focus or "SPY")

        fpx = px.get(f)
        fvw = vws.get(f)
        d_focus = self.pct_delta(f)
        dxy_d = self.pct_delta("DXY")
        tnx_d = self.pct_delta("TNX")

        def fmt3_safe(val: Optional[float]) -> str:
            return fmt3(val, none="n/a")

        def dir_label(d: Optional[float]) -> str:
            if d is None:
                return "flat"
            if d > 0.001:
                return "up"
            if d < -0.001:
                return "down"
            return "flat"

        # Focus line (+ VWAP spread when available)
        parts: List[str] = []
        chg = (d_focus or 0.0)
        if fpx is not None:
            if fvw is not None:
                spread = fpx - fvw
                parts.append(
                    f"{f} {fmt3_safe(fpx)} ({chg:+0.2%}) vwap {fmt3_safe(fvw)} ({fmt3_safe(spread)}) — {self.bias()}")
            else:
                parts.append(f"{f} {fmt3_safe(fpx)} ({chg:+0.2%}) — {self.bias()}")

        # Macro line
        macro_bits: List[str] = []
        if "DXY" in px:
            macro_bits.append(f"DXY {dir_label(dxy_d)} ({(dxy_d or 0):+0.2%})")
        if "TNX" in px:
            macro_bits.append(f"TNX {dir_label(tnx_d)} ({(tnx_d or 0):+0.2%})")

        # TA line
        ta = self.ta_pack()
        ta_bits: List[str] = []
        if ta.get("rsi14") is not None:
            ta_bits.append(f"RSI14 {ta['rsi14']:.0f}")
        if ta.get("macd") is not None and ta.get("macd_signal") is not None and ta.get("macd_hist") is not None:
            macd_arrow = "↑" if ta["macd"] > ta["macd_signal"] else "↓"
            ta_bits.append(f"MACD {ta['macd']:+.3f}{macd_arrow}")
        if ta.get("bb_pos") is not None:
            ta_bits.append(f"BBPos {fmt3_safe(ta['bb_pos'])}")
        if ta.get("sma5") is not None and ta.get("sma20") is not None:
            slope = (ta["sma5"] or 0) - (ta["sma20"] or 0)
            ta_bits.append(f"SMA5-20 {fmt3_safe(slope)}")

        # Extras (built once; placed based on `structured`)
        extras: List[str] = []
        if include_extras:
            for s in symbols:
                if s in {f, "DXY", "TNX", "$DXY", "$TNX", "UUP", "IEF", "TLT"}:
                    continue
                v = px.get(s)
                vw = vws.get(s)
                if v is not None and vw is not None:
                    extras.append(f"{s} {fmt3_safe(v)}/vwap {fmt3_safe(vw)} ({fmt3_safe(v - vw)})")
                elif v is not None:
                    extras.append(f"{s} {fmt3_safe(v)}")

        # Compose body according to `structured`
        if structured and parts:
            lines: List[str] = [parts[0]]
            if macro_bits:
                lines.append("Macro: " + "; ".join(macro_bits))
            if ta_bits:
                lines.append("TA: " + ", ".join(ta_bits))
            if extras:
                lines.append("Watchlist: " + "; ".join(extras))
            result = "\n".join(lines)
        else:
            flat_parts = []
            if parts:
                flat_parts.append(parts[0])
            if macro_bits:
                flat_parts.append("; ".join(macro_bits))
            if ta_bits:
                flat_parts.append("TA: " + ", ".join(ta_bits))
            if extras:
                flat_parts.append("Watchlist: " + "; ".join(extras))
            result = " | ".join(flat_parts) if flat_parts else "No prices yet"

        # Deterministic bias line (unchanged)
        try:
            px_f = fpx
            vw_f = fvw
            macd = ta.get("macd")
            macd_sig = ta.get("macd_signal")
            rsi = ta.get("rsi14")

            votes = 0.0
            if px_f is not None and vw_f is not None:
                votes += 1 if px_f > vw_f else -1 if px_f < vw_f else 0
            if macd is not None and macd_sig is not None:
                votes += 1 if macd >= macd_sig else -1
            if dxy_d is not None:
                votes += -1 if dxy_d > 0 else 1 if dxy_d < 0 else 0
            if tnx_d is not None:
                votes += -1 if tnx_d > 0 else 1 if tnx_d < 0 else 0
            if rsi is not None:
                votes += -0.5 if rsi >= 70 else 0.5 if rsi <= 30 else 0

            if votes >= 1.5:
                fb_bias = "bullish"
            elif votes <= -1.5:
                fb_bias = "bearish"
            else:
                fb_bias = "neutral"

            features = sum(x is not None for x in [px_f, vw_f, macd, macd_sig, dxy_d, tnx_d, rsi])
            base_conf = min(0.9, 0.35 + 0.1 * max(0, features - 2))
            conf = max(0.3, min(0.9, base_conf * (1 + 0.25 * abs(votes))))

            rationale = (
                "Above VWAP & MACD≥sig" if fb_bias == "bullish" else
                "Below VWAP & MACD≤sig" if fb_bias == "bearish" else
                "Mixed VWAP/MACD/macro"
            )
            if rsi is not None and ((fb_bias == "bullish" and rsi >= 70) or (fb_bias == "bearish" and rsi <= 30)):
                rationale += "; RSI extreme"

            result = result + f"\nAI bias: {fb_bias} ({int(round(conf * 100))}%) — {rationale}"
        except (TypeError, ValueError, ArithmeticError) as e:
            _log = globals().get("log")
            if callable(_log):
                _log("(bias calc) skipped due to data type/arith issue:", e)

        return result


# =========================== Q&A helpers (OpenAI only) ===========================

def fmt3(x, *, none="n/a"):
    if x is None:
        return none
    try:
        d = Decimal(str(float(x))).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
        s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    except (InvalidOperation, ValueError, TypeError):
        return none


def _build_telemetry_for_qa(brain: MarketBrain) -> Dict[str, Any]:
    px = brain.latest_prices()
    f = brain.focus
    ta = brain.ta_pack()
    telemetry = {
        "focus": f,
        "focus_px": px.get(f),
        "deltas": {
            "focus": brain.pct_delta(f),
            "DXY": brain.pct_delta("DXY"),
            "TNX": brain.pct_delta("TNX"),
        },
        "prices": {k: v for k, v in px.items() if v is not None},
        "vwap": brain.latest_vwap(),
        "ta": ta,
        "t": time.time(),
    }
    telemetry["prices"].pop("$DXY", None)
    telemetry["prices"].pop("$TNX", None)
    return telemetry


def _qa_prompt(question: str, telemetry: Dict[str, Any], max_words: int = 120) -> str:
    f = telemetry.get("focus")
    px = telemetry.get("focus_px")
    deltas = telemetry.get("deltas", {})
    ta = telemetry.get("ta", {})
    vw = telemetry.get("vwap", {})

    def s(v): return fmt3(v, none="n/a")

    data_lines = [
        f"focus={f}",
        f"price={s(px)}",
        f"vwap={s(vw.get(f))}",
        f"rsi14={s(ta.get('rsi14'))}",
        f"macd={s(ta.get('macd'))}",
        f"macd_signal={s(ta.get('macd_signal'))}",
        f"macd_hist={s(ta.get('macd_hist'))}",
        f"bb_mid={s(ta.get('bb_mid'))}",
        f"bb_up={s(ta.get('bb_up'))}",
        f"bb_lo={s(ta.get('bb_lo'))}",
        f"sma5={s(ta.get('sma5'))}",
        f"sma20={s(ta.get('sma20'))}",
        f"focus_delta={s(deltas.get('focus'))}",
        f"dxy_delta={s(deltas.get('DXY'))}",
        f"tnx_delta={s(deltas.get('TNX'))}",
    ]
    data_block = "\n".join(data_lines)

    return (
        "You are a veteran macro/quant trading assistant.\n"
        "IMPORTANT: If you mention ANY numeric value, copy it EXACTLY from DATA below. If a value is missing, write 'n/a'.\n"
        "SMA are 5/20-sample intraday. Keep it < {max_words} words. Be concise.\n\n"
        "DATA:\n" + data_block + "\n\n"
        "QUESTION:\n" + question + "\n\n"
        "ANSWER:"
    ).replace("{max_words}", str(max_words))


def ai_answer(
    question: str,
    telemetry: Dict[str, Any],
    model: str = "gpt-5-nano",
    timeout: int = 60,
    max_words: int = 120
) -> Optional[str]:
    prompt = _qa_prompt(question, telemetry, max_words=max_words)
    if str(model).strip().lower() in AI_DISABLED:
        return None
    return _openai_responses_answer(prompt, model=model, timeout=timeout)

# =========================== History seeding (Polygon minute aggregates) ===========================

def _seed_brain_with_history(provider: PolygonQuoteProvider, brain: MarketBrain, symbol: str, bars: int = 60) -> bool:
    candles = provider.get_today_minute_aggregates(symbol) or []
    if not candles:
        log(f"No aggregates available for {symbol}")
        return False

    brain.seed_vwap_from_candles(symbol, candles)

    count = 0
    for c in candles[-bars:]:
        close = coerce_float(c.get("close") or c.get("c"))
        if close is None:
            continue
        ts = coerce_ts(c.get("datetime") or c.get("t") or c.get("timestamp") or time.time())
        brain.ingest({symbol: Quote(symbol=symbol, last=close, bid=None, ask=None, timestamp=ts)})
        count += 1

    return count > 0

# =========================== Q&A modes ===========================



def _csv_safe(x) -> str:
    return "" if x is None else str(x)


def qa_once(
    provider: BaseQuoteProvider,
    user_symbols: List[str],
    focus: str,
    model: str,
    timeout: int,
    max_words: int,
    history: int,
    question: str,
):
    display_symbols = expand_symbols(user_symbols)
    brain = MarketBrain(focus=focus, max_trail=history)
    try:
        if isinstance(provider, PolygonQuoteProvider):
            _seed_brain_with_history(provider, brain, focus, bars=min(80, history))
    except Exception as e:
        log(f"(seed warning) {e}")

    quotes = provider.get_quotes(display_symbols)
    enrich_with_macros(quotes)
    brain.ingest(quotes)
    telemetry = _build_telemetry_for_qa(brain)
    ans = ai_answer(question, telemetry, model=model, timeout=timeout, max_words=max_words)
    print(ans if ans else "Q&A unavailable (model off or request failed).")


class BackgroundSampler(threading.Thread):
    def __init__(self, provider: BaseQuoteProvider, display_symbols: List[str], brain: MarketBrain, interval: int = 5):
        super().__init__(daemon=True)
        self.provider = provider
        self.display_symbols = display_symbols
        self.brain = brain
        self.interval = max(2, int(interval))
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                quotes = self.provider.get_quotes(self.display_symbols)
                enrich_with_macros(quotes)
                self.brain.ingest(quotes)
            except Exception as e:
                log(f"bg-sampler error: {e}")
            self._stop_evt.wait(self.interval)


def _fmt2(v):
    try:
        return fmt3(v, none="—")
    except (TypeError, ValueError):
        return "—"


def _indicators_block(brain: MarketBrain) -> str:
    ta = brain.ta_pack()
    bits = []
    rsi = ta.get("rsi14")
    if rsi is not None:
        band = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
        bits.append(f"RSI14 {rsi:.1f} ({band})")
    macd, sig = ta.get("macd"), ta.get("macd_signal")
    if macd is not None and sig is not None:
        dir_ = "bullish" if macd >= sig else "bearish"
        bits.append(f"MACD {macd:+.3f} vs sig {sig:+.3f} ({dir_})")
    bb_pos = ta.get("bb_pos")
    if bb_pos is not None:
        bits.append(f"BB pos {bb_pos:+.2f}")
    vwap = brain.vwap(brain.focus)
    px = brain.latest_prices().get(brain.focus)
    if vwap is not None and px is not None:
        spread = px - vwap
        rel = "above" if spread > 0 else "below" if spread < 0 else "at"
        bits.append(f"VWAP: {rel} by {spread:+.2f}")
    return " | ".join(bits) if bits else "Indicators warming up…"


def qa_chat(provider: BaseQuoteProvider, user_symbols: List[str], focus: str, model: str, timeout: int, max_words: int, history: int, interval: int):
    display_symbols = expand_symbols(user_symbols)
    brain = MarketBrain(focus=focus, max_trail=history)
    try:
        if isinstance(provider, PolygonQuoteProvider):
            _seed_brain_with_history(provider, brain, focus, bars=min(120, history))
    except Exception as e:
        log(f"(seed warning) {e}")

    sampler = BackgroundSampler(provider, display_symbols, brain, interval=interval)
    sampler.start()

    def _print_plan(trade_plan: Optional[Dict[str, Any]]) -> None:
        if not trade_plan:
            print("WAIT (—%) — intra\nEntry: — | Stop: — | TP: —\nBias: neutral | Note: No clear edge\n")
            return
        conf_val = trade_plan.get("confidence", 0.0)
        try:
            pct = int(round(float(conf_val) * 100))
        except (TypeError, ValueError):
            pct = 0
        action = str(trade_plan.get("action", "wait")).upper()
        timeframe = str(trade_plan.get("timeframe", "intra"))
        entry_txt = str(trade_plan.get("entry", "—"))
        stop_txt = str(trade_plan.get("stop", "—"))
        tp_txt = str(trade_plan.get("take_profit", "—"))
        bias_txt = str(trade_plan.get("bias", "neutral"))
        note_raw = trade_plan.get("note", "—")
        note_txt = "—" if note_raw in (None, "") else str(note_raw)
        line1 = f"{action} ({pct}%) — {timeframe}"
        line2 = f"Entry: {entry_txt} | Stop: {stop_txt} | TP: {tp_txt}"
        line3 = f"Bias: {bias_txt} | Note: {note_txt}"
        print(f"{line1}\n{line2}\n{line3}\n")

    print("Q&A chat — type 'help' for commands; 'quit' to leave.")
    try:
        while True:
            try:
                q = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                continue

            cmd = q.lower().strip()

            if cmd in {"quit", "exit", "q"}:
                break

            if cmd in {"help", "h", "?"}:
                print(CHAT_HELP, "\n")
                continue

            if cmd in {"time", "now"}:
                print(f"{virginia_now_str()}\n")
                continue

            if cmd in {"tele", "telemetry"}:
                print(json.dumps(_build_telemetry_for_qa(brain), indent=2), "\n")
                continue

            if cmd == "gates":
                ok_c, why_c, trig_c = brain.pre_order_check("calls")
                ok_p, why_p, trig_p = brain.pre_order_check("puts")
                print(json.dumps({
                    "calls": {"ok": ok_c, "why": why_c, "triggers": trig_c},
                    "puts": {"ok": ok_p, "why": why_p, "triggers": trig_p},
                    "params": {"vwap_buf": brain.vwap_buf, "consecutive": brain.gate_consecutive}
                }, indent=2), "\n")
                continue

            if cmd == "snap":
                f = brain.focus
                px = brain.latest_prices().get(f)
                vw = brain.latest_vwap().get(f)
                ta = brain.ta_pack()
                parts = []
                if px is not None: parts.append(f"PX {fmt3(px)}")
                if vw is not None and px is not None: parts.append(f"VWAP {fmt3(vw)} (Δ {fmt3(px - vw)})")
                if ta.get("rsi14") is not None: parts.append(f"RSI {ta['rsi14']:.1f}")
                print(" | ".join(parts) if parts else "No telemetry", "\n"); continue

            if cmd.startswith(("price", "px")) or "price of" in cmd:
                syms = SYMBOL_RE.findall(q.upper()) or [brain.focus]
                pxs, vws = brain.latest_prices(), brain.latest_vwap()
                lines = []
                for s in syms:
                    v = pxs.get(s)
                    vw = vws.get(s)
                    delta = (v - vw) if (v is not None and vw is not None) else None
                    lines.append(f"{s:>6}: {_fmt2(v)}  vwap {_fmt2(vw)}  Δ {_fmt2(delta)}")
                print("\n".join(lines) + "\n"); continue

            if cmd in {"ind", "indicators", "status"}:
                print(_indicators_block(brain) + "\n"); continue

            if cmd in {"analysis", "market analysis"}:
                telemetry = _build_telemetry_for_qa(brain)
                ans = ai_answer("market analysis", telemetry, model=model, timeout=timeout, max_words=max_words)
                print((ans or "No answer") + "\n")
                continue

            # Simple deterministic plans
            if cmd.startswith("plan"):
                want_long = " long" in f" {cmd} "
                want_short = " short" in f" {cmd} "
                ta = brain.ta_pack()
                f = brain.focus
                px = brain.latest_prices().get(f)
                vwap = brain.latest_vwap().get(f)
                bb_mid = ta.get("bb_mid"); bb_up = ta.get("bb_up"); bb_lo = ta.get("bb_lo")
                buf = max(brain.vwap_buf, 0.35)
                if None in (px, vwap, bb_mid, bb_up, bb_lo):
                    _print_plan(None)
                    continue
                if want_long or (not want_short and px > vwap and ta.get("macd") is not None and ta.get("macd_signal") is not None and ta["macd"] >= ta["macd_signal"]):
                    plan = {"action":"long","timeframe":"intra","entry":"buy pullback to VWAP",
                            "stop":fmt3(vwap - buf),"take_profit":fmt3((bb_up or px)+0.20),
                            "bias":"bullish","confidence":0.55,"note":"Above VWAP + MACD≥sig"}
                elif want_short or (px < vwap and ta.get("macd") is not None and ta.get("macd_signal") is not None and ta["macd"] <= ta["macd_signal"]):
                    plan = {"action":"short","timeframe":"intra","entry":"sell bounce to VWAP",
                            "stop":fmt3(vwap + buf),"take_profit":fmt3((bb_lo or px)-0.20),
                            "bias":"bearish","confidence":0.55,"note":"Below VWAP + MACD≤sig"}
                else:
                    plan = None
                _print_plan(plan); continue

            telemetry = _build_telemetry_for_qa(brain)
            ans = ai_answer(q, telemetry, model=model, timeout=timeout, max_words=max_words)
            print(f"A> {ans}\n" if ans else "A> (no answer — model off or request failed)\n")

    finally:
        sampler.stop()

# =========================== Loop ===========================

CHAT_HELP = """\
LLAMA_DRAMA — interactive commands (OpenAI-only)
----------------------------------
help / h / ?         Show this help
quit / exit / q      Leave chat
ta / status          Print TA pack + VWAP for focus
time / now           Print current market time (America/New_York)
tele / telemetry     Dump full telemetry JSON
gates                Show CALLS/PUTS trade gates + triggers
snap                 One-line deterministic snapshot (no LLM)
plan long            1-min scalp plan (long) using VWAP + BB
plan short           1-min scalp plan (short) using VWAP + BB
market analysis      Ask the model for a short analysis (numbers locked to TELEMETRY)
Give me a 1-minute scalp plan with stop/target using VWAP and BB.
"""

def pretty_prices(quotes: Dict[str, Quote]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, q in quotes.items():
        if k in {"DXY", "TNX"}:
            continue
        v = q.px
        if v is None:
            continue
        try:
            out[k] = round(float(v), 3)
        except (TypeError, ValueError):
            continue
    return out


class TickerLoop:
    def __init__(
        self,
        provider: BaseQuoteProvider,
        symbols: List[str],
        interval: int = 20,
        focus: str = "SPY",
        model: str = "gpt-5-nano",
        timeout: int = 45,
        emit_json: bool = False,
        csv_path: Optional[str] = None,
        once: bool = False,
        verbose: bool = False,
        vwap_buf: float = 0.10,
        gate_consecutive: int = 2,
        print_gates: bool = False,
        # News controls
        news_on: bool = False,
        news_since_h: int = 12,
        news_limit: int = 12,
        news_weight: float = 0.30,
        print_news: bool = False,
    ):
        self.display_symbols = expand_symbols(symbols)
        self.request_symbols = normalize_provider_symbols(self.display_symbols)
        self.provider = provider
        self.interval = max(1, int(interval))
        self.brain = MarketBrain(focus=focus)
        self.brain.vwap_buf = float(vwap_buf)
        self.brain.gate_consecutive = int(gate_consecutive)
        self.model = model
        self.timeout = timeout
        self._stop = Event()
        self.emit_json = emit_json
        self.csv_path = csv_path
        self.once = once
        self.verbose = verbose
        self._failures = 0
        self._last_ok: Optional[float] = None
        self._print_gates = print_gates

        # News config — optional
        try:
            from news_client import (
                NewsFetcher,
                news_bias_score,
                compress_bullets,
                map_symbols_to_keywords,
            )
        except ImportError:
            self.NewsFetcher = None
            self.news_bias_score = None
            self.compress_bullets = None
            self.map_symbols_to_keywords = None

        self.news_on = bool(news_on) and bool(NEWSAPI_KEY) and callable(self.NewsFetcher)
        self.news_since_h = int(news_since_h)
        self.news_limit = int(news_limit)
        self.news_weight = float(news_weight)
        self.print_news = bool(print_news)
        self._news_fetcher = None
        if self.news_on:
            try:
                self._news_fetcher = self.NewsFetcher()  # type: ignore[call-arg]
            except Exception as e:
                log(f"(news) failed to initialize NewsFetcher: {e}")
                self.news_on = False

        # Quiet-print defaults
        self.window_points: int = 5
        self.min_print: float = 0.001
        self.always_print: bool = False

        try:
            if isinstance(self.provider, PolygonQuoteProvider):
                _seed_brain_with_history(self.provider, self.brain, focus, bars=min(120, 3 * self.interval))
        except Exception as e:
            log(f"(seed warning) {e}")

    def _get_news(self) -> tuple[list, float, str]:
        if not self.news_on or self._news_fetcher is None:
            return [], 0.0, ""
        symbols = [s for s in getattr(self, "display_symbols", []) if isinstance(s, str)]
        macro_defaults = ["Federal Reserve", "Treasury yields", "inflation", "CPI", "PCE", "jobs", "FOMC"]

        if callable(self.map_symbols_to_keywords):
            try:
                keywords = self.map_symbols_to_keywords(symbols)  # type: ignore[misc]
            except (ValueError, TypeError):
                keywords = list({*(symbols or []), *macro_defaults})
        else:
            keywords = list({*(symbols or []), *macro_defaults})

        try:
            items = self._news_fetcher.get(  # type: ignore[union-attr]
                keywords, since_hours=int(self.news_since_h), limit=int(self.news_limit)
            )
        except (Timeout, RequestException, ValueError, TypeError) as e:
            log(f"(news) fetch error: {e}")
            return [], 0.0, ""

        if not items:
            return [], 0.0, ""

        try:
            bias = float(self.news_bias_score(items)) if callable(self.news_bias_score) else 0.0  # type: ignore[misc]
        except (ValueError, TypeError):
            bias = 0.0

        try:
            bullets = (
                self.compress_bullets(items, k=min(5, len(items)))  # type: ignore[misc]
                if callable(self.compress_bullets) else ""
            )
        except (ValueError, TypeError):
            bullets = ""

        return items, bias, bullets

    def _snapshot_payload(self) -> Dict[str, Any]:
        px = self.brain.latest_prices()
        vwp = self.brain.latest_vwap()
        f = self.brain.focus
        d_focus = self.brain.pct_delta(f)
        dxy_d = self.brain.pct_delta("DXY")
        tnx_d = self.brain.pct_delta("TNX")
        ta = self.brain.ta_pack()

        pruned_px: Dict[str, float] = {k: v for k, v in px.items() if v is not None}
        payload: Dict[str, Any] = {
            "t": time.time(),
            "focus": f,
            "bias": self.brain.bias(),
            "deltas": {"focus": d_focus, "DXY": dxy_d, "TNX": tnx_d},
            "prices": pruned_px,
            "vwap": {k: v for k, v in vwp.items() if k in pruned_px or k == f},
            "ta": ta,
            "ai": None,   # deterministic text bias only
            "gates": None,
            "news": None,
            "context": None,
            "bias_code": None,
        }

        ok_c, why_c, trig_c = self.brain.pre_order_check("calls")
        ok_p, why_p, trig_p = self.brain.pre_order_check("puts")
        payload["gates"] = {
            "calls": {"ok": ok_c, "why": why_c, "triggers": trig_c},
            "puts": {"ok": ok_p, "why": why_p, "triggers": trig_p},
            "params": {"vwap_buf": self.brain.vwap_buf, "consecutive": self.brain.gate_consecutive},
        }
        return payload

    def _write_csv(self, payload: dict):
        if not self.csv_path:
            return
        file_exists = os.path.exists(self.csv_path)
        try:
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if not file_exists:
                    w.writerow(["t", "focus", "bias", "focus_delta", "dxy_delta", "tnx_delta", "prices_json", "vwap_json", "ai_json", "gates_json", "news_json"])
                deltas = payload.get("deltas", {})
                w.writerow(
                    [
                        int(payload.get("t", time.time())),
                        payload.get("focus", ""),
                        payload.get("bias", ""),
                        _csv_safe(deltas.get("focus")),
                        _csv_safe(deltas.get("DXY")),
                        _csv_safe(deltas.get("TNX")),
                        json.dumps(payload.get("prices", {}), separators=(",", ":")),
                        json.dumps(payload.get("vwap", {}), separators=(",", ":")),
                        json.dumps(payload.get("ai"), separators=(",", ":")) if payload.get("ai") is not None else "",
                        json.dumps(payload.get("gates", {}), separators=(",", ":")),
                        json.dumps(payload.get("news", {}), separators=(",", ":")) if payload.get("news") else "",
                    ]
                )
        except Exception as e:
            log("CSV write error:", e)

    def _poll_once(self) -> Tuple[Dict[str, Quote], str]:
        if self.verbose:
            log("Requesting:", self.request_symbols)
        quotes = self.provider.get_quotes(self.display_symbols)
        enrich_with_macros(quotes)
        self.brain.ingest(quotes)
        text = self.brain.narrative(
            self.display_symbols,
            structured=True,
            include_extras=False,
        )

        if self.news_on and self.print_news:
            items, bias, _ = self._get_news()
            text += f" | News: {len(items)} items, bias {bias:+.2f}"
        if self._print_gates:
            ok_c, why_c, _ = self.brain.pre_order_check("calls")
            ok_p, why_p, _ = self.brain.pre_order_check("puts")
            text += f" | Gates: CALLS={ok_c} ({why_c}) PUTS={ok_p} ({why_p})"
        return quotes, text

    def _should_print(self) -> bool:
        if self.always_print:
            return True
        d_focus = self.brain.pct_delta(self.brain.focus, window=self.window_points)
        dxy_d = self.brain.pct_delta("DXY", window=self.window_points)
        tnx_d = self.brain.pct_delta("TNX", window=self.window_points)
        return any(abs(x or 0.0) >= self.min_print for x in (d_focus, dxy_d, tnx_d))

    def run(self):
        backoff = 5
        max_backoff = 60

        def _handle_exc(prefix, err):
            nonlocal backoff
            log(prefix, f"{type(err).__name__}: {err}")
            if self.verbose:
                traceback.print_exc()
            self._failures += 1
            time.sleep(backoff)
            backoff = min(max_backoff, backoff * 2)

        # Initial poll
        try:
            quotes, text = self._poll_once()
            self._last_ok = time.time()
            self._failures = 0
            log("Prices:", pretty_prices(quotes))
            if self._should_print():
                print(text)
            else:
                log(f"No material change (<{self.min_print:.2%}); skipping print.")
            payload = self._snapshot_payload()
            if self.emit_json:
                print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
            self._write_csv(payload)
        except Exception as e:
            _handle_exc("Initial poll failed", e)

        if self.once:
            return

        # Subsequent polls
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                quotes, text = self._poll_once()
                self._last_ok = time.time()
                self._failures = 0
                backoff = 5
                log("Prices:", pretty_prices(quotes))
                if self._should_print():
                    print(text)
                else:
                    log(f"No material change (<{self.min_print:.2%}); skipping print.")
                payload = self._snapshot_payload()
                if self.emit_json:
                    print(json.dumps(payload, separators=(",", ":")))
                self._write_csv(payload)
            except Exception as e:
                _handle_exc("Poll failed", e)

    def stop(self):
        self._stop.set()

# =========================== CLI ===========================

def main():
    p = argparse.ArgumentParser(description="QuantEcho — Polygon data + VWAP/TA + optional OpenAI Q&A")
    p.add_argument("--history", type=int, default=180, help="Max snapshots to retain (controls TA horizon)")
    p.add_argument("--window", type=int, default=5, help="Points used for delta calc")
    p.add_argument("--min-print", type=float, default=0.0010, help="Skip prints if abs deltas for focus/DXY/TNX are all below this")
    p.add_argument("--always-print", action="store_true", help="Print every poll even if unchanged")
    p.add_argument("--symbols", nargs="*", default=["SPY", "NVDA", "QQQ", "XLF"], help="Symbols (UUP/IEF/TLT proxies auto-added)")
    p.add_argument("--interval", type=int, default=20, help="Seconds between summaries (min 5)")
    p.add_argument("--focus", default="SPY", help="Primary symbol for bias calc")
    p.add_argument("--model", default=os.getenv("OPENAI_MODEL", "gpt-5-nano"), help="OpenAI model; use 'off' to disable")
    p.add_argument("--timeout", type=int, default=45, help="Seconds to wait for model responses")
    p.add_argument("--json", action="store_true", help="Also emit a JSON payload per poll")
    p.add_argument("--once", action="store_true", help="Poll once and exit")
    p.add_argument("--csv", default=None, help="Append poll snapshots to this CSV path")
    p.add_argument("--verbose", action="store_true", help="Print exception tracebacks on errors")
    # News flags
    p.add_argument("--news", action="store_true", help="Fetch NewsAPI headlines and include in JSON + model prompt")
    p.add_argument("--news-since-h", type=int, default=6, help="Look back this many hours for news")
    p.add_argument("--news-limit", type=int, default=12, help="Max headlines to consider")
    p.add_argument("--news-weight", type=float, default=0.30, help="Weight for news bias when nudging AI summary (0..1)")
    p.add_argument("--print-news", action="store_true", help="Append a one-liner with news count+bias to prints")

    # Q&A flags
    p.add_argument("--ask", default=None, help="Ask the model a single question using a fresh snapshot, then exit")
    p.add_argument("--chat", action="store_true", help="Interactive Q&A mode (fresh snapshot per question)")
    p.add_argument("--qa-max-words", type=int, default=120, help="Max words for Q&A answers")

    args = p.parse_args()

    # OpenAI key check
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not key and str(args.model).lower().strip() not in AI_DISABLED:
        log("[cfg] OPENAI_API_KEY not set; Q&A lines will be disabled until you set it.")

    if not POLYGON_API_KEY:
        log("[cfg] POLYGON_API_KEY not set — you will get empty snapshots.")

    provider = PolygonQuoteProvider(api_key=POLYGON_API_KEY, base_url=POLYGON_BASE_URL, verbose=args.verbose)

    if args.ask:
        qa_once(
            provider,
            user_symbols=args.symbols,
            focus=args.focus,
            model=args.model,
            timeout=args.timeout,
            max_words=args.qa_max_words,
            history=args.history,
            question=args.ask,
        )
        return

    if args.chat:
        qa_chat(
            provider,
            user_symbols=args.symbols,
            focus=args.focus,
            model=args.model,
            timeout=args.timeout,
            max_words=args.qa_max_words,
            history=args.history,
            interval=args.interval,
        )
        return

    loop = TickerLoop(
        provider,
        symbols=args.symbols,
        interval=args.interval,
        focus=args.focus,
        model=args.model,
        timeout=args.timeout,
        emit_json=args.json,
        csv_path=args.csv,
        once=args.once,
        verbose=args.verbose,
        vwap_buf=getattr(args, "vwap_buf", 0.10) if hasattr(args, "vwap_buf") else 0.10,
        gate_consecutive=getattr(args, "gate_consecutive", 2) if hasattr(args, "gate_consecutive") else 2,
        print_gates=getattr(args, "print_gates", False),
        news_on=bool(args.news),
        news_since_h=int(args.news_since_h),
        news_limit=int(args.news_limit),
        news_weight=float(args.news_weight),
        print_news=bool(args.print_news),
    )
    loop.window_points = args.window
    loop.min_print = float(args.min_print)
    loop.always_print = bool(args.always_print)
    loop.brain._max_trail = args.history

    try:
        loop.run()
    except KeyboardInterrupt:
        log("Stopping…")
    finally:
        loop.stop()

if __name__ == "__main__":
    main()
