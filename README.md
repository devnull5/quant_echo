# QuantEcho

[![CI](https://github.com/devnull5/quant_echo/actions/workflows/ci.yml/badge.svg)](https://github.com/devnull5/quant_echo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)

Desktop dashboard (Tk) that polls market data for a watchlist from **the data source you choose**,
computes intraday TA on a focus symbol (VWAP, EMA9/20, RSI-14, MACD 12/26/9), flags signals, and
asks a **local AI model (Ollama)** for a read on it, so no cloud AI account is needed and nothing
leaves your machine except market-data requests. **Read-only:** it never places orders.

> **Disclaimer.** QuantEcho is an educational/research tool, **not financial advice**. AI output can
> be wrong, and market data may be delayed, incomplete or glitchy. You are solely responsible for any
> trading decisions. Each data source has its own terms of use (the Yahoo endpoint is unofficial and
> meant for personal use); check them before relying on or redistributing data.

## Data sources

On first launch a picker offers three zero-hassle starts: **Crypto** (Coinbase, live, no signup),
**US stocks** (free Alpaca key, ~2 min) or **Demo**. Change source any time from the **Source**
dropdown; keys go in **⚙ Settings** (saved to your OS keychain) or `.env`.

| Source | Key? | Quotes | 1-min bars / TA | Free-tier notes | Verified live |
|---|---|---|---|---|---|
| Charles Schwab | app key + secret | ✓ real-time, bid/ask | ✓ | Run `python -m quantecho.auth`; token lasts 7 days | parser vs. API docs |
| Alpaca | key id + secret | ✓ bid/ask | ✓ | Free = IEX feed; `sip` needs paid plan | endpoint + auth errors |
| Tiingo (IEX) | token | ✓ bid/ask | ✓ | ~50 req/hr free → polls every 60s | endpoint + auth errors |
| Finnhub | key | ✓ | paid only | 60 req/min | endpoint + auth errors |
| Polygon.io / Massive | key | paid (free = prev close) | ✓ (delayed on free) | 5 req/min free | endpoint + auth errors |
| Twelve Data | key | ✓ | ✓ | 8 credits/min (1 per symbol) | ✓ quotes + bars (demo key) |
| Alpha Vantage | key | ✓ | ✓ | 25 requests/**day** free | ✓ quote + bar format (demo key) |
| **Coinbase** (crypto) | none | ✓ real-time, bid/ask, 24h % | ✓ (UTC day) | Official public API, ~10 req/s | ✓ quotes + bars |
| Simulated | none | fake | fake | Offline demo / UI testing (default) | n/a |
| Yahoo Finance ⚠️ | none | ✓ | ✓ | **Personal use only**: unofficial endpoint; hidden unless you tick *Show personal-use-only sources* | ✓ quotes + bars |

**Why is Yahoo hidden?** It has no official API and its terms don't allow redistributing its data, so
QuantEcho doesn't offer it by default. You can still enable it for yourself in ⚙ Settings. For stocks,
use a free key from Alpaca, Twelve Data, Tiingo or Finnhub: you sign up, you agree to their terms,
and the data flows straight to you.

"Endpoint + auth errors" means the real API was hit and its error responses are handled, but no
real data has been parsed yet. If you use one of those, please open an issue with what you see.

**Reliability.** Polling is throttled to each source's free-tier limit. On errors it backs off
exponentially (up to 10 min), honours `Retry-After` on HTTP 429, backs off fully on bad keys
(401/403), logs a failure once rather than every cycle, and reports when it recovers.

## Setup

```bash
git clone https://github.com/devnull5/quant_echo.git
cd quant_echo
python -m venv .venv
.venv\Scripts\pip install -e ".[schwab]"     # macOS/Linux: .venv/bin/pip install -e ".[schwab]"
copy .env.example .env                        # optional: keys for the sources you use (cp on macOS/Linux)
```

Drop `[schwab]` if you don't use Schwab. Yahoo and Simulated work with no keys at all.
Tk ships with Python on Windows/macOS; on Linux install `python3-tk`.

### Local AI (Ollama)

1. Install from https://ollama.com (Windows: `winget install Ollama.Ollama`).
2. Pull the default model: `ollama pull qwen3:14b`
3. In the app, **⚙ Settings → Model** lists what's installed (↻ to refresh).

Default is **Qwen3 14B** — Apache-2.0 (free for commercial use), fits a 12 GB GPU.
Other commercially-usable options the picker lists first:

| Model | License | Notes |
|---|---|---|
| `qwen3:14b` | Apache-2.0 | default, best on 12 GB VRAM |
| `qwen3:8b` | Apache-2.0 | ~2x faster |
| `qwen3:30b-a3b` | Apache-2.0 | MoE, ~20 GB GPU+RAM |
| `mistral-small3.2` | Apache-2.0 | 24B, ~16 GB |
| `granite3.3:8b` | Apache-2.0 | IBM |
| `gpt-oss:20b` | Apache-2.0 | ~14 GB |

(Llama and Gemma are usable commercially too but under custom licenses with extra terms.)

The model is preloaded onto the GPU at startup. On an RTX 3060: ~33 tok/s; a quick Ask takes a few
seconds, a deep-think Analyze ~25-35 s. Answers stream into the log. **Deep think** turns on the model's reasoning mode (slower, better on
tricky questions); **Analyze focus** always uses it. The AI sees a compact context: last price vs prev
close, regular-session open/high/low, VWAP anchored at 9:30, EMA9/20, RSI, MACD, relative volume,
the last hour of 5-minute bars, recent signals, and a **FACTS** block of code-computed readings
(above/below VWAP, RSI zone, distance to high/low…) so the model interprets rather than does math.

### Signals, auto-analyze, journal

- **⚡ Signals** (focus symbol) are logged on transitions only: VWAP cross, EMA9/20 cross,
  RSI overbought/oversold, MACD histogram flip, new session high/low. They're also fed to the AI.
- **Auto-analyze every N min** runs a deep-think analysis on a timer while polling is on.
- **📓 Journal**: every AI answer (with the telemetry it saw) is appended to
  `~/.quanteco_gpt/journal/YYYY-MM-DD.md`.
- Volume glitches (single bars >15x the session median, common in Yahoo 1-min data) are capped
  before VWAP/relative volume are computed.

## Run

```bash
python -m quantecho
```

- Double-click a watchlist row to switch the focus symbol.
- **Ask** sends your question (plus focus telemetry if ticked); **Analyze focus** asks for a
  structured bias / signals / levels / risk read.
- **Keys** with "Remember keys" ticked go to your OS keychain (Windows Credential Manager, macOS
  Keychain, Linux Secret Service), never to the settings file. If no keychain exists, use `.env`.
- **📄 Log** opens `~/.quanteco_gpt/logs/quantecho.log` (rotating, 5 x 1 MB, keys redacted):
  provider starts, errors, backoffs, AI timings and any crash tracebacks.

## Layout

| Path | What |
|---|---|
| `quantecho/providers/` | one module per data source + `base.py` (interface, HTTP, time utils) |
| `quantecho/llm.py` | Ollama client |
| `quantecho/ta.py` | EMA, RSI, MACD, VWAP |
| `quantecho/telemetry.py` | thread-safe store + LLM context text |
| `quantecho/poller.py` | background polling thread |
| `quantecho/gui.py` | Tk dashboard |
| `quantecho/auth.py` | Schwab browser login → token file |
| `quantecho/brackets.py` | bracket/exit-ladder logic (not wired in) |
| `legacy/` | previous single-file versions, kept for reference |

**Adding a source:** create `quantecho/providers/<name>.py` with a class implementing
`get_quotes` / `get_session_minute_bars` and a `SPEC = ProviderSpec(...)`, then list it in
`providers/__init__.py`.

## Tests

```bash
python -m unittest discover -s tests -v
```

Every provider is tested against recorded response shapes — no network or keys needed.

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md), especially for adding a new data provider.

## License

[MIT](LICENSE) © Filip Mj (devnull5). Third-party models and data sources keep their own licenses/terms.
