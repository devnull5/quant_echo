"""Long calls/puts from the chart's closed-bar TREND signals.

Entry: trend BUY (EMA9 crosses above EMA20, price above VWAP) -> buy a call;
       trend SELL (EMA9 crosses below EMA20, price below VWAP) -> buy a put.
Contract: expiration nearest to target_dte within [min_dte, max_dte]; strike nearest ATM (or N strikes OTM).
Exits (first that fires): take-profit, stop-loss, opposite trend signal, time exit before expiry,
end-of-day flatten for options expiring today/tomorrow.

These rules are mechanical and have no proven edge. Test in sim/sandbox before risking money.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from ..chart import Signal
from .occ import OptionContract

NY = ZoneInfo("America/New_York")


@dataclass
class StrategyParams:
    signal_source: str = "trend"          # "trend" (per-symbol chart signals) or "confluence" (NVDA intermarket)
    use_reversal_signals: bool = False   # trend signals only by default (reversals are noisier)
    target_dte: int = 7
    min_dte: int = 2
    max_dte: int = 14
    strikes_otm: int = 0                 # 0 = nearest ATM, 1 = one strike out of the money, ...
    take_profit_pct: float = 0.50        # +50% on premium
    stop_loss_pct: float = 0.40          # -40% on premium
    exit_on_opposite_signal: bool = True
    flatten_at: str = "15:45"            # ET: close positions whose expiry is today or tomorrow
    max_hold_days: int = 5

    def to_dict(self) -> Dict:
        return asdict(self)


def entry_side(sig: Signal, p: StrategyParams) -> Optional[str]:
    """'C' for a bullish entry, 'P' for bearish, None if the signal shouldn't trade."""
    if sig.kind == "reversal" and not p.use_reversal_signals:
        return None
    return "C" if sig.side == "buy" else "P"


def choose_expiry(expiries: List[date], today: date, p: StrategyParams) -> Optional[date]:
    ok = [e for e in expiries if p.min_dte <= (e - today).days <= p.max_dte]
    if not ok:
        return None
    return min(ok, key=lambda e: (abs((e - today).days - p.target_dte), e))


def choose_strike(strikes: List[float], spot: float, right: str, p: StrategyParams) -> Optional[float]:
    if not strikes:
        return None
    s = sorted(strikes)
    atm_i = min(range(len(s)), key=lambda i: abs(s[i] - spot))
    i = atm_i + (p.strikes_otm if right == "C" else -p.strikes_otm)
    return s[max(0, min(i, len(s) - 1))]


def pick_contract(root: str, chain: Dict[date, Dict[float, Dict[str, str]]], spot: float, right: str,
                  today: date, p: StrategyParams) -> Optional[OptionContract]:
    exp = choose_expiry(list(chain), today, p)
    if exp is None:
        return None
    strikes = [k for k, legs in chain[exp].items() if right in legs]
    k = choose_strike(strikes, spot, right, p)
    return OptionContract(root.upper(), exp, right, k) if k is not None else None


def exit_reason(entry_price: float, mark: float, contract: OptionContract, opened: datetime, now: datetime,
                last_signal: Optional[Signal], p: StrategyParams) -> Optional[str]:
    """Why to close now (None = hold)."""
    now = now.astimezone(NY)
    if entry_price > 0:
        chg = (mark - entry_price) / entry_price
        if chg >= p.take_profit_pct:
            return f"take profit ({chg:+.0%})"
        if chg <= -p.stop_loss_pct:
            return f"stop loss ({chg:+.0%})"
    if p.exit_on_opposite_signal and last_signal is not None and last_signal.kind in ("trend", "confluence"):
        against = (contract.is_call and last_signal.side == "sell") or (not contract.is_call and last_signal.side == "buy")
        if against:
            return f"opposite signal ({last_signal.reason})"
    h, m = map(int, p.flatten_at.split(":"))
    if (contract.expiry - now.date()).days <= 1 and now.time() >= dtime(h, m):
        return "flatten before expiry"
    if (now.date() - opened.astimezone(NY).date()).days >= p.max_hold_days:
        return f"max hold {p.max_hold_days} days"
    return None


def entry_limit(bid: float, ask: float) -> float:
    """Patient-but-fillable limit: mid plus a quarter of the half-spread, rounded to the cent."""
    mid = (bid + ask) / 2
    return round(mid + (ask - mid) * 0.25 + 1e-9, 2)


def exit_limit(bid: float, ask: float, urgent: bool = False) -> float:
    if urgent:
        return round(max(0.01, bid), 2)
    mid = (bid + ask) / 2
    return round(max(0.01, mid - (mid - bid) * 0.25), 2)


def summarize(contract: OptionContract, qty: int, price: float) -> Tuple[str, float]:
    return f"{qty}x {contract.label()} @ {price:.2f}", qty * price * 100
