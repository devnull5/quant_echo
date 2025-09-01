# ollama_voice.py — Schwab-only (text), with DXY/TNX context (no futures)
#
# NEW in this build:
# - True intraday VWAP per symbol (seeded from today's minute candles; maintained via quote.totalVolume deltas)
# - Trade gates for 0DTE with hysteresis (N consecutive snapshots beyond VWAP buffer)
# - Narrative augmented with VWAP spread; JSON payload includes VWAP + Gates; Q&A telemetry includes VWAP
#
# Existing goodies kept:
# - Schwab Trader API ONLY (no dummy, no TTS)
# - Skips futures (/ES, /YM)
# - DXY via UUP; TNX via IEF/TLT (invert)
# - Resilient loop (exponential backoff), bias calc, TA pack (RSI, MACD, BB, SMA tilt)
# - Q&A single-shot (--ask "...") and interactive (--chat)

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event, RLock
from typing import Any, Dict, List, Optional, Tuple

from requests.exceptions import ConnectionError as ReqConnectionError, RequestException, Timeout

# ====== HARD-CODED SCHWAB CREDS (REPLACE THESE) ======
API_KEY = "BNsxRXtiqGbb06N7eAWUYGHrFDwetM7eyCu149U4QoixBNA4"  # exact client id from portal
APP_SECRET = "Teopc9FVQ7EVZ36puE3izYW6Uy1FBLc8Px0GmKc7ZCl9cvv332qTbpdbFO6k3uCf"  # app secret
CALLBACK_URL = "https://127.0.0.1:8182"  # must match portal exactly
TOKEN_PATH = r"C:\Users\Filip\Projects\schwab_bot\tokens.json"
# =====================================================

# When any of these are passed as the model name, Ollama is treated as disabled
OLLAMA_DISABLED = {"off", "none", "", "0", "false", "disabled"}


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


# ---------- Macro aliases (DXY/TNX) ----------
DXY_ALIASES = ["$DXY", "DXY", "UUP"]
TNX_ALIASES = ["$TNX", "TNX", "IEF", "TLT"]
INVERSE_PROXIES = {"IEF", "TLT"}
REQUEST_FILTER_DROP = {"DXY", "TNX", "$DXY", "$TNX"}  # placeholders we synthesize locally


def expand_symbols(user_syms: List[str]) -> List[str]:
    """Add UUP and IEF/TLT proxies; leave user list otherwise."""
    base = list(user_syms)
    for s in DXY_ALIASES + TNX_ALIASES:
        if s not in base:
            base.append(s)
    return base


def normalize_provider_symbols(user_syms: List[str]) -> List[str]:
    """
    - Drop placeholders (DXY/TNX) and $-prefixed pseudo-tickers
    - Drop futures-like roots (anything starting with '/'), since Schwab retail doesn't return them
    - Keep equity/ETF symbols and proxies (UUP, IEF, TLT, etc.)
    """
    out: List[str] = []
    for s in user_syms:
        if s in REQUEST_FILTER_DROP or s.startswith("$"):
            continue
        if s.startswith("/"):
            log(f"Skipping futures-like symbol '{s}' (Schwab retail does not return futures quotes).")
            continue
        if s not in out:
            out.append(s)
    return out


def _fmt_local(ts: float | None = None, fmt: str = "%Y-%m-%d %H:%M:%S %Z%z") -> str:
    dt = (datetime.fromtimestamp(ts).astimezone() if ts is not None else datetime.now().astimezone())
    return dt.strftime(fmt)


def _fmt_utc(ts: float | None = None, fmt: str = "%Y-%m-%d %H:%M:%S UTC") -> str:
    dt = (datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else datetime.now(timezone.utc))
    return dt.strftime(fmt)


try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Py 3.9+
except ModuleNotFoundError:
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception  # fallback type


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


def virginia_now_str(fmt: str = "%Y-%m-%d %I:%M:%S %p %Z") -> str:
    """Return current time in Virginia (America/New_York)."""
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York")).strftime(fmt)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().strftime(fmt)


def _is_rth(ts: float | None = None) -> bool:
    """True during NYSE RTH (Mon–Fri, 09:30–16:00 America/New_York)."""
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


# =========================== Quote Provider (Schwab only) ===========================

@dataclass
class Quote:
    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    timestamp: Optional[float] = None
    total_volume: Optional[float] = None  # NEW: cumulative volume (for live VWAP updates)

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2.0
        return None

    @property
    def px(self) -> Optional[float]:
        return (
            self.last
            if self.last is not None
            else (self.mid if self.mid is not None else (self.bid if self.bid is not None else self.ask))
        )


class BaseQuoteProvider:
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        raise NotImplementedError


