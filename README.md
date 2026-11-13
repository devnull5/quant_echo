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

The default stock watchlist is **SPY plus its top 20 holdings** (AAPL, NVDA, MSFT, AMZN, GOOGL, AVGO,
META, GOOG, LLY, TSLA, BRK.B, JPM, MU, AMD, V, XOM, JNJ, WMT, MA, ABBV; weights as of 2026-11-04,
see `quantecho/watchlists.py`). Sources that charge per symbol automatically poll more slowly so a
21-symbol list stays inside their free tier.

On launch a picker offers three zero-hassle starts: **Crypto** (Coinbase, live, no signup),
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

### Local AI

QuantEcho's AI runs **on your PC**: nothing is sent to a cloud AI service. Two backends (⚙ Settings → Local AI):

**Built-in (default).** The Windows app ships its own engine, [llama.cpp](https://github.com/ggml-org/llama.cpp)
(MIT, Vulkan build: NVIDIA / AMD / Intel GPUs, CPU fallback) with **Qwen3 4B** (Apache-2.0). Nothing to install.
The engine starts on your first question, uses the best dedicated GPU (only as much VRAM as is free) and
shuts down with the app, even if the app crashes. Measured: RTX 3060 ≈ 80 tok/s (deep-think analysis ≈ 13 s);
CPU-only ≈ 16 tok/s on a desktop CPU (≈ 1 min), slower on laptops. If another AI app is holding your GPU
memory (e.g. Ollama), the built-in engine falls back to the CPU and gets much slower.

Running from source? `python packaging/fetch_ai.py` downloads the pinned, SHA-256-verified engine and model
into `build/` (≈ 2.6 GB, gitignored).

**Ollama (optional).** For bigger models: install from https://ollama.com, `ollama pull qwen3:14b`, then pick
Ollama in Settings. Commercially-usable models the picker lists first:

| Model | License | Notes |
|---|---|---|
| `qwen3:14b` | Apache-2.0 | best on 12 GB VRAM (~33 tok/s on an RTX 3060) |
| `qwen3:8b` | Apache-2.0 | ~2x faster |
| `qwen3:30b-a3b` | Apache-2.0 | MoE, ~20 GB GPU+RAM |
| `mistral-small3.2` | Apache-2.0 | 24B, ~16 GB |
| `granite3.3:8b` | Apache-2.0 | IBM |
| `gpt-oss:20b` | Apache-2.0 | ~14 GB |

Answers stream into the log. **Deep think** turns on the model's reasoning mode (slower, better on
tricky questions); **Analyze focus** always uses it. The AI sees a compact context: last price vs prev
close, regular-session open/high/low, VWAP anchored at 9:30, EMA9/20, RSI, MACD, relative volume,
the last hour of 5-minute bars, recent signals, and a **FACTS** block of code-computed readings
(above/below VWAP, RSI zone, support/resistance candidates…) so the model interprets rather than does math.

### Live chart

The focus symbol gets a live 1-minute candlestick chart (last 2 hours) with volume, **VWAP**, **EMA 9/20**
and rule-based markers, updated as each quote arrives:

| Marker | Rule |
|---|---|
| ▲ **B** (trend buy) | EMA9 crosses above EMA20 while price is above VWAP |
| ▼ **S** (trend sell) | EMA9 crosses below EMA20 while price is below VWAP |
| ▲ **b** (reversal buy) | RSI14 climbs back above 30 (leaving oversold) |
| ▼ **s** (reversal sell) | RSI14 drops back below 70 (leaving overbought) |

Markers appear only on closed candles and the same direction is suppressed for 5 bars. Hover a candle for
OHLCV and the reason for its marker. These are mechanical technical signals, **not trade recommendations**.

### Signals, auto-analyze, journal

- **⚡ Signals** (focus symbol) are logged on transitions only: VWAP cross, EMA9/20 cross,
  RSI overbought/oversold, MACD histogram flip, new session high/low. They're also fed to the AI.
- **Auto-analyze every N min** runs a deep-think analysis on a timer while polling is on.
- **📓 Journal**: every AI answer (with the telemetry it saw) is appended to
  `~/.quanteco_gpt/journal/YYYY-MM-DD.md`.
- Volume glitches (single bars >15x the session median, common in Yahoo 1-min data) are capped
  before VWAP/relative volume are computed.

## Options trading bot (tastytrade, experimental, source-only)

`quantecho/trading/` can trade **long calls and puts only** (max loss = premium paid) on an allowlist
(default **SPY, NVDA**), entering on the chart's closed-bar **trend** signals (B → call, S → put). It is
**off by default and not included in the Microsoft Store build.** Not financial advice: the signals have no
proven edge, so run it in SIM and sandbox for a long time before considering real money.

