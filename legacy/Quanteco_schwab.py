# quanteco_gpt.py — Schwab API core + GPT analysis + Tk GUI (no Polygon, no Ollama)
from __future__ import annotations

import json
import os
import queue
import threading
import time
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
import urllib.error  # kept for OpenAI HTTP handling in ask_openai_responses

APP_NAME = "QuantEco GPT (Schwab)"

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
    try:
        if isinstance(x, numbers.Real) and not isinstance(x, bool):
            return math.isfinite(float(x))
        xf = float(x)
        return math.isfinite(xf)
    except (TypeError, ValueError, OverflowError):
        return False


def _coerce_float(x: Any) -> Optional[float]:
    if isinstance(x, bool):
        return None
    try:
        if isinstance(x, numbers.Real):
            v = float(x)
            return v if math.isfinite(v) else None
        v = float(x)
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
        p = _coerce_float(b.get("close"))
        v = _coerce_float(b.get("volume"))
        if p is None or v is None:
            continue
        pv += p * v
        vv += v
    if vv <= 0:
        return None
    return pv / vv


# ============================== Schwab provider ==============================

class SchwabProvider:
    """
    Initialize a Schwab client from an existing token file.
    If the token is missing/corrupt and allow_login_flow=True, run the browser login flow
    (do this from a console helper, not inside Tk).
    """
    def __init__(
        self,
        api_key: str,
        app_secret: str,
        callback_url: str,
        token_path: str,
        timeout: int = 15,
        *,
        allow_login_flow: bool = False,   # default: never prompt inside the GUI
    ):
        from schwab.auth import client_from_token_file, client_from_login_flow
        import json

        token_path  = token_path.strip()
        api_key     = api_key.strip()
        app_secret  = app_secret.strip()
        callback_url = callback_url.strip()

        # Only catch concrete "token is unusable" errors.
        token_errors = (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError, ValueError)

        try:
            # Non-interactive path: use the saved token.
            self.client = client_from_token_file(
                token_path=token_path,
                api_key=api_key,
                app_secret=app_secret,
            )
        except token_errors as e:
            if not allow_login_flow:
                raise RuntimeError(
                    f"Schwab token not usable ({e.__class__.__name__}). "
                    "Create/refresh the token with the console bootstrap script, then retry."
                ) from e

            # Explicitly allowed to do the interactive flow (run from console, not Tk)
            self.client = client_from_login_flow(
                api_key=api_key,
                app_secret=app_secret,
                callback_url=callback_url,
                token_path=token_path,
            )

        self.timeout = int(timeout)



    # ---- Quotes ----
    def get_quotes(self, symbols: List[str]) -> Dict[str, Quote]:
        """
        Returns {SYM: Quote}. Uses schwab get_quotes API.
        Common keys (subject to library version):
          lastPrice, bidPrice, askPrice, totalVolume, quoteTimeInLong (ms)
        """
        if not symbols:
            return {}
        try:
            # Many schwab-py builds use .get_quotes(list[str]) -> dict
            raw = self.client.get_quotes(symbols)
        except Exception as e:
            raise RuntimeError(f"Schwab get_quotes failed: {e}") from e

        out: Dict[str, Quote] = {}
        if isinstance(raw, dict):
            for sym, row in raw.items():
                if not isinstance(row, dict):
                    continue
                def g(k, alt=None):  # tolerant getter with alternates
                    v = row.get(k)
                    return row.get(alt) if v is None and alt else v

                last = _coerce_float(g("lastPrice", "mark"))
                bid  = _coerce_float(g("bidPrice", "bid"))
                ask  = _coerce_float(g("askPrice", "ask"))
                vol  = _coerce_float(g("totalVolume", "total_volume"))
                ts   = g("quoteTimeInLong", "tradeTimeInLong")
                ts_ms = int(ts) if isinstance(ts, (int, float)) else None

                out[(sym or "").upper()] = Quote(
                    symbol=(sym or "").upper(),
                    last=last, bid=bid, ask=ask,
                    timestamp_ms=ts_ms, total_volume=vol
                )
        return out

    # ---- Minute bars (today or fallback to prior session) ----
    def get_today_minute_bars(self, symbol: str, hours_back: int = 8) -> List[Dict[str, Any]]:
        """
        Uses Schwab price history minute candles.
        Typical response (subject to version):
          {'candles':[{'datetime': 1700000000000,'open':..,'high':..,'low':..,'close':..,'volume':..}, ...]}
        """
        sym = (symbol or "SPY").upper()
        try:
            # Many builds expose Client.get_price_history(...) signature similar to TDA's
            # Period=1 day, frequency=1 minute
            payload = self.client.get_price_history(
                symbol=sym,
                period_type="day",            # or Client.PriceHistory.PeriodType.DAY
                period=1,
                frequency_type="minute",      # or Client.PriceHistory.FrequencyType.MINUTE
                frequency=1,
                needExtendedHoursData=True,
            )
        except Exception as e:
            raise RuntimeError(f"Schwab price history failed: {e}") from e

        rows = []
        candles = payload.get("candles") if isinstance(payload, dict) else None
        if isinstance(candles, list):
            for c in candles:
                if not isinstance(c, dict):
                    continue
                ts = c.get("datetime")
                if not isinstance(ts, (int, float)):
                    continue
                rows.append({
                    "t": int(ts),
                    "open": c.get("open"),
                    "high": c.get("high"),
                    "low": c.get("low"),
                    "close": c.get("close"),
                    "volume": c.get("volume"),
                })

        # Clip to last N hours (based on local tz)
        if rows:
            cutoff_ms = int((datetime.now().timestamp() - max(1, int(hours_back)) * 3600) * 1000)
            rows = [r for r in rows if isinstance(r.get("t"), int) and r["t"] >= cutoff_ms]

        return rows


