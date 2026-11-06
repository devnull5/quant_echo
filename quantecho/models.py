from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Quote:
    symbol: str
    last: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    timestamp_ms: Optional[int]
    total_volume: Optional[float]
    net_change: Optional[float] = None
    net_pct: Optional[float] = None
