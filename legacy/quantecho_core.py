# QuantEcho_core.py — Schwab-only (text), with DXY/TNX context (no futures)
# ---------------------------------------------------------------------------------
# What changed / fixed in this rework:
# - Clean imports (no duplicates), correct NEWSAPI_KEY env read.
# - MarketBrain no longer references undefined news_* names.
# - News pipeline moved to TickerLoop (uses news_client.py functions).
# - True intraday VWAP (seed from today's 1m candles + update via quote.totalVolume).
# - Trade gates for 0DTE with hysteresis (N consecutive snapshots beyond VWAP buffer).
# - Narrative includes VWAP spread; JSON payload includes VWAP + Gates; Q&A telemetry includes VWAP.
# - Robust Ollama requests with retries/timeouts; Schwab client init hardened.
# - Futures skipped; DXY via UUP; TNX via IEF/TLT (inverted).
#
# Usage examples:
#   python ollama_voice.py --chat --interval 5 --ollama "llama3" --history 300 --print-gates
#   python ollama_voice.py --ask "market analysis" --ollama "llama3" --history 300
#   python ollama_voice.py --interval 5 --ollama off --history 180 --json --csv .\logs\session.csv
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
from datetime import datetime
from threading import Event, RLock
from typing import Any, List, Optional, Tuple
from ai_openai_helper import OpenAIHelper
import requests
from requests.exceptions import RequestException, Timeout, ConnectionError as ReqConnectionError
# ===== Formatting helpers (3 d.p. max) =====
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
# at top of file (near other env reads)
DEBUG_QUOTE_FIELDS = os.getenv("DEBUG_QUOTE_FIELDS", "0") == "1"

# ---- context_builder (optional) ----
try:
    from context_builder import build_context, compute_bias_score
except ImportError:
    build_context = None
    compute_bias_score = None




def fmt3(x, *, none="n/a"):
    """Return a string with up to 3 decimals (trim trailing zeros)."""
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