class SchwabQuoteProvider(BaseQuoteProvider):
    def __init__(self):
        self.client = self._init_client()

    @staticmethod
    def _init_client():
        from schwab.auth import client_from_login_flow, easy_client

        log(f"[schwab] callback_url='{CALLBACK_URL}' token_exists={os.path.exists(TOKEN_PATH)}")
        token_dir = os.path.dirname(TOKEN_PATH)
        if token_dir and not os.path.isdir(token_dir):
            os.makedirs(token_dir, exist_ok=True)

        if os.path.exists(TOKEN_PATH):
            try:
                return easy_client(api_key=API_KEY, app_secret=APP_SECRET, callback_url=CALLBACK_URL, token_path=TOKEN_PATH)
            except (OSError, ValueError) as e:
                log(f"Token issue ({e}); starting Schwab login flow…")
                return client_from_login_flow(api_key=API_KEY, app_secret=APP_SECRET, callback_url=CALLBACK_URL, token_path=TOKEN_PATH)
        else:
            log(f"No token file at {TOKEN_PATH}; starting Schwab login flow…")
            return client_from_login_flow(api_key=API_KEY, app_secret=APP_SECRET, callback_url=CALLBACK_URL, token_path=TOKEN_PATH)

    # ---- use module-level coercers to avoid duplicates ----
    @staticmethod
    def _coerce_float(x) -> Optional[float]:
        return coerce_float(x)

    @staticmethod
    def _coerce_ts(x) -> Optional[float]:
        return coerce_ts(x)

    @staticmethod
    def _extract_payload_for_symbol(sym: str, data: object) -> Optional[dict]:
        """
        Pull the payload for a single symbol from response JSON.
        """
        candidates = [sym, sym.lstrip("/")]
        if isinstance(data, dict):
            for c in candidates:
                v = data.get(c)
                if isinstance(v, dict):
                    return v
            q = data.get("quotes")
            if isinstance(q, dict):
                for c in candidates:
                    v = q.get(c)
                    if isinstance(v, dict):
                        return v
            elif isinstance(q, list):
                for row in q:
                    if isinstance(row, dict) and (row.get("symbol") or row.get("key")) in candidates:
                        return row
            d = data.get("data")
            if isinstance(d, list):
                for row in d:
                    if isinstance(row, dict) and (row.get("symbol") or row.get("key")) in candidates:
                        return row
            if "quote" in data and (data.get("symbol") or data.get("key")) in candidates:
                return data
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and (row.get("symbol") or row.get("key")) in candidates:
                    return row
        return None

    def _pick_price_fields(self, payload: dict):
        q = payload.get("quote") or {}
        f = payload.get("fundamental") or {}
        x = payload.get("extended") or {}
        in_rth = _is_rth()

        # Extended: only outside RTH
        ext_last = self._coerce_float(x.get("lastPrice")) if not in_rth else None
        ext_bid = self._coerce_float(x.get("bidPrice") or x.get("bid")) if not in_rth else None
        ext_ask = self._coerce_float(x.get("askPrice") or x.get("ask")) if not in_rth else None

        # Regular session candidates
        reg_last_candidates = [q.get("regularMarketLastPrice"), q.get("lastPrice"), q.get("mark")]
        reg_last = next((self._coerce_float(v) for v in reg_last_candidates if self._coerce_float(v) is not None), None)

        last = ext_last if ext_last is not None else reg_last
        bid = ext_bid if ext_bid is not None else self._coerce_float(q.get("bidPrice") or q.get("bid"))
        ask = ext_ask if ext_ask is not None else self._coerce_float(q.get("askPrice") or q.get("ask"))

        if last is None:
            last = self._coerce_float(f.get("closePrice"))
        return last, bid, ask

    def _extract_timestamp(self, payload: dict) -> Optional[float]:
        q = payload.get("quote") or {}
        x = payload.get("extended") or {}
        candidates = [
            q.get("quoteTimeInLong"),
            q.get("regularMarketTradeTimeInLong"),
            q.get("tradeTimeInLong"),
            q.get("time"),
            q.get("timestamp"),
            payload.get("timestamp"),
            payload.get("datetime"),
            x.get("quoteTimeInLong"),
            x.get("tradeTimeInLong"),
        ]
        for c in candidates:
            ts = self._coerce_ts(c)
            if ts is not None:
                return ts
        return None

    def _extract_total_volume(self, payload: dict) -> Optional[float]:
        q = payload.get("quote") or {}
        tv = (
                q.get("totalVolume")
                or q.get("total_volume")
                or q.get("totalVolumeAllSessions")
                or q.get("regularMarketVolume")
        )
        return self._coerce_float(tv) if tv is not None else None


    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """Fetch quotes per symbol (Schwab), including extended session when available."""
        res: Dict[str, Quote] = {}
        to_query = normalize_provider_symbols(symbols)
        if not to_query:
            log("⚠️ No provider symbols after normalization.")
            return res

        for sym in to_query:
            try:
                resp = self.client.get_quote(sym, fields=["quote", "fundamental", "extended"])
            except (TypeError, ValueError, AttributeError):
                resp = self.client.get_quote(sym)
            except (ReqConnectionError, Timeout, RequestException) as net_err:
                log(f"⚠️ Network issue fetching {sym}: {net_err}")
                continue

            try:
                data = resp.json() if hasattr(resp, "json") else resp
            except (ValueError, AttributeError) as parse_err:
                log(f"⚠️ JSON parse error for {sym}: {parse_err}")
                continue

            payload = self._extract_payload_for_symbol(sym, data)
            if not payload:
                kind = type(data).__name__
                log(f"⚠️ No data for {sym}; type={kind}; snapshot={str(data)[:400]}")
                continue

            last, bid, ask = self._pick_price_fields(payload)
            ts = self._extract_timestamp(payload)
            # Stale-last guard during RTH: if last tick is older than 90s, prefer mid
            if ts and _is_rth(ts) and (time.time() - ts) > 90:
                last = None
            total_volume = self._extract_total_volume(payload)
            res[sym] = Quote(symbol=sym, last=last, bid=bid, ask=ask, timestamp=ts, total_volume=total_volume)
        return res


