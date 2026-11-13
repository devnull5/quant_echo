"""Brokers behind one interface: local SIM (modeled prices) and tastytrade (sandbox / live)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..logsetup import log
from .occ import OptionContract, parse_occ
from .pricing import OptionQuote, model_quote, synthetic_chain
from .tasty import DEFAULT_TICKS, PENNY_ALL, TastytradeClient, TickRules, build_option_order, round_to_tick

Chain = Dict[date, Dict[float, Dict[str, str]]]


@dataclass
class OrderResult:
    status: str                     # "filled" | "working" | "rejected" | "cancelled"
    order_id: str = ""
    fill_price: Optional[float] = None
    message: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class Broker:
    name = "broker"
    real_money = False

    def tick_rules(self, root: str) -> TickRules:
        return [(None, 0.01)] if root.upper() in PENNY_ALL else DEFAULT_TICKS

    def round_price(self, c: OptionContract, price: float, buy: bool) -> float:
        """Valid limit for this contract (buys round up, sells down)."""
        return round_to_tick(price, self.tick_rules(c.root), up=buy)

    def chain(self, root: str, spot: float) -> Chain: ...
    def quote(self, c: OptionContract, spot: float) -> OptionQuote: ...
    def submit(self, c: OptionContract, action: str, qty: int, limit: float, ext_id: str) -> OrderResult: ...
    def status(self, order_id: str, c: OptionContract, spot: float) -> OrderResult: ...
    def cancel(self, order_id: str) -> None: ...

    def open_positions(self, allowed: List[str]) -> List[Tuple[OptionContract, int, float]]:
        """Long option positions already held at the broker: (contract, qty, avg open price)."""
        return []


class SimBroker(Broker):
    """Local paper trading. Option prices from Black-Scholes (labeled modeled). Deterministic fills:
    a buy fills when limit >= ask (at ask) or limit >= mid (at limit); a sell when limit <= bid or <= mid."""

    name = "SIM (local paper, modeled prices)"

    def __init__(self, iv: Optional[Dict[str, float]] = None, quote_client: Optional[TastytradeClient] = None):
        self.iv = iv or {}
        self.quote_client = quote_client      # optional: real quotes (read-only live creds), local fills
        if quote_client is not None:
            self.name = "SIM (local paper, REAL quotes)"
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._n = 0
        self._lock = threading.Lock()

    def chain(self, root: str, spot: float) -> Chain:
        return synthetic_chain(root, spot)

    def quote(self, c: OptionContract, spot: float) -> OptionQuote:
        if self.quote_client is not None:
            q = _real_quote(self.quote_client, c)
            if q:
                return q
        return model_quote(c, spot, self.iv.get(c.root))

    def _try_fill(self, o: Dict[str, Any], q: OptionQuote) -> Optional[float]:
        lim, mid = o["limit"], (q.bid + q.ask) / 2
        if o["action"].startswith("Buy"):
            if lim >= q.ask:
                return q.ask
            if lim >= mid:
                return lim
        else:
            if lim <= q.bid:
                return q.bid
            if lim <= mid:
                return lim
        return None

    def submit(self, c, action, qty, limit, ext_id) -> OrderResult:
        build_option_order(c.symbol, action, qty, limit, ext_id)   # same validation as the real thing
        with self._lock:
            self._n += 1
            oid = f"SIM-{self._n}"
            self._orders[oid] = {"c": c, "action": action, "qty": qty, "limit": limit, "status": "working"}
        return OrderResult("working", oid, message="accepted (sim)")

    def status(self, order_id, c, spot) -> OrderResult:
        with self._lock:
            o = self._orders.get(order_id)
            if not o:
                return OrderResult("rejected", order_id, message="unknown order")
            if o["status"] == "working":
                px = self._try_fill(o, self.quote(o["c"], spot))
                if px is not None:
                    o["status"], o["fill"] = "filled", round(px, 2)
            return OrderResult(o["status"], order_id, o.get("fill"))

    def cancel(self, order_id) -> None:
        with self._lock:
            if order_id in self._orders and self._orders[order_id]["status"] == "working":
                self._orders[order_id]["status"] = "cancelled"


def _real_quote(client: TastytradeClient, c: OptionContract) -> Optional[OptionQuote]:
    try:
        q = client.quotes(options=[c.symbol]).get(c.symbol)
    except Exception as e:
        log.warning(f"real option quote failed for {c.symbol}: {e}; using model")
        return None
    if q and q.get("bid") is not None and q.get("ask") is not None:
        bid, ask = float(q["bid"]), float(q["ask"])
        mark = float(q.get("mark") or q.get("mid") or (bid + ask) / 2)
        return OptionQuote(bid, ask, mark, modeled=False)
    return None


_TERMINAL_BAD = {"Rejected", "Cancelled", "Expired", "Removed", "Partially Removed"}
_WORKING = {"Received", "Routed", "In Flight", "Contingent", "Live", "Cancel Requested", "Replace Requested"}


def _fill_price(order: Dict[str, Any]) -> Optional[float]:
    tot_q = tot_v = 0.0
    for leg in order.get("legs") or []:
        for f in leg.get("fills") or []:
            try:
                q = float(f.get("quantity") or f.get("fill-quantity") or 0)
                p = float(f.get("fill-price") or f.get("price") or 0)
            except (TypeError, ValueError):
                continue
            tot_q += q
            tot_v += q * p
    return round(tot_v / tot_q, 4) if tot_q else None


class TastyBroker(Broker):
    """tastytrade sandbox or live. Every order is dry-run first; any error/warning blocks it."""

    def __init__(self, client: TastytradeClient, quote_client: Optional[TastytradeClient] = None,
                 iv: Optional[Dict[str, float]] = None):
        self.client, self.quote_client = client, quote_client
        self.real_money = client.env == "live"
        self.iv = iv or {}
        self.name = f"tastytrade {'LIVE' if self.real_money else 'sandbox'}"

    def chain(self, root: str, spot: float) -> Chain:
        return {e.expiry: e.strikes for e in self.client.option_chain(root)}

    def tick_rules(self, root: str) -> TickRules:
        return self.client.tick_rules.get(root.upper()) or super().tick_rules(root)

    def quote(self, c: OptionContract, spot: float) -> OptionQuote:
        if self.quote_client is not None:   # production read-only credentials -> real quotes
            q = _real_quote(self.quote_client, c)
            if q:
                return q
        return model_quote(c, spot, self.iv.get(c.root))  # sandbox has no market data

    def submit(self, c, action, qty, limit, ext_id) -> OrderResult:
        order = build_option_order(c.symbol, action, qty, limit, ext_id)
        dr = self.client.dry_run(order)
        problems = [w.get("message", str(w)) for w in (dr.get("errors") or []) + (dr.get("warnings") or [])]
        if problems:
            return OrderResult("rejected", message="dry-run: " + "; ".join(problems), details=dr)
        placed = self.client.place_order(order)
        return self._map(placed)

    def _map(self, o: Dict[str, Any]) -> OrderResult:
        st, oid = o.get("status", ""), str(o.get("id", ""))
        if st == "Filled":
            return OrderResult("filled", oid, _fill_price(o), details=o)
        if st in _TERMINAL_BAD:
            return OrderResult("cancelled" if st == "Cancelled" else "rejected", oid,
                               message=o.get("reject-reason") or st, details=o)
        return OrderResult("working", oid, message=st, details=o)

    def status(self, order_id, c, spot) -> OrderResult:
        r = self._map(self.client.order(order_id))
        if r.status == "filled" and r.fill_price is None:   # fills can lag the status briefly
            r = self._map(self.client.order(order_id))
        return r

    def cancel(self, order_id) -> None:
        try:
            self.client.cancel(order_id)
        except Exception as e:
            log.warning(f"tastytrade cancel {order_id} failed: {e}")

    def open_positions(self, allowed: List[str]) -> List[Tuple[OptionContract, int, float]]:
        out = []
        for p in positions_for(self.client, allowed):
            if str(p.get("quantity-direction", "Long")).lower() != "long":
                continue
            qty = int(float(p.get("quantity") or 0))
            if qty > 0:
                out.append((parse_occ(p["symbol"]), qty, float(p.get("average-open-price") or 0)))
        return out


def positions_for(client: TastytradeClient, allowed: List[str]) -> List[Dict[str, Any]]:
    """Open equity-option positions on allowed underlyings (to avoid doubling up after a restart)."""
    out = []
    for p in client.positions():
        if p.get("instrument-type") != "Equity Option":
            continue
        try:
            c = parse_occ(p.get("symbol", ""))
        except ValueError:
            continue
        if c.root in allowed:
            out.append(p)
    return out


ConfirmFn = Callable[[str], bool]