def round_floats(obj: Any, places: int = 3):
    """Recursively round floats in dict/list/tuple to `places` decimal digits."""
    if isinstance(obj, float):
        q = Decimal("0." + "0" * (places - 1) + "1")  # e.g., places=3 → Decimal("0.001")
        return float(Decimal(str(obj)).quantize(q, rounding=ROUND_HALF_UP))
    if isinstance(obj, dict):
        return {k: round_floats(v, places) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        seq = [round_floats(v, places) for v in obj]
        return type(obj)(seq)
    return obj

def _debug_quote_choice(sym: str, payload: dict, *, enabled: bool = False) -> None:
    """Emit a one-shot debug line about which fields we're using.
    Only prints when `enabled=True` or DEBUG_QUOTE_FIELDS=1."""
    if not (enabled or DEBUG_QUOTE_FIELDS):
        return

    q = payload.get("quote") or {}
    x = payload.get("extended") or {}
    f = payload.get("fundamental") or {}

    # use your existing `log()` so it timestamps and flushes
    log("DEBUG", sym, {
        "in_rth": _is_rth(),
        "q.lastPrice": q.get("lastPrice"),
        "q.regularMarketLastPrice": q.get("regularMarketLastPrice"),
        "q.mark": q.get("mark"),
        "q.bid": q.get("bidPrice") or q.get("bid"),
        "q.ask": q.get("askPrice") or q.get("ask"),
        "x.lastPrice": x.get("lastPrice"),
        "x.bid": x.get("bidPrice") or x.get("bid"),
        "x.ask": x.get("askPrice") or x.get("ask"),
        "f.closePrice": f.get("closePrice"),
    })



# ---- optional news_client imports with safe fallbacks ----
try:
    from news_client import (
        NewsFetcher,
        news_bias_score,
        compress_bullets,
        map_symbols_to_keywords,
    )
except ImportError:
    NewsFetcher = None                 # type: ignore[assignment]
    news_bias_score = None             # type: ignore[assignment]
    compress_bullets = None            # type: ignore[assignment]
    map_symbols_to_keywords = None     # type: ignore[assignment]

# ====== ENV / CONFIG ======
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")  # export NEWSAPI_KEY=...
SYMBOL_RE = re.compile(r"\b[A-Z]{1,5}(?:/[A-Z]{2,4})?\b")

# ====== SCHWAB CREDS (set via env or edit here) ======
API_KEY = os.getenv("APP_KEY", "")     # exact client id from portal
APP_SECRET = os.getenv("APP_SECRET", "")
CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", "tokens.json")
# =====================================================

# When any of these are passed as the model name, Ollama is treated as disabled
OLLAMA_DISABLED = {"off", "none", "", "0", "false", "disabled"}

CHAT_HELP = """\
LLAMA_DRAMA — interactive commands
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

CLI start options
-----------------
--chat                       Interactive Q&A chat mode
--ask "..."                  One-shot Q&A, then exit
--symbols SPY NVDA QQQ XLF   Symbols to track (UUP/IEF/TLT proxies auto-added)
--focus SPY                  Primary symbol for TA/bias
--interval 5                 Seconds between polls (min 5)
--history 300                Snapshots to retain (controls TA horizon)
--window 5                   Points used for delta calc
--min-print 0.001            Skip prints if deltas are smaller than this
--always-print               Print every poll even if unchanged
--vwap-buf 0.10              VWAP buffer (points) for gate checks
--gate-consecutive 2         N consecutive snapshots needed for gates
--print-gates                Append CALLS/PUTS gate status to narrative
--json                       Also emit a JSON payload per poll
--csv .\\logs\\session.csv   Append poll snapshots to a CSV file
--ollama "llama3"            Ollama model (use 'off' to disable)
--ollama-url http://127.0.0.1:11434
--ollama-timeout 75          Read timeout (seconds)
--qa-max-words 120           Max words for Q&A answers
--news                       Include NewsAPI headlines (set NEWSAPI_KEY env)
--news-since-h 6             Lookback window for headlines (hours)
--news-limit 12              Max articles to consider
--news-weight 0.30           Weight when nudging AI bias
--print-news                 Append a one-liner with news count+bias
--verbose                    Print tracebacks on errors
"""

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
    total_volume: Optional[float] = None  # cumulative volume (for live VWAP updates)

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
    def __init__(self, verbose: bool = False):
        self.verbose = bool(verbose)
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

        # Parse candidates
        q_last = self._coerce_float(q.get("lastPrice"))
        q_reg_last = self._coerce_float(q.get("regularMarketLastPrice"))
        q_mark = self._coerce_float(q.get("mark"))
        q_bid = self._coerce_float(q.get("bidPrice") or q.get("bid"))
        q_ask = self._coerce_float(q.get("askPrice") or q.get("ask"))

        x_last = self._coerce_float(x.get("lastPrice")) if not in_rth else None
        x_bid = self._coerce_float(x.get("bidPrice") or x.get("bid")) if not in_rth else None
        x_ask = self._coerce_float(x.get("askPrice") or x.get("ask")) if not in_rth else None

        if in_rth:
            # During RTH: prefer *regular session* last; avoid 'mark' unless necessary
            last = q_reg_last if q_reg_last is not None else q_last
            if last is None and q_mark is not None:
                last = q_mark
            if last is None:
                last = self._coerce_float(f.get("closePrice"))
            bid, ask = q_bid, q_ask
        else:
            # After-hours/premarket: drive off extended NBBO mid when available.
            # Only fall back to any "last" if we don't have a sane book.
            bid = x_bid if x_bid is not None else q_bid
            ask = x_ask if x_ask is not None else q_ask
            # Guard against inverted/garbage B/A
            if (bid is not None and ask is not None) and (bid > ask):
                bid, ask = None, None

            if bid is not None and ask is not None:
                # Tell Quote.px to use mid by leaving 'last' as None
                last = None
            else:
                # No book—fall back in order of freshness
                last = x_last if x_last is not None else (q_last if q_last is not None else q_reg_last)
                if last is None and q_mark is not None:
                    last = q_mark
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
            if sym == "SPY":
                _debug_quote_choice(sym, payload, enabled=self.verbose)

            last, bid, ask = self._pick_price_fields(payload)
            ts = self._extract_timestamp(payload)
            # Stale-last guard during RTH: if last tick is older than 90s, prefer mid
            if ts and _is_rth(ts) and (time.time() - ts) > 90:
                last = None
            total_volume = self._extract_total_volume(payload)
            res[sym] = Quote(symbol=sym, last=last, bid=bid, ask=ask, timestamp=ts, total_volume=total_volume)
        return res

# =========================== Ollama helpers ===========================

def _ollama_post(url: str, path: str, payload: dict, read_to: int, retries: int = 2) -> requests.Response:
    u = f"{url.rstrip('/')}{path}"
    last_err: Exception | None = None
    for i in range(retries + 1):
        try:
            return requests.post(u, json=payload, timeout=(10, read_to))
        except (ReqConnectionError, Timeout) as e:
            last_err = e
            if i < retries:
                time.sleep(1.2 * (2 ** i))
                continue
            break
    raise RequestException(f"Ollama POST failed after {retries+1} attempts to {u}") from last_err

def ollama_summarize(prompt: str, model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 45) -> Optional[str]:
    try:
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m"}
        resp = _ollama_post(url, "/api/generate", payload, read_to=timeout)
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("response", "").strip() or None
    except Exception as e:
        log("(No Ollama summary)", e)
    return None

from typing import Optional, Dict, Any  # ensure at top of file

def ollama_bias(
    telemetry: dict,
    model: str = "llama3",
    url: str = "http://127.0.0.1:11434",
    timeout: int = 45,
    verbose: bool = False,   # <— NEW
) -> Optional[Dict[str, Any]]:
    """
    Return {'bias': 'bullish|neutral|bearish', 'confidence': 0..1, 'rationale': str} or None.
    Prefers LLM JSON output; falls back to a deterministic vote if parsing fails.
    """

    # --- build strict JSON prompt ---
    prompt = (
        "You are a veteran macro/quant trader. Using TELEMETRY JSON, classify the IMMEDIATE U.S. "
        "equity market bias for short-term traders.\n"
        "Weigh macro links (DXY↑=>eq↓, TNX↑=>growth↓), intraday VWAP relation for focus, and TA (RSI/MACD/BB). "
        "Also consider CONTEXT.daily (200DMA, MA stack, 1w/1m returns, drawdown, vol percentile) and BIAS_CODE.score. "
        "If signals conflict, choose NEUTRAL with lower confidence.\n\n"
        "RESPONSE RULES (STRICT):\n"
        " - Output ONE JSON object ONLY. No prose, no markdown, no code fences.\n"
        " - Exact schema: {\"bias\":\"bullish|neutral|bearish\",\"confidence\":0..1,\"rationale\":\"<=12 words\"}\n\n"
        f"TELEMETRY:\n{json.dumps(telemetry, separators=(',', ':'))}"
    )

    # --- call Ollama with JSON mode and low temperature ---
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "30m",
            "format": "json",
            "options": {"temperature": 0.1, "num_ctx": 2048},
        }
        resp = _ollama_post(url, "/api/generate", payload, read_to=timeout)
        resp.raise_for_status()
        txt = (resp.json() or {}).get("response", "").strip()
    except (ReqConnectionError, Timeout, RequestException) as e:
        if verbose:
            log("(ollama_bias) request error:", e)
        txt = ""

    # --- helpers: safe JSON slice extraction (balanced braces, quote-aware) ---
    def _extract_json_object_slice(s: str) -> str | None:
        if not s:
            return None
        s = s.strip()
        if s.startswith("```"):
            s = s.strip("`\n\r ")
        start = None
        depth = 0
        in_str = False
        esc = False
        for i, ch in enumerate(s):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and start is not None:
                        return s[start:i+1]
        return None

    # --- parse / sanitize any accidental wrappers ---
    data: Optional[Dict[str, Any]] = None
    if isinstance(txt, str) and txt.strip():
        try:
            candidate = _extract_json_object_slice(txt)
            if candidate is not None:
                data = json.loads(candidate)
            else:
                data = None  # no JSON object present → fall back deterministically
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            if verbose:
                log(f"(ollama_bias) non-json reply; using fallback: {e.__class__.__name__}")
            data = None

    # --- validate LLM output ---
    if isinstance(data, dict):
        bias = str(data.get("bias", "")).lower()
        if bias in {"bullish", "neutral", "bearish"}:
            try:
                conf = float(data.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            rationale = str(data.get("rationale") or "").strip()
            if rationale:
                words = rationale.split()
                if len(words) > 12:
                    rationale = " ".join(words[:12])
            return {"bias": bias, "confidence": conf, "rationale": rationale or "Signals mixed; keep risk tight"}

    # --- deterministic fallback (votes from VWAP/MACD/DXY/TNX, with RSI dampener) ---
    try:
        f = telemetry.get("FOCUS") or telemetry.get("focus") or telemetry.get("focus_symbol")
        px = telemetry.get("last_prices", {}).get(f)
        vwap = telemetry.get("vwap", {}).get(f)
        ta = telemetry.get("ta", {}) or telemetry.get("TA", {})
        macd = ta.get("macd"); macd_sig = ta.get("macd_signal") or ta.get("signal")
        rsi = ta.get("rsi14") or ta.get("rsi")
        dxy_d = telemetry.get("macro", {}).get("dxy_delta")
        tnx_d = telemetry.get("macro", {}).get("tnx_delta")

        votes = 0.0
        if px is not None and vwap is not None:
            votes += 1 if px > vwap else -1 if px < vwap else 0
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

        features = sum(x is not None for x in [px, vwap, macd, macd_sig, dxy_d, tnx_d, rsi])
        base_conf = min(0.9, 0.35 + 0.1 * max(0, features - 2))
        conf = max(0.3, min(0.9, base_conf * (1 + 0.25 * abs(votes))))

        rationale = (
            "Above VWAP & MACD≥sig" if fb_bias == "bullish" else
            "Below VWAP & MACD≤sig" if fb_bias == "bearish" else
            "Mixed VWAP/MACD/macro"
        )
        if rsi is not None and ((fb_bias == "bullish" and rsi >= 70) or (fb_bias == "bearish" and rsi <= 30)):
            rationale += "; RSI extreme"

        return {"bias": fb_bias, "confidence": round(conf, 3), "rationale": rationale}
    except (KeyError, TypeError, ValueError, ArithmeticError) as e:
        if verbose:
            log("(ollama_bias) fallback error:", e)
        return None


def ollama_trade_plan(telemetry: dict, model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 45) -> Optional[Dict[str, object]]:
    """
    Return a minimal, executable plan:
      {
        "action": "long|short|wait",
        "timeframe": "scalp|intra|swing",
        "entry": "string (<= 8 words)",
        "stop": "string (<= 8 words)",
        "take_profit": "string (<= 8 words)",
        "bias": "bullish|neutral|bearish",
        "confidence": 0..1,
        "note": "string (<= 10 words)"
      }
    """
    # Short, rulesy prompt: no essays, just the plan.
    prompt = (
        "You are a veteran macro/quant day trader. Using TELEMETRY JSON, output a one-glance trading plan "
        "for short-term U.S. equities. Prioritize VWAP relation, RSI/MACD/BB, and macro links (DXY↑=>eq↓, TNX↑=>growth↓). "
        "If signals conflict or edge is thin, choose action=wait and lower confidence.\n\n"
        "STRICT JSON ONLY with EXACT keys and formats:\n"
        "{"
        "\"action\":\"long|short|wait\","
        "\"timeframe\":\"scalp|intra|swing\","
        "\"entry\":\"<=8 words\","
        "\"stop\":\"<=8 words\","
        "\"take_profit\":\"<=8 words\","
        "\"bias\":\"bullish|neutral|bearish\","
        "\"confidence\":0..1,"
        "\"note\":\"<=10 words\""
        "}\n\n"
        f"TELEMETRY:\n{json.dumps(telemetry, separators=(',', ':'))}"
    )

    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": "30m",
            # Ask Ollama to format JSON; if unsupported by your model, it will be ignored harmlessly.
            "format": "json",
            "options": {"temperature": 0.2}
        }
        resp = _ollama_post(url, "/api/generate", payload, read_to=timeout)
        resp.raise_for_status()
        txt = (resp.json() or {}).get("response", "").strip()
    except (ReqConnectionError, Timeout, RequestException) as e:
        log("(ollama_trade_plan) error:", e)
        return None

    try:
        # Be forgiving if the model adds text around the JSON.
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end != -1:
            txt = txt[start:end+1]
        data = json.loads(txt)
    except Exception as e:
        log("(ollama_trade_plan) bad JSON:", e)
        return None

    # Validate / coerce
    action = str(data.get("action", "")).lower()
    if action not in {"long", "short", "wait"}:
        action = "wait"

    timeframe = str(data.get("timeframe", "")).lower()
    if timeframe not in {"scalp", "intra", "swing"}:
        timeframe = "intra"

    bias = str(data.get("bias", "")).lower()
    if bias not in {"bullish", "neutral", "bearish"}:
        bias = "neutral"

    try:
        conf = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    entry = str(data.get("entry") or "").strip()
    stop = str(data.get("stop") or "").strip()
    tp = str(data.get("take_profit") or "").strip()
    note = str(data.get("note") or "").strip()

    return {
        "action": action,
        "timeframe": timeframe,
        "entry": entry[:64],
        "stop": stop[:64],
        "take_profit": tp[:64],
        "bias": bias,
        "confidence": conf,
        "note": note[:64],
    }





