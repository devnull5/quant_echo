"""Your own (manual) option trades -> the P&L ledger, kept apart from the bot's sim/sandbox/live rows.

Paste the broker's order history as text, e.g. (newest first, as the app lists it):

    Sell SPY $717 Call 11/25
    Individual · Nov 25
    $950.00
    10 contracts at $0.95

    Buy SPY $717 Call 11/25
    ...
    10 contracts at $0.87

Canceled orders are skipped. Buys and sells are matched first-in-first-out per contract into round trips and
written to pnl_ledger.csv with mode "manual". Importing the same text twice adds nothing.

    python -m quantecho.trading.manual trades.txt [--date 2026-11-25] [--dry-run]
"""
from __future__ import annotations

import csv
import re
import sys
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from .occ import OptionContract

MODE = "manual"
LEDGER_FIELDS = ("closed_at", "mode", "contract", "qty", "entry", "exit", "pnl", "reason")
_HEAD = re.compile(r"^(?P<side>buy|sell)\s+(?P<root>[A-Z.]{1,6})\s+\$?(?P<k>\d+(?:\.\d+)?)\s+(?P<cp>call|put)\s+"
                   r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\s*$", re.I)
_FILL = re.compile(r"^(?P<q>\d+)\s+contracts?\s+at\s+\$?(?P<px>\d+(?:\.\d+)?)\s*$", re.I)


@dataclass
class Fill:
    side: str                 # "buy" | "sell"
    contract: OptionContract
    qty: int
    price: float


@dataclass
class RoundTrip:
    contract: OptionContract
    qty: int
    entry: float
    exit: float

    @property
    def pnl(self) -> float:
        return round((self.exit - self.entry) * self.qty * 100, 2)

    @property
    def pct(self) -> float:
        return (self.exit - self.entry) / self.entry if self.entry else 0.0


def parse_fills(text: str, year: Optional[int] = None) -> List[Fill]:
    """Filled orders in CHRONOLOGICAL order (the pasted list is newest first). Canceled ones are skipped."""
    year = year or date.today().year
    fills: List[Fill] = []
    cur: Optional[Tuple[str, OptionContract]] = None
    for raw in text.splitlines():
        line = raw.strip()
        h = _HEAD.match(line)
        if h:
            y = int(h["y"]) if h["y"] else year
            y = y + 2000 if y < 100 else y
            c = OptionContract(h["root"].upper(), date(y, int(h["m"]), int(h["d"])),
                               "C" if h["cp"].lower() == "call" else "P", float(h["k"]))
            cur = (h["side"].lower(), c)
            continue
        if cur and line.lower() in ("canceled", "cancelled", "rejected", "expired"):
            cur = None
            continue
        f = _FILL.match(line)
        if f and cur:
            fills.append(Fill(cur[0], cur[1], int(f["q"]), float(f["px"])))
            cur = None
    return fills[::-1]


def round_trips(fills: List[Fill]) -> Tuple[List[RoundTrip], Dict[str, int]]:
    """FIFO-match buys to sells per contract. Returns (round trips, contracts still open by label)."""
    lots: Dict[OptionContract, Deque[List[float]]] = {}
    out: List[RoundTrip] = []
    for f in fills:
        q = lots.setdefault(f.contract, deque())
        if f.side == "buy":
            q.append([f.qty, f.price])
            continue
        left = f.qty
        while left and q:
            take = min(left, int(q[0][0]))
            out.append(RoundTrip(f.contract, take, q[0][1], f.price))
            q[0][0] -= take
            left -= take
            if not q[0][0]:
                q.popleft()
        # a sell with nothing bought in this paste (opened earlier) can't be priced: ignored
    still = {c.label(): int(sum(x[0] for x in q)) for c, q in lots.items() if q}
    return out, still


def _key(row: Dict[str, str]) -> Tuple:
    return (row["closed_at"], row["contract"], str(int(float(row["qty"]))), f"{float(row['entry']):.2f}",
            f"{float(row['exit']):.2f}")


def record(trips: List[RoundTrip], ledger: Path, on: date) -> int:
    """Append round trips as mode='manual' rows dated `on` (16:00 ET: no fill times in the paste).
    Rows already in the ledger (same day, contract, qty, prices; counting repeats) are not added again."""
    have: Dict[Tuple, int] = {}
    if ledger.exists():
        with ledger.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("mode") == MODE:
                    have[_key(r)] = have.get(_key(r), 0) + 1
    closed_at = datetime(on.year, on.month, on.day, 16, 0).isoformat() + "-05:00"
    rows = []
    for t in trips:
        r = {"closed_at": closed_at, "mode": MODE, "contract": t.contract.label(), "qty": str(t.qty),
             "entry": f"{t.entry:.2f}", "exit": f"{t.exit:.2f}", "pnl": str(t.pnl), "reason": "manual (imported)"}
        k = _key(r)
        if have.get(k):
            have[k] -= 1
            continue
        rows.append(r)
    if rows:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        new = not ledger.exists()
        with ledger.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)
    return len(rows)


def lifetime(ledger: Path, mode: str = MODE) -> Tuple[float, int]:
    total, n = 0.0, 0
    if ledger.exists():
        with ledger.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("mode") == mode:
                    total += float(r.get("pnl") or 0)
                    n += 1
    return round(total, 2), n


def summary(trips: List[RoundTrip], still: Dict[str, int]) -> str:
    if not trips:
        return "No completed round trips found."
    wins = [t for t in trips if t.pnl > 0]
    pcts = sorted(t.pct for t in trips)
    lines = [f"{len(trips)} round trips, {len(wins)} winners, net ${sum(t.pnl for t in trips):+,.2f}, "
             f"median {pcts[len(pcts) // 2]:+.0%} (range {pcts[0]:+.0%} to {pcts[-1]:+.0%})"]
    lines += [f"  {t.qty}x {t.contract.label()}  {t.entry:.2f} -> {t.exit:.2f}  {t.pct:+.0%}  ${t.pnl:+,.2f}"
              for t in trips]
    if still:
        lines.append("  still open (not recorded): " + ", ".join(f"{q}x {k}" for k, q in still.items()))
    return "\n".join(lines)


def import_text(text: str, on: Optional[date] = None, ledger: Optional[Path] = None,
                dry_run: bool = False) -> Tuple[str, int]:
    """Parse, match and (unless dry_run) record. `on` defaults to the expiry of the last trade's contract."""
    from .bot import JOURNAL_DIR
    ledger = ledger or JOURNAL_DIR / "pnl_ledger.csv"
    trips, still = round_trips(parse_fills(text, (on or date.today()).year))
    on = on or (trips[-1].contract.expiry if trips else date.today())
    added = 0 if dry_run else record(trips, ledger, on)
    total, n = lifetime(ledger)
    text_out = summary(trips, still)
    if trips:
        text_out += (f"\n{'Would record' if dry_run else 'Recorded'} {len(trips) if dry_run else added} new "
                     f"row(s) for {on}" + ("" if dry_run or added == len(trips) else " (the rest were already there)")
                     + f". Your lifetime manual P&L: ${total:+,.2f} over {n} trades.")
    return text_out, added


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Import your own option trades into the QuantEcho P&L ledger")
    ap.add_argument("file", help="text file with the pasted order history ('-' = stdin)")
    ap.add_argument("--date", help="trade date YYYY-MM-DD (default: the last contract's expiry)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    text = sys.stdin.read() if a.file == "-" else Path(a.file).read_text(encoding="utf-8")
    out, _ = import_text(text, date.fromisoformat(a.date) if a.date else None, dry_run=a.dry_run)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