def _parse_confidence(v) -> float:
    f = coerce_float(v)
    return 0.0 if f is None else max(0.0, min(1.0, f))


# =========================== Ollama (optional) ===========================

def ollama_summarize(prompt: str, model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 45) -> Optional[str]:
    try:
        import requests
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m"}
        resp = requests.post(f"{url}/api/generate", json=payload, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            text = data.get("response")
            return (text or "").strip() or None
    except Exception as e:
        log("(No Ollama summary)", e)
    return None


def ollama_bias(telemetry: dict, model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 45) -> Optional[dict]:
    """Return {"bias": "bullish|neutral|bearish", "confidence": 0..1, "rationale": str} or None."""
    try:
        import json as _json
        import requests
        prompt = (
            "You are a veteran macro/quant trader. Using the TELEMETRY JSON, classify the immediate U.S. "
            "equity market bias for short-term traders.\n"
            "Account for classic macro links: USD strength (DXY) vs equities, and yields (TNX) vs growth/QQQ. "
            "IF telemetry.ta exists, weigh technicals as follows:\n"
            "- RSI14 with 70/30 bands (overbought/oversold),\n"
            "- MACD vs Signal and Histogram sign/momentum (crossovers matter),\n"
            "- Bollinger Band position (bb_pos: -1 lower, +1 upper; extremes imply stretch),\n"
            "- SMA5 vs SMA20 tilt (above favors bullish tilt, below favors bearish),\n"
            "- Consider VWAP relation when present.\n"
            "When signals conflict, prefer NEUTRAL or lower confidence. Output STRICT JSON only.\n\n"
            'JSON schema: { "bias": "bullish|neutral|bearish", "confidence": 0..1, "rationale": "≤20 words" }\n\n'
            "TELEMETRY:\n"
            f"{_json.dumps(telemetry, separators=(',', ':'))}"
        )
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m"}
        resp = requests.post(f"{url}/api/generate", json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None
        txt = (resp.json() or {}).get("response", "").strip()
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end != -1:
            txt = txt[start : end + 1]
        data = _json.loads(txt)
        bias = str(data.get("bias", "")).lower()
        if bias not in {"bullish", "neutral", "bearish"}:
            return None
        conf = _parse_confidence(data.get("confidence", 0))
        rationale = str(data.get("rationale", "")).strip()
        return {"bias": bias, "confidence": conf, "rationale": rationale}
    except Exception as e:
        log("(No Ollama bias)", e)
        return None


# =========================== Market Logic (+ VWAP) ===========================

@dataclass
class Snapshot:
    t: float
    prices: Dict[str, float]
    vwap: Dict[str, float]


class VWAPCalc:
    """Maintains same-day intraday VWAP using cumulative volume + price deltas."""
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
        """Initialize from today's minute candles. Each candle should include (open, high, low, close, volume, datetime)."""
        for c in candles:
            ts = coerce_ts(c.get("datetime") or c.get("time") or c.get("timestamp") or time.time())
            if ts is None:
                continue
            key = self._key(ts)
            if not self.day_key:
                self.day_key = key
            if key != self.day_key:
                # only accumulate today's candles
                continue
            vol = coerce_float(c.get("volume"))
            o = coerce_float(c.get("open"))
            h = coerce_float(c.get("high"))
            l = coerce_float(c.get("low"))
            cl = coerce_float(c.get("close") or c.get("closePrice"))
            if vol is None or vol <= 0 or cl is None:
                continue
            # Use typical price if OHLC present, else close
            typical = cl
            if o is not None and h is not None and l is not None:
                typical = (h + l + cl) / 3.0
            self.cum_vol += vol
            self.cum_dollar += typical * vol

    def update_live(self, ts: Optional[float], px: Optional[float], cum_volume: Optional[float]):
        if ts is None or px is None or cum_volume is None:
            return
        key = self._key(ts)
        if not self.day_key or self.day_key != key:
            self.reset_for(ts)
        # compute delta volume; guard resets
        if cum_volume < self.cum_vol:
            # feed reset (e.g., new session or provider switch)
            self.reset_for(ts)
        delta = cum_volume - self.cum_vol
        if delta > 0:
            self.cum_dollar += px * delta
            self.cum_vol = cum_volume

    def value(self) -> Optional[float]:
        return (self.cum_dollar / self.cum_vol) if self.cum_vol > 0 else None


def enrich_with_macros(quotes: Dict[str, Quote]) -> None:
    """Synthesize DXY and TNX from proxies in `quotes`."""
    def px(sym: str) -> Optional[float]:
        q = quotes.get(sym)
        return q.px if q else None

    # DXY canonical (use UUP level for direction if DXY missing)
    dxy_val = None
    for cand in DXY_ALIASES:
        v = px(cand)
        if v is not None:
            dxy_val = v
            break
    if dxy_val is not None:
        quotes["DXY"] = Quote("DXY", last=float(dxy_val), bid=None, ask=None)

    # TNX canonical (invert bond price proxies)
    tnx_val = None
    for cand in TNX_ALIASES:
        v = px(cand)
        if v is not None:
            tnx_val = -float(v) if cand in INVERSE_PROXIES else float(v)
            break
    if tnx_val is not None:
        quotes["TNX"] = Quote("TNX", last=float(tnx_val), bid=None, ask=None)


class MarketBrain:
    def __init__(self, focus: str = "SPY", max_trail: int = 180):
        self.focus = focus
        self._trail: List[Snapshot] = []
        self._max_trail = int(max_trail)
        self._lock = RLock()
        self._tz = ZoneInfo("America/New_York") if ZoneInfo else datetime.now().astimezone().tzinfo
        self._vwap: Dict[str, VWAPCalc] = {}

        # Gate params (overridden via CLI)
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
        """Count how many snapshots contain a price for the symbol (defaults to focus)."""
        sym = sym or self.focus
        count = sum(1 for snap in self._trail if snap.prices.get(sym) is not None)
        return min(count, max_points) if isinstance(max_points, int) else count

    def seed_vwap_from_candles(self, symbol: str, candles: List[dict]) -> None:
        """Public helper to initialize today's VWAP from minute candles."""
        self._get_vwap_calc(symbol).seed_from_candles(candles)  # OK to use protected inside the class





    # ---- TA helpers ----
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
    def _ema(vals: List[float], n: int) -> Optional[float]:
        if n <= 0 or len(vals) < n:
            return None
        k = 2.0 / (n + 1.0)
        ema = sum(vals[:n]) / n
        for x in vals[n:]:
            ema = (x - ema) * k + ema
        return float(ema)

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
        """Return (mid, upper, lower, pos) where pos ~ -1 at lower band, +1 at upper band."""
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

    # ---- VWAP helpers ----
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

    # ---- Internals ----
    def ingest(self, quotes: Dict[str, Quote]):
        def _pick_ts() -> float:
            fq = quotes.get(self.focus)
            if fq and fq.timestamp:
                return float(fq.timestamp)
            ts_list = [quote_obj.timestamp for quote_obj in quotes.values() if quote_obj.timestamp is not None]
            if ts_list:
                return float(max(ts_list))
            return time.time()

        ts = _pick_ts()
        prices: Dict[str, float] = {}
        vmap: Dict[str, float] = {}

        # update VWAP states from live quotes (cum volume)
        for s, qt in quotes.items():  # avoid shadowing ‘q’
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
        up, dn = 0.0018, -0.0018  # ~0.18% window
        if d > up:
            return "bullish"
        if d < dn:
            return "bearish"
        return "neutral"

    # ---- Trade gates (calls/puts) ----
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

    def narrative(self, symbols: List[str], ollama_model: str = "llama3", ollama_url: str = "http://127.0.0.1:11434", ollama_timeout: int = 45) -> str:
        px = self.latest_prices()
        vws = self.latest_vwap()
        f = self.focus
        fpx = px.get(f)
        fvw = vws.get(f)
        d_focus = self.pct_delta(f)
        dxy_d = self.pct_delta("DXY")
        tnx_d = self.pct_delta("TNX")

        def dir_label(d: Optional[float]) -> str:
            if d is None: return "flat"
            if d > 0.001: return "up"
            if d < -0.001: return "down"
            return "flat"

        parts: List[str] = []
        chg = (d_focus or 0.0)
        if fpx is not None:
            if fvw is not None:
                spread = fpx - fvw
                parts.append(f"{f} {fpx:.2f} ({chg:+0.2%}) vwap {fvw:.2f} ({spread:+.2f}) — {self.bias()}")
            else:
                parts.append(f"{f} {fpx:.2f} ({chg:+0.2%}) — {self.bias()}")

        macro_bits = []
        if "DXY" in px:
            macro_bits.append(f"DXY {dir_label(dxy_d)} ({(dxy_d or 0):+0.2%})")
        if "TNX" in px:
            macro_bits.append(f"TNX {dir_label(tnx_d)} ({(tnx_d or 0):+0.2%})")
        if macro_bits:
            parts.append("; ".join(macro_bits))

        ta = self.ta_pack()
        ta_bits: List[str] = []
        if ta.get("rsi14") is not None:
            ta_bits.append(f"RSI14 {ta['rsi14']:.0f}")
        if ta.get("macd") is not None and ta.get("macd_signal") is not None and ta.get("macd_hist") is not None:
            macd_arrow = "↑" if ta["macd"] > ta["macd_signal"] else "↓"
            ta_bits.append(f"MACD {ta['macd']:+.3f}{macd_arrow}")
        if ta.get("bb_pos") is not None:
            ta_bits.append(f"BBPos {ta['bb_pos']:+.2f}")
        if ta.get("sma5") is not None and ta.get("sma20") is not None:
            slope = ta["sma5"] - ta["sma20"]
            ta_bits.append(f"SMA5-20 {slope:+.2f}")
        if ta_bits:
            parts.append("TA: " + ", ".join(ta_bits))

        extras = []
        for s in symbols:
            if s in {f, "DXY", "TNX", "$DXY", "$TNX", "UUP", "IEF", "TLT"}:
                continue
            v = px.get(s)
            vw = vws.get(s)
            if v is not None and vw is not None:
                extras.append(f"{s} {v:.2f}/vwap {vw:.2f} ({v - vw:+.2f})")
            elif v is not None:
                extras.append(f"{s} {v:.2f}")
        if extras:
            parts.append("; ".join(extras))

        base = " | ".join(parts) if parts else "No prices yet"

        fancy = ""
        ollama_on = str(ollama_model).strip().lower() not in OLLAMA_DISABLED
        if ollama_on:
            context_rules = (
                "Rules: Rising DXY often pressures equities; rising TNX weighs on growth. "
                "Consider VWAP: above favors longs; below favors shorts. TA bands guide stretch."
            )
            fact_lines = [base]
            if dxy_d is not None or tnx_d is not None:
                fact_lines.append(f"Focus bias {self.bias()}; DXY trend {dir_label(dxy_d)}; TNX trend {dir_label(tnx_d)}.")
            ta_json = json.dumps(ta, separators=(",", ":"))
            vwap_json = json.dumps(vws, separators=(",", ":"))
            prompt = "\n".join(
                [
                    f"Speak ONE concise sentence (<22 words), no emojis. {context_rules}",
                    f"TA: {ta_json}",
                    f"VWAP: {vwap_json}",
                    f"Telemetry: {' | '.join(fact_lines)}",
                ]
            )
            fancy = ollama_summarize(prompt, model=ollama_model, url=ollama_url, timeout=ollama_timeout) or ""

        result = fancy if fancy else base
        if ollama_on:
            pruned_prices: Dict[str, float] = {}
            for s in symbols:
                if s in {"DXY", "TNX", "$DXY", "$TNX"}:
                    continue
                v = px.get(s)
                if v is not None:
                    pruned_prices[s] = v
            telemetry = {
                "focus": f,
                "focus_px": fpx,
                "focus_delta": d_focus,
                "dxy_delta": dxy_d,
                "tnx_delta": tnx_d,
                "prices": pruned_prices,
                "ta": ta,
                "vwap": {k: v for k, v in vws.items() if k in pruned_prices or k == f},
            }
            ai = ollama_bias(telemetry, model=ollama_model, url=ollama_url, timeout=ollama_timeout)
            if ai:
                conf_pct = int(round(ai["confidence"] * 100))
                result += f" | AI bias: {ai['bias']} ({conf_pct}%) — {ai['rationale']}"
        return result


# =========================== Q&A helpers ===========================

def _build_telemetry_for_qa(brain: MarketBrain) -> Dict[str, Any]:
    """Create a clean telemetry blob for Q&A."""
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
        "vwap": brain.latest_vwap(),  # NEW
        "ta": ta,
        "t": time.time(),
    }
    telemetry["prices"].pop("$DXY", None)
    telemetry["prices"].pop("$TNX", None)
    return telemetry


def _qa_prompt(question: str, telemetry: Dict[str, Any], max_words: int = 120) -> str:
    return (
        "You are a veteran macro/quant trading assistant.\n"
        "Answer the user's question using the TELEMETRY JSON (spot prices, DXY/TNX proxies, TA, and VWAP for the focus).\n"
        "If data is insufficient or market is closed, say so briefly. Be concise, actionable, and avoid emojis.\n"
        f"Limit to < {max_words} words.\n\n"
        "TELEMETRY:\n"
        f"{json.dumps(telemetry, separators=(',', ':'))}\n\n"
        f"QUESTION:\n{question}\n\n"
        "ANSWER:"
    )


def ollama_answer(question: str, telemetry: Dict[str, Any], model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 60, max_words: int = 120) -> Optional[str]:
    if str(model).strip().lower() in OLLAMA_DISABLED:
        return None
    try:
        import requests
        prompt = _qa_prompt(question, telemetry, max_words=max_words)
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m"}
        resp = requests.post(f"{url}/api/generate", json=payload, timeout=timeout)
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("response", "").strip() or None
    except Exception as e:
        log("(Q&A error)", e)
        return None


def _get_today_minute_candles(client, symbol: str) -> Optional[List[dict]]:
    """Best-effort fetch of today's minute candles for seeding VWAP/TA."""
    def _enum(enum_cls, *names):
        if enum_cls is None:
            return None
        members = getattr(enum_cls, "__members__", None)
        for raw in names:
            if members:
                key = str(raw).upper()
                if key in members:
                    return members[key]
            for cand in (str(raw), str(raw).upper(), str(raw).lower(), str(raw).capitalize()):
                if hasattr(enum_cls, cand):
                    return getattr(enum_cls, cand)
        return None

    price_history_ns = getattr(client, "PriceHistory", None)
    period_type_enum = getattr(price_history_ns, "PeriodType", None) if price_history_ns else None
    frequency_type_enum = getattr(price_history_ns, "FrequencyType", None) if price_history_ns else None

    pt_day = _enum(period_type_enum, "DAY", "day")
    ft_min = _enum(frequency_type_enum, "MINUTE", "minute")

    kwargs = {}
    if pt_day and ft_min:
        kwargs = dict(period_type=pt_day, period=1, frequency_type=ft_min, frequency=1)

    # 1) Request (catch argument/attr + network timeouts)
    try:
        resp = client.get_price_history(symbol, **kwargs) if kwargs else client.get_price_history(symbol)
    except (TypeError, ValueError, AttributeError, ReqConnectionError, Timeout, RequestException) as e:
        log(f"price_history request error for {symbol}: {e}")
        return None

    # 2) Parse payload (catch bad JSON / shape)
    try:
        data = resp.json() if hasattr(resp, "json") else resp
    except (ValueError, AttributeError) as e:
        log(f"price_history parse error for {symbol}: {e}")
        return None

    # 3) Extract candles
    if isinstance(data, dict):
        c = data.get("candles") or data.get("data") or data.get("quotes")
        return c if isinstance(c, list) and c else None
    return None


def _seed_brain_with_history(provider: BaseQuoteProvider, brain: MarketBrain, symbol: str, bars: int = 60) -> bool:
    """Warm TA AND initialize same-day VWAP using minute/daily candles."""
    client = getattr(provider, "client", None)
    if client is None:
        return False

    # minute candles preferred
    candles = _get_today_minute_candles(client, symbol)
    if not candles:
        # fallback to larger lookback from previous build
        try:
            # Request (argument/attr + network errors)
            resp = client.get_price_history(symbol)
        except (TypeError, ValueError, AttributeError, ReqConnectionError, Timeout, RequestException) as e:
            log(f"price_history fallback request error for {symbol}: {e}")
            candles = None
        else:
            try:
                # Parse payload (bad JSON/shape)
                data = resp.json() if hasattr(resp, "json") else resp
            except (ValueError, AttributeError) as e:
                log(f"price_history fallback parse error for {symbol}: {e}")
                candles = None
            else:
                if isinstance(data, dict):
                    c = data.get("candles") or data.get("data") or data.get("quotes")
                    candles = c if isinstance(c, list) and c else None
                else:
                    candles = None

    # Seed TA trail (prices) and VWAP from today's candles
    brain.seed_vwap_from_candles(symbol, candles)

    count = 0
    for c in candles[-bars:]:
        close = coerce_float(c.get("close") or c.get("closePrice") or c.get("close_price"))
        if close is None:
            continue
        ts = coerce_ts(c.get("datetime") or c.get("time") or c.get("timestamp") or time.time())
        # ingest builds snapshot and also records VWAP (already seeded)
        brain.ingest({symbol: Quote(symbol=symbol, last=close, bid=None, ask=None, timestamp=ts)})
        count += 1

    return count > 0


def qa_once(
    provider: BaseQuoteProvider,
    user_symbols: List[str],
    focus: str,
    model: str,
    url: str,
    timeout: int,
    max_words: int,
    history: int,
    question: str,
):
    display_symbols = expand_symbols(user_symbols)

    brain = MarketBrain(focus=focus, max_trail=history)
    _seed_brain_with_history(provider, brain, focus, bars=min(80, history))

    # Use display_symbols: fetch a fresh snapshot, ingest into brain
    quotes = provider.get_quotes(display_symbols)
    enrich_with_macros(quotes)
    brain.ingest(quotes)

    telemetry = _build_telemetry_for_qa(brain)
    ans = ollama_answer(question, telemetry, model=model, url=url, timeout=timeout, max_words=max_words)
    if ans:
        print(ans)
    else:
        print("Q&A unavailable (Ollama off or request failed).")



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


def qa_chat(provider: BaseQuoteProvider, user_symbols: List[str], focus: str, model: str, url: str, timeout: int, max_words: int, history: int, interval: int):
    """Interactive Q&A. A background sampler keeps ingesting quotes so TA/VWAP warm up while you chat."""
    display_symbols = expand_symbols(user_symbols)
    brain = MarketBrain(focus=focus, max_trail=history)
    try:
        _seed_brain_with_history(provider, brain, focus, bars=min(120, history))
    except Exception as e:
        log(f"(seed warning) {e}")

    sampler = BackgroundSampler(provider, display_symbols, brain, interval=interval)
    sampler.start()

    print("Q&A chat — type 'quit' or 'exit' to leave.")
    try:
        while True:
            try:
                q = input("Q> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                continue
            cmd = q.lower()
            if cmd in {"quit", "exit", "q"}:
                break
            elif cmd in {"ta", "status"}:
                need = 26 + 9 + 2  # MACD warmup
                have = brain.series_len(brain.focus)
                remaining = max(0, need - have)
                eta_min = remaining * max(1, interval) / 60.0
                telemetry_status = _build_telemetry_for_qa(brain)
                print(f"series_len={have}  |  MACD ready: {have >= need}  |  approx ETA: ~{eta_min:.1f} min (interval={interval}s)")
                print(json.dumps(telemetry_status["ta"], indent=2))
                vwp = brain.latest_vwap().get(brain.focus)
                if vwp is not None:
                    print(f"VWAP({brain.focus}) ~ {vwp:.2f}")
                continue
            elif cmd in {"time", "now"}:
                print(f"{virginia_now_str()}")
                continue
            telemetry = _build_telemetry_for_qa(brain)
            ans = ollama_answer(q, telemetry, model=model, url=url, timeout=timeout, max_words=max_words)
            print(f"A> {ans}\n" if ans else "A> (no answer — Ollama disabled or request failed)\n")
    finally:
        sampler.stop()


# =========================== Loop ===========================

def _pretty_prices(quotes: Dict[str, Quote]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, q in quotes.items():
        if k in {"DXY", "TNX"}:
            continue
        v = q.px
        if v is None:
            continue
        try:
            out[k] = round(float(v), 2)
        except (TypeError, ValueError):
            continue
    return out


def _csv_safe(x) -> str:
    return "" if x is None else str(x)


class TickerLoop:
    def __init__(self,
        provider: BaseQuoteProvider,
        symbols: List[str],
        interval: int = 20,
        focus: str = "SPY",
        ollama_model: str = "llama3",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_timeout: int = 45,
        emit_json: bool = False,
        csv_path: Optional[str] = None,
        once: bool = False,
        verbose: bool = False,
        vwap_buf: float = 0.10,
        gate_consecutive: int = 2,
        print_gates: bool = False,
    ):
        self.display_symbols = expand_symbols(symbols)  # includes UUP/IEF/TLT
        self.request_symbols = normalize_provider_symbols(self.display_symbols)  # filtered for Schwab
        self.provider = provider
        self.interval = max(5, int(interval))
        self.brain = MarketBrain(focus=focus)
        self.brain.vwap_buf = float(vwap_buf)
        self.brain.gate_consecutive = int(gate_consecutive)
        self.ollama_model = ollama_model
        self.ollama_url = ollama_url
        self.ollama_timeout = ollama_timeout
        self._stop = Event()
        self.emit_json = emit_json
        self.csv_path = csv_path
        self.once = once
        self.verbose = verbose
        self._failures = 0
        self._last_ok: Optional[float] = None
        self._print_gates = print_gates

        # ---- quiet-print defaults (overridable by CLI) ----
        self.window_points: int = 5  # how many snapshots for pct delta
        self.min_print: float = 0.001  # 0.10% threshold to print
        self.always_print: bool = False

        # Warm brain (TA+VWAP) using minute candles
        try:
            _seed_brain_with_history(self.provider, self.brain, focus, bars=min(120, 3 * self.interval))
        except Exception as e:
            log(f"(seed warning) {e}")

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
            "ai": None,
            "gates": None,
        }
        if str(self.ollama_model).strip().lower() not in OLLAMA_DISABLED:
            pruned_disp: Dict[str, float] = {}
            for s in self.display_symbols:
                if s in {"DXY", "TNX", "$DXY", "$TNX"}:
                    continue
                val = px.get(s)
                if val is not None:
                    pruned_disp[s] = val
            telemetry = {
                "focus": f,
                "focus_px": px.get(f),
                "focus_delta": d_focus,
                "dxy_delta": dxy_d,
                "tnx_delta": tnx_d,
                "prices": pruned_disp,
                "ta": ta,
                "vwap": {k: v for k, v in vwp.items() if k in pruned_disp or k == f},
            }
            payload["ai"] = ollama_bias(telemetry, model=self.ollama_model, url=self.ollama_url, timeout=self.ollama_timeout)

        # Always include gate snapshot
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
                    w.writerow(["t", "focus", "bias", "focus_delta", "dxy_delta", "tnx_delta", "prices_json", "vwap_json", "ai_json", "gates_json"])
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
                    ]
                )
        except Exception as e:
            log("CSV write error:", e)

    def _poll_once(self) -> Tuple[Dict[str, Quote], str]:
        log("Requesting:", self.request_symbols)
        quotes = self.provider.get_quotes(self.display_symbols)
        enrich_with_macros(quotes)
        self.brain.ingest(quotes)
        text = self.brain.narrative(
            self.display_symbols,
            ollama_model=self.ollama_model,
            ollama_url=self.ollama_url,
            ollama_timeout=self.ollama_timeout,
        )
        if self._print_gates:
            ok_c, why_c, trig_c = self.brain.pre_order_check("calls")
            ok_p, why_p, trig_p = self.brain.pre_order_check("puts")
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
            log("Prices:", _pretty_prices(quotes))
            if self._should_print():
                print(text)
            else:
                log(f"No material change (<{self.min_print:.2%}); skipping print.")
            payload = self._snapshot_payload()
            if self.emit_json:
                print(json.dumps(payload, separators=(",", ":")))
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
                log("Prices:", _pretty_prices(quotes))
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
    p = argparse.ArgumentParser(description="Live market summaries using Schwab (text-only, no futures) + VWAP + Q&A")
    p.add_argument("--ollama-url", default="http://127.0.0.1:11434", help="Ollama server URL")
    p.add_argument("--history", type=int, default=180, help="Max snapshots to retain (controls TA horizon)")
    p.add_argument("--window", type=int, default=5, help="Points used for delta calc")
    p.add_argument("--min-print", type=float, default=0.0010, help="Skip prints if abs deltas for focus/DXY/TNX are all below this")
    p.add_argument("--always-print", action="store_true", help="Print every poll even if unchanged")
    p.add_argument("--ollama-timeout", type=int, default=45, help="Seconds to wait for a response")
    p.add_argument("--symbols", nargs="*", default=["SPY", "NVDA", "QQQ", "XLF"], help="Symbols (UUP/IEF/TLT proxies auto-added)")
    p.add_argument("--interval", type=int, default=20, help="Seconds between summaries (min 5)")
    p.add_argument("--focus", default="SPY", help="Primary symbol for bias calc")
    p.add_argument("--ollama", default=os.getenv("OLLAMA_MODEL", "llama3"), help="Ollama model name; use 'off' or 'none' to disable")
    p.add_argument("--json", action="store_true", help="Also emit a JSON payload per poll")
    p.add_argument("--once", action="store_true", help="Poll once and exit")
    p.add_argument("--csv", default=None, help="Append poll snapshots to this CSV path")
    p.add_argument("--verbose", action="store_true", help="Print exception tracebacks on errors")

    # ---- NEW VWAP / Gates ----
    p.add_argument("--vwap-buf", type=float, default=0.10, help="Absolute VWAP buffer (points) for above/below tests (default 0.10)")
    p.add_argument("--gate-consecutive", type=int, default=2, help="Consecutive snapshots required beyond VWAP buffer (default 2)")
    p.add_argument("--print-gates", action="store_true", help="Append one-line pre-order gate status to each print")

    # ---- NEW Q&A flags ----
    p.add_argument("--ask", default=None, help="Ask the model a single question using a fresh snapshot, then exit")
    p.add_argument("--chat", action="store_true", help="Interactive Q&A mode (fresh snapshot per question)")
    p.add_argument("--qa-max-words", type=int, default=120, help="Max words for Q&A answers")

    args = p.parse_args()
    log(f"[cfg] using callback_url='{CALLBACK_URL}' token_path='{TOKEN_PATH}'")

    provider = SchwabQuoteProvider()

    # ---- Q&A modes take precedence ----
    if args.ask:
        qa_once(
            provider,
            user_symbols=args.symbols,
            focus=args.focus,
            model=args.ollama,
            url=args.ollama_url,
            timeout=args.ollama_timeout,
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
            model=args.ollama,
            url=args.ollama_url,
            timeout=args.ollama_timeout,
            max_words=args.qa_max_words,
            history=args.history,
            interval=args.interval,
        )
        return

    # ---- Normal live loop ----
    loop = TickerLoop(
        provider,
        symbols=args.symbols,
        interval=args.interval,
        focus=args.focus,
        ollama_model=args.ollama,
        ollama_url=args.ollama_url,
        ollama_timeout=args.ollama_timeout,
        emit_json=args.json,
        csv_path=args.csv,
        once=args.once,
        verbose=args.verbose,
        vwap_buf=args.vwap_buf,
        gate_consecutive=args.gate_consecutive,  # <— new kwarg
        print_gates=args.print_gates,
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