# =========================== Market Logic (+ VWAP) ===========================

@dataclass
class Snapshot:
    t: float                 # unix seconds
    prices: Dict[str, float] # last trade (or mid) per symbol
    vwap: Dict[str, float]   # current intraday VWAP per symbol

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
        """Initialize from today's minute candles."""
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

def enrich_with_macros(quotes: Dict[str, Quote]) -> None:
    """Synthesize DXY and TNX from proxies in `quotes`."""
    def px(sym: str) -> Optional[float]:
        q = quotes.get(sym)
        return q.px if q else None

    # DXY canonical (use UUP level if DXY missing)
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
        sym = sym or self.focus
        count = sum(1 for snap in self._trail if snap.prices.get(sym) is not None)
        return min(count, max_points) if isinstance(max_points, int) else count

    def seed_vwap_from_candles(self, symbol: str, candles: List[dict]) -> None:
        self._get_vwap_calc(symbol).seed_from_candles(candles)

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
            ts_list = [q.timestamp for q in quotes.values() if q.timestamp is not None]
            if ts_list:
                return float(max(ts_list))
            return time.time()

        ts = _pick_ts()
        prices: Dict[str, float] = {}
        vmap: Dict[str, float] = {}

        # update VWAP states from live quotes (cum volume)
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

    def narrative(
        self,
        symbols: List[str],
        *,
        structured: bool = True,
        include_extras: bool = False,
        ollama_model: str = "llama3",
        ollama_url: str = "http://127.0.0.1:11434",
        ollama_timeout: int = 45,
    ) -> str:
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
                parts.append(f"{f} {fmt3(fpx)} ({chg:+0.2%}) vwap {fmt3(fvw)} ({fmt3(spread)}) — {self.bias()}")

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
            ta_bits.append(f"BBPos {fmt3(ta['bb_pos'])}")
        if ta.get("sma5") is not None and ta.get("sma20") is not None:
            slope = (ta["sma5"] or 0) - (ta["sma20"] or 0)
            ta_bits.append(f"SMA5-20 {fmt3(slope)}")
        if ta_bits:
            parts.append("TA: " + ", ".join(ta_bits))

        extras: List[str] = []
        if include_extras:
            for s in symbols:
                if s in {f, "DXY", "TNX", "$DXY", "$TNX", "UUP", "IEF", "TLT"}:
                    continue
                v = px.get(s)
                vw = vws.get(s)
                if v is not None and vw is not None:
                    extras.append(f"{s} {fmt3(v)}/vwap {fmt3(vw)} ({fmt3(v - vw)})")
                elif v is not None:
                    extras.append(f"{s} {v:.2f}")

        base_one_line = " | ".join(parts) if parts else "No prices yet"
        if structured and parts:
            lines: List[str] = [parts[0]]
            if macro_bits:
                lines.append("Macro: " + "; ".join(macro_bits))
            if ta_bits:
                lines.append("TA: " + ", ".join(ta_bits))
            if include_extras and extras:
                lines.append("Watchlist: " + "; ".join(extras))
            base = "\n".join(lines)
        else:
            base = base_one_line

        fancy = ""
        ollama_on = str(ollama_model).strip().lower() not in OLLAMA_DISABLED
        if ollama_on:
            context_rules = (
                "Rules: Rising DXY often pressures equities; rising TNX weighs on growth. "
                "Consider VWAP: above favors longs; below favors shorts. TA bands guide stretch."
            )
            fact_lines = [base_one_line]
            if dxy_d is not None or tnx_d is not None:
                fact_lines.append(
                    f"Focus bias {self.bias()}; DXY trend {dir_label(dxy_d)}; TNX trend {dir_label(tnx_d)}.")
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

        result = base
        if fancy:
            result += "\n" + fancy

        if ollama_on:
            pruned_disp: Dict[str, float] = {}
            for s in symbols:
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
                "vwap": {k: v for k, v in vws.items() if k in pruned_disp or k == f},
            }
            ai = ollama_bias(telemetry, model=ollama_model, url=ollama_url, timeout=ollama_timeout)
            if isinstance(ai, dict):
                bias = str(ai.get("bias", "")).lower()
                rationale = str(ai.get("rationale", "")).strip()
                conf_pct = int(round(max(0.0, min(1.0, float(ai.get("confidence") or 0))) * 100))
                result += f"\nAI bias: {bias} ({conf_pct}%) — {rationale}"

        return result

