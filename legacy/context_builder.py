# =========================
# File: context_builder.py
# =========================
# Purpose: Build compact historical/intraday context snapshots per symbol for Ollama_voice
# - Daily OHLCV (up to ~3y) → MA(20/50/200), RSI14, drawdown, 52w distances, returns, vol percentile
# - Intraday (today 1m) → true VWAP (typical price), BB position on last N closes
# - Returns a *small JSON-able dict*; no huge arrays passed to LLM
#
# Dependencies: pandas, numpy (already common in your project)
# Data source: Schwab client (same one in SchwabQuoteProvider)
#
# Usage from QuantEcho_core.py (inside _snapshot_payload):
#     from context_builder import build_context, compute_bias_score
#     ctx = build_context(self.provider.client, self.brain.focus)
#     payload["context"] = ctx
#     payload["bias_code"] = {"score": round(compute_bias_score(ctx), 2)}
#     # Also include in telemetry you pass to ollama_bias()
#
from __future__ import annotations
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pandas.errors import OutOfBoundsDatetime
import numpy as np
import pandas as pd
from pandas.api.types import is_integer_dtype, is_float_dtype, is_datetime64_any_dtype

def _pick_ts_col(df: pd.DataFrame) -> Optional[pd.Series]:
    """Return the first timestamp-like column Series, or None."""
    for col in ("datetime", "time", "timestamp"):
        if col in df.columns:
            return df[col]
    return None

def _to_utc_datetime(series: pd.Series) -> pd.Series:
    """Coerce various timestamp representations to UTC-aware datetimes."""
    if is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce", utc=True)

    if is_integer_dtype(series) or is_float_dtype(series):
        s = pd.to_numeric(series, errors="coerce")
        # Heuristic: seconds ~1e9..1e10, millis ~1e12..1e13
        med = np.nanmedian(s.values.astype("float64"))
        unit = "ms" if med >= 1e11 else "s"
        return pd.to_datetime(s, unit=unit, errors="coerce", utc=True)

    # strings or mixed
    return pd.to_datetime(series, errors="coerce", utc=True)


# --- Helpers to tolerate Schwab enum names across versions ---
def _enum(price_history_ns, *names):
    if price_history_ns is None:
        return None
    for n in names:
        for cand in (n, n.upper(), n.lower(), n.capitalize()):
            if hasattr(price_history_ns, cand):
                return getattr(price_history_ns, cand)
    return None

# --- Technicals ---
def rsi(series: pd.Series, n: int = 14) -> float | None:
    if len(series) < n + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(n).mean()
    avg_loss = loss.rolling(n).mean()
    rs = avg_gain / avg_loss
    val = 100 - (100 / (1 + rs))
    out = float(val.iloc[-1]) if not math.isnan(val.iloc[-1]) else None
    if out is not None and not math.isfinite(out):
        return None
    return out

def bollinger_pos(series: pd.Series, n: int = 20, k: float = 2.0) -> float | None:
    if len(series) < n:
        return None
    window = series.tail(n)
    mid = window.mean()
    sd = window.std(ddof=0)
    if sd == 0:
        return 0.0
    last = series.iloc[-1]
    # position scaled -1..+1
    pos = (last - mid) / (k * sd)
    return float(max(-1.0, min(1.0, pos)))

def drawdown_min(series: pd.Series) -> float | None:
    if series.empty:
        return None
    roll_max = series.cummax()
    dd = series / roll_max - 1.0
    return float(dd.min())

# Annualized vol percentile over ~1y based on daily returns std
def vol_percentile(series: pd.Series, lookback_days: int = 252) -> float | None:
    if series is None or series.size < 60:
        return None

    lb = min(series.size, lookback_days)
    s = series.tail(lb).astype(float)

    ret = s.pct_change()
    ret = ret.replace([np.inf, -np.inf], np.nan).dropna()
    if ret.empty:
        return None

    vol = ret.std(ddof=0) * np.sqrt(252)
    if not np.isfinite(vol):
        return None

    rolling = ret.rolling(20).std(ddof=0) * np.sqrt(252)
    rolling = rolling.dropna()
    if rolling.empty:
        return None

    # Make the type checker happy and keep semantics the same:
    comp: pd.Series = rolling.lt(vol)              # Series[bool]
    pct = float(comp.astype(float).mean())         # fraction of True
    return pct


