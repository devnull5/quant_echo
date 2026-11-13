"""Hard risk limits. Checked before EVERY entry; the strategy cannot override them."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


@dataclass
class RiskLimits:
    allowed_symbols: List[str] = field(default_factory=lambda: ["SPY", "NVDA"])
    max_premium_per_trade: float = 300.0     # $ debit per entry (contracts x price x 100)
    max_contracts_per_trade: int = 2
    max_open_positions: int = 2
    one_position_per_symbol: bool = True
    daily_loss_limit: float = 300.0           # realized + open P&L; hitting it halts new entries for the day
    max_entries_per_day: int = 6
    max_spread_pct: float = 0.10              # skip contracts whose bid/ask spread is > 10% of mid
    entry_start: str = "09:30"                # ET; market open
    entry_end: str = "15:30"                  # ET; no new entries late in the day
    trading_days_only: bool = True

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class DayState:
    day: str = ""
    entries: int = 0
    realized_pnl: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def roll(self, today: str) -> None:
        if self.day != today:
            self.day, self.entries, self.realized_pnl, self.halted, self.halt_reason = today, 0, 0.0, False, ""


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def size_position(limits: RiskLimits, price: float) -> int:
    """Contracts that fit the per-trade premium budget (0 = too expensive to take even one)."""
    if price <= 0:
        return 0
    n = math.floor(limits.max_premium_per_trade / (price * 100))
    return max(0, min(n, limits.max_contracts_per_trade))


def market_open(now: datetime) -> bool:
    """Regular options session, Mon-Fri 09:30-16:00 ET (exchange holidays aren't modeled; the broker rejects)."""
    now = now.astimezone(NY)
    return now.weekday() < 5 and dtime(9, 30) <= now.time() < dtime(16, 0)


def in_entry_window(limits: RiskLimits, now: datetime) -> bool:
    now = now.astimezone(NY)
    if limits.trading_days_only and now.weekday() >= 5:
        return False
    return _hm(limits.entry_start) <= now.time() <= _hm(limits.entry_end)


def check_entry(limits: RiskLimits, state: DayState, symbol: str, open_symbols: List[str], price: float,
                spread_pct: float, open_pnl: float, now: Optional[datetime] = None) -> Tuple[bool, List[str]]:
    """All reasons an entry is blocked (empty list = allowed)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    state.roll(now.date().isoformat())
    why: List[str] = []
    if state.halted:
        why.append(f"halted for today: {state.halt_reason}")
    if symbol.upper() not in [s.upper() for s in limits.allowed_symbols]:
        why.append(f"{symbol} not on the allowlist {limits.allowed_symbols}")
    if limits.trading_days_only and now.weekday() >= 5:
        why.append("weekend")
    if not (_hm(limits.entry_start) <= now.time() <= _hm(limits.entry_end)):
        why.append(f"outside entry window {limits.entry_start}-{limits.entry_end} ET")
    if len(open_symbols) >= limits.max_open_positions:
        why.append(f"max open positions ({limits.max_open_positions}) reached")
    if limits.one_position_per_symbol and symbol.upper() in [s.upper() for s in open_symbols]:
        why.append(f"already holding {symbol}")
    if state.entries >= limits.max_entries_per_day:
        why.append(f"max entries per day ({limits.max_entries_per_day}) reached")
    if state.realized_pnl + open_pnl <= -abs(limits.daily_loss_limit):
        why.append(f"daily loss limit ${limits.daily_loss_limit:,.0f} reached")
    if spread_pct > limits.max_spread_pct:
        why.append(f"bid/ask spread {spread_pct:.0%} > {limits.max_spread_pct:.0%}")
    if size_position(limits, price) < 1:
        why.append(f"one contract (${price * 100:,.0f}) exceeds max premium ${limits.max_premium_per_trade:,.0f}")
    return (not why), why
