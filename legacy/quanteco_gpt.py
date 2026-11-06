# quanteco_gpt.py — single-file Polygon core + GPT-5 analysis + Tk GUI
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple
from typing import Any, Optional
import math
import numbers
import tkinter as tk
from tkinter import ttk, messagebox
import sys
import subprocess
import platform
from pathlib import Path

APP_NAME = "QuantEco GPT"

# ---------- simple config file helpers ----------
def _config_path() -> Path:
    """Return a per-user config file path (no admin privileges required)."""
    home = Path.home()
    if platform.system() == "Windows":
        base = Path(os.getenv("APPDATA") or home / "AppData" / "Roaming")
        return base / "QuantEcoGPT" / "config.json"
    # macOS/Linux
    base = Path(os.getenv("XDG_CONFIG_HOME") or (home / ".config"))
    return base / "quanteco_gpt" / "config.json"

def _load_config() -> dict:
    try:
        p = _config_path()
        if not p.exists():
            return {}
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

def _save_config(d: dict) -> None:
    p = _config_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(p)
        try:
            # best-effort privacy on POSIX
            os.chmod(p, 0o600)
        except OSError:
            pass
    except OSError:
        # silent failure is fine; UI remains usable
        pass


# ============================== Data classes ==============================

@dataclass
class Quote:
    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    timestamp_ms: Optional[int]
    total_volume: Optional[float]


@dataclass
class TA:
    vwap: Optional[float]
    rsi14: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]


# ============================== Math / TA ==============================

def _is_finite(x: Any) -> bool:
    """
    Return True iff x can be coerced to a finite float.
    Only catches the concrete errors float() can raise.
    """
    try:
        # Fast path for real numbers (avoids str parsing surprises)
        if isinstance(x, numbers.Real) and not isinstance(x, bool):
            return math.isfinite(float(x))
        xf = float(x)  # may raise TypeError/ValueError/OverflowError
        return math.isfinite(xf)
    except (TypeError, ValueError, OverflowError):
        return False


def _coerce_float(x: Any) -> Optional[float]:
    """
    Convert x to a finite float or return None.
    Avoids overly broad exceptions and ignores bools.
    """
    if isinstance(x, bool):
        return None
    try:
        # Fast path for real numbers
        if isinstance(x, numbers.Real):
            v = float(x)
            return v if math.isfinite(v) else None
        # Strings or other types rely on float() parsing
        v = float(x)  # may raise TypeError/ValueError/OverflowError
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def ema(values: Iterable[float], period: int) -> List[float]:
    vals = [v for v in values if _is_finite(v)]
    if not vals:
        return []
    k = 2 / (period + 1)
    out: List[float] = []
    ema_val: Optional[float] = None
    for v in vals:
        ema_val = v if ema_val is None else (v - ema_val) * k + ema_val
        out.append(ema_val)
    return out


def rsi14(closes: List[float]) -> Optional[float]:
    if len(closes) < 15:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        chg = closes[i] - closes[i - 1]
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    # Wilder’s smoothing
    period = 14
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))