# =========================== Q&A helpers ===========================

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

def ollama_answer(question: str, telemetry: Dict[str, Any], model: str = "llama3", url: str = "http://127.0.0.1:11434", timeout: int = 60, max_words: int = 120) -> Optional[str]:
    if str(model).strip().lower() in OLLAMA_DISABLED:
        return None
    try:
        prompt = _qa_prompt(question, telemetry, max_words=max_words)
        payload = {"model": model, "prompt": prompt, "stream": False, "keep_alive": "30m"}
        resp = _ollama_post(url, "/api/generate", payload, read_to=timeout)
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("response", "").strip() or None
    except Exception as e:
        log("(Q&A error)", e)
        return None

# =========================== History seeding (minute candles) ===========================

def _enum(enum_cls, *names):
    if not enum_cls:
        return None
    for n in names:
        for cand in (n, n.upper(), n.lower(), n.capitalize()):
            if hasattr(enum_cls, cand):
                return getattr(enum_cls, cand)
    return None

def _get_today_minute_candles(client, symbol: str) -> Optional[List[dict]]:
    """Fetch today's 1-minute candles using Schwab enums (no int fallback)."""
    price_history_ns = getattr(client, "PriceHistory", None)
    period_type_enum = getattr(price_history_ns, "PeriodType", None)
    period_enum = getattr(price_history_ns, "Period", None)
    frequency_type_enum = getattr(price_history_ns, "FrequencyType", None)
    frequency_enum = getattr(price_history_ns, "Frequency", None)

    pt_day = _enum(period_type_enum, "DAY")
    p_1d = _enum(period_enum, "ONE_DAY", "DAY_1", "OneDay")
    ft_min = _enum(frequency_type_enum, "MINUTE")
    f_1m = _enum(frequency_enum, "EVERY_MINUTE", "ONE_MINUTE", "EveryMinute", "OneMinute")

    kwargs = {}
    if pt_day and p_1d and ft_min and f_1m:
        kwargs = dict(period_type=pt_day, period=p_1d, frequency_type=ft_min, frequency=f_1m)

    try:
        resp = client.get_price_history(symbol, **kwargs) if kwargs else client.get_price_history(symbol)
    except Exception as e:
        log(f"price_history request error for {symbol}: {e}")
        return None

    try:
        data = resp.json() if hasattr(resp, "json") else resp
    except Exception as e:
        log(f"price_history parse error for {symbol}: {e}")
        return None

    if isinstance(data, dict):
        candles = data.get("candles") or data.get("data") or data.get("quotes")
        return candles if isinstance(candles, list) and candles else None
    return None

