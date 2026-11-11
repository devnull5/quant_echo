"""Black-Scholes option pricing + a synthetic option chain for local SIM mode (no market data needed).

Modeled prices are an approximation: real options carry skew, event premia and wider spreads. Anything
priced here is labeled "modeled" in the UI and journal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .occ import OptionContract

NY = ZoneInfo("America/New_York")
DEFAULT_IV = {"SPY": 0.16, "QQQ": 0.20, "NVDA": 0.45}
DEFAULT_RATE = 0.04
STRIKE_STEP = {"SPY": 1.0, "QQQ": 1.0, "NVDA": 2.5}


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot: float, strike: float, years: float, iv: float, call: bool, rate: float = DEFAULT_RATE) -> float:
    if years <= 0 or iv <= 0:
        return max(0.0, spot - strike) if call else max(0.0, strike - spot)
    sq = iv * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / sq
    d2 = d1 - sq
    if call:
        return spot * _ncdf(d1) - strike * math.exp(-rate * years) * _ncdf(d2)
    return strike * math.exp(-rate * years) * _ncdf(-d2) - spot * _ncdf(-d1)


def bs_delta(spot: float, strike: float, years: float, iv: float, call: bool, rate: float = DEFAULT_RATE) -> float:
    if years <= 0 or iv <= 0:
        return (1.0 if spot > strike else 0.0) if call else (-1.0 if spot < strike else 0.0)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / (iv * math.sqrt(years))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


def years_to_expiry(expiry: date, now: Optional[datetime] = None) -> float:
    """Time until 16:00 ET on expiry day, in years (calendar time)."""
    now = now or datetime.now(NY)
    close = datetime.combine(expiry, dtime(16, 0), tzinfo=NY)
    return max(0.0, (close - now.astimezone(NY)).total_seconds() / (365.0 * 86400))


@dataclass
class OptionQuote:
    bid: float
    ask: float
    mark: float
    modeled: bool = True

    @property
    def spread_pct(self) -> float:
        return (self.ask - self.bid) / self.mark if self.mark > 0 else 1.0


def model_quote(c: OptionContract, spot: float, iv: Optional[float] = None, now: Optional[datetime] = None) -> OptionQuote:
    iv = iv if iv else DEFAULT_IV.get(c.root, 0.35)
    mark = bs_price(spot, c.strike, years_to_expiry(c.expiry, now), iv, c.is_call)
    half = max(0.01, mark * (0.008 if c.root in ("SPY", "QQQ") else 0.015))   # typical liquid-name spreads
    mark = max(0.01, mark)
    return OptionQuote(bid=round(max(0.0, mark - half), 2), ask=round(mark + half, 2), mark=round(mark, 2))


def synthetic_expirations(root: str, today: date, horizon_days: int = 45) -> List[date]:
    """SPY/QQQ: every weekday. Others: Fridays."""
    days = [today + timedelta(d) for d in range(horizon_days + 1)]
    if root in ("SPY", "QQQ"):
        return [d for d in days if d.weekday() < 5]
    return [d for d in days if d.weekday() == 4]


def nearest_strikes(root: str, spot: float, n: int = 10) -> List[float]:
    step = STRIKE_STEP.get(root, 1.0 if spot < 200 else 5.0)
    atm = round(spot / step) * step
    return [round(atm + i * step, 2) for i in range(-n, n + 1)]


def synthetic_chain(root: str, spot: float, today: Optional[date] = None) -> Dict[date, Dict[float, Dict[str, str]]]:
    today = today or datetime.now(NY).date()
    out = {}
    for exp in synthetic_expirations(root, today):
        out[exp] = {k: {"C": OptionContract(root, exp, "C", k).symbol, "P": OptionContract(root, exp, "P", k).symbol}
                    for k in nearest_strikes(root, spot)}
    return out