def macd_12269(closes: List[float]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if len(closes) < 35:  # 26 + 9
        return None, None, None

    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    if not ema12 or not ema26:
        return None, None, None

    # Align series by tail
    n = min(len(ema12), len(ema26))
    macd_line_series = [ema12[-n + i] - ema26[-n + i] for i in range(n)]

    sig_series = ema(macd_line_series, 9)
    if not sig_series:
        return None, None, None

    macd_val = macd_line_series[-1]
    sig_val = sig_series[-1]
    hist_val = macd_val - sig_val
    return macd_val, sig_val, hist_val



def vwap_from_bars(bars: List[Dict[str, Any]]) -> Optional[float]:
    pv = 0.0
    vv = 0.0
    for b in bars:
        p = _coerce_float(b.get("close"))  # Polygon minute bars: o/h/l/c/v
        v = _coerce_float(b.get("volume"))
        if p is None or v is None:
            continue
        pv += p * v
        vv += v
    if vv <= 0:
        return None
    return pv / vv


# ============================== Polygon client ==============================

class PolygonClient:
    BASE = "https://api.polygon.io"

    def __init__(self, api_key: str, timeout: int = 10):
        self.api_key = (api_key or "").strip()
        self.timeout = int(timeout)

    def _get_json(self, path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY missing.")
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{self.BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_quotes(self, symbols: List[str]) -> Dict[str, object]:
        """
        Return {symbol -> Quote-like} using Polygon v2 snapshot (modern endpoint only).
        Avoids the legacy /v2/snapshot/locale/... fallback that can 403.
        """
        from urllib.error import HTTPError, URLError
        import json as _json
        import inspect

        out: Dict[str, object] = {}
        if not symbols:
            return out

        # Optional: map to your Quote dataclass if present
        quote_ctor = Quote  # you defined it in this file
        ctor_keys = set(inspect.signature(quote_ctor).parameters) if callable(quote_ctor) else set()

        def _make_quote(
                symb: str,
                last_val: Optional[float],
                bid_val: Optional[float],
                ask_val: Optional[float],
                ts_millis: Any,
                vol_val: Optional[float],
        ) -> object:
            """Create a Quote instance if the ctor matches; else a plain dict."""
            if ctor_keys:
                try:
                    payload = {
                        "symbol": symb,
                        "last": last_val,
                        "bid": bid_val,
                        "ask": ask_val,
                        "timestamp_ms": ts_millis,
                        "total_volume": vol_val,
                    }
                    kwargs = {k: payload[k] for k in ctor_keys if k in payload}
                    return quote_ctor(**kwargs)  # type: ignore[misc]
                except (TypeError, ValueError, AttributeError):
                    # fall through to dict
                    pass

            return {
                "symbol": symb,
                "last": last_val,
                "bid": bid_val,
                "ask": ask_val,
                "timestamp_ms": ts_millis,
                "total_volume": vol_val,
            }

        def _as_items(payload: Any) -> List[Dict[str, Any]]:
            if isinstance(payload, dict) and isinstance(payload.get("tickers"), list):
                return [item for item in payload["tickers"] if isinstance(item, dict)]
            return []

        chunk = 40
        for i in range(0, len(symbols), chunk):
            batch = [s.upper() for s in symbols[i:i + chunk]]
            tickers = ",".join(batch)

            try:
                data = self._get_json("/v2/snapshot", {
                    "locale": "us",
                    "market": "stocks",
                    "ticker.any_of": tickers,
                })
            except (HTTPError, URLError, TimeoutError, ValueError, _json.JSONDecodeError):
                # If you want to see the exact API error in your UI:
                # raise RuntimeError("Polygon snapshot failed") from None
                continue

            for row in _as_items(data):
                try:
                    sym = str(row.get("ticker") or "").upper().strip()
                    if not sym:
                        continue
                    last_trade = row.get("lastTrade") or {}
                    last_quote = row.get("lastQuote") or {}

                    # Bid/ask first (so we can use a mid as a fallback)
                    bid = _coerce_float(last_quote.get("bp"))
                    ask = _coerce_float(last_quote.get("ap"))
                    if bid is not None and ask is not None and bid > ask:
                        bid, ask = None, None

                    # Primary last from lastTrade
                    last = _coerce_float(last_trade.get("p"))
                    ts_ms = last_trade.get("t")

                    # Fallbacks for off-hours: day close, prev close, or bid/ask mid
                    if last is None:
                        day_close = _coerce_float((row.get("day") or {}).get("c"))
                        prev_close = _coerce_float((row.get("prevDay") or {}).get("c"))
                        mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
                        last = day_close or prev_close or mid

                    vol = _coerce_float((row.get("day") or {}).get("v"))
                    out[sym] = _make_quote(sym, last, bid, ask, ts_ms, vol)

                except (TypeError, ValueError, AttributeError):
                    continue

        return out

    def get_today_minute_bars(self, symbol: str, hours_back: int = 8) -> List[Dict[str, Any]]:
        """Return last `hours_back` hours of 1-min bars for today if available,
        else the most recent regular session, using at most one extra HTTP call."""
        from urllib.error import HTTPError, URLError
        import json as _json
        from datetime import datetime, timedelta

        try:
            from zoneinfo import ZoneInfo as _ZoneInfo, ZoneInfoNotFoundError as _ZoneInfoNotFoundError
        except ImportError:  # pragma: no cover
            _ZoneInfo = None

            class _ZoneInfoNotFoundError(Exception):
                pass

        # Resolve ET
        if _ZoneInfo is not None:
            try:
                ny_tz = _ZoneInfo("America/New_York")
            except _ZoneInfoNotFoundError:
                ny_tz = datetime.now().astimezone().tzinfo
        else:
            ny_tz = datetime.now().astimezone().tzinfo

        def _fetch_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
            path = f"/v2/aggs/ticker/{str(symbol).upper()}/range/1/minute/{start_date}/{end_date}"
            params = {"adjusted": "true", "sort": "asc", "limit": 50000}
            try:
                payload = self._get_json(path, params)
            except HTTPError as e:
                # Read a short error body safely (no broad except)
                detail = ""
                if hasattr(e, "read"):
                    body = e.read()
                    if isinstance(body, (bytes, bytearray)):
                        try:
                            detail = body.decode("utf-8", errors="ignore")
                        except UnicodeDecodeError:
                            detail = ""
                code = getattr(e, "code", "HTTPError")
                print(f"[Polygon] HTTP {code} for {symbol} {start_date}->{end_date} {detail[:140]}")
                return []
            except (URLError, TimeoutError, ValueError, _json.JSONDecodeError) as e:
                print(f"[Polygon] {_fetch_range.__name__} network/parse error: {e} {start_date}->{end_date}")
                return []

            result_rows = payload.get("results") if isinstance(payload, dict) else None
            return result_rows if isinstance(result_rows, list) else []

        def _as_bars(raw_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for row in raw_rows:
                if isinstance(row, dict):
                    out.append({
                        "t": row.get("t"),
                        "open": row.get("o"),
                        "high": row.get("h"),
                        "low": row.get("l"),
                        "close": row.get("c"),
                        "volume": row.get("v"),
                    })
            return out

        def _et_session_bounds(d: datetime) -> tuple[int, int]:
            d_et = d.astimezone(ny_tz)
            day = d_et.date()
            dt_open = datetime(day.year, day.month, day.day, 9, 30, tzinfo=ny_tz)
            dt_close = datetime(day.year, day.month, day.day, 16, 0, tzinfo=ny_tz)
            return int(dt_open.timestamp() * 1000), int(dt_close.timestamp() * 1000)

        def _clip_to_session(
                bars: List[Dict[str, Any]],
                start_ts_ms: int,
                end_ts_ms: int,
        ) -> List[Dict[str, Any]]:
            filtered: List[Dict[str, Any]] = []
            for bar in bars:
                bar_ts = bar.get("t")
                if isinstance(bar_ts, int) and start_ts_ms <= bar_ts <= end_ts_ms:
                    filtered.append(bar)
            return filtered

        now = datetime.now(ny_tz)
        hrs = max(1, int(hours_back))

        # ---- Single-day fetches only (avoid multi-day minute entitlement)
        today_str = now.strftime("%Y-%m-%d")
        sess_start_ms, sess_end_ms = _et_session_bounds(now)

        # exactly today
        rows = _fetch_range(today_str, today_str)

        # If nothing yet (pre-open/holiday/closed), try exactly yesterday
        if not rows:
            yesterday_dt = now - timedelta(days=1)
            yesterday_str = yesterday_dt.strftime("%Y-%m-%d")
            rows = _fetch_range(yesterday_str, yesterday_str)
            if not rows:
                return []

        # Group by ET day & clip
        def _day_key(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=ny_tz).strftime("%Y-%m-%d")

        by_day: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            ts = r.get("t")
            if isinstance(ts, int):
                by_day.setdefault(_day_key(ts), []).append(r)

        def _clip_day(day_dt: datetime) -> List[Dict[str, Any]]:
            s_ms, e_ms = _et_session_bounds(day_dt)
            return _clip_to_session(_as_bars(by_day.get(day_dt.strftime("%Y-%m-%d"), [])), s_ms, e_ms)

        # Prefer today; otherwise yesterday
        today_bars = _clip_day(now)
        if today_bars:
            cutoff_ms = int((now - timedelta(hours=hrs)).timestamp() * 1000)
            return [b for b in today_bars if b["t"] >= max(cutoff_ms, sess_start_ms)]

        yesterday_dt = now - timedelta(days=1)
        yesterday_bars = _clip_day(yesterday_dt)
        if not yesterday_bars:
            return []
        cutoff_ms = int((now - timedelta(hours=hrs)).timestamp() * 1000)
        start_ms, _ = _et_session_bounds(yesterday_dt)
        return [b for b in yesterday_bars if b["t"] >= max(cutoff_ms, start_ms)]


# ============================== Brain: compute features & prompt ==============================

def compute_ta_from_bars(bars: List[Dict[str, Any]]) -> TA:
    closes: List[float] = [float(b["close"]) for b in bars if _is_finite(b.get("close"))]
    vwap_val = vwap_from_bars(bars)
    rsi_val = rsi14(closes) if closes else None
    macd_val, sig_val, hist_val = macd_12269(closes) if closes else (None, None, None)
    return TA(vwap=vwap_val, rsi14=rsi_val, macd=macd_val, macd_signal=sig_val, macd_hist=hist_val)


def mk_context(symbol: str, quote: Optional[object], ta: TA, bars: List[Dict[str, Any]]) -> str:
    """Builds a compact market context. Accepts Quote dataclass OR dict OR None."""
    def _fmt(x: Optional[float], nd: int = 2) -> str:
        return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "n/a"

    # Extract fields from either Quote-like object or dict
    last = bid = ask = None  # type: Optional[float]
    ts = None                # type: Optional[int]

    if quote is not None:
        if isinstance(quote, dict):
            last = _coerce_float(quote.get("last", quote.get("px")))
            bid = _coerce_float(quote.get("bid"))
            ask = _coerce_float(quote.get("ask"))
            ts = quote.get("timestamp_ms") or quote.get("timestamp")  # ms epoch expected
            ts = int(ts) if isinstance(ts, (int, float)) else None
        else:
            # dataclass / object with attrs (safe getattr + type checks)
            last = _coerce_float(getattr(quote, "last", None))
            bid = _coerce_float(getattr(quote, "bid", None))
            ask = _coerce_float(getattr(quote, "ask", None))
            ts_v = getattr(quote, "timestamp_ms", getattr(quote, "timestamp", None))
            ts = int(ts_v) if isinstance(ts_v, (int, float)) else None

    stamp = ""
    if isinstance(ts, int):
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone()
        stamp = dt.strftime("%Y-%m-%d %H:%M:%S %Z")

    lines: List[str] = [
        f"FOCUS: {symbol.upper()}",
        f"Last {_fmt(last)}  Bid {_fmt(bid)}  Ask {_fmt(ask)}  @ {stamp or 'n/a'}",
        f"VWAP {_fmt(ta.vwap)}  RSI14 {_fmt(ta.rsi14)}  MACD {_fmt(ta.macd,3)}  Sig {_fmt(ta.macd_signal,3)}  Hist {_fmt(ta.macd_hist,3)}",
        "Recent 1m bars (newest last):",
    ]
    tail = bars[-30:] if len(bars) > 30 else bars
    for b in tail:
        t = b.get("t")
        c = _coerce_float(b.get("close"))
        v = _coerce_float(b.get("volume"))
        if not isinstance(t, int) or c is None or v is None:
            continue
        dt = datetime.fromtimestamp(t / 1000, tz=timezone.utc).astimezone()
        lines.append(f"  {dt.strftime('%H:%M')}  close={c:.2f}  vol={int(v)}")
    return "\n".join(lines)






def ask_openai_responses(
    question: str,
    model: str,
    api_key: str,
    timeout: int = 45,
    using_market_data: bool = False,
) -> str:
    """
    Routes to the correct OpenAI endpoint & params:
      - GPT-5 family -> /v1/responses + max_output_tokens
      - Others      -> /v1/chat/completions + max_tokens

    using_market_data: set True when the prompt actually includes live telemetry/context
                       (i.e., the 'Include market context' checkbox AND we attached data).
    """
    import json, urllib.request, urllib.error

    def _headers():
        h = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": APP_NAME,
            "Accept": "application/json",
        }
        org = os.getenv("OPENAI_ORG") or os.getenv("OPENAI_ORGANIZATION")
        proj = os.getenv("OPENAI_PROJECT")
        if org:
            h["OpenAI-Organization"] = org
        if proj:
            h["OpenAI-Project"] = proj
        return h

    def _http_json(url: str, json_body: dict) -> dict:
        payload = json.dumps(json_body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=_headers(), method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rid = resp.headers.get("x-request-id")
            data = json.loads(resp.read().decode("utf-8"))
            if rid:
                data["_request_id"] = rid
            return data

    def _extract_from_responses(resp: dict) -> str:
        txt = resp.get("output_text")
        if isinstance(txt, str) and txt.strip():
            return txt.strip()

        # Newer GPT-5 schema
        inner = resp.get("response")
        if isinstance(inner, dict):
            t2 = inner.get("output_text")
            if isinstance(t2, str) and t2.strip():
                return t2.strip()

        out = resp.get("output")
        if isinstance(out, list):
            parts = []
            for block in out:
                if not isinstance(block, dict):
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    for seg in content:
                        t = seg.get("text") or seg.get("content")
                        if isinstance(t, str) and t.strip():
                            parts.append(t.strip())
                else:
                    t = block.get("text") or block.get("content")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            if parts:
                return "\n".join(parts).strip()

        # Optional: last-ditch check
        for k in ("text", "message", "output"):
            maybe = resp.get(k)
            if isinstance(maybe, str) and maybe.strip():
                return maybe.strip()

        return "(no answer)"

    def _extract_from_chat(resp: dict) -> str:
        choices = resp.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            first_choice = choices[0]
            message_obj = first_choice.get("message") or {}
            content_text = message_obj.get("content")
            if isinstance(content_text, str) and content_text.strip():
                return content_text.strip()
        return "(no answer)"

    lower = (model or "").lower()
    use_responses = lower.startswith("gpt-5") or lower.startswith("o5")

    try:
        if use_responses:
            # ---- Choose system instructions based on whether we actually attached telemetry ----
            if using_market_data:
                # Live Market Mode
                instructions = (
                    "You are QuantEcho GPT-5 — a predictive intraday SPY trading coach.\n"
                    "Analyze real-time telemetry (price, VWAP, RSI, MACD, volume, EMAs, news) to infer the next 15–60 min bias.\n"
                    "Keep answers timestamped, concise, and data-driven. Focus on actionable setups and risk levels.\n"
                    "Output:\n"
                    "[HH:MM ET] Market Summary\n"
                    "Bias: <Bullish/Bearish/Neutral> (<prob%>)\n"
                    "Key Drivers: <3–5 bullet points>\n"
                    "Levels: VWAP=<>, R1=<>, S1=<>\n"
                    "Notes: <1–2 concise remarks>"
                )
            else:
                # Pivot Mode (no live data)
                instructions = (
                    "You are QuantEcho GPT-5 — a market strategist and intraday trading coach.\n"
                    "When live telemetry isn't available, think like a trader reviewing market behavior, probabilities, and setups.\n"
                    "Discuss strategy, risk management, psychology, and intraday structure.\n"
                    "Use realistic, data-aware reasoning but no fake numbers.\n"
                    "Keep tone confident, educational, and conversational, as if mentoring a trader between sessions."
                )

            req_body = {
                "model": model,
                "instructions": instructions,
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": question}]}
                ],
                "max_output_tokens": 700,
                # Intentionally omit temperature/top_p for GPT-5 family unless you need them
            }

            try:
                resp_json = _http_json("https://api.openai.com/v1/responses", req_body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="ignore")
                if "Unsupported parameter" in detail:
                    # Strip any flagged params then retry once.
                    for p in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed", "logit_bias"):
                        if f"'{p}'" in detail:
                            req_body.pop(p, None)
                    resp_json = _http_json("https://api.openai.com/v1/responses", req_body)
                else:
                    raise
            return _extract_from_responses(resp_json)

        # ---- Chat Completions API (legacy/4.x/etc.) ----
        req_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a concise market assistant. Keep answers crisp and actionable."},
                {"role": "user", "content": question},
            ],
            "max_tokens": 700,
            "temperature": 0.3,
        }
        try:
            resp_json = _http_json("https://api.openai.com/v1/chat/completions", req_body)
            return _extract_from_chat(resp_json)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            # Some models want 'max_completion_tokens' instead of 'max_tokens'
            if "Unsupported parameter" in detail and "max_tokens" in detail and "max_completion_tokens" in detail:
                req_body.pop("max_tokens", None)
                req_body["max_completion_tokens"] = 700
                resp_json = _http_json("https://api.openai.com/v1/chat/completions", req_body)
                return _extract_from_chat(resp_json)
            # Some models also reject 'temperature' on chat; strip & retry once.
            if "Unsupported parameter" in detail and "'temperature'" in detail:
                req_body.pop("temperature", None)
                resp_json = _http_json("https://api.openai.com/v1/chat/completions", req_body)
                return _extract_from_chat(resp_json)
            raise

    except urllib.error.HTTPError as http_err:
        # Safely read and decode the response body
        raw = http_err.read()
        detail = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)

        # Try to extract a structured message from JSON
        try:
            payload_json = json.loads(detail)
        except json.JSONDecodeError:
            err_msg = (detail.strip() or getattr(http_err, "reason", "") or str(http_err))
        else:
            err_obj = payload_json.get("error")
            if isinstance(err_obj, dict):
                err_msg = err_obj.get("message") or err_obj.get("code") or ""
            else:
                err_msg = payload_json.get("message") or ""
            if not err_msg:
                err_msg = detail.strip() or getattr(http_err, "reason", "") or str(http_err)

        return f"[OpenAI HTTP {http_err.code}] {err_msg}"

    except urllib.error.URLError as url_err:
        reason = getattr(url_err, "reason", None)
        return f"[OpenAI Network] {reason or str(url_err)}"

    except (json.JSONDecodeError, UnicodeDecodeError) as parse_err:
        return f"[OpenAI Parse] {type(parse_err).__name__}: {parse_err}"