def _seed_brain_with_history(provider: BaseQuoteProvider, brain: MarketBrain, symbol: str, bars: int = 60) -> bool:
    """Warm TA AND initialize same-day VWAP using minute/daily candles."""
    client = getattr(provider, "client", None)
    if client is None:
        return False

    candles = _get_today_minute_candles(client, symbol) or []
    if not candles:
        try:
            resp = client.get_price_history(symbol)
        except (TypeError, ValueError, AttributeError, ReqConnectionError, Timeout, RequestException) as e:
            log(f"price_history fallback request error for {symbol}: {e}")
            return False
        try:
            data = resp.json() if hasattr(resp, "json") else resp
        except (ValueError, AttributeError) as e:
            log(f"price_history fallback parse error for {symbol}: {e}")
            return False
        if isinstance(data, dict):
            c = data.get("candles") or data.get("data") or data.get("quotes")
            candles = c if isinstance(c, list) and c else []
        else:
            candles = []

    if not candles:
        log(f"No candles available for {symbol}")
        return False

    brain.seed_vwap_from_candles(symbol, candles)

    count = 0
    for c in candles[-bars:]:
        close = coerce_float(c.get("close") or c.get("closePrice") or c.get("close_price"))
        if close is None:
            continue
        ts = coerce_ts(c.get("datetime") or c.get("time") or c.get("timestamp") or time.time())
        brain.ingest({symbol: Quote(symbol=symbol, last=close, bid=None, ask=None, timestamp=ts)})
        count += 1

    return count > 0