| Mode | What happens |
|---|---|
| `sim` | Local paper trading. Option prices are **modeled** (Black-Scholes, per-symbol IV) unless real quotes are configured. |
| `sandbox` | Real orders to tastytrade's paper environment (`api.cert.tastyworks.com`, resets daily). |
| `live` | Real money. Refuses to start without per-order confirmation. |

**Rules** (`strategy.py`): expiry nearest 7 DTE (2-14), ATM strike; exit at +50% / -40%, on the opposite trend
signal, before expiry (15:45 ET when expiring today/tomorrow) or after 5 days.
**Dynamic exit** (on by default): once the premium is up +15% a trailing stop arms and sells if the gain
falls 5 points off its best (e.g. ran to +30%, sells at +25%; confirmed on two checks so one bad quote can't
trigger it). While armed it also sells when the underlying closes back across EMA9 (the move is over).
**End of day** (on by default): every position is closed from 15:45 ET, so the bot is flat overnight
(untick "Close all by 15:45" to only flatten options expiring today/tomorrow). No orders are sent outside
09:30-16:00 ET. An unfilled exit is cancelled and re-priced only after the broker confirms the cancel; a
rejected exit backs off 30 s. On start the bot adopts option positions already held at the broker.
**Join trend at start** (on by default): if no crossover has fired yet when the bot starts (or the entry
window opens), it enters once per symbol when EMA9/EMA20 and VWAP already agree on a direction.
**Hard limits** (`risk.py`, not overridable by the strategy): $300 max premium per trade, 2 contracts, 2 open
positions, one per symbol, $300 daily loss limit (halts entries), 6 entries/day, max 10% bid/ask spread,
entries only 09:30-15:30 ET on weekdays. **Kill switch** cancels working orders, sells everything at the bid
and halts for the day.
**Order safety** (`tasty.py`): limit orders only; every order is **dry-run first** (any warning blocks it);
a unique `external-identifier` per order, and a timed-out submit is looked up, **never blindly resent**.
Every decision is journaled to `~/.quanteco_gpt/trades/YYYY-MM-DD.jsonl`.

**Using it:** click **📈 Trading** in the toolbar (source checkouts only). Pick a mode, enter credentials
(stored in Windows Credential Manager), use **Test** to verify the connection, set limits/strategy, then
**▶ Start bot**. The bot needs a *stock* data source (e.g. Yahoo, Alpaca or Simulated) selected in the main
window. With **Confirm every order** (always on for LIVE) each order pops up for your yes/no; unanswered
in 60 s = no. LIVE also requires typing `REAL MONEY`. Closing QuantEcho while positions are open asks first,
because nothing manages them after the bot stops.

**tastytrade setup** (OAuth2; the old username/password login was retired in 2026): create an OAuth
application and a grant at my.tastytrade.com (Manage → My Profile → API) to get a **client secret** and
**refresh token**. The sandbox needs its own sandbox account and credentials (developer.tastytrade.com/sandbox).
The sandbox's market data is static placeholder values (not real prices), so sandbox mode uses modeled
prices to choose limits unless you also provide read-only production credentials for real quotes.
Limit prices are rounded to each underlying's tick sizes from the chain (e.g. NVDA options: $0.05 at/above
$3, $0.01 below).

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
| `quantecho/llm.py` | AI clients: built-in engine + Ollama (streaming, thinking on/off) |
| `quantecho/engine.py` | bundled llama.cpp server: GPU pick, start/stop, dies with the app |
| `quantecho/chart.py` | live candle chart + buy/sell signal rules (pure functions are unit-tested) |
| `quantecho/selftest.py` | `--selftest [--online] [--ai]` checks for packaged builds |
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

## Windows app / Microsoft Store package

```powershell
.venv\Scripts\pip install pyinstaller pillow
powershell -ExecutionPolicy Bypass -File packaging\build_msix.ps1      # -> build\msix\QuantEcho_<ver>_x64.msix
powershell -ExecutionPolicy Bypass -File packaging\install_dev.ps1     # local test install (Developer Mode)
```

The build makes a standalone app (no Python needed), runs its built-in self-test
(`QuantEcho.exe --selftest`), and packs an MSIX that the Store signs on submission. Listing text,
certification notes and screenshots are in [`packaging/STORE_LISTING.md`](packaging/STORE_LISTING.md)
and `packaging/store/`. Privacy policy: [PRIVACY.md](PRIVACY.md).

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md), especially for adding a new data provider.

## License

[MIT](LICENSE) © Filip Mj (devnull5). Third-party models and data sources keep their own licenses/terms.