# ============================== Brain: compute features & prompt ==============================

def compute_ta_from_bars(bars: List[Dict[str, Any]]) -> TA:
    closes: List[float] = [float(b["close"]) for b in bars if _is_finite(b.get("close"))]
    vwap_val = vwap_from_bars(bars)
    rsi_val = rsi14(closes) if closes else None
    macd_val, sig_val, hist_val = macd_12269(closes) if closes else (None, None, None)
    return TA(vwap=vwap_val, rsi14=rsi_val, macd=macd_val, macd_signal=sig_val, macd_hist=hist_val)


def mk_context(symbol: str, quote: Optional[object], ta: TA, bars: List[Dict[str, Any]]) -> str:
    def _fmt(x: Optional[float], nd: int = 2) -> str:
        return f"{x:.{nd}f}" if isinstance(x, (int, float)) else "n/a"

    last = bid = ask = None
    ts = None
    if quote is not None:
        if isinstance(quote, dict):
            last = _coerce_float(quote.get("last", quote.get("px")))
            bid = _coerce_float(quote.get("bid"))
            ask = _coerce_float(quote.get("ask"))
            ts = quote.get("timestamp_ms") or quote.get("timestamp")
            ts = int(ts) if isinstance(ts, (int, float)) else None
        else:
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


# ============================== OpenAI ask/analyze ==============================

