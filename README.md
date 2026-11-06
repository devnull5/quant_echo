# QuantEcho

Desktop dashboard (Tk) that polls market data for a watchlist from **the data source you choose**,
computes intraday TA on a focus symbol (VWAP, RSI-14, MACD 12/26/9), and asks a **local AI model
(Ollama)** for a read on it. **Read-only:** it never places orders.

## Data sources

Pick one from the **Source** dropdown. Keys go in **⚙ Settings** or `.env`.

| Source | Key? | Quotes | 1-min bars / TA | Free-tier notes |
|---|---|---|---|---|
| Yahoo Finance | none | ✓ | ✓ | Unofficial endpoint; default |
| Charles Schwab | app key + secret | ✓ real-time, bid/ask | ✓ | Run `python -m quantecho.auth`; token lasts 7 days |
| Alpaca | key id + secret | ✓ bid/ask | ✓ | Free = IEX feed; `sip` needs paid plan |
| Tiingo (IEX) | token | ✓ bid/ask | ✓ | ~50 req/hr free → polls every 60s |
| Finnhub | key | ✓ | paid only | 60 req/min |
| Polygon.io / Massive | key | paid (free = prev close) | ✓ (delayed on free) | 5 req/min free |
| Twelve Data | key | ✓ | ✓ | 8 credits/min (1 per symbol) |
| Alpha Vantage | key | ✓ | ✓ | 25 requests/**day** free |
| Simulated | none | fake | fake | Offline demo / UI testing |

The app automatically slows polling to each source's free-tier limit.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # optional: fill in keys for the sources you use
```

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
- Keys are saved to `~/.quanteco_gpt/settings.json` only when "Remember keys" is ticked.

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
