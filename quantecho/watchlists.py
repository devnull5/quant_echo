"""Built-in watchlists."""
from __future__ import annotations

# Top 20 holdings of SPY by weight (source: stockanalysis.com SPY holdings, updated 2026-11-04).
# Weights drift; refresh occasionally. GOOGL and GOOG are both listed because both share classes
# are separate top-20 holdings.
SPY_TOP20 = [
    "AAPL", "NVDA", "MSFT", "AMZN", "GOOGL", "AVGO", "META", "GOOG", "LLY", "TSLA",
    "BRK.B", "JPM", "MU", "AMD", "V", "XOM", "JNJ", "WMT", "MA", "ABBV",
]
SPY_TOP20_AS_OF = "2026-11-04"

# Default equity watchlist: the index itself (focus) + its top 20 constituents.
DEFAULT_EQUITY = " ".join(["SPY", *SPY_TOP20])