# ============================== Polling loop ==============================

class Poller(threading.Thread):
    def __init__(
        self,
        client,
        symbols: List[str],
        focus: str,
        interval: int,
        out_queue: "queue.Queue[str]",
        telemetry,
        bars_refresh_s: int = 150,
    ):
        super().__init__(daemon=True)
        self.client = client
        self.symbols = [s.upper() for s in symbols]
        self.focus = (focus or "SPY").upper()
        self.interval = max(1, int(interval))
        self.q = out_queue
        self.telemetry = telemetry
        self._stop = threading.Event()
        self._next_bars_at = 0.0
        self._bars_refresh_s = int(bars_refresh_s)
        self.last_focus: Optional[float] = None
        self._last_quote_time: Dict[str, float] = {}
        self._quote_refresh_s = 60
        self._last_bar_check = 0.0

    def stop(self) -> None:
        self._stop.set()

    def println(self, s: str) -> None:
        self.q.put(f"[{time.strftime('%H:%M:%S')}] {s}\n")

    @staticmethod
    def _get_last_px(q: object) -> Optional[float]:
        if q is None:
            return None
        if hasattr(q, "last"):
            try:
                return float(getattr(q, "last"))
            except (TypeError, ValueError):
                return None
        if isinstance(q, dict):
            v = q.get("last", q.get("px"))
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        return None

    def _fetch_quotes(self) -> Dict[str, object]:
        quotes = {}
        now = time.monotonic()

        # throttle focus too
        if (now - self._last_quote_time.get(self.focus, 0)) >= self._quote_refresh_s:
            try:
                quotes.update(self.client.get_quotes([self.focus]))
                self._last_quote_time[self.focus] = now
            except Exception as e:
                self.println(f"Focus quote error: {type(e).__name__}: {e}")

        # throttle batch of others
        if (now - self._last_quote_time.get("_batch", 0)) >= self._quote_refresh_s:
            others = [s for s in self.symbols if s != self.focus]
            if others:
                try:
                    quotes.update(self.client.get_quotes(others))
                    for sym in others:
                        self._last_quote_time[sym] = now
                    self._last_quote_time["_batch"] = now
                except Exception as e:
                    self.println(f"Other quotes error: {type(e).__name__}: {e}")

        return quotes

    def _refresh_bars(self):
        try:
            bars = self.client.get_today_minute_bars(self.focus, hours_back=8)
        except Exception as e:
            self.println(f"Bars error: {type(e).__name__}: {e}")
            bars = []

        self.telemetry.update_bars(self.focus, bars)

        # Backoff if nothing came back (off-hours/holiday/entitlement)
        if bars:
            self._next_bars_at = time.monotonic() + self._bars_refresh_s
        else:
            self._next_bars_at = time.monotonic() + max(self._bars_refresh_s, 600)

        n = len(bars)
        if n:
            last_t = bars[-1].get("t")
            if isinstance(last_t, int):
                ts = datetime.fromtimestamp(last_t / 1000, tz=timezone.utc).astimezone()
                stale = "(last session)" if ts.date() != datetime.now().astimezone().date() else ""
                self.println(f"Bars: {self.focus} {n}\u00d71m (through {ts.strftime('%Y-%m-%d %H:%M %Z')} {stale})")
            else:
                self.println(f"Bars: {self.focus} {n}\u00d71m (timestamp n/a)")
        else:
            self.println(f"Bars: {self.focus} none (off-hours or no data).")

    def run(self) -> None:
        self.println(f"Polling loop started — focus={self.focus} symbols={self.symbols}")

        while not self._stop.is_set():
            loop_start = time.monotonic()

            # Quotes
            quotes = self._fetch_quotes()
            if quotes:
                self.telemetry.update_quotes(quotes)

            fq = self.telemetry._snaps.get(self.focus)
            if fq:
                last_px = self._get_last_px(fq.quote)
                if last_px is not None:
                    prev = self.last_focus
                    self.last_focus = last_px
                    if prev is None:
                        self.println(f"Prices: {self.focus}={last_px:.2f}")
                    else:
                        pct = (last_px - prev) / prev if prev else 0.0
                        if abs(pct) >= 0.001:
                            self.println(f"Prices: {self.focus}={last_px:.2f} ({pct:+.2%})")
                        else:
                            self.println("No material change (<0.10%); skipping print.")

            # Bars
            if loop_start >= self._next_bars_at:
                self._refresh_bars()

            # Sleep
            sleep_total = self.interval
            for _ in range(int(sleep_total * 10)):
                if self._stop.is_set():
                    break
                time.sleep(0.1)

        self.println("Polling loop stopped")