# True intraday VWAP from candles
def intraday_vwap(candles: pd.DataFrame) -> float | None:
    """
    Compute true intraday VWAP = sum(typical_price * volume) / sum(volume).
    Expects columns: high, low, close, volume.
    Returns a float or None if not computable.
    """
    if candles is None or candles.empty:
        return None

    # Pull required columns safely
    c = candles.get("close")
    v = candles.get("volume")
    h = candles.get("high")
    l = candles.get("low")

    if c is None or v is None or c.empty or v.empty:
        return None

    # Ensure numeric types
    c = pd.to_numeric(c, errors="coerce")
    v = pd.to_numeric(v, errors="coerce")

    # Typical price = (H+L+C)/3 if H/L are valid, else fallback to close
    if h is not None and l is not None and not h.empty and not l.empty:
        h = pd.to_numeric(h, errors="coerce")
        l = pd.to_numeric(l, errors="coerce")
        typical = (h + l + c) / 3.0
    else:
        typical = c

    # Drop any rows where either typical or volume is NaN
    mask = typical.notna() & v.notna()
    typical = typical[mask]
    v = v[mask]

    v_sum = v.replace(0, np.nan).sum()
    if not np.isfinite(v_sum) or v_sum == 0:
        return None

    vwap = (typical * v).sum() / v_sum
    return float(vwap) if np.isfinite(vwap) else None



# Fetch daily price history (~3y) and today 1m candles via Schwab client
@dataclass
class HistData:
    daily: pd.DataFrame  # columns: datetime, open, high, low, close, volume
    intraday: pd.DataFrame  # columns: datetime, open, high, low, close, volume (today)


def _fetch_price_history(client, symbol: str) -> HistData:
    ph = getattr(client, "PriceHistory", None)
    pt_day = _enum(ph.PeriodType if ph else None, "DAY") if ph else None
    p_10y  = _enum(ph.Period      if ph else None, "TEN_YEARS", "YEAR_10") if ph else None
    ft_day = _enum(ph.FrequencyType if ph else None, "DAILY") if ph else None
    f_1d   = _enum(ph.Frequency     if ph else None, "DAILY") if ph else None
    ft_min = _enum(ph.FrequencyType if ph else None, "MINUTE") if ph else None
    f_1m   = _enum(ph.Frequency     if ph else None, "EVERY_MINUTE", "ONE_MINUTE") if ph else None

    # ---------------- Daily (request long range, then slice to ~3y) ----------------
    kwargs_d: dict[str, object] = {}
    if pt_day and p_10y and ft_day and f_1d:
        kwargs_d = dict(period_type=pt_day, period=p_10y, frequency_type=ft_day, frequency=f_1d)

    resp_d = client.get_price_history(symbol, **kwargs_d) if kwargs_d else client.get_price_history(symbol)
    data_d = resp_d.json() if hasattr(resp_d, "json") else resp_d
    candles_d = (data_d.get("candles") if isinstance(data_d, dict) else data_d) or []
    df_d = pd.DataFrame(candles_d)

    if not df_d.empty:
        ts = _pick_ts_col(df_d)  # one of: datetime/time/timestamp
        if ts is not None:
            dt = _to_utc_datetime(ts)
            df_d = df_d.assign(datetime=dt)
            keep = [c for c in ("datetime", "open", "high", "low", "close", "volume") if c in df_d.columns]
            df_d = df_d[keep].dropna(how="any")
            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3 * 365)
            df_d = df_d[df_d["datetime"] >= cutoff]
        else:
            df_d = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    else:
        df_d = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    # ---------------- Intraday (today 1m) ----------------
    kwargs_i: dict[str, object] = {}
    if pt_day and ft_min and f_1m:
        p_1d = _enum(ph.Period if ph else None, "ONE_DAY", "DAY_1") if ph else None
        if p_1d:
            kwargs_i = dict(period_type=pt_day, period=p_1d, frequency_type=ft_min, frequency=f_1m)

    resp_i = client.get_price_history(symbol, **kwargs_i) if kwargs_i else client.get_price_history(symbol)
    data_i = resp_i.json() if hasattr(resp_i, "json") else resp_i
    candles_i = (data_i.get("candles") if isinstance(data_i, dict) else data_i) or []
    df_i = pd.DataFrame(candles_i)  # ensure it exists

    if not df_i.empty:
        ts2 = _pick_ts_col(df_i)
        if ts2 is not None:
            dt2 = _to_utc_datetime(ts2)
            df_i = df_i.assign(datetime=dt2)
            keep_i = [c for c in ("datetime", "open", "high", "low", "close", "volume") if c in df_i.columns]
            df_i = df_i[keep_i].dropna(how="any")

            # keep only bars from "today" in America/New_York (fallback to UTC)
            # Safe zoneinfo import + fallback names (lowercase to please linters)
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # Py 3.9+
                zoneinfo_cls = ZoneInfo
                zoneinfo_not_found_error = ZoneInfoNotFoundError
            except (ModuleNotFoundError, ImportError):
                zoneinfo_cls = None
                zoneinfo_not_found_error = Exception  # not used when cls is None

            if zoneinfo_cls is not None:
                try:
                    ny = zoneinfo_cls("America/New_York")
                    today_ny = datetime.now(ny).date()
                    start_ny = datetime(today_ny.year, today_ny.month, today_ny.day, 0, 0, 0, tzinfo=ny)
                    end_ny   = start_ny + pd.Timedelta(days=1)
                    start_utc = pd.Timestamp(start_ny).tz_convert("UTC")
                    end_utc   = pd.Timestamp(end_ny).tz_convert("UTC")
                    df_i = df_i[(df_i["datetime"] >= start_utc) & (df_i["datetime"] < end_utc)]
                except (zoneinfo_not_found_error,
                        KeyError, AttributeError, TypeError, ValueError, OverflowError, OutOfBoundsDatetime):
                    # any tz/column conversion hiccup → fallback to UTC "today"
                    today_utc = pd.Timestamp.now(tz="UTC").normalize()
                    df_i = df_i[df_i["datetime"] >= today_utc]
            else:
                # no zoneinfo available → fallback to UTC "today"
                today_utc = pd.Timestamp.now(tz="UTC").normalize()
                df_i = df_i[df_i["datetime"] >= today_utc]
        else:
            df_i = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    else:
        df_i = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    return HistData(daily=df_d, intraday=df_i)






