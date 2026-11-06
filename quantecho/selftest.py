"""Self-test for packaged builds:  QuantEcho.exe --selftest [--online] [--out report.txt]

A windowed .exe can't print, so results go to a report file (default: ~/.quanteco_gpt/selftest.txt) and
the exit code (0 = all passed). Checks the things frozen builds typically lose: data files (time zones,
icon), Tk, the keyring backend, provider imports, TA math, and optionally live network sources.
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path
from typing import Callable, List, Tuple


def _checks(online: bool) -> List[Tuple[str, Callable[[], str]]]:
    def tz():
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime(2026, 1, 1, 12, tzinfo=ZoneInfo("America/New_York")).isoformat()

    def tk_window():
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        v = r.tk.call("info", "patchlevel")
        r.destroy()
        return f"Tk {v}"

    def icon():
        p = Path(__file__).resolve().parent / "assets" / "icon.png"
        if not p.exists():
            raise FileNotFoundError(p)
        return f"{p.stat().st_size} bytes"

    def keyring_backend():
        from .config import keyring_available, _kr
        kr = _kr()
        name = type(kr.get_keyring()).__name__ if kr else "none"
        if not keyring_available():
            raise RuntimeError(f"no usable keyring backend ({name})")
        return name

    def providers():
        from .providers import PROVIDERS
        return f"{len(PROVIDERS)} sources: " + ", ".join(PROVIDERS)

    def demo_pipeline():
        import queue
        from .poller import Poller
        from .providers.simulated import SimulatedProvider
        from .telemetry import TelemetryStore
        tel = TelemetryStore()
        Poller(SimulatedProvider(seed=1), ["SPY", "AAPL"], "SPY", 1, queue.Queue(), tel).poll_once()
        ta = tel.ta("SPY")
        if ta.vwap is None or ta.rsi14 is None:
            raise RuntimeError("TA not computed")
        return f"VWAP {ta.vwap:.2f} RSI {ta.rsi14:.1f}"

    def https():
        import certifi  # httpx's CA bundle must be bundled for any HTTPS source
        return Path(certifi.where()).name

    checks = [("time zones", tz), ("Tk", tk_window), ("icon asset", icon), ("keyring", keyring_backend),
              ("provider registry", providers), ("demo data + TA", demo_pipeline), ("TLS CA bundle", https)]
    if online:
        def coinbase():
            from .providers import create
            q = create("coinbase", {}).get_quotes(["BTC-USD"])["BTC-USD"]
            return f"BTC-USD {q.last}"

        def ollama():
            from . import llm
            models = llm.list_models()
            return ", ".join(models) or "reachable? no models (not an error)"
        checks += [("Coinbase live", coinbase), ("Ollama (optional)", ollama)]
    return checks


def run(argv: List[str]) -> int:
    online = "--online" in argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else Path.home() / ".quanteco_gpt" / "selftest.txt"
    lines = [f"QuantEcho self-test  {time.strftime('%Y-%m-%d %H:%M:%S')}  frozen={getattr(sys, 'frozen', False)}"
             f"  python={sys.version.split()[0]}"]
    failed = 0
    for name, fn in _checks(online):
        try:
            lines.append(f"PASS  {name}: {fn()}")
        except Exception as e:  # report everything, keep going
            optional = "optional" in name
            failed += 0 if optional else 1
            lines.append(f"{'WARN' if optional else 'FAIL'}  {name}: {type(e).__name__}: {e}")
            if not optional:
                lines.append("      " + traceback.format_exc().strip().replace("\n", "\n      "))
    lines.append(f"RESULT: {'OK' if not failed else f'{failed} FAILED'}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        print("\n".join(lines))
    except Exception:
        pass  # no console in windowed builds
    return 1 if failed else 0
