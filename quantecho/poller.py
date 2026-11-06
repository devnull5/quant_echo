"""Background thread: polls quotes every `interval` s and focus bars every `bars_every` s."""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, List, Optional

from .logsetup import log as filelog
from .providers.base import AuthError, RateLimited
from .signals import SignalTracker
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
        self.signals = SignalTracker(self.focus)
        self.max_backoff = 600.0
        self._streak = 0               # consecutive failed quote polls
        self._last_err: Optional[str] = None
        self._next_wait: float = float(self.interval)
        self._bars_state: Optional[str] = None  # last reported: live / stale / prior / none

    def stop(self) -> None:
        self._stop_evt.set()

    def log(self, msg: str, level: str = "info") -> None:
        getattr(filelog, level)(msg)
        self.events.put(("log", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    def _backoff(self, e: Exception, what: str) -> float:
        """Exponential backoff (honours Retry-After). Logs on first failure / when the error changes,
        not every cycle, so a dead connection doesn't flood the log."""
        self._streak += 1
        if isinstance(e, AuthError):
            wait = self.max_backoff  # credentials won't fix themselves
        elif isinstance(e, RateLimited) and e.retry_after:
            wait = min(max(e.retry_after, self.interval), self.max_backoff)
        else:
            wait = min(self.interval * (2 ** min(self._streak, 10)), self.max_backoff)
        sig = f"{type(e).__name__}: {e}"
        if sig != self._last_err or self._streak in (1, 5, 20):
            kind = "Rate limited" if isinstance(e, RateLimited) else "Auth error" if isinstance(e, AuthError) else f"{what} error"
            self.log(f"{kind} ({sig}) — retrying in {wait:.0f}s (attempt {self._streak})", "warning")
            self._last_err = sig
        return wait

    def _recovered(self) -> None:
        if self._streak:
            self.log(f"Recovered after {self._streak} failed attempt(s)")
        self._streak, self._last_err = 0, None

    def poll_once(self) -> None:
        self._next_wait = float(self.interval)
        try:
            quotes = self.provider.get_quotes(self.symbols)
        except Exception as e:  # network/auth errors must not kill the thread
            self._next_wait = self._backoff(e, "Quotes")
            quotes = {}
        else:
            self._recovered()
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

        if time.monotonic() >= self._next_bars and not self._streak:
            self._refresh_bars()
        self._check_signals()

    def _check_signals(self) -> None:
        q = self.telemetry.quote(self.focus)
        if not q or q.last is None:
            return
        hi, lo = self.telemetry.session_range(self.focus)
        hi = max(hi, q.last) if hi is not None else None
        lo = min(lo, q.last) if lo is not None else None
        evs = self.signals.update(q.last, self.telemetry.ta(self.focus), hi, lo)
        if evs:
            self.telemetry.add_signals(self.focus, [e.line() for e in evs])
            for e in evs:
                self.events.put(("signal", e))

    def _refresh_bars(self) -> None:
        try:
            bars = self.provider.get_session_minute_bars(self.focus)
        except RateLimited as e:
            self.log(f"Bars rate limited ({e}) — will retry later", "warning")
            bars = []
        except Exception as e:
            self.log(f"Bars error: {type(e).__name__}: {e}", "warning")
            bars = []
        if bars:
            self.telemetry.update_bars(self.focus, bars)
            self.events.put(("quotes", None))
        # back off when there is no data (off-hours, auth trouble)
        self._next_bars = time.monotonic() + (self.bars_every if bars else max(self.bars_every, 300))
        # Only surface bar status when it CHANGES (first load, live -> stale, prior session, none);
        # routine refreshes every minute go to the log file only.
        if bars:
            ts = datetime.fromtimestamp(bars[-1]["t"] / 1000, tz=timezone.utc).astimezone()
            age_min = (datetime.now().astimezone() - ts).total_seconds() / 60
            if ts.date() != datetime.now().astimezone().date():
                state, note = "prior", " (prior session)"
            elif age_min > 15:
                state, note = "stale", f" (no new bars for {age_min:.0f} min)"
            else:
                state, note = "live", ""
            msg = f"Bars: {self.focus} {len(bars)}x1m through {ts:%Y-%m-%d %H:%M}{note}"
        else:
            state, msg = "none", f"Bars: {self.focus} none returned"
        if state != self._bars_state:
            self.log(msg)
            self._bars_state = state
        else:
            filelog.debug(msg)

    def run(self) -> None:
        n = len(self.symbols)
        listing = " ".join(self.symbols) if n <= 6 else f"{n} symbols"
        self.log(f"Polling started — focus {self.focus}, {listing}, every {self.interval}s")
        while not self._stop_evt.is_set():
            try:
                self.poll_once()
            except Exception:  # never let the polling thread die silently
                filelog.exception("Unexpected error in poll loop")
                self.log("Unexpected error in poll loop (details in log file)", "error")
                self._next_wait = min(self.interval * 4, self.max_backoff)
            self._stop_evt.wait(self._next_wait)
        self.log("Polling stopped")
