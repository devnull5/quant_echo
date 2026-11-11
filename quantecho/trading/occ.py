"""OCC option symbols as tastytrade uses them: ROOT padded to 6 + yymmdd + C/P + strike*1000 (8 digits).

    "SPY   261120C00600000"  = SPY 2026-11-20 600 Call
    "NVDA  261127P00185500"  = NVDA 2026-11-27 185.5 Put
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_RE = re.compile(r"^(?P<root>[A-Z0-9]{1,6})\s*(?P<d>\d{6})(?P<cp>[CP])(?P<k>\d{8})$")


@dataclass(frozen=True)
class OptionContract:
    root: str
    expiry: date
    right: str        # "C" or "P"
    strike: float

    @property
    def symbol(self) -> str:
        return occ_symbol(self.root, self.expiry, self.right, self.strike)

    @property
    def is_call(self) -> bool:
        return self.right == "C"

    def label(self) -> str:
        return f"{self.root} {self.expiry:%b %d} {self.strike:g}{'C' if self.is_call else 'P'}"


def occ_symbol(root: str, expiry: date, right: str, strike: float) -> str:
    right = right.upper()[0]
    if right not in "CP":
        raise ValueError("right must be C or P")
    k = int(round(strike * 1000))
    if not 0 < k < 100_000_000:
        raise ValueError(f"strike out of range: {strike}")
    root = root.upper()
    if not 1 <= len(root) <= 6:
        raise ValueError(f"bad root: {root}")
    return f"{root:<6}{expiry:%y%m%d}{right}{k:08d}"


def parse_occ(symbol: str) -> OptionContract:
    m = _RE.match(symbol.strip().upper())
    if not m:
        raise ValueError(f"not an OCC option symbol: {symbol!r}")
    d = m.group("d")
    return OptionContract(m.group("root"), date(2000 + int(d[:2]), int(d[2:4]), int(d[4:6])), m.group("cp"),
                          int(m.group("k")) / 1000.0)