def build_context(client, symbol: str) -> Dict[str, Any]:
    """Return compact context JSON for the given symbol."""
    h = _fetch_price_history(client, symbol)

    # --- Copy and normalize shapes (preserve data; just ensure columns exist) ---
    required = ["datetime", "open", "high", "low", "close", "volume"]

    daily = (h.daily.copy() if isinstance(h.daily, pd.DataFrame) else pd.DataFrame())
    daily = daily.reindex(columns=required, fill_value=np.nan)

    intraday = (h.intraday.copy() if isinstance(h.intraday, pd.DataFrame) else pd.DataFrame())
    intraday = intraday.reindex(columns=required, fill_value=np.nan)

    # Set DatetimeIndex if possible (no-op on empty)
    if "datetime" in daily.columns:
        # Coerce to UTC-aware; safe on mixed types/NaT
        daily["datetime"] = _to_utc_datetime(daily["datetime"])
        # Drop rows with bad datetimes so index set doesn’t fail
        daily = daily[daily["datetime"].notna()]
        if not daily.empty:
            daily.set_index("datetime", inplace=True)

    # ===== DAILY CONTEXT =====
    ctx_daily: Dict[str, Any] = {}
    d_close = daily["close"] if "close" in daily.columns else pd.Series(dtype=float)

    if isinstance(d_close, pd.Series) and not d_close.empty:
        n = len(d_close)

        # Safe last close
        try:
            close_val = float(d_close.iloc[-1])
        except (ValueError, TypeError, IndexError):
            close_val = None

        # Moving averages (only if enough history)
        sma20  = float(d_close.rolling(20).mean().iloc[-1])   if n >= 20  else None
        sma50  = float(d_close.rolling(50).mean().iloc[-1])   if n >= 50  else None
        sma200 = float(d_close.rolling(200).mean().iloc[-1])  if n >= 200 else None

        # Other indicators (helpers already return None safely)
        rsi14  = rsi(d_close, 14)
        dd_min = drawdown_min(d_close)
        ret_1w = float(d_close.iloc[-1] / d_close.iloc[-5]  - 1.0) if n >= 6   else None
        ret_1m = float(d_close.iloc[-1] / d_close.iloc[-21] - 1.0) if n >= 22  else None
        vol_pct = vol_percentile(d_close)

        hi_52w = float(d_close.rolling(252).max().iloc[-1]) if n >= 252 else None
        lo_52w = float(d_close.rolling(252).min().iloc[-1]) if n >= 252 else None

        above_200 = (sma200 is not None and close_val is not None and close_val > sma200)
        ma_stack = int(sum(int(close_val > x) for x in (sma20, sma50, sma200)
                           if (x is not None and close_val is not None))) if close_val is not None else 0

        if close_val is not None:
            ctx_daily = {
                "close": close_val,
                "sma20": sma20,
                "sma50": sma50,
                "sma200": sma200,
                "rsi14": rsi14,
                "drawdown_min": dd_min,
                "ret_1w": ret_1w,
                "ret_1m": ret_1m,
                "vol_pct_1y": vol_pct,
                "hi_52w": hi_52w,
                "lo_52w": lo_52w,
                "above_200dma": bool(above_200),
                "ma_stack": ma_stack,
            }
        # else: leave ctx_daily empty if last close couldn’t be coerced

    # ===== INTRADAY CONTEXT =====
    ctx_intraday: Dict[str, Any] = {}
    if not intraday.empty and "close" in intraday.columns:
        close_col = intraday["close"]
        if isinstance(close_col, pd.Series) and not close_col.empty:
            # VWAP uses full OHLCV; returns None if not computable
            vwap_val = intraday_vwap(intraday)

            try:
                last_close = float(close_col.iloc[-1])
            except (ValueError, TypeError, IndexError):
                last_close = None

            bb_pos = (bollinger_pos(close_col.astype(float), 20, 2.0)
                      if len(close_col) >= 20 else None)

            if last_close is not None:
                ctx_intraday = {
                    "vwap": vwap_val,
                    "last": last_close,
                    "px_above_vwap": (vwap_val is not None and last_close > vwap_val),
                    "bb_pos_20_2": bb_pos,
                }

    return {
        "symbol": symbol,
        "daily": ctx_daily,
        "intraday": ctx_intraday,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }




def compute_bias_score(ctx: Dict[str, Any]) -> float:
    """Simple deterministic bias score combining daily factors.
    Range is unbounded-ish but typically ~[-3, +3]. Tune later.
    """
    d = ctx.get("daily", {})
    if not d:
        return 0.0
    score = 0.0
    if d.get("above_200dma"):
        score += 2.0
    score += max(-1.0, min(1.0, float(d.get("ret_1w") or 0.0) / 0.02))  # ±1 for ±2% week
    score += max(-1.0, min(1.0, float(d.get("ret_1m") or 0.0) / 0.05))  # ±1 for ±5% month
    score += (float(d.get("ma_stack") or 0) - 1) * 0.5  # -0.5, 0.0, +0.5, +1.0
    vol_pct = d.get("vol_pct_1y")
    if isinstance(vol_pct, (int, float)) and vol_pct is not None:
        if vol_pct > 0.8:
            score -= 0.5
        elif vol_pct < 0.2:
            score += 0.2
    return score


# =========================
# PATCH: QuantEcho_core.py
# =========================
# 1) Add imports at top (near other imports):
#     from context_builder import build_context, compute_bias_score
#
# 2) In class TickerLoop._snapshot_payload(), after you compute `ta` and before building `telemetry`, add:
#     ctx = build_context(self.provider.client, self.brain.focus)
#
# 3) Still inside _snapshot_payload(), after you create `telemetry = { ... }`, attach context + bias_code:
#     telemetry["context"] = ctx
#     telemetry["bias_code"] = {"score": round(compute_bias_score(ctx), 2)}
#
# 4) Also include them in the `payload` returned to the GUI/CSV:
#     payload["context"] = ctx
#     payload["bias_code"] = {"score": round(compute_bias_score(ctx), 2)}
#
# 5) In ollama_bias(), update the prompt to surface context + bias_code (replace its `prompt = (...)` with):
#
#     prompt = (
#         "You are a veteran macro/quant trader. Using the TELEMETRY JSON, classify the immediate U.S. "
#         "equity market bias for short-term traders.\n"
#         "Weigh macro links (DXY↑ -> equities↓, TNX↑ -> growth↓), intraday VWAP relation for focus, and TA (RSI/MACD/BB).\n"
#         "Also consider CONTEXT.daily (200DMA, MA stack, 1w/1m returns, drawdown, vol percentile) and BIAS_CODE.score (deterministic pre-score).\n"
#         "If signals conflict, choose NEUTRAL with lower confidence.\n"
#         "STRICT JSON ONLY: {\"bias\":\"bullish|neutral|bearish\",\"confidence\":0..1,\"rationale\":\"<=20 words\"}\n\n"
#         f"TELEMETRY:\n{json.dumps(telemetry, separators=(',', ':'))}"
#     )
#
# That’s it. The model now gets compact historical context + a code anchor, while your GUI gets the same.