def ask_openai_responses(
    question: str,
    model: str,
    api_key: str,
    timeout: int = 45,
    using_market_data: bool = False,
) -> str:
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
            instructions = (
                "You are QuantEcho GPT-5 — a market strategist and intraday trading coach.\n"
                "If telemetry is attached, analyze it for a 15–60 min bias; otherwise discuss setups and risk.\n"
                "Be concise and actionable."
            )
            if using_market_data:
                instructions = (
                    "You are QuantEcho GPT-5 — a predictive intraday trading coach.\n"
                    "Analyze telemetry (price, VWAP, RSI, MACD, volume) and infer the 15–60 min bias.\n"
                    "Return a timestamped summary, bias with confidence, 3–5 drivers, and key levels."
                )

            req_body = {
                "model": model,
                "instructions": instructions,
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": question}]}
                ],
                "max_output_tokens": 700,
            }

            try:
                resp_json = _http_json("https://api.openai.com/v1/responses", req_body)
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="ignore")
                if "Unsupported parameter" in detail:
                    for p in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "seed", "logit_bias"):
                        if f"'{p}'" in detail:
                            req_body.pop(p, None)
                    resp_json = _http_json("https://api.openai.com/v1/responses", req_body)
                else:
                    raise
            return _extract_from_responses(resp_json)

        # Chat Completions API
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
            if "Unsupported parameter" in detail and "max_tokens" in detail and "max_completion_tokens" in detail:
                req_body.pop("max_tokens", None)
                req_body["max_completion_tokens"] = 700
                resp_json = _http_json("https://api.openai.com/v1/chat/completions", req_body)
                return _extract_from_chat(resp_json)
            if "Unsupported parameter" in detail and "'temperature'" in detail:
                req_body.pop("temperature", None)
                resp_json = _http_json("https://api.openai.com/v1/chat/completions", req_body)
                return _extract_from_chat(resp_json)
            raise

    except urllib.error.HTTPError as http_err:
        raw = http_err.read()
        detail = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
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
    """
    Schwab-backed poller: fetches quotes and 1m bars, updates telemetry, prints changes.
    """
    def __init__(
        self,
        provider: SchwabProvider,
        symbols: List[str],
        focus: str,
        interval: int,
        out_queue: "queue.Queue[str]",
        telemetry,
        bars_refresh_s: int = 150,
    ):
        super().__init__(daemon=True)
        self.provider = provider
        self.symbols = [s.upper() for s in symbols]
        self.focus = (focus or "SPY").upper()
        self.interval = max(1, int(interval))
        self.q = out_queue
        self.telemetry = telemetry
        self._stop = threading.Event()
        self._next_bars_at = 0.0
        self._bars_refresh_s = int(bars_refresh_s)
        self.last_focus: Optional[float] = None

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

    def _refresh_bars(self):
        try:
            bars = self.provider.get_today_minute_bars(self.focus, hours_back=8)
        except Exception as e:
            self.println(f"Bars error: {type(e).__name__}: {e}")
            bars = []

        self.telemetry.update_bars(self.focus, bars)
        self._next_bars_at = time.monotonic() + (self._bars_refresh_s if bars else max(self._bars_refresh_s, 600))

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
        self.println(f"Polling loop started — Schwab, focus={self.focus} symbols={self.symbols}")

        while not self._stop.is_set():
            loop_start = time.monotonic()

            # Quotes
            try:
                quotes = self.provider.get_quotes([self.focus] + [s for s in self.symbols if s != self.focus])
            except Exception as e:
                self.println(f"Quotes error: {type(e).__name__}: {e}")
                quotes = {}

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

            # Bars (periodic)
            if loop_start >= self._next_bars_at:
                self._refresh_bars()

            # Sleep in small chunks so stop() is responsive
            for _ in range(int(self.interval * 10)):
                if self._stop.is_set():
                    break
                time.sleep(0.1)

        self.println("Polling loop stopped")


# -------- Settings persistence (legacy path retained for continuity) --------
SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".quanteco_gpt")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

def _load_settings_legacy() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, PermissionError):
        return {}

