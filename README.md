# QuantEcho

Desktop dashboard (Tk) that streams Schwab market data for a watchlist, computes
intraday TA on a focus symbol (VWAP, RSI-14, MACD 12/26/9), and asks an OpenAI
model for a read on it. **Read-only:** it never places orders.

## Setup

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env        # then fill in your keys
```

You need a Schwab developer app (App Key + Secret) whose callback URL exactly
matches `SCHWAB_CALLBACK_URL` (default `https://127.0.0.1:8182`).

## Run

```bash
python -m quantecho.auth      # one-time browser login -> tokens.json (redo every 7 days)
python -m quantecho           # launch the dashboard
```

- **⚙ Keys** shows the credentials row. Keys are written to
  `~/.quanteco_gpt/settings.json` only if "Remember keys" is ticked; otherwise use `.env`.
- Double-click a watchlist row to switch the focus symbol.
- **Ask** sends your question (plus focus telemetry if ticked); **Analyze focus** asks for a
  structured bias / signals / levels / risk read.

## Layout

| Path | What |
|---|---|
| `quantecho/schwab_provider.py` | Schwab quotes + 1-min candles (latest session) |
| `quantecho/ta.py` | EMA, RSI, MACD, VWAP |
| `quantecho/telemetry.py` | thread-safe store + LLM context text |
| `quantecho/poller.py` | background polling thread |
| `quantecho/ai.py` | OpenAI Responses API (stdlib HTTP) |
| `quantecho/gui.py` | Tk dashboard |
| `quantecho/brackets.py` | bracket/exit-ladder logic (not wired in yet) |
| `legacy/` | previous single-file versions (Polygon, Ollama, etc.) kept for reference |

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use a fake Schwab client, so no network or credentials are needed.
