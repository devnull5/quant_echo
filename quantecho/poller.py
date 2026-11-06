"""Background thread: polls quotes every `interval` s and focus bars every `bars_every` s."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

from .telemetry import TelemetryStore


class Poller(threading.Thread):
    def __init__(
        self,
        provider: Any,
        symbols: List[str],
        focus: str,
        interval: int,
        events: "queue.Queue[tuple]",
        telemetry: TelemetryStore,
        bars_every: int = 60,
        move_threshold: float = 0.001,
    ):
        super().__init__(daemon=True, name="quantecho-poller")
        self.provider = provider
        self.focus = (focus or "SPY").upper()
        syms = [s.upper() for s in symbols if s.strip()]
        self.symbols = [self.focus] + [s for s in dict.fromkeys(syms) if s != self.focus]
        self.interval = max(1, int(interval))
        self.events = events
        self.telemetry = telemetry
        self.bars_every = max(15, int(bars_every))
        self.move_threshold = move_threshold
        self._stop_evt = threading.Event()
        self._last_printed: Optional[float] = None
        self._next_bars = 0.0

    def stop(self) -> None:
        self._stop_evt.set()

    def log(self, msg: str) -> None:
        self.events.put(("log", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    def poll_once(self) -> None:
        try:
            quotes = self.provider.get_quotes(self.symbols)
        except Exception as e:  # network/auth errors must not kill the thread
            self.log(f"Quotes error: {type(e).__name__}: {e}")
            quotes = {}
        if quotes:
            self.telemetry.update_quotes(quotes)
            self.events.put(("quotes", None))
            fq = quotes.get(self.focus)
            if fq and fq.last is not None:
                prev = self._last_printed
                if prev is None or abs(fq.last - prev) / prev >= self.move_threshold:
                    chg = "" if prev is None else f" ({(fq.last - prev) / prev:+.2%} since last print)"
                    self.log(f"{self.focus} {fq.last:.2f}{chg}")
                    self._last_printed = fq.last

        if time.monotonic() >= self._next_bars:
            self._refresh_bars()

    def _refresh_bars(self) -> None:
        try:
            bars = self.provider.get_session_minute_bars(self.focus)
        except Exception as e:
            self.log(f"Bars error: {type(e).__name__}: {e}")
            bars = []
        if bars:
            self.telemetry.update_bars(self.focus, bars)
            self.events.put(("quotes", None))
        # back off when there is no data (off-hours, auth trouble)
        self._next_bars = time.monotonic() + (self.bars_every if bars else max(self.bars_every, 300))
        if bars:
            ts = datetime.fromtimestamp(bars[-1]["t"] / 1000, tz=timezone.utc).astimezone()
            stale = "" if ts.date() == datetime.now().astimezone().date() else " (prior session)"
            self.log(f"Bars: {self.focus} {len(bars)}x1m through {ts:%Y-%m-%d %H:%M}{stale}")
        else:
            self.log(f"Bars: {self.focus} none returned")

    def run(self) -> None:
        self.log(f"Polling started — focus={self.focus} symbols={self.symbols} every {self.interval}s")
        while not self._stop_evt.is_set():
            self.poll_once()
            self._stop_evt.wait(self.interval)
        self.log("Polling stopped")