# =========================== Q&A modes ===========================

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
    quotes = provider.get_quotes(display_symbols)
    enrich_with_macros(quotes)
    brain.ingest(quotes)
    telemetry = _build_telemetry_for_qa(brain)
    ans = ollama_answer(question, telemetry, model=model, url=url, timeout=timeout, max_words=max_words)
    print(ans if ans else "Q&A unavailable (Ollama off or request failed).")

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
    """Format numeric values to ≤3 d.p., return '—' for None/invalid."""
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

def qa_chat(provider: BaseQuoteProvider, user_symbols: List[str], focus: str, model: str, url: str, timeout: int, max_words: int, history: int, interval: int):
    display_symbols = expand_symbols(user_symbols)
    brain = MarketBrain(focus=focus, max_trail=history)
    try:
        _seed_brain_with_history(provider, brain, focus, bars=min(120, history))
    except Exception as e:
        log(f"(seed warning) {e}")

    sampler = BackgroundSampler(provider, display_symbols, brain, interval=interval)
    sampler.start()

    from typing import Any, Dict, Optional

    # --- helper to format/print concise plan ---
    def _print_plan(trade_plan: Optional[Dict[str, Any]]) -> None:
        if not trade_plan:
            print("WAIT (—%) — intra\nEntry: — | Stop: — | TP: —\nBias: neutral | Note: No clear edge\n")
            return

        # safely coerce confidence -> int%
        conf_val = trade_plan.get("confidence", 0.0)
        try:
            pct = int(round(float(conf_val) * 100))
        except (TypeError, ValueError):
            pct = 0

        action = str(trade_plan.get("action", "wait")).upper()
        timeframe = str(trade_plan.get("timeframe", "intra"))
        entry_txt = str(trade_plan.get("entry", "—"))
        stop_txt = str(trade_plan.get("stop", "—"))  # renamed to avoid shadowing
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
            if cmd in {"ind", "indicators"}:
                print(_indicators_block(brain) + "\n"); continue

            # --- patched: concise trading plan instead of verbose narrative ---
            if cmd in {"analysis", "market analysis", "status"}:
                telemetry = _build_telemetry_for_qa(brain)
                plan = ollama_trade_plan(telemetry, model=model, url=url, timeout=timeout)
                if plan:
                    _print_plan(plan)
                else:
                    # deterministic fallback if LLM unavailable
                    ta = brain.ta_pack()
                    f = brain.focus
                    px = brain.latest_prices().get(f)
                    vwap = brain.latest_vwap().get(f)
                    buf = max(brain.vwap_buf, 0.35)
                    if None in (px, vwap) or ta.get("bb_up") is None or ta.get("bb_lo") is None:
                        print("WAIT (—%) — intra\nEntry: — | Stop: — | TP: —\nBias: neutral | Note: Need VWAP/BB\n")
                    else:
                        # quick heuristic: above vwap & macd>0 => long, below vwap & macd<0 => short, else wait
                        macd, signal = ta.get("macd"), ta.get("macd_signal")
                        if px > vwap and (macd is not None and signal is not None and macd >= signal):
                            stop = vwap - buf; tgt = ta["bb_up"] + 0.20
                            print(f"LONG (—%) — intra\nEntry: buy pullback to VWAP\nStop: {fmt3(stop)}\nTP: {fmt3(tgt)}\nBias: bullish | Note: Above VWAP + MACD≥sig\n")
                        elif px < vwap and (macd is not None and signal is not None and macd <= signal):
                            stop = vwap + buf; tgt = ta["bb_lo"] - 0.20
                            print(f"SHORT (—%) — intra\nEntry: sell bounce to VWAP\nStop: {fmt3(stop)}\nTP: {fmt3(tgt)}\nBias: bearish | Note: Below VWAP + MACD≤sig\n")
                        else:
                            print("WAIT (—%) — intra\nEntry: — | Stop: — | TP: —\nBias: neutral | Note: No confluence\n")
                continue

            # --- new: explicit 'plan' command for quick plan anytime ---
            if cmd in {"plan", "trade plan"}:
                telemetry = _build_telemetry_for_qa(brain)
                plan = ollama_trade_plan(telemetry, model=model, url=url, timeout=timeout)
                _print_plan(plan)
                continue

            # --- unified 'plan' command family (handles: 'plan', 'trade plan', 'plan long', 'plan  short now', etc.) ---
            if cmd.startswith("plan") or cmd in {"trade plan"}:
                # normalize (tolerates extra spaces/words)
                cmd_spaced = f" {cmd} "
                want_long = " long" in cmd_spaced
                want_short = " short" in cmd_spaced

                if want_long or want_short:
                    side = "long" if want_long else "short"
                    ta = brain.ta_pack()
                    f = brain.focus
                    px = brain.latest_prices().get(f)
                    vwap = brain.latest_vwap().get(f)
                    bb_mid = ta.get("bb_mid")
                    bb_up = ta.get("bb_up")
                    bb_lo = ta.get("bb_lo")
                    buf = max(brain.vwap_buf, 0.35)

                    if None in (px, vwap, bb_mid, bb_up, bb_lo):
                        print("WAIT (—%) — intra\nEntry: — | Stop: — | TP: —\nBias: neutral | Note: Need VWAP/BB\n")
                    else:
                        if side == "long":
                            stop_px = vwap - buf
                            tgt_px = (bb_up or px) + 0.20
                            print(
                                f"LONG (—%) — intra\n"
                                f"Entry: buy pullback to VWAP | Stop: {fmt3(stop_px)} | TP: {fmt3(tgt_px)}\n"
                                f"Bias: bullish | Note: Above VWAP or MACD≥sig\n"
                            )
                        else:
                            stop_px = vwap + buf
                            tgt_px = (bb_lo or px) - 0.20
                            print(
                                f"SHORT (—%) — intra\n"
                                f"Entry: sell bounce to VWAP | Stop: {fmt3(stop_px)} | TP: {fmt3(tgt_px)}\n"
                                f"Bias: bearish | Note: Below VWAP or MACD≤sig\n"
                            )
                    print()  # ensure newline
                    continue

                # generic 'plan' -> use LLM JSON plan (SINGLE copy — do not repeat)
                telemetry = _build_telemetry_for_qa(brain)
                plan = ollama_trade_plan(telemetry, model=model, url=url, timeout=timeout)
                _print_plan(plan if isinstance(plan, dict) else None)
                continue

            # default: send to QA answerer
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
            out[k] = round(float(v), 3)
        except (TypeError, ValueError):
            continue
    return out

