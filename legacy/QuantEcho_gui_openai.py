# quantecho_gui.py — Dark dashboard UI with NEWS panel (Polygon backend, GPT-5 Q&A)
from __future__ import annotations
import io
import os
import re
import sys
import json
import time
import queue
import inspect
import threading
import webbrowser
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr, suppress
from typing import Any, TextIO, cast, Dict, List
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import logging, subprocess

from news_client import (
    NewsFetcher,
    map_symbols_to_keywords,
    compress_bullets,
    news_bias_score,
)

# --------------------- regex helpers ---------------------
RSI_RE  = re.compile(r"\bRSI(?:\s*\(\s*\d+\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)
MACD_RE = re.compile(r"\bMACD(?:\s*\(\s*\d+\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)
VWAP_RE = re.compile(r"\bVWAP(?:\s*\(\s*\d+\s*\))?\s*[:=]?\s*([+-]?\d+(?:\.\d+)?)", re.I)

def _coerce_int(x, default: int = 0) -> int:
    if isinstance(x, bool):
        return default
    if isinstance(x, int):
        return x
    if isinstance(x, float):
        return int(x) if math.isfinite(x) else default
    if isinstance(x, str):
        s = x.strip()
        try:
            v = float(s)
            return int(v) if math.isfinite(v) else default
        except ValueError:
            return default
    return default

def _coerce_float(x) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, str):
        s = x.strip()
        try:
            v = float(s)
            return v if math.isfinite(v) else None
        except ValueError:
            return None
    return None

# --------------------- import your core module ---------------------
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import quantecho_core as core  # your backend module
    core = cast(Any, core)
except Exception as err:
    raise SystemExit(
        "Could not import quantecho_core. Make sure this GUI file is next to it.\n" + str(err)
    )
# ---- Force-Polygon shim & block Schwab fallback -------------------------------
import urllib.request, urllib.parse, json as _json
from datetime import datetime, timezone

# 1) Block Schwab: if core tries to construct it, fail loudly (no OAuth spam)
if hasattr(core, "SchwabQuoteProvider"):
    class _SchwabBlocked:
        def __init__(self, *a, **kw):
            raise RuntimeError("Schwab provider disabled by GUI. Use Polygon API instead.")
    core.SchwabQuoteProvider = _SchwabBlocked  # type: ignore[attr-defined]

# 2) Provide PolygonQuoteProvider if core doesn't have one
if not hasattr(core, "PolygonQuoteProvider"):
    class PolygonQuoteProvider:
        """
        Minimal Polygon provider shim for TickerLoop. Reads POLYGON_API_KEY from env
        or constructor kw. Implements:
          - get_quotes(symbols) -> Dict[str, core.Quote]
          - get_today_minute_aggregates(symbol) -> List[dict]
        Optional no-op:
          - get_price_history(...) -> []
        """
        def __init__(self, *, api_key: str | None = None, base_url: str = "https://api.polygon.io"):
            self.api_key = (api_key or os.getenv("POLYGON_API_KEY", "")).strip()
            if not self.api_key:
                raise RuntimeError("POLYGON_API_KEY is not set")
            self.base_url = base_url

        # --- HTTP helper
        def _get(self, path: str, params: dict | None = None, timeout: int = 12) -> dict:
            q = dict(params or {})
            q["apiKey"] = self.api_key
            url = f"{self.base_url}{path}?{urllib.parse.urlencode(q)}"
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return _json.loads(resp.read().decode("utf-8"))

        # --- Quotes: use snapshots batch (fast & cheap)
        def get_quotes(self, symbols: list[str]) -> dict[str, object]:
            """Return a dict of symbol -> Quote-like object using Polygon snapshot batch."""
            from urllib.error import HTTPError, URLError
            import json
            from typing import Any, Dict, List

            out: Dict[str, object] = {}
            if not symbols:
                return out

            def _as_items(d: Any) -> List[Dict[str, Any]]:
                """Extract a well-typed tickers list from Polygon response dict."""
                if isinstance(d, dict) and isinstance(d.get("tickers"), list):
                    return [elem for elem in d["tickers"] if isinstance(elem, dict)]
                return []

            chunk_size = 40  # keep under Polygon's ~50 limit
            for i in range(0, len(symbols), chunk_size):
                chunk = [s.upper() for s in symbols[i:i + chunk_size]]
                tickers = ",".join(chunk)

                # Try modern endpoint first
                try:
                    data = self._get("/v2/snapshot", {"ticker.any_of": tickers})
                except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                    data = None

                items: List[Dict[str, Any]] = _as_items(data)

                # Fallback to legacy endpoint if needed
                if not items:
                    try:
                        data2 = self._get(
                            "/v2/snapshot/locale/us/markets/stocks/tickers",
                            {"tickers": tickers},
                        )
                    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                        data2 = None
                    items = _as_items(data2)

                # Parse each item
                for item in items:
                    try:
                        sym = str(item.get("ticker") or "").upper().strip()
                        if not sym:
                            continue

                        last_trade = item.get("lastTrade") or {}
                        last_quote = item.get("lastQuote") or {}

                        last = _coerce_float(last_trade.get("p"))
                        ts = last_trade.get("t")

                        bid = _coerce_float(last_quote.get("bp"))
                        ask = _coerce_float(last_quote.get("ap"))
                        if bid is not None and ask is not None and bid > ask:
                            bid, ask = None, None  # rare NBBO inversion — discard both

                        vol = _coerce_float((item.get("day") or {}).get("v"))

                        # Prefer core.Quote; otherwise fall back to a plain dict
                        quote_cls = getattr(core, "Quote", None)
                        if quote_cls is not None:
                            try:
                                quote_obj = quote_cls(
                                    symbol=sym, last=last, bid=bid, ask=ask,
                                    timestamp=ts, total_volume=vol
                                )
                            except (TypeError, ValueError, AttributeError):
                                quote_obj = {
                                    "symbol": sym, "last": last, "bid": bid, "ask": ask,
                                    "timestamp": ts, "total_volume": vol,
                                }
                        else:
                            quote_obj = {
                                "symbol": sym, "last": last, "bid": bid, "ask": ask,
                                "timestamp": ts, "total_volume": vol,
                            }

                        out[sym] = quote_obj

                    except (AttributeError, TypeError, ValueError):
                        # Skip only this malformed item
                        continue

            return out

        # --- Today 1m aggregates (NY session envelope)
        def get_today_minute_aggregates(self, symbol: str) -> list[dict[str, object]]:
            """Return today's 1-minute bars for `symbol` (NY midnight → now+1d)."""
            from urllib.error import HTTPError, URLError
            import json
            from datetime import datetime, timedelta

            # Resolve timezone (prefer America/New_York) without broad excepts or CamelCase aliasing
            try:
                import zoneinfo as _zoneinfo  # py3.9+
            except ImportError:  # pragma: no cover
                _zoneinfo = None  # type: ignore[assignment]

            if _zoneinfo is not None and hasattr(_zoneinfo, "ZoneInfo"):
                err_cls = getattr(_zoneinfo, "ZoneInfoNotFoundError", Exception)
                try:
                    tz = _zoneinfo.ZoneInfo("America/New_York")
                except err_cls:
                    tz = datetime.now().astimezone().tzinfo
            else:
                tz = datetime.now().astimezone().tzinfo

            now = datetime.now(tz)
            start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
            date_from = start.strftime("%Y-%m-%d")
            date_to = (now + timedelta(days=1)).strftime("%Y-%m-%d")

            path = f"/v2/aggs/ticker/{symbol.upper()}/range/1/min/{date_from}/{date_to}"
            params = {"adjusted": "true", "sort": "asc", "limit": 50000}

            # Network / decode guarded
            try:
                data = self._get(path, params)
            except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                return []

            rows = data.get("results") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return []

            out: list[dict[str, object]] = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                try:
                    out.append(
                        {
                            "datetime": r.get("t"),  # ms epoch
                            "open": r.get("o"),
                            "high": r.get("h"),
                            "low": r.get("l"),
                            "close": r.get("c"),
                            "volume": r.get("v"),
                        }
                    )
                except (TypeError, ValueError):
                    continue

            return out

        @staticmethod
        def get_price_history(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
            """No-op for cores that probe price history; intentionally returns empty."""
            return []


    # expose under core namespace so your existing code sees it
    core.PolygonQuoteProvider = PolygonQuoteProvider  # type: ignore[attr-defined]
# ------------------------------------------------------------------------------

APP_NAME = "QuantEcho"
CONF_DIR = Path(os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "QuantEcho"
CONF_PATH = CONF_DIR / "config.json"

DEFAULTS: Dict[str, Any] = {
    # === Data provider (Polygon) ===
    "polygon_api_key": os.getenv("POLYGON_API_KEY", ""),
    # symbols / loop
    "symbols": "SPY NVDA QQQ XLF",
    "focus": "SPY",
    "interval": 20,
    "history": 180,
    "window": 5,
    "min_print": 0.001,
    "always_print": False,
    "vwap_buf": 0.10,
    "gate_consecutive": 2,
    "print_gates": True,
    "emit_json": True,
    "csv_path": "",

    # === GPT-5 model & OpenAI key ===
    # (We keep the param names 'ollama_model'/'ollama_timeout' to remain compatible with core signatures.)
    "ollama_model": os.getenv("AI_MODEL", "gpt-5-nano"),   # UI label: "GPT-5 model"
    "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
    "ollama_timeout": 45,                                   # UI label: "Model timeout (s)"

    "qa_max_words": 120,
    "question": "Current market trend?",

    # --- NEWS options (GUI + backend passthrough) ---
    "news": bool(os.getenv("NEWSAPI_KEY", "")),
    "news_since_h": 6,
    "news_limit": 25,
    "news_weight": 0.30,
    "print_news": False,
    "news_api_key": os.getenv("NEWSAPI_KEY", ""),
}

# --------------------- stream bridge ---------------------
class StreamToQueue(io.TextIOBase):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__()
        self.q = q
    def writable(self) -> bool:  # type: ignore[override]
        return True
    def write(self, s: str) -> int:  # type: ignore[override]
        if s:
            self.q.put(s)
        return len(s)
    def flush(self) -> None:  # type: ignore[override]
        pass

# --------------------- background workers ---------------------
class LoopWorker(threading.Thread):
    def __init__(self, out_q: "queue.Queue[str]", params: dict):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.params = params
        self._stop_evt = threading.Event()
        self.loop: Any | None = None

    def stop(self) -> None:
        self._stop_evt.set()
        loop = self.loop
        if loop is None:
            return
        with suppress(AttributeError, RuntimeError):
            loop.stop()

    def run(self):
        # ---- Runtime overrides / env passthroughs ----
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
            setattr(core, "NEWSAPI_KEY", self.params["news_api_key"])

        # Polygon API key → env for core
        poly_key = (self.params.get("polygon_api_key") or os.getenv("POLYGON_API_KEY") or "").strip()
        if poly_key:
            os.environ["POLYGON_API_KEY"] = poly_key

        # OpenAI key (GPT-5 Q&A)
        key = (self.params.get("openai_api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
        if key:
            os.environ["OPENAI_API_KEY"] = key

        try:
            # ---- Require Polygon provider; do NOT fall back to Schwab ----
            provider_ctor = getattr(core, "PolygonQuoteProvider", None)
            if provider_ctor is None:
                self.out_q.put(
                    "\n[GUI] Polygon provider missing in core. "
                    "Update quantecho_core to a build that includes PolygonQuoteProvider.\n"
                )
                return

            if not poly_key:
                self.out_q.put(
                    "\n[GUI] POLYGON_API_KEY is not set. "
                    "Open Settings and paste your Polygon API key (or set env POLYGON_API_KEY).\n"
                )
                return

            # Instantiate provider (catch ctor errors like missing/invalid key)
            try:
                provider = provider_ctor()
            except Exception as e:
                self.out_q.put(f"\n[GUI] Failed to initialize Polygon provider: {type(e).__name__}: {e}\n")
                return

            # ---- Build loop kwargs (filtered by TickerLoop signature) ----
            maybe_kwargs = {
                "symbols": self.params.get("symbols", DEFAULTS["symbols"]).split(),
                "interval": int(self.params.get("interval", 20)),
                "focus": self.params.get("focus", "SPY"),
                # model args (kept for core compatibility)
                "model": self.params.get("ollama_model", DEFAULTS["ollama_model"]),
                "timeout": int(self.params.get("ollama_timeout", 45)),
                "emit_json": bool(self.params.get("emit_json", False)),
                "csv_path": self.params.get("csv_path") or None,
                "once": False,
                "verbose": False,
                "vwap_buf": float(self.params.get("vwap_buf", 0.10)),
                "gate_consecutive": int(self.params.get("gate_consecutive", 2)),
                "print_gates": bool(self.params.get("print_gates", True)),
                # NEWS knobs
                "news_on": bool(self.params.get("news", False)),
                "news_since_h": int(self.params.get("news_since_h", 6)),
                "news_limit": int(self.params.get("news_limit", 12)),
                "news_weight": float(self.params.get("news_weight", 0.30)),
                "print_news": bool(self.params.get("print_news", False)),
            }
            init_sig = inspect.signature(core.TickerLoop.__init__)
            allowed = {k: v for k, v in maybe_kwargs.items() if k in init_sig.parameters}

            # ---- Create and tune the loop ----
            self.loop = core.TickerLoop(provider, **allowed)

            loop_obj = self.loop
            if loop_obj is not None:
                if hasattr(loop_obj, "window_points"):
                    loop_obj.window_points = int(self.params.get("window", 5))
                if hasattr(loop_obj, "min_print"):
                    loop_obj.min_print = float(self.params.get("min_print", 0.001))
                if hasattr(loop_obj, "always_print"):
                    loop_obj.always_print = bool(self.params.get("always_print", False))
                brain = getattr(loop_obj, "brain", None)
                if brain is not None and hasattr(brain, "_max_trail"):
                    brain._max_trail = int(self.params.get("history", 180))

            # ---- Run loop with stdout/stderr bridged to GUI ----
            stream_io: TextIO = cast(TextIO, StreamToQueue(self.out_q))
            with redirect_stdout(stream_io), redirect_stderr(stream_io):
                assert self.loop is not None
                self.loop.run()

        except Exception as exc:
            self.out_q.put(f"\n[GUI] Loop stopped: {type(exc).__name__}: {exc}\n")


class AskWorker(threading.Thread):
    def __init__(self, out_q: "queue.Queue[str]", params: dict):
        super().__init__(daemon=True)
        self.out_q = out_q
        self.params = params

    def _print(self, s: str) -> None:
        self.out_q.put(s + "\n")

    def _ask_openai_direct(self, question: str, model: str, max_words: int, timeout_s: int) -> None:
        """Minimal, robust call to OpenAI Responses API (GPT-5 family)."""
        import io
        import json
        import urllib.request, urllib.error
        from typing import IO

        key = (self.params.get("openai_api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
        if not key:
            self._print("[GUI] OpenAI: no API key configured.")
            return

        body = {
            "model": model,
            "instructions": "You are a concise market assistant. Keep answers tight.",
            "input": question,
            "max_output_tokens": max(128, min(8192, int(max_words * 4))),
        }

        # ---- Build request payload (type-checker happy) ----
        payload_bytes: bytes = json.dumps(body).encode("utf-8")
        from typing import cast
        reader = cast(IO[bytes], io.BytesIO(payload_bytes))

        req = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=reader,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                d = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            self._print(f"[GUI] OpenAI HTTP {e.code}: {detail.strip() or e.reason}")
            return
        except urllib.error.URLError as e:
            self._print(f"[GUI] OpenAI network error: {getattr(e, 'reason', e)}")
            return
        except Exception as e:
            self._print(f"[GUI] OpenAI request error: {type(e).__name__}: {e}")
            return

        # ---- Extract text robustly ----
        text = (d.get("output_text") or "").strip()
        if not text:
            parts: list[str] = []
            for block in (d.get("output") or []):
                for c in (block.get("content") or []):
                    if isinstance(c, dict) and c.get("type") in {"output_text", "summary_text", "refusal"}:
                        t = (c.get("text") or "").strip()
                        if t:
                            parts.append(t)
            if parts:
                text = " ".join(parts).strip()
        if not text:
            for m in (d.get("messages") or []):
                if m.get("role") == "assistant":
                    cont = m.get("content")
                    if isinstance(cont, str) and cont.strip():
                        text = cont.strip()
                        break
                    if isinstance(cont, list):
                        buf = [str(x.get("text")) for x in cont if isinstance(x, dict) and x.get("text")]
                        if buf:
                            text = " ".join(buf).strip()
                            break
            if not text:
                choices = d.get("choices")
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message", {})
                    if isinstance(msg, dict):
                        cont = msg.get("content")
                        if isinstance(cont, str) and cont.strip():
                            text = cont.strip()

        self._print(text or "(no answer)")

    def run(self):
        import os, inspect

        # Env passthroughs
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
            setattr(core, "NEWSAPI_KEY", self.params["news_api_key"])

        poly_key = (self.params.get("polygon_api_key") or os.getenv("POLYGON_API_KEY") or "").strip()
        if poly_key:
            os.environ["POLYGON_API_KEY"] = poly_key

        openai_key = (self.params.get("openai_api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
        if openai_key:
            os.environ["OPENAI_API_KEY"] = openai_key

        # Inputs
        model_str = (self.params.get("ollama_model") or "gpt-5-nano").strip()
        is_gpt5 = model_str.lower().startswith("gpt-5")
        timeout_s = int(self.params.get("ollama_timeout", 45))
        max_words = int(self.params.get("qa_max_words", 120))
        history = int(self.params.get("history", 180))
        symbols = self.params.get("symbols", "").split()
        focus = self.params.get("focus", "SPY")
        question = self.params.get("question") or "Current market trend?"

        base_url = "https://api.openai.com/v1" if is_gpt5 else os.getenv("OLLAMA_URL", "http://localhost:11434")
        self._print(f"[GUI] Q&A → provider={'OpenAI' if is_gpt5 else 'Ollama'} "
                    f"ui_model={model_str} api_model={model_str} url={base_url} key={'yes' if (openai_key if is_gpt5 else True) else 'no'}")

        # Require OpenAI key for GPT-5
        if is_gpt5 and not openai_key:
            self._print("[GUI] OpenAI: no API key configured. Set one in Settings.")
            return

        # Provider (Polygon preferred; fall back to Schwab if core lacks Polygon)
        provider_ctor = getattr(core, "PolygonQuoteProvider", None) or getattr(core, "SchwabQuoteProvider", None)
        provider = provider_ctor() if provider_ctor else None

        used_core = False
        if provider and hasattr(core, "qa_once"):
            try:
                qa_sig = inspect.signature(core.qa_once)
                kwargs = {
                    "provider": provider,
                    "user_symbols": symbols,
                    "focus": focus,
                    "timeout": timeout_s,
                    "max_words": max_words,
                    "history": history,
                    "question": question,
                }
                # Handle model/url vs ollama_model/ollama_url differences
                if "model" in qa_sig.parameters:
                    kwargs["model"] = model_str
                elif "ollama_model" in qa_sig.parameters:
                    kwargs["ollama_model"] = model_str
                if is_gpt5:
                    if "url" in qa_sig.parameters:
                        kwargs["url"] = "https://api.openai.com/v1"
                else:
                    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
                    if "url" in qa_sig.parameters:
                        kwargs["url"] = ollama_url
                    if "ollama_url" in qa_sig.parameters:
                        kwargs["ollama_url"] = ollama_url

                allowed = {k: v for k, v in kwargs.items() if k in qa_sig.parameters}
                core.qa_once(**allowed)
                used_core = True
            except Exception as e:
                self._print(f"(Q&A via core failed: {type(e).__name__}: {e})")

        # If core path didn’t produce an answer or failed, do a direct OpenAI call (for GPT-5)
        if is_gpt5 and not used_core:
            self._ask_openai_direct(question, model_str, max_words, timeout_s)




# --------------------- THEME ---------------------
COLORS = {
    "bg": "#0f1216",
    "panel": "#141922",
    "panel2": "#121720",
    "sunken": "#0c1016",
    "border": "#1e2630",
    "text": "#dfe5ee",
    "muted": "#9aa9bf",
    "teal": "#20c997",
    "green": "#18d18b",
    "blue": "#4ea1ff",
    "amber": "#f7b955",
    "red": "#ff6363",
}

FONT_MONO = ("Consolas", 10)
FONT_UI = ("Segoe UI", 10)
FONT_HUGE = ("Segoe UI", 24, "bold")
FONT_BIG = ("Segoe UI", 15, "bold")
FONT_CAP = ("Segoe UI", 10, "bold")

def apply_dark_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(bg=COLORS["bg"])

    style.configure("TFrame", background=COLORS["panel2"])
    style.configure("TPanedwindow", background=COLORS["bg"], borderwidth=0)
    style.configure("TLabel", background=COLORS["panel2"], foreground=COLORS["text"], font=FONT_UI)
    style.configure("TButton", background=COLORS["panel"], foreground=COLORS["text"], padding=6)
    style.map("TButton", background=[("active", COLORS["sunken"])])
    style.configure("TEntry", fieldbackground=COLORS["sunken"], background=COLORS["sunken"],
                    foreground=COLORS["text"], insertcolor=COLORS["text"], bordercolor=COLORS["border"])
    style.configure("Card.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("Toolbar.TFrame", background=COLORS["panel"], relief="flat")
    style.configure("Title.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI Semibold", 11))
    style.configure("Section.TLabel", font=("Segoe UI", 11, "bold"))
    style.configure("Muted.TLabel", foreground=COLORS["muted"], font=FONT_UI)

    style.configure(
        "Quotes.Treeview",
        background=COLORS["sunken"],
        fieldbackground=COLORS["sunken"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        rowheight=22,
        font=("Consolas", 10),
    )
    style.configure("Quotes.Treeview.Heading", background=COLORS["panel"], foreground=COLORS["muted"], font=("Segoe UI", 10, "bold"))
    return style

# --------------------- UI ---------------------
class App(tk.Tk):

    def _ensure_headlines(self, k: int = 25) -> None:
        if not self.params.get("news", False):
            return
        symbols = self.params.get("symbols", DEFAULTS["symbols"]).split()
        keywords = map_symbols_to_keywords(symbols)
        since_h = int(self.params.get("news_since_h", 6))
        items = self.news_fetcher.get(keywords, since_hours=since_h, limit=int(k)) or []
        if items:
            self._news_rows = items
        bias = news_bias_score(self._news_rows) if self._news_rows else 0.0
        self._after_news_update(self._news_rows, bias)

    status: ttk.Label
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(1080, 680)
        self.style = apply_dark_theme(self)

        # Load settings
        self.params = self._load_settings()

        # Expose keys so news_client/core can read them at call-time
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
        if self.params.get("polygon_api_key"):
            os.environ["POLYGON_API_KEY"] = self.params["polygon_api_key"]

        # Create the fetcher
        self.news_fetcher = NewsFetcher(ttl_seconds=int(os.getenv("NEWS_TTL", "300")))
        self._news_timer_id = None
        self._news_rows: List[dict] = []

        self.out_q: "queue.Queue[str]" = queue.Queue()
        self.loop_worker: LoopWorker | None = None
        self._awaiting_answer: bool = False

        self._build_toolbar()
        self._build_body()
        self.after(60, self._pump_output, ())

    def _on_restart(self) -> None:
        self._toast("Restarting…")
        with suppress(tk.TclError, RuntimeError):
            self._on_stop()
        try:
            self.after(200, self._exec_restart)
        except tk.TclError as e:
            logging.debug("after() failed; restarting immediately: %r", e)
            self._exec_restart()

    def _exec_restart(self, *_: Any) -> None:
        with suppress(ValueError, AttributeError):
            self._sync_vars_to_params()
        try:
            self._save_settings()
        except (OSError, PermissionError) as e:
            logging.warning("Saving settings failed; continuing with restart: %s", e)
        exe = sys.executable
        args = [exe] + (sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv)
        try:
            os.execv(exe, args)
        except OSError as e:
            logging.warning("execv failed (%s); falling back to spawn.", e)
            try:
                subprocess.Popen(args, close_fds=(os.name != "nt"))
            except (OSError, FileNotFoundError) as e2:
                logging.error("Failed to spawn replacement process: %s", e2)
                with suppress(Exception):
                    messagebox.showerror("Restart failed", f"Couldn't restart:\n{e2}")
                return
            os._exit(0)

    # ---------- Telemetry -> UI ----------
    def _apply_telemetry(self, obj: dict) -> None:
        ta = obj.get("ta") or {}
        vwap = obj.get("vwap") or {}
        focus = obj.get("focus") or self.params.get("focus", "SPY")

        r = ta.get("rsi14")
        if isinstance(r, (int, float)):
            cast(ttk.Label, self.card_rsi["value"]).configure(text=self._fmt2(r))
            state = "Overbought" if r >= 70 else "Oversold" if r <= 30 else "Neutral"
            color_key = "red" if r >= 70 else "green" if r <= 30 else "muted"
            cast(ttk.Label, self.card_rsi["status"]).configure(text=state, foreground=COLORS[color_key])

        m = ta.get("macd")
        if isinstance(m, (int, float)):
            cast(ttk.Label, self.card_macd["value"]).configure(text=("+" if m >= 0 else "") + self._fmt2(m))
            cast(ttk.Label, self.card_macd["status"]).configure(
                text=("Bullish" if m >= 0 else "Bearish"),
                foreground=COLORS["green" if m >= 0 else "red"],
            )

        vw_f = vwap.get(focus) if isinstance(vwap, dict) else None
        if isinstance(vw_f, (int, float)):
            cast(ttk.Label, self.card_vwap["value"]).configure(text=self._fmt2(vw_f))

        prices = obj.get("prices") or {}
        if isinstance(prices, dict):
            for sym, val in prices.items():
                try:
                    p = float(val)
                except (TypeError, ValueError):
                    continue
                if sym in self.ticker_cards:
                    cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

        news = obj.get("news")
        if isinstance(news, dict):
            cnt = _coerce_int(news.get("count", 0), 0)
            bias_val = _coerce_float(news.get("bias"))
            if bias_val is not None:
                self.status.configure(text=f"News: {cnt} items, bias {bias_val:+.2f}")
                self.after(2500, self._set_ready, ())
            items = news.get("items") or []
            if isinstance(items, list):
                self._update_news_items(items)

    def _append_chat(self, role: str, msg: str) -> None:
        stamp = time.strftime("[%H:%M:%S] ")
        self.prints_text.insert("end", stamp, ("time",))
        self.prints_text.insert("end", f"{role}> {msg.strip()}\n")
        self.prints_text.see("end")

    # ---------- SETTINGS ----------
    @staticmethod
    def _fmt2(x: float | int | str | None) -> str:
        if x is None:
            return "—"
        try:
            d = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError, TypeError):
            return "—"
        return f"{d:.2f}"

    @staticmethod
    def _load_settings() -> dict[str, Any]:
        data = DEFAULTS.copy()
        try:
            if CONF_PATH.exists():
                with open(CONF_PATH, "r", encoding="utf-8") as f:
                    data.update(json.load(f))
        except Exception as exc:
            messagebox.showwarning("Settings", f"Could not load settings: {exc}")
        return data

    def _save_settings(self) -> None:
        try:
            CONF_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONF_PATH, "w", encoding="utf-8") as f:
                json.dump(self.params, f, indent=2)
            self._toast("Settings saved")
        except Exception as exc:
            messagebox.showerror("Settings", f"Could not save: {exc}")

    def _open_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=COLORS["panel2"])
        win.transient(self)
        win.grab_set()

        frm = ttk.Frame(win, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        def add_row(r, label, widget):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky=tk.W, padx=(0, 8), pady=4)
            widget.grid(row=r, column=1, sticky=tk.EW, pady=4)

        # Tk variables
        self.var_polygon_key = tk.StringVar(value=self.params.get("polygon_api_key", ""))

        self.var_symbols = tk.StringVar(value=self.params.get("symbols"))
        self.var_focus = tk.StringVar(value=self.params.get("focus"))
        self.var_interval = tk.IntVar(value=int(self.params.get("interval", 20)))
        self.var_history = tk.IntVar(value=int(self.params.get("history", 180)))
        self.var_window = tk.IntVar(value=int(self.params.get("window", 5)))
        self.var_min_print = tk.DoubleVar(value=float(self.params.get("min_print", 0.001)))
        self.var_always_print = tk.BooleanVar(value=bool(self.params.get("always_print", False)))
        self.var_vwap_buf = tk.DoubleVar(value=float(self.params.get("vwap_buf", 0.10)))
        self.var_gate_cons = tk.IntVar(value=int(self.params.get("gate_consecutive", 2)))
        self.var_print_gates = tk.BooleanVar(value=bool(self.params.get("print_gates", True)))
        self.var_emit_json = tk.BooleanVar(value=bool(self.params.get("emit_json", False)))
        self.var_csv = tk.StringVar(value=self.params.get("csv_path", ""))

        # GPT-5 model + API key + timeout
        self.var_model = tk.StringVar(value=self.params.get("ollama_model", DEFAULTS["ollama_model"]))
        self.var_openai_key = tk.StringVar(value=self.params.get("openai_api_key", ""))
        self.var_model_timeout = tk.IntVar(value=int(self.params.get("ollama_timeout", 45)))

        self.var_qa_words = tk.IntVar(value=int(self.params.get("qa_max_words", 120)))
        self.var_question = tk.StringVar(value=self.params.get("question", DEFAULTS["question"]))

        # NEWS vars
        self.var_news = tk.BooleanVar(value=bool(self.params.get("news", False)))
        self.var_news_since_h = tk.IntVar(value=int(self.params.get("news_since_h", 6)))
        self.var_news_limit = tk.IntVar(value=int(self.params.get("news_limit", 12)))
        self.var_news_weight = tk.DoubleVar(value=float(self.params.get("news_weight", 0.30)))
        self.var_print_news = tk.BooleanVar(value=bool(self.params.get("print_news", False)))
        self.var_news_api_key = tk.StringVar(value=self.params.get("news_api_key", ""))

        # entries
        add_row(0, "Polygon API Key", ttk.Entry(frm, textvariable=self.var_polygon_key, show="•"))
        add_row(1, "Symbols", ttk.Entry(frm, textvariable=self.var_symbols))
        add_row(2, "Focus", ttk.Entry(frm, textvariable=self.var_focus))

        def spin(var, a, b, step=1, width=8):
            sb_cls = getattr(ttk, "Spinbox", None) or tk.Spinbox
            return sb_cls(frm, from_=a, to=b, increment=step, textvariable=var, width=width)

        add_row(3, "Interval (s)", spin(self.var_interval, 1, 300))
        add_row(4, "History pts", spin(self.var_history, 60, 10000, step=10))
        add_row(5, "Delta window", spin(self.var_window, 1, 50))
        add_row(6, "Min print Δ", ttk.Entry(frm, textvariable=self.var_min_print))
        add_row(7, "VWAP buffer", ttk.Entry(frm, textvariable=self.var_vwap_buf))
        add_row(8, "Gate consecutive", spin(self.var_gate_cons, 1, 10))

        flags = ttk.Frame(frm)
        ttk.Checkbutton(flags, text="Always print", variable=self.var_always_print).pack(side="left")
        ttk.Checkbutton(flags, text="Print gates", variable=self.var_print_gates).pack(side="left", padx=12)
        ttk.Checkbutton(flags, text="Emit JSON", variable=self.var_emit_json).pack(side="left", padx=12)
        add_row(9, "Flags", flags)

        csv_row = ttk.Frame(frm)
        ttk.Entry(csv_row, textvariable=self.var_csv).pack(side="left", fill="x", expand=True)
        ttk.Button(
            csv_row,
            text="Browse",
            command=lambda: self._pick_path(self.var_csv, save=True, default_extension=".csv")
        ).pack(side="left", padx=6)
        add_row(10, "CSV path", csv_row)

        # GPT-5 model & key
        add_row(11, "GPT-5 model", ttk.Entry(frm, textvariable=self.var_model))
        add_row(12, "OpenAI API key", ttk.Entry(frm, textvariable=self.var_openai_key, show="•"))
        add_row(13, "Model timeout (s)", spin(self.var_model_timeout, 5, 120))

        add_row(14, "Q&A max words", spin(self.var_qa_words, 30, 400))
        add_row(15, "Default question", ttk.Entry(frm, textvariable=self.var_question))

        # --- NEWS controls ---
        news_flags = ttk.Frame(frm)
        ttk.Checkbutton(news_flags, text="Enable news", variable=self.var_news).pack(side="left")
        ttk.Checkbutton(news_flags, text="Print news line", variable=self.var_print_news).pack(side="left", padx=12)
        add_row(16, "News", news_flags)

        def spin_news(var, a, b, step=1):
            sb_cls = getattr(ttk, "Spinbox", None) or tk.Spinbox
            return sb_cls(frm, from_=a, to=b, increment=step, textvariable=var, width=8)

        add_row(17, "News since (h)", spin_news(self.var_news_since_h, 1, 48))
        add_row(18, "News limit", spin_news(self.var_news_limit, 1, 50))
        add_row(19, "News weight", ttk.Entry(frm, textvariable=self.var_news_weight))
        add_row(20, "News API key", ttk.Entry(frm, textvariable=self.var_news_api_key, show="•"))

        buttons = ttk.Frame(frm)
        buttons.grid(row=21, column=0, columnspan=2, sticky=tk.E, pady=(8, 2))

        ttk.Button(buttons, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(
            buttons,
            text="Save",
            command=lambda: (
                self._sync_vars_to_params(),
                self._save_settings(),
                self._apply_settings_changes(),
                win.destroy()
            )
        ).pack(side="right", padx=8)

    def _sync_vars_to_params(self) -> None:
        if not hasattr(self, "var_symbols"):
            return
        self.params.update({
            # Polygon
            "polygon_api_key": self.var_polygon_key.get().strip(),

            "symbols": self.var_symbols.get().strip(),
            "focus": self.var_focus.get().strip() or "SPY",
            "interval": int(self.var_interval.get()),
            "history": int(self.var_history.get()),
            "window": int(self.var_window.get()),
            "min_print": float(self.var_min_print.get()),
            "always_print": bool(self.var_always_print.get()),
            "vwap_buf": float(self.var_vwap_buf.get()),
            "gate_consecutive": int(self.var_gate_cons.get()),
            "print_gates": bool(self.var_print_gates.get()),
            "emit_json": bool(self.var_emit_json.get()),
            "csv_path": self.var_csv.get().strip(),

            # Model + OpenAI
            "ollama_model": self.var_model.get().strip() or "gpt-5-nano",
            "openai_api_key": self.var_openai_key.get().strip(),
            "ollama_timeout": int(self.var_model_timeout.get()),

            "qa_max_words": int(self.var_qa_words.get()),
            "question": self.var_question.get().strip() or DEFAULTS["question"],

            # news
            "news": bool(self.var_news.get()),
            "news_since_h": int(self.var_news_since_h.get()),
            "news_limit": int(self.var_news_limit.get()),
            "news_weight": float(self.var_news_weight.get()),
            "print_news": bool(self.var_print_news.get()),
            "news_api_key": self.var_news_api_key.get().strip(),
        })

    def _apply_settings_changes(self) -> None:
        if self.params.get("news_api_key"):
            os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
        if self.params.get("openai_api_key"):
            os.environ["OPENAI_API_KEY"] = self.params["openai_api_key"]
        if self.params.get("polygon_api_key"):
            os.environ["POLYGON_API_KEY"] = self.params["polygon_api_key"]

        if self.params.get("news", False):
            self._start_news_loop()
        else:
            self._stop_news_loop()

    @staticmethod
    def _pick_path(var: tk.Variable, save: bool, default_extension: str = ".json") -> None:
        if save:
            path = filedialog.asksaveasfilename(title="Select file", defaultextension=default_extension)
        else:
            path = filedialog.askopenfilename(title="Select file")
        if path:
            var.set(path)

    # ---------- LAYOUT ----------
    def _build_toolbar(self):
        bar = ttk.Frame(self, style="Toolbar.TFrame")
        bar.pack(side="top", fill="x")

        left = ttk.Frame(bar, style="Toolbar.TFrame")
        left.pack(side="left", padx=10, pady=8)
        ttk.Button(left, text="Start", command=self._on_start).pack(side="left")
        ttk.Button(left, text="Stop", command=self._on_stop).pack(side="left", padx=(8, 0))

        right = ttk.Frame(bar, style="Toolbar.TFrame")
        right.pack(side="right", padx=10, pady=8)
        ttk.Button(right, text="Settings", command=self._open_settings).pack(side="right")
        ttk.Button(right, text="Restart", command=self._on_restart).pack(side="right", padx=(8, 0))

    def _build_body(self):
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        status_bar = ttk.Frame(self, style="Toolbar.TFrame")
        status_bar.pack(fill="x", side="bottom")
        self.status = ttk.Label(status_bar, text="Ready", style="Title.TLabel")
        self.status.pack(side="left", padx=12, pady=8)

        paned = ttk.Panedwindow(body, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # LEFT column
        left = ttk.Frame(paned)
        paned.add(left, weight=1)

        self._section(left, "PRINTS")
        self.prints_text = tk.Text(
            left, height=18, wrap="word",
            bg=COLORS["sunken"], fg=COLORS["text"],
            relief="flat", insertbackground=COLORS["text"]
        )
        self.prints_text.configure(font=FONT_MONO)
        self.prints_text.pack(fill="both", expand=True, padx=12, pady=(6, 10))
        self._init_text_tags(self.prints_text)

        ask_row = ttk.Frame(left)
        ask_row.pack(fill="x", padx=12, pady=(6, 12))
        self.ask_entry = ttk.Entry(ask_row)
        self.ask_entry.insert(0, self.params.get("question", DEFAULTS["question"]))
        self.ask_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(ask_row, text="Ask", command=self._on_ask).pack(side="left", padx=8)
        self.ask_entry.bind("<Return>", lambda _e: self._on_ask())

        # RIGHT column
        right = ttk.Frame(paned)
        paned.add(right, weight=2)

        self._section(right, "INDICATORS")
        cards = ttk.Frame(right)
        cards.pack(fill="x", padx=12)

        self.card_rsi = self._indicator_card(cards, "RSI", "—", "Neutral", "amber")
        self.card_macd = self._indicator_card(cards, "MACD", "—", "—", "teal")
        self.card_vwap = self._indicator_card(cards, "VWAP", "—", "—", "blue")
        self.card_bb = self._indicator_card(cards, "BB 'B", "—", "—", "amber")

        row2 = ttk.Frame(right)
        row2.pack(fill="x", padx=12, pady=(8, 0))
        ttk.Label(row2, text="TICKERS    ").pack(side="left")
        self.live_dot = self._dot(row2, COLORS["green"])
        ttk.Label(row2, text="LIVE", style="Muted.TLabel").pack(side="left", padx=(6, 0))

        self.ticker_bar = ttk.Frame(right)
        self.ticker_bar.pack(fill="x", padx=12, pady=6)
        self.ticker_cards: dict[str, dict[str, tk.Widget]] = {}
        for sym in self.params.get("symbols", DEFAULTS["symbols"]).split()[:5]:
            self._add_ticker_card(sym)

        # NEWS panel
        hdr = ttk.Frame(right)
        hdr.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(hdr, text="NEWS", style="Section.TLabel").pack(side="left")

        self.news_updated_lbl = ttk.Label(hdr, text="", style="Muted.TLabel")
        self.news_updated_lbl.pack(side="left", padx=(10, 0))

        ttk.Button(
            hdr,
            text="Refresh",
            command=lambda: self._ensure_headlines(k=int(self.params.get("news_limit", 25)))
        ).pack(side="right")

        self.news_tree = ttk.Treeview(
            right,
            columns=("when", "src", "title", "sent"),
            show="headings",
            style="Quotes.Treeview",
        )
        self.news_tree.heading("when", text="WHEN")
        self.news_tree.heading("src", text="SRC")
        self.news_tree.heading("title", text="TITLE")
        self.news_tree.heading("sent", text="SENT")

        self.news_tree.column("when", width=110, anchor="w")
        self.news_tree.column("src", width=100, anchor="w")
        self.news_tree.column("title", width=540, anchor="w")
        self.news_tree.column("sent", width=70, anchor="e")

        self.news_tree.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self._news_urls: Dict[str, str] = {}

        def _open_selected(_evt=None):
            sel = self.news_tree.selection()
            if not sel:
                return
            iid = sel[0]
            url = self._news_urls.get(iid)
            if url:
                webbrowser.open_new_tab(url)

        self.news_tree.bind("<Double-1>", _open_selected)

        if self.params.get("news", False):
            if self.params.get("news_api_key"):
                os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
            self._start_news_loop()
            self.status.configure(text="News on — double-click a headline to open")
            self.after(2500, self._set_ready, ())

    @staticmethod
    def _section(parent: tk.Widget, title: str) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(bar, text=title, style="Section.TLabel").pack(side="left")

    @staticmethod
    def _indicator_card(parent: tk.Widget, title: str, value: str, status: str, accent_key: str) -> dict[str, tk.Widget]:
        card = ttk.Frame(parent, style="Card.TFrame")
        card.pack(side="left", padx=(0, 10), pady=0, ipadx=10, ipady=10)
        ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="w")
        val = ttk.Label(card, text=value, font=FONT_HUGE)
        val.pack(anchor="w")
        stat = ttk.Label(card, text=status, font=FONT_UI)
        stat.pack(anchor="w")
        c = tk.Canvas(card, width=120, height=2, highlightthickness=0, bg=COLORS["panel"])
        c.pack(fill="x", pady=(8, 0))
        c.create_rectangle(0, 0, 120, 2, fill=COLORS[accent_key], width=0)
        return {"frame": card, "value": val, "status": stat}

    def _add_ticker_card(self, sym: str):
        frame = ttk.Frame(self.ticker_bar, style="Card.TFrame")
        frame.pack(side="left", padx=(0,10), ipadx=10, ipady=8)
        top = ttk.Frame(frame, style="Card.TFrame")
        top.pack(fill="x")
        self._dot(top, COLORS["red"])
        ttk.Label(top, text=f"  {sym}", font=FONT_BIG).pack(side="left")
        last = ttk.Label(frame, text="—", font=("Consolas", 14))
        last.pack(anchor="w")
        vol = ttk.Label(frame, text="• 0", style="Muted.TLabel")
        vol.pack(anchor="w")
        self.ticker_cards[sym] = {"frame": frame, "last": last, "vol": vol}

    @staticmethod
    def _dot(parent: tk.Widget, color: str) -> tk.Canvas:
        c = tk.Canvas(parent, width=10, height=10, bg=COLORS["panel"], highlightthickness=0)
        c.pack(side="left")
        c.create_oval(2, 2, 8, 8, fill=color, outline=color)
        return c

    @staticmethod
    def _init_text_tags(t: tk.Text) -> None:
        t.tag_configure("time", foreground=COLORS["blue"])
        t.tag_configure("good", foreground=COLORS["green"])
        t.tag_configure("warn", foreground=COLORS["amber"])
        t.tag_configure("bad", foreground=COLORS["red"])
        t.tag_configure("muted", foreground=COLORS["muted"])

    def _set_ready(self, _args: tuple | None = None) -> None:
        self.status.configure(text="Ready")

    # ---------- UX helpers ----------
    def _toast(self, msg: str) -> None:
        self.status.configure(text=msg)
        self.after(2500, self._set_ready, ())

    # ---------- Start/Stop/Ask ----------
    def _on_start(self):
        if self.loop_worker and self.loop_worker.is_alive():
            messagebox.showinfo("Running", "Loop already running.")
            return

        self._sync_vars_to_params()

        # Keys → env
        if self.params.get("openai_api_key"):
            os.environ["OPENAI_API_KEY"] = self.params["openai_api_key"]
        if self.params.get("polygon_api_key"):
            os.environ["POLYGON_API_KEY"] = self.params["polygon_api_key"]

        # Drain any leftover output and clear the prints panel
        with suppress(queue.Empty):
            while True:
                self.out_q.get_nowait()
        self.prints_text.delete("1.0", tk.END)

        # Start background loop
        self.loop_worker = LoopWorker(self.out_q, self.params.copy())
        self.loop_worker.start()

        # News loop
        if self.params.get("news"):
            if self.params.get("news_api_key"):
                os.environ["NEWSAPI_KEY"] = self.params["news_api_key"]
            self._start_news_loop()
        else:
            self._stop_news_loop()

        self._toast("Loop started")

    def _on_stop(self):
        if self.loop_worker:
            self.loop_worker.stop()
            for _ in range(40):
                if not self.loop_worker.is_alive():
                    break
                time.sleep(0.1)
        self._toast("Loop stopped")
        self._stop_news_loop()

    def _on_ask(self):
        q = (self.ask_entry.get().strip()
             or self.params.get("question", DEFAULTS["question"]))
        self.ask_entry.delete(0, tk.END)

        self._append_chat("Q", q)
        self._awaiting_answer = True

        p = self.params.copy()
        p["question"] = q

        lower_q = q.lower()
        if self.params.get("news", False) and ("news" in lower_q or "headline" in lower_q):
            self._ensure_headlines(k=25)
            ctx = self._news_context_for_ask(k=25)
            words = int(self.params.get("qa_max_words", 120))
            p["question"] = (
                f"{q}\n\nUse ONLY the following headlines (newest first). "
                f"Summarize in {words} words or fewer with 3–6 bullets and a 1-line bias.\n{ctx}"
            )

        worker = AskWorker(self.out_q, p)
        worker.start()
        self._toast("Asked")

    def _pump_output(self, _args: tuple | None = None) -> None:
        try:
            while True:
                chunk = self.out_q.get_nowait()
                for raw in chunk.splitlines():
                    line = raw.rstrip("\n")
                    s = line.strip()
                    if not s:
                        continue

                    # suppress debuggy provider lines if any
                    if s.startswith("[polygon]") or s.startswith("Requesting:"):
                        continue
                    if s == "DEBUG" or s.startswith("DEBUG "):
                        continue

                    # discard Python repr of symbol lists like "['SPY','QQQ']"
                    if s.startswith("[") and s.endswith("]"):
                        parts = [p.strip() for p in s[1:-1].split(",")]
                        if parts and all(re.fullmatch(r"'[A-Z]{1,5}'", p) for p in parts):
                            continue

                    if s.startswith("Prices:"):
                        self._parse_for_prices(s)
                        continue
                    # inside _pump_output loop, right after existing filters like DEBUG etc.
                    if "invalid_client: Unauthorized" in s:
                        continue
                    if s.startswith("(seed warning)") and "invalid_client" in s:
                        continue

                    if s.startswith("{") and s.endswith("}"):
                        try:
                            obj = json.loads(s)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            self._apply_telemetry(obj)
                        continue

                    if self._awaiting_answer:
                        self._append_chat("A", s)
                        self._awaiting_answer = False
                        self._parse_for_indicators(s)
                        self._parse_for_prices(s)
                        continue

                    self._append_prints(line + "\n")
                    self._parse_for_indicators(line)
                    self._parse_for_prices(line)

        except queue.Empty:
            pass
        self.after(60, self._pump_output, ())

    import re
    TIME_PAT: re.Pattern[str] = re.compile(r"(\[\d{2}:\d{2}:\d{2}])")
    def _append_prints(self, text: str):
        t = self.prints_text
        pos = 0
        for m in self.TIME_PAT.finditer(text):
            a, b = m.span()
            if a > pos:
                t.insert("end", text[pos:a])
            t.insert("end", m.group(1), ("time",))
            pos = b
        if pos < len(text):
            rest = text[pos:]
            for word, tag in (("OVERBOUGHT", "bad"), ("OVERSOLD", "good"), ("Gate:", "muted")):
                if word in rest:
                    parts = rest.split(word)
                    for i, p in enumerate(parts):
                        t.insert("end", p)
                        if i < len(parts) - 1:
                            t.insert("end", word, (tag,))
                    rest = ""
                    break
            if rest:
                t.insert("end", rest)
        t.see("end")

    def _parse_for_indicators(self, text: str) -> None:
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("{") and s.endswith("}"):
                try:
                    obj = json.loads(s)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    ta = obj.get("ta") or obj
                    rsi = ta.get("rsi") or ta.get("RSI") or ta.get("rsi14")
                    macd = ta.get("macd") or ta.get("MACD")
                    vwap = (obj.get("vwap") or ta.get("vwap") or ta.get("VWAP"))
                    if rsi is not None:
                        try:
                            r = float(rsi)
                            cast(ttk.Label, self.card_rsi["value"]).configure(text=self._fmt2(r))
                            state = "Overbought" if r >= 70 else "Oversold" if r <= 30 else "Neutral"
                            color_key = "red" if r >= 70 else "green" if r <= 30 else "muted"
                            cast(ttk.Label, self.card_rsi["status"]).configure(text=state, foreground=COLORS[color_key])
                        except (TypeError, ValueError):
                            pass
                    if macd is not None:
                        try:
                            m = float(macd)
                            cast(ttk.Label, self.card_macd["value"]).configure(text=("+" if m >= 0 else "") + self._fmt2(m))
                            cast(ttk.Label, self.card_macd["status"]).configure(
                                text=("Bullish" if m >= 0 else "Bearish"),
                                foreground=COLORS["green" if m >= 0 else "red"],
                            )
                        except (TypeError, ValueError):
                            pass
                    if vwap is not None:
                        try:
                            v = float(vwap if isinstance(vwap, (int, float, str)) else vwap.get(obj.get("focus", ""), None))
                            cast(ttk.Label, self.card_vwap["value"]).configure(text=self._fmt2(v))
                        except (TypeError, ValueError, AttributeError):
                            pass

        if "above vwap" in text.lower():
            cast(ttk.Label, self.card_vwap["status"]).configure(text="Above", foreground=COLORS["blue"])
        elif "below vwap" in text.lower():
            cast(ttk.Label, self.card_vwap["status"]).configure(text="Below", foreground=COLORS["amber"])

        if "upper band" in text.lower() or ("upper" in text and "band" in text):
            cast(ttk.Label, self.card_bb["status"]).configure(text="Near upper band", foreground=COLORS["amber"])

    def _parse_for_prices(self, text: str) -> None:
        for sym, price in re.findall(r"[\"']([A-Z/]{2,6})[\"']\s*:\s*([0-9]+(?:\.[0-9]+)?)", text):
            try:
                p = float(price)
            except ValueError:
                continue
            if sym in self.ticker_cards:
                cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

        sym_re = r"([A-Z]{2,5}(?:/[A-Z]{2,4})?)\s*[:=]?\s*(\d{1,5}(?:[.,]\d{1,2})?)"
        for sym, price in re.findall(sym_re, text):
            try:
                p = float(price.replace(",", "."))  # tolerate 123,45
            except ValueError:
                continue
            if sym in self.ticker_cards:
                cast(ttk.Label, self.ticker_cards[sym]["last"]).configure(text=self._fmt2(p))

    # ---------- NEWS helpers ----------
    def _start_news_loop(self) -> None:
        self._stop_news_loop()
        self._refresh_news_async()

    def _stop_news_loop(self) -> None:
        timer_id = getattr(self, "_news_timer_id", None)
        if timer_id:
            try:
                self.after_cancel(timer_id)
            except tk.TclError:
                pass
            finally:
                self._news_timer_id = None

    def _refresh_news_async(self) -> None:
        if not self.params.get("news", False):
            return

        symbols = self.params.get("symbols", DEFAULTS["symbols"]).split()
        keywords = map_symbols_to_keywords(symbols)
        since_h = int(self.params.get("news_since_h", 6))
        limit = int(self.params.get("news_limit", 12))

        def _worker() -> None:
            try:
                items = self.news_fetcher.get(keywords, since_hours=since_h, limit=limit) or []
                self._news_rows = items
                bias = news_bias_score(items) if items else 0.0

                def _cb(*_ignored: object) -> None:
                    self._after_news_update(items, bias)
                    return None

                try:
                    self.after(0, _cb)
                except tk.TclError:
                    pass
            except Exception as e:
                logging.warning("News fetch failed: %s", e)

        threading.Thread(target=_worker, daemon=True).start()

    def _after_news_update(self, items: List[dict], bias: float) -> None:
        self._update_news_items(items)
        cur_rows = self._news_rows if getattr(self, "_news_rows", []) else (items or [])
        cnt = len(cur_rows)
        try:
            bias_val = float(bias)
        except (TypeError, ValueError):
            bias_val = 0.0

        self.status.configure(text=f"News: {cnt} items, bias {bias_val:+.2f}")
        if cnt and bool(self.params.get("print_news", False)):
            line = (compress_bullets(cur_rows, k=1) or ["(news updated)"])[0]
            self._append_prints(f"[{datetime.now().strftime('%H:%M:%S')}] News> {line}\n")

        if hasattr(self, "news_updated_lbl"):
            self.news_updated_lbl.configure(text=f"(updated {datetime.now().strftime('%H:%M')})")

        self.after(2500, self._set_ready, ())

        refresh_every_s = max(90, int(self.params.get("interval", 20)) * 6)
        tid = getattr(self, "_news_timer_id", None)
        if tid:
            with suppress(tk.TclError):
                self.after_cancel(tid)

        def _cb(*_ignored: object) -> None:
            self._refresh_news_async()
            return None

        self._news_timer_id = self.after(refresh_every_s * 1000, _cb)

    def _news_context_for_ask(self, k: int = 12) -> str:
        """Condense current headlines for GPT-5 when user asks about news."""
        items = self._news_rows[: max(1, k)]
        if not items:
            return "(no headlines loaded)"
        bullets = compress_bullets(items, k=len(items))
        bias = news_bias_score(items)
        tilt = "bearish" if bias < -0.25 else "bullish" if bias > 0.25 else "mixed"
        return f"(headline bias: {bias:+.2f} → {tilt})\n" + "\n".join(bullets)

    def _update_news_items(self, items: List[dict[str, object]]) -> None:
        if not items:
            return

        def _ts_key(n: dict) -> float:
            ts = n.get("published") or n.get("time") or n.get("ts")
            if isinstance(ts, (int, float)):
                try:
                    return float(ts)
                except (TypeError, ValueError):
                    return 0.0
            if isinstance(ts, str):
                s = ts.strip()
                try:
                    if s.endswith("Z"):
                        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(s)
                    return dt.replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, OSError, TypeError):
                    return 0.0
            return 0.0

        items = sorted(items, key=_ts_key, reverse=True)

        for iid in self.news_tree.get_children(""):
            self.news_tree.delete(iid)
        self._news_urls.clear()

        def _fmt_when(ts: object) -> str:
            if ts is None:
                return "—"
            if isinstance(ts, (int, float)):
                try:
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                    return dt.astimezone().strftime("%H:%M")
                except (ValueError, OSError, TypeError):
                    return "—"
            if isinstance(ts, str):
                s = ts.strip()
                try:
                    if s.endswith("Z"):
                        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(s)
                    return dt.astimezone().strftime("%H:%M")
                except (ValueError, OSError, TypeError):
                    return s[:5] if len(s) >= 5 else (s or "—")
            return "—"

        def _fmt_sent(x: Any) -> str:
            if x is None:
                return "—"
            if isinstance(x, (int, float)):
                return f"{float(x):+.2f}"
            if isinstance(x, str):
                s = x.strip().replace("%", "")
                try:
                    return f"{float(s):+.2f}"
                except (ValueError, TypeError):
                    return s if s else "—"
            return "—"

        for it in items:
            title = str(it.get("title") or "—")
            src = str(it.get("source") or it.get("src") or "—")
            url = str(it.get("url") or "")
            when = _fmt_when(it.get("published") or it.get("time") or it.get("ts"))
            sent = _fmt_sent(it.get("sentiment") or it.get("bias"))

            iid = self.news_tree.insert(
                "", "end",
                values=(str(when), str(src), str(title), str(sent))
            )
            if url:
                self._news_urls[iid] = url

    # ---------- teardown ----------
    def destroy(self) -> None:
        with suppress(Exception):
            self._save_settings()
        if self.loop_worker and self.loop_worker.is_alive():
            with suppress(Exception):
                self.loop_worker.stop()
        super().destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