# -------- Settings persistence --------
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".quanteco_gpt")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")


def _load_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        # first run: no settings yet
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        # file exists but is corrupt / not valid JSON
        return {}
    except PermissionError:
        # cannot read the file; behave as if no settings
        return {}



def _save_settings(data: dict) -> None:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # atomic-ish replace
    try:
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        # fallback if replace not available
        os.remove(SETTINGS_PATH) if os.path.exists(SETTINGS_PATH) else None
        os.rename(tmp, SETTINGS_PATH)

# ============================== GUI ==============================

DARK = {
    "bg": "#0f1216",
    "panel": "#141922",
    "sunken": "#0c1016",
    "text": "#dfe5ee",
    "muted": "#9aa9bf",
}

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1040x720")
        self.configure(bg=DARK["bg"])
        self._style()

        # state
        self.q: "queue.Queue[str]" = queue.Queue()
        self.poller: Optional[Poller] = None
        self.telemetry = TelemetryStore(stale_s=45)

        # load persisted settings (if any)
        _settings = _load_settings()
        remember = bool(_settings.get("remember", True))

        # prefs
        self.var_symbols = tk.StringVar(value=_settings.get("symbols") or os.getenv("SYMBOLS", "SPY NVDA QQQ"))
        self.var_focus = tk.StringVar(value=_settings.get("focus") or os.getenv("FOCUS", "SPY"))
        self.var_interval = tk.IntVar(value=int(_settings.get("interval") or os.getenv("INTERVAL", 20)))
        self.var_model = tk.StringVar(value=_settings.get("model") or os.getenv("AI_MODEL", "gpt-5"))

        # keys (only if remember=True; else pull from environment)
        self.var_poly = tk.StringVar(
            value=(_settings.get("polygon_key") if remember else os.getenv("POLYGON_API_KEY", "")) or "")
        self.var_openai = tk.StringVar(
            value=(_settings.get("openai_key") if remember else os.getenv("OPENAI_API_KEY", "")) or "")

        self.var_remember = tk.BooleanVar(value=remember)

        # settings
        self.var_symbols = tk.StringVar(value=_settings.get("symbols") or os.getenv("SYMBOLS", "SPY NVDA QQQ"))
        self.var_focus = tk.StringVar(value=_settings.get("focus") or os.getenv("FOCUS", "SPY"))
        self.var_interval = tk.IntVar(value=20)
        self.var_poly = tk.StringVar(value=_settings.get("polygon_key") or os.getenv("POLYGON_API_KEY", ""))
        self.var_model = tk.StringVar(value=_settings.get("model") or os.getenv("AI_MODEL", "gpt-5"))
        self.var_openai = tk.StringVar(value=_settings.get("openai_key") or os.getenv("OPENAI_API_KEY", ""))

        # remember toggle
        self.var_remember = tk.BooleanVar(value=bool(_settings.get("remember", True)))



        self._build()
        self.after(60, lambda _=None: self._pump())


    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TFrame", background=DARK["panel"])
        s.configure("Toolbar.TFrame", background=DARK["panel"])
        s.configure("TLabel", background=DARK["panel"], foreground=DARK["text"])
        s.configure("TButton", background=DARK["panel"], foreground=DARK["text"], padding=6)
        s.configure("TEntry", fieldbackground=DARK["sunken"], background=DARK["sunken"],
                    foreground=DARK["text"], insertcolor=DARK["text"])

    def on_save_settings(self) -> None:
        # only persist keys if remember is on
        remember = bool(self.var_remember.get())
        data = {
            "symbols": self.var_symbols.get().strip(),
            "focus": self.var_focus.get().strip(),
            "interval": int(self.var_interval.get()),
            "model": self.var_model.get().strip(),
            "remember": remember,
        }
        if remember:
            data["polygon_key"] = self.var_poly.get().strip()
            data["openai_key"] = self.var_openai.get().strip()
        else:
            # scrub any existing keys on disk
            data["polygon_key"] = ""
            data["openai_key"] = ""

        try:
            _save_settings(data)
            self.println(f"[{time.strftime('%H:%M:%S')}] Keys & settings saved.\n")
        except Exception as e:
            messagebox.showerror("Save failed", f"Could not save settings:\n{e}")

    def _build(self):
        # top bar
        bar = ttk.Frame(self, style="Toolbar.TFrame")
        bar.pack(side="top", fill="x")
        ttk.Label(bar, text="Symbols").pack(side="left", padx=(10, 6))
        ttk.Entry(bar, textvariable=self.var_symbols, width=30).pack(side="left")
        ttk.Label(bar, text="Focus").pack(side="left", padx=(10, 6))
        ttk.Entry(bar, textvariable=self.var_focus, width=8).pack(side="left")
        ttk.Label(bar, text="Interval(s)").pack(side="left", padx=(10, 6))
        ttk.Entry(bar, textvariable=self.var_interval, width=6).pack(side="left")
        ttk.Button(bar, text="Start", command=self.on_start).pack(side="left", padx=10)
        ttk.Button(bar, text="Stop", command=self.on_stop).pack(side="left")
        ttk.Button(bar, text="Restart", command=self.on_restart).pack(side="left", padx=(8, 0))

        # creds row
        creds = ttk.Frame(self, style="Toolbar.TFrame")
        creds.pack(side="top", fill="x")
        ttk.Label(creds, text="Polygon key").pack(side="left", padx=(10, 6))
        ttk.Entry(creds, textvariable=self.var_poly, show="•", width=34).pack(side="left")
        ttk.Label(creds, text="OpenAI key").pack(side="left", padx=(10, 6))
        ttk.Entry(creds, textvariable=self.var_openai, show="•", width=34).pack(side="left")
        ttk.Label(creds, text="Model").pack(side="left", padx=(10, 6))
        ttk.Entry(creds, textvariable=self.var_model, width=16).pack(side="left")
        # NEW: remember & save
        # at the end of the top bar (or wherever you placed them)
        self.chk_rem = ttk.Checkbutton(bar, text="Remember keys", variable=self.var_remember)
        self.chk_rem.pack(side="left", padx=(8, 6))

        ttk.Button(bar, text="Save", command=self.on_save_settings).pack(side="left", padx=(0, 8))

        # prints
        self.text = tk.Text(self, height=22, wrap="word",
                            bg=DARK["sunken"], fg=DARK["text"], insertbackground=DARK["text"], relief="flat")
        self.text.pack(fill="both", expand=True, padx=10, pady=10)

        # analysis row
        self.var_cached_bars = tk.BooleanVar(value=True)  # default: use cache
        ana = ttk.Frame(self, style="Toolbar.TFrame")
        ana.pack(side="bottom", fill="x")
        ttk.Button(ana, text="Analyze with GPT-5", command=self.on_analyze).pack(side="right", padx=10, pady=8)

        # --- Q&A row ---
        qa = ttk.Frame(self, style="Toolbar.TFrame")
        qa.pack(side="bottom", fill="x")

        self.var_question = tk.StringVar(value="What’s the current market bias for my focus symbol?")
        self.var_with_ctx = tk.BooleanVar(value=True)

        ttk.Entry(qa, textvariable=self.var_question).pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        ttk.Checkbutton(qa, text="Include market context", variable=self.var_with_ctx).pack(side="left", padx=(6, 6))
        ttk.Button(qa, text="Ask", command=self.on_ask).pack(side="right", padx=10)
        ttk.Checkbutton(ana, text="Use cached bars", variable=self.var_cached_bars).pack(side="right", padx=6)

        # allow Enter to submit
        def _enter(_evt=None):
            self.on_ask()

        self.bind("<Return>", _enter)

    def _warmup_telemetry_once(self, focus: str, symbols: List[str]) -> None:
        """
        Populate telemetry immediately if the loop hasn't yet.
        Uses Polygon only to fill the TelemetryStore; GPT reads telemetry.
        Narrow exception handling: we only catch RuntimeError (e.g., missing key).
        """
        poly = self.var_poly.get().strip()
        if not poly:
            self.q.put("[GUI] Warmup skipped: missing Polygon key.\n")
            return

        client = PolygonClient(poly, timeout=20)
        if self.var_cached_bars.get() and self.telemetry.has(focus):
            self.q.put("[Warmup] Skipping bar fetch — using cached data.\n")
            return

        # Quotes warmup
        try:
            quotes = client.get_quotes(symbols)
        except RuntimeError as e:
            # Raised by PolygonClient when POLYGON_API_KEY is missing/invalid
            self.q.put(f"[Warmup] Quotes fetch aborted: {e}\n")
            return
        else:
            if quotes:
                self.telemetry.update_quotes(quotes)
            else:
                self.q.put("[Warmup] Quotes empty; continuing.\n")

        # Bars warmup for focus symbol
        try:
            bars = client.get_today_minute_bars(focus, hours_back=8)
        except RuntimeError as e:
            self.q.put(f"[Warmup] Bars fetch aborted: {e}\n")
            return
        else:
            if bars:
                self.telemetry.update_bars(focus, bars)
            else:
                self.q.put("[Warmup] No minute bars returned; telemetry has quotes only.\n")

    def on_save_keys(self) -> None:
        """Persist API keys and a few prefs if 'Remember keys' is enabled."""
        if not self.var_remember.get():
            # If user disabled remembering, clear saved keys but keep prefs
            cfg = _load_config()
            cfg["remember"] = False
            cfg["keys"] = {}
            cfg["prefs"] = {
                "symbols": self.var_symbols.get().strip(),
                "focus": self.var_focus.get().strip(),
                "interval": int(self.var_interval.get()),
                "model": self.var_model.get().strip(),
            }
            _save_config(cfg)
            self.println(f"[{time.strftime('%H:%M:%S')}] Settings saved (keys not stored).\n")
            return

        cfg = {
            "remember": True,
            "keys": {
                "polygon_api_key": self.var_poly.get().strip(),
                "openai_api_key": self.var_openai.get().strip(),
            },
            "prefs": {
                "symbols": self.var_symbols.get().strip(),
                "focus": self.var_focus.get().strip(),
                "interval": int(self.var_interval.get()),
                "model": self.var_model.get().strip(),
            },
        }
        _save_config(cfg)
        self.println(f"[{time.strftime('%H:%M:%S')}] Keys & settings saved.\n")

    def on_restart(self) -> None:
        # persist current state first
        self.on_save_settings()

        if self.poller and self.poller.is_alive():
            self.poller.stop()
            self.poller.join(timeout=1.0)

        py = sys.executable
        args = [py] + sys.argv
        self.println(f"[{time.strftime('%H:%M:%S')}] Restarting…\n")
        try:
            os.execv(py, args)
        except OSError:
            try:
                subprocess.Popen(args, close_fds=(os.name != "nt"))
            finally:
                os._exit(0)

    def println(self, s: str):
        self.text.insert("end", s)
        self.text.see("end")

    def _pump(self):
        try:
            while True:
                chunk = self.q.get_nowait()
                self.println(chunk)
        except queue.Empty:
            pass
        self.after(60, lambda _=None: self._pump())

    def on_start(self):
        if self.poller and self.poller.is_alive():
            messagebox.showinfo("Running", "Loop already running.")
            return
        poly = self.var_poly.get().strip()
        if not poly:
            messagebox.showwarning("Polygon", "Missing POLYGON_API_KEY.")
            return
        symbols = self.var_symbols.get().split()
        focus = (self.var_focus.get() or "SPY").upper()
        interval = int(self.var_interval.get())

        client = PolygonClient(poly, timeout=10)
        self.poller = Poller(
            client, symbols, focus, interval, self.q,
            telemetry=self.telemetry, bars_refresh_s=max(150, interval)  # sensible default
        )
        self.poller.start()
        self.println(f"[{time.strftime('%H:%M:%S')}] Started — focus={focus}, symbols={symbols}\n")

    def on_stop(self):
        if self.poller:
            self.poller.stop()
            self.poller = None
            self.println(f"[{time.strftime('%H:%M:%S')}] Stop requested\n")

    def on_ask(self):
        key = self.var_openai.get().strip()
        if not key:
            self.println("[GUI] OpenAI key missing.\n")
            return

        model = (self.var_model.get().strip() or "gpt-5")
        q = (self.var_question.get().strip() or "Hello")
        self.println(f"[{time.strftime('%H:%M:%S')}] Q> {q}\n")

        include_ctx = bool(self.var_with_ctx.get())
        focus = (self.var_focus.get() or "SPY").upper()
        symbols = self.var_symbols.get().split()

        def worker():
            try:
                prompt = q
                if include_ctx:
                    ctx = self.telemetry.get_context(focus)
                    if ctx is None:
                        # Warmup once (fills store), then try again — still reads from telemetry
                        self.q.put("[GUI] No telemetry yet; warming up …\n")
                        self._warmup_telemetry_once(focus, symbols)
                        ctx = self.telemetry.get_context(focus)

                    if ctx:
                        prompt = f"{q}\n\nTelemetry:\n{ctx}\n"
                    else:
                        self.q.put("[GUI] Telemetry unavailable; proceeding without context.\n")

                ans = ask_openai_responses(prompt, model=model, api_key=key, timeout=45)
            except Exception as e:
                ans = f"[Ask error] {type(e).__name__}: {e}"
            self.q.put(f"[{time.strftime('%H:%M:%S')}] A> {ans}\n")

        threading.Thread(target=worker, daemon=True).start()

    def on_analyze(self):
        key = self.var_openai.get().strip()
        if not key:
            self.println("[GUI] OpenAI key missing.\n")
            return
        model = self.var_model.get().strip() or "gpt-5"
        focus = (self.var_focus.get() or "SPY").upper()
        symbols = self.var_symbols.get().split()

        def worker():
            try:
                ctx = self.telemetry.get_context(focus)
                if ctx is None:
                    self.q.put("[GUI] No telemetry yet; warming up …\n")
                    self._warmup_telemetry_once(focus, symbols)
                    ctx = self.telemetry.get_context(focus)

                if not ctx:
                    ans = "[Analyze] Telemetry unavailable; start the loop or provide a Polygon key."
                else:
                    prompt = (
                        "You are an intraday market assistant.\n"
                        "Given the telemetry below, provide:\n"
                        "1) One-sentence directional bias (bullish/bearish/neutral) with confidence %.\n"
                        "2) 3–5 bullets on key signals (VWAP / RSI / MACD / momentum).\n"
                        "3) One risk to the view and a simple trade idea with stop.\n\n"
                        f"{ctx}\n\n"
                        "Answer crisply."
                    )
                    ans = ask_openai_responses(prompt, model=model, api_key=key, timeout=45)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="ignore")
                ans = f"[OpenAI HTTP {e.code}] {detail.strip() or e.reason}"
            except urllib.error.URLError as e:
                ans = f"[OpenAI Network] {getattr(e, 'reason', e)}"
            except Exception as e:
                ans = f"[Analyze error] {type(e).__name__}: {e}"
            self.q.put(f"[{time.strftime('%H:%M:%S')}] A> {ans}\n")

        self.println(f"[{time.strftime('%H:%M:%S')}] Analyzing {focus} with {model}…\n")
        threading.Thread(target=worker, daemon=True).start()


