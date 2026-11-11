"""Background feed for the confluence grid: 1-min bars for every leg + batched quotes folded into the
forming candle. Emits ("confl_data", {symbol: bars}) on the app's event queue.

Load (Yahoo, 6 symbols): bars every 20 s = 18 req/min + one batched quote call every 5 s = 12 req/min.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Dict, List

from .chart import apply_tick
from .logsetup import log


class ConfluenceFeed(threading.Thread):
    def __init__(self, provider: Any, symbols: List[str], events: "queue.Queue[tuple]",
                 bars_every_s: float = 20.0, quotes_every_s: float = 5.0):
        super().__init__(daemon=True, name="quantecho-confluence")
        self.provider, self.symbols, self.events = provider, [s.upper() for s in symbols], events
        self.bars_every_s, self.quotes_every_s = bars_every_s, quotes_every_s
        self.bars: Dict[str, List[Dict]] = {}
        self._stop_evt = threading.Event()
        self._next_bars = 0.0

    def stop(self) -> None:
        self._stop_evt.set()

    def poll_once(self) -> None:
        if time.monotonic() >= self._next_bars:
            for s in self.symbols:
                if self._stop_evt.is_set():
                    return
                try:
                    b = self.provider.get_session_minute_bars(s)
                    if b:
                        self.bars[s] = b
                except Exception as e:
                    log.info(f"confluence bars {s}: {type(e).__name__}: {e}")
            self._next_bars = time.monotonic() + self.bars_every_s
        try:
            quotes = self.provider.get_quotes(self.symbols)
        except Exception as e:
            log.info(f"confluence quotes: {type(e).__name__}: {e}")
            quotes = {}
        for s, q in quotes.items():
            if s in self.bars and q.last is not None:
                self.bars[s] = apply_tick(self.bars[s], q.last, q.timestamp_ms or int(time.time() * 1000))
        self.events.put(("confl_data", {k: list(v) for k, v in self.bars.items()}))

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("confluence feed error")
            self._stop_evt.wait(self.quotes_every_s)
