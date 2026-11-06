# Contributing to QuantEcho

Thanks for helping! Bug reports, new data providers and UI improvements are all welcome.

## Dev setup

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[schwab]"     # macOS/Linux: .venv/bin/pip ...
python -m unittest discover -s tests -v
```

Tests use recorded response shapes and fakes. They must pass **without network access or API keys**.

## Ground rules

- **Never commit secrets.** `.env`, `tokens.json` and `*_tokens.json` are gitignored; keep it that way.
  Put new credential settings in a provider's `fields` so they're read from `.env` / the settings UI.
- **QuantEcho is read-only.** PRs that add order placement or account-modifying calls won't be merged
  into the core app.
- Error messages must not leak keys: use `providers.base.Http`, which redacts them.
- Keep `ruff check --select F,E9,B quantecho tests` clean.

## Adding a data provider

1. Create `quantecho/providers/<name>.py` with a class implementing
   `get_quotes(symbols) -> {SYM: Quote}` and `get_session_minute_bars(symbol) -> [bar, ...]`
   (bars are `{"t": epoch_ms, "open", "high", "low", "close", "volume"}`, oldest first; use
   `base.latest_session()`).
2. Export `SPEC = ProviderSpec(id=..., label=..., factory=..., fields=(...), notes=..., min_interval=...)`
   and set `min_interval` to respect the free tier's rate limit.
3. Register it in `quantecho/providers/__init__.py`.
4. Add a parser test in `tests/test_core.py` using a realistic payload from the provider's docs.

## Reporting bugs

Include the provider, model, OS/Python version and the log lines from the app. **Redact any keys.**