# ============================== Telemetry ==============================

@dataclass
class TelemetrySnapshot:
    symbol: str
    ts_ms: int
    quote: Optional[object]
    bars: List[Dict[str, Any]]
    ta: TA

class TelemetryStore:
    """
    Thread-safe in-memory market telemetry store.
    Poller populates it; GPT prompts read from it.
    """
    def __init__(self, stale_s: int = 45):
        self._lock = threading.RLock()
        self._snaps: Dict[str, TelemetrySnapshot] = {}
        self._stale_s = int(stale_s)

    def update_quotes(self, quotes: Dict[str, object]) -> None:
        now_ms = int(time.time() * 1000)
        with self._lock:
            for sym, q in (quotes or {}).items():
                u = (sym or "").upper()
                prev = self._snaps.get(u)
                self._snaps[u] = TelemetrySnapshot(
                    symbol=u,
                    ts_ms=now_ms,
                    quote=q,
                    bars=(prev.bars if prev else []),
                    ta=(prev.ta if prev else TA(None, None, None, None, None)),
                )

    def update_bars(self, symbol: str, bars: List[Dict[str, Any]]) -> None:
        sym = (symbol or "").upper()
        now_ms = int(time.time() * 1000)
        ta = compute_ta_from_bars(bars or [])
        if bars:
            ts_last = bars[-1].get("t")
            if isinstance(ts_last, int):
                dt_last = datetime.fromtimestamp(ts_last / 1000, tz=timezone.utc).astimezone()
                print(f"[DEBUG] Last bar timestamp for {symbol}: {dt_last.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        with self._lock:
            prev = self._snaps.get(sym)
            self._snaps[sym] = TelemetrySnapshot(
                symbol=sym,
                ts_ms=now_ms,
                quote=(prev.quote if prev else None),
                bars=(bars or []),
                ta=ta,
            )

    def get_context(self, symbol: str) -> Optional[str]:
        sym = (symbol or "").upper()
        with self._lock:
            snap = self._snaps.get(sym)
            if not snap:
                return None
            return mk_context(sym, snap.quote, snap.ta, snap.bars)

    def has(self, symbol: str) -> bool:
        with self._lock:
            return (symbol or "").upper() in self._snaps

if __name__ == "__main__":
    app = App()
    app.mainloop()
