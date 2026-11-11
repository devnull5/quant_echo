"""Real-time ticks for the FOCUS symbol, so the chart moves between watchlist polls.

- Coinbase: official public WebSocket feed (every trade, keyless), auto-reconnect with backoff.
- Other sources: a light poller for just the focus symbol at the source's `focus_tick_s` rate
  (0 = disabled for sources whose free tier can't afford it).

Each tick is put on the GUI event queue as ("tick", Tick). Bursts are coalesced: at most one tick per
`min_gap_s` per symbol reaches the GUI, with trade sizes summed so candle volume stays right.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .logsetup import log
from .providers.base import parse_iso
from .ta import coerce_float

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"


@dataclass
class Tick:
    symbol: str
    price: float
    ts_ms: int
    size: float = 0.0              # traded size since the previous emitted tick (0 if unknown)
    bid: Optional[float] = None
    ask: Optional[float] = None
    open_24h: Optional[float] = None


class _Coalescer:
    """Emit at most one tick per min_gap_s; accumulate size in between."""

    def __init__(self, events: "queue.Queue[tuple]", min_gap_s: float):
        self.events, self.min_gap_s = events, min_gap_s
        self._pending: Optional[Tick] = None
        self._size = 0.0
        self._last_emit = 0.0
        self._lock = threading.Lock()

    def add(self, t: Tick) -> None:
        with self._lock:
            self._size += t.size
            self._pending = t
            now = time.monotonic()
            if now - self._last_emit >= self.min_gap_s:
                self._flush(now)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._flush(time.monotonic())

    def _flush(self, now: float) -> None:
        t = self._pending
        t.size, self._size, self._pending, self._last_emit = self._size, 0.0, None, now
        self.events.put(("tick", t))


class TickStream(threading.Thread):
    def __init__(self, symbol: str, events: "queue.Queue[tuple]", min_gap_s: float = 0.04):
        super().__init__(daemon=True, name="quantecho-ticks")
        self.symbol = symbol.upper()
        self.out = _Coalescer(events, min_gap_s)
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def stopped(self) -> bool:
        return self._stop_evt.is_set()


class PollTickStream(TickStream):
    """Polls provider.get_quotes([focus]) every `every_s` seconds."""

    def __init__(self, provider: Any, symbol: str, events, every_s: float):
        super().__init__(symbol, events)
        self.provider, self.every_s = provider, max(0.2, every_s)

    def run(self) -> None:
        fails = 0
        while not self.stopped():
            try:
                q = self.provider.get_quotes([self.symbol]).get(self.symbol)
                if q and q.last is not None:
                    self.out.add(Tick(self.symbol, q.last, q.timestamp_ms or int(time.time() * 1000),
                                      bid=q.bid, ask=q.ask))
                    self.out.flush()
                fails = 0
            except Exception as e:  # main poller reports errors; just back off quietly here
                fails += 1
                if fails == 1:
                    log.info(f"Fast ticks paused ({type(e).__name__}: {e})")
            self._stop_evt.wait(self.every_s * (2 ** min(fails, 5)))


def parse_coinbase(msg: dict) -> Optional[Tick]:
    if msg.get("type") != "ticker":
        return None
    price = coerce_float(msg.get("price"))
    if price is None:
        return None
    return Tick(symbol=str(msg.get("product_id", "")).upper(), price=price,
                ts_ms=parse_iso(msg.get("time") or "") or int(time.time() * 1000),
                size=coerce_float(msg.get("last_size")) or 0.0,
                bid=coerce_float(msg.get("best_bid")), ask=coerce_float(msg.get("best_ask")),
                open_24h=coerce_float(msg.get("open_24h")))


class CoinbaseTickStream(TickStream):
    """Official Coinbase Exchange WebSocket 'ticker' channel for one product."""

    def __init__(self, symbol: str, events, product_id: str):
        super().__init__(symbol, events)
        self.product_id = product_id
        self._ws = None

    def stop(self) -> None:
        super().stop()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def run(self) -> None:
        import websocket  # websocket-client

        backoff = 1.0
        while not self.stopped():
            try:
                self._ws = websocket.create_connection(COINBASE_WS, timeout=15)
                self._ws.send(json.dumps({"type": "subscribe", "product_ids": [self.product_id],
                                          "channels": ["ticker", "heartbeat"]}))
                backoff = 1.0
                while not self.stopped():
                    raw = self._ws.recv()
                    if not raw:
                        break
                    t = parse_coinbase(json.loads(raw))
                    if t:
                        t.symbol = self.symbol  # keep the user's spelling (e.g. "BTC" vs "BTC-USD")
                        self.out.add(t)
                    else:
                        self.out.flush()  # heartbeat (~1/s): release a tick held back by coalescing
            except Exception as e:
                if self.stopped():
                    break
                log.info(f"Coinbase stream reconnecting in {backoff:.0f}s ({type(e).__name__}: {e})")
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    if self._ws:
                        self._ws.close()
                except Exception:
                    pass
                self.out.flush()


def make_stream(provider_id: str, provider: Any, focus: str, events, focus_tick_s: float) -> Optional[TickStream]:
    if provider_id == "coinbase":
        from .providers.coinbase import normalize_symbol
        return CoinbaseTickStream(focus, events, normalize_symbol(focus))
    if focus_tick_s > 0:
        return PollTickStream(provider, focus, events, focus_tick_s)
    return None
