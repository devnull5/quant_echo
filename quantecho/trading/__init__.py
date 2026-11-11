"""QuantEcho options trading (tastytrade). OFF by default and excluded from the Microsoft Store build.

Scope by design: LONG calls/puts only (max loss = premium paid), on an allowlist (default SPY, NVDA),
entered on the chart's closed-bar trend signals, with hard risk limits and a kill switch.

Modes:
  off      - nothing runs
  sim      - local paper trading; option prices are MODELED (Black-Scholes) unless real quotes are configured
  sandbox  - real orders to tastytrade's paper environment (api.cert.tastyworks.com)
  live     - real money; requires explicit typed confirmation, and per-order confirmation by default

This is not financial advice. Signals have no proven edge; test in sim/sandbox first.
"""