def _save_settings_legacy(data: dict) -> None:
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
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
        self.geometry("1120x760")
        self.configure(bg=DARK["bg"])
        self._style()

        # state
        self.q: "queue.Queue[str]" = queue.Queue()
        self.poller: Optional[Poller] = None
        self.telemetry = TelemetryStore(stale_s=45)

        # load persisted settings (if any)
        _settings = _load_settings_legacy()
        remember = bool(_settings.get("remember", True))

        # prefs
        self.var_symbols = tk.StringVar(value=_settings.get("symbols") or os.getenv("SYMBOLS", "SPY NVDA QQQ"))
        self.var_focus = tk.StringVar(value=_settings.get("focus") or os.getenv("FOCUS", "SPY"))
        self.var_interval = tk.IntVar(value=int(_settings.get("interval") or os.getenv("INTERVAL", 20)))
        self.var_model = tk.StringVar(value=_settings.get("model") or os.getenv("AI_MODEL", "gpt-5"))

        # Schwab creds (remember optional)
        self.var_app_key = tk.StringVar(value=(_settings.get("schwab_app_key") if remember else os.getenv("SCHWAB_APP_KEY", "")))
        self.var_app_secret = tk.StringVar(value=(_settings.get("schwab_app_secret") if remember else os.getenv("SCHWAB_APP_SECRET", "")))
        self.var_callback = tk.StringVar(value=_settings.get("schwab_callback") or os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182"))
        self.var_token_path = tk.StringVar(value=_settings.get("schwab_token_path") or os.getenv("SCHWAB_TOKEN_PATH", str(Path.home() / "schwab_tokens.json")))

        # OpenAI
        self.var_openai = tk.StringVar(value=(_settings.get("openai_key") if remember else os.getenv("OPENAI_API_KEY", "")) or "")
        self.var_remember = tk.BooleanVar(value=remember)

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
        remember = bool(self.var_remember.get())
        data = {
            "symbols": self.var_symbols.get().strip(),
            "focus": self.var_focus.get().strip(),
            "interval": int(self.var_interval.get()),
            "model": self.var_model.get().strip(),
            "schwab_callback": self.var_callback.get().strip(),
            "schwab_token_path": self.var_token_path.get().strip(),
            "remember": remember,
        }
        if remember:
            data["openai_key"] = self.var_openai.get().strip()
            data["schwab_app_key"] = self.var_app_key.get().strip()
            data["schwab_app_secret"] = self.var_app_secret.get().strip()
        else:
            data["openai_key"] = ""
            data["schwab_app_key"] = ""
            data["schwab_app_secret"] = ""

        try:
            _save_settings_legacy(data)
            self.println(f"[{time.strftime('%H:%M:%S')}] Settings saved.\n")
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

        # creds row — Schwab + OpenAI
        creds = ttk.Frame(self, style="Toolbar.TFrame")
        creds.pack(side="top", fill="x")
        ttk.Label(creds, text="Schwab App Key").pack(side="left", padx=(10, 6))
        ttk.Entry(creds, textvariable=self.var_app_key, show="•", width=26).pack(side="left")
        ttk.Label(creds, text="App Secret").pack(side="left", padx=(10, 6))
        ttk.Entry(creds, textvariable=self.var_app_secret, show="•", width=26).pack(side="left")

        row2 = ttk.Frame(self, style="Toolbar.TFrame"); row2.pack(side="top", fill="x")
        ttk.Label(row2, text="Callback URL").pack(side="left", padx=(10, 6))
        ttk.Entry(row2, textvariable=self.var_callback, width=38).pack(side="left")
        ttk.Label(row2, text="Token Path").pack(side="left", padx=(10, 6))
        ttk.Entry(row2, textvariable=self.var_token_path, width=36).pack(side="left")

        row3 = ttk.Frame(self, style="Toolbar.TFrame"); row3.pack(side="top", fill="x")
        ttk.Label(row3, text="OpenAI key").pack(side="left", padx=(10, 6))
        ttk.Entry(row3, textvariable=self.var_openai, show="•", width=36).pack(side="left")
        ttk.Label(row3, text="Model").pack(side="left", padx=(10, 6))
        ttk.Entry(row3, textvariable=self.var_model, width=16).pack(side="left")

        # remember & save
        self.chk_rem = ttk.Checkbutton(bar, text="Remember keys", variable=self.var_remember)
        self.chk_rem.pack(side="left", padx=(8, 6))
        ttk.Button(bar, text="Save", command=self.on_save_settings).pack(side="left", padx=(0, 8))

        # prints
        self.text = tk.Text(self, height=22, wrap="word",
                            bg=DARK["sunken"], fg=DARK["text"], insertbackground=DARK["text"], relief="flat")
        self.text.pack(fill="both", expand=True, padx=10, pady=10)

        # analysis row
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

        def _enter(_evt=None):
            self.on_ask()
        self.bind("<Return>", _enter)

    def on_restart(self) -> None:
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

    def _mk_provider(self) -> SchwabProvider:
        return SchwabProvider(
            api_key=self.var_app_key.get().strip(),
            app_secret=self.var_app_secret.get().strip(),
            callback_url=self.var_callback.get().strip(),
            token_path=self.var_token_path.get().strip(),
            timeout=15,
        )

    def on_start(self):
        if self.poller and self.poller.is_alive():
            messagebox.showinfo("Running", "Loop already running.")
            return

        # sanity check Schwab creds
        if not self.var_app_key.get().strip() or not self.var_app_secret.get().strip():
            messagebox.showwarning("Schwab", "Missing Schwab App Key / App Secret.")
            return

        symbols = self.var_symbols.get().split()
        focus = (self.var_focus.get() or "SPY").upper()
        interval = int(self.var_interval.get())

        try:
            provider = self._mk_provider()
        except Exception as e:
            messagebox.showerror("Schwab auth", f"Could not initialize Schwab client:\n{e}")
            return

        self.poller = Poller(
            provider, symbols, focus, interval, self.q,
            telemetry=self.telemetry, bars_refresh_s=max(150, interval)
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

        def worker():
            try:
                prompt = q
                using_ctx = False
                if include_ctx:
                    ctx = self.telemetry.get_context(focus)
                    if ctx:
                        using_ctx = True
                        prompt = f"{q}\n\nTelemetry:\n{ctx}\n"
                    else:
                        self.q.put("[GUI] Telemetry unavailable; proceeding without context.\n")

                ans = ask_openai_responses(prompt, model=model, api_key=key, timeout=45, using_market_data=using_ctx)
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

        def worker():
            try:
                ctx = self.telemetry.get_context(focus)
                if not ctx:
                    ans = "[Analyze] Telemetry unavailable; start the loop to fetch bars/quotes."
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
                    ans = ask_openai_responses(prompt, model=model, api_key=key, timeout=45, using_market_data=True)
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