def _csv_safe(x) -> str:
    return "" if x is None else str(x)

class TickerLoop:
    def __init__(
        self,
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
        # News controls
        news_on: bool = False,
        news_since_h: int = 12,
        news_limit: int = 12,
        news_weight: float = 0.30,
        print_news: bool = False,
    ):
        self.display_symbols = expand_symbols(symbols)  # includes UUP/IEF/TLT
        self.request_symbols = normalize_provider_symbols(self.display_symbols)  # filtered for Schwab
        self.provider = provider
        self.interval = max(1, int(interval))  # allow 1s loops for snappy GUI
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

        # News config
        self.news_on = bool(news_on) and bool(NEWSAPI_KEY)
        self.news_since_h = int(news_since_h)
        self.news_limit = int(news_limit)
        self.news_weight = float(news_weight)
        self.print_news = bool(print_news)

        # News fetcher (TTL cache) if enabled
        self._news_fetcher = None
        if self.news_on:
            if callable(NewsFetcher):
                try:
                    # Use env NEWS_TTL (default 900 inside NewsFetcher)
                    self._news_fetcher = NewsFetcher()

                except Exception as e:
                    log(f"(news) failed to initialize NewsFetcher: {e}")
                    self.news_on = False
            else:
                log("(news) disabled — news_client not available")
                self.news_on = False

        # Quiet-print defaults (overridable by CLI)
        self.window_points: int = 5
        self.min_print: float = 0.001
        self.always_print: bool = False

        # Warm brain (TA+VWAP) using minute candles
        try:
            _seed_brain_with_history(self.provider, self.brain, focus, bars=min(120, 3 * self.interval))
        except Exception as e:
            log(f"(seed warning) {e}")

    def _get_news(self) -> tuple[list, float, str]:
        """Returns: (items, bias, bullets_for_prompt)"""
        if not self.news_on or self._news_fetcher is None:
            return [], 0.0, ""

        # Build keywords
        symbols = [s for s in getattr(self, "display_symbols", []) if isinstance(s, str)]
        macro_defaults = ["Federal Reserve", "Treasury yields", "inflation", "CPI", "PCE", "jobs", "FOMC"]

        if callable(map_symbols_to_keywords):
            try:
                keywords = map_symbols_to_keywords(symbols)  # type: ignore[misc]
            except (ValueError, TypeError):
                keywords = list({*(symbols or []), *macro_defaults})
        else:
            keywords = list({*(symbols or []), *macro_defaults})

        # Fetch
        try:
            items = self._news_fetcher.get(  # type: ignore[union-attr]
                keywords, since_hours=int(self.news_since_h), limit=int(self.news_limit)
            )
        except (Timeout, RequestException, ValueError, TypeError) as e:
            log(f"(news) fetch error: {e}")
            return [], 0.0, ""

        if not items:
            return [], 0.0, ""

        # Score + compress (optional)
        try:
            bias = float(news_bias_score(items)) if callable(news_bias_score) else 0.0  # type: ignore[misc]
        except (ValueError, TypeError):
            bias = 0.0

        try:
            bullets = (
                compress_bullets(items, k=min(5, len(items)))  # type: ignore[misc]
                if callable(compress_bullets) else ""
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

        # --- Optional compact context (daily+intraday) for focus symbol ---
        ctx = None
        bias_code = None
        # Avoid type-checker grief: provider is typed BaseQuoteProvider, but SchwabQuoteProvider has .client
        schwab_client = getattr(self.provider, "client", None)
        if schwab_client is not None and callable(build_context):
            try:
                ctx = build_context(schwab_client, f)
            except Exception as e:
                log("(context) build_context error:", e)

        if ctx is not None and callable(compute_bias_score):
            try:
                bias_code = {"score": round(float(compute_bias_score(ctx)), 2)}
            except Exception as e:
                log("(context) compute_bias_score error:", e)

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
            "news": None,
            "context": ctx,  # <= defined above (or None)
            "bias_code": bias_code,  # <= defined above (or None)
        }

        bias = 0.0  # default news bias

        # --- Build telemetry for the model (now includes compact news) ---
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
                "prices": round_floats(pruned_disp, 3),
                "ta": round_floats(ta, 3),
                "vwap": round_floats({k: v for k, v in vwp.items() if k in pruned_disp or k == f}, 3),
            }
            # Attach context + bias_code to the model too
            if ctx is not None:
                telemetry["context"] = ctx
            if bias_code is not None:
                telemetry["bias_code"] = bias_code

            ai = ollama_bias(
                telemetry,
                model=self.ollama_model,
                url=self.ollama_url,
                timeout=self.ollama_timeout
            )
            payload["ai"] = ai

            # Optional confidence nudge using news bias (unchanged)
            if self.news_on and ai:
                try:
                    conf = float(ai.get("confidence") or 0.0)
                    sign = 1 if ai.get("bias") == "bullish" else -1 if ai.get("bias") == "bearish" else 0
                    conf = max(0.0, min(1.0, conf + sign * self.news_weight * float(bias or 0.0) * 0.2))
                    ai["confidence"] = conf
                except (TypeError, ValueError):
                    pass

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
            log("Requesting:", self.request_symbols)  # <— was unconditional
        quotes = self.provider.get_quotes(self.display_symbols)
        enrich_with_macros(quotes)
        self.brain.ingest(quotes)
        text = self.brain.narrative(
            self.display_symbols,
            ollama_model=self.ollama_model,
            ollama_url=self.ollama_url,
            ollama_timeout=self.ollama_timeout,
            structured=True,
            include_extras=False,
        )
        # Optional news one-liner
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
            log("Prices:", _pretty_prices(quotes))
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
    # News flags
    p.add_argument("--news", action="store_true", help="Fetch NewsAPI headlines and include in JSON + model prompt")
    p.add_argument("--news-since-h", type=int, default=6, help="Look back this many hours for news")
    p.add_argument("--news-limit", type=int, default=12, help="Max headlines to consider")
    p.add_argument("--news-weight", type=float, default=0.30, help="Weight for news bias when nudging AI summary (0..1)")
    p.add_argument("--print-news", action="store_true", help="Append a one-liner with news count+bias to prints")
    # VWAP / Gates
    p.add_argument("--vwap-buf", type=float, default=0.10, help="Absolute VWAP buffer (points) for above/below tests (default 0.10)")
    p.add_argument("--gate-consecutive", type=int, default=2, help="Consecutive snapshots required beyond VWAP buffer (default 2)")
    p.add_argument("--print-gates", action="store_true", help="Append one-line pre-order gate status to each print")
    # Q&A flags
    p.add_argument("--ask", default=None, help="Ask the model a single question using a fresh snapshot, then exit")
    p.add_argument("--chat", action="store_true", help="Interactive Q&A mode (fresh snapshot per question)")
    p.add_argument("--qa-max-words", type=int, default=120, help="Max words for Q&A answers")

    args = p.parse_args()
    log(f"[cfg] using callback_url='{CALLBACK_URL}' token_path='{TOKEN_PATH}'")

    provider = SchwabQuoteProvider(verbose=args.verbose)


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
        gate_consecutive=args.gate_consecutive,
        print_gates=args.print_gates,
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
