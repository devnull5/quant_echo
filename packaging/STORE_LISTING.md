# Microsoft Store listing — QuantEcho (copy/paste into Partner Center)

## Basics

- **Product name:** QuantEcho
- **Category:** Business (or Personal finance)
- **Pricing:** Free
- **Privacy policy URL:** https://github.com/devnull5/quant_echo/blob/main/PRIVACY.md
- **Website:** https://github.com/devnull5/quant_echo
- **Support contact:** https://github.com/devnull5/quant_echo/issues
- **Package:** `build\msix\QuantEcho_<version>_x64.msix` from `packaging\build_msix.ps1` (run it with
  the Identity Name / Publisher / Publisher display name shown in Partner Center → Product identity)

## Short description (≤ 100 characters)

Live charts, buy/sell signal markers and a private AI analyst that runs on your PC. Read-only.

## Description

QuantEcho is a free, open-source market dashboard for watching stocks and crypto in real time, with
intraday indicators, automatic signal alerts and an AI analyst that runs entirely on your own PC.

• Pick your data source: live crypto from Coinbase with no signup, US stocks with your own free key
  (Alpaca, Twelve Data, Tiingo, Finnhub and more), or an offline demo mode.
• Watch a full watchlist (SPY and its top 20 holdings by default) with price, bid/ask, day change and volume.
• Live candlestick chart with VWAP and EMA 9/20 overlays, volume, and buy/sell markers from clear,
  documented rules (EMA crosses confirmed by VWAP, RSI reversals). Hover any candle for details.
• Indicators on your focus symbol: VWAP, EMA 9/20, RSI 14, MACD, relative volume, day range.
• Signal alerts when price crosses VWAP, EMAs cross, RSI goes overbought/oversold, MACD flips or a new
  high/low is set.
• Ask questions or get a structured analysis (bias, key signals, support/resistance, invalidation, risk)
  from a built-in AI (Qwen3 4B) that runs on your own PC. Nothing to install, no account, and your data
  never leaves your computer. Fastest with a graphics card; also works on CPU.
• A daily journal saves every AI analysis with the data it saw.
• Your API keys are stored securely in Windows Credential Manager.

QuantEcho is read-only: it never places trades or connects to a brokerage account to trade.

Power users can switch the AI to Ollama (ollama.com) to use larger models.

Not financial advice. Market data may be delayed or incomplete and AI output can be wrong; you are solely
responsible for any trading decisions. Each data source has its own terms of use.

## What's new in this version

First Microsoft Store release: live crypto (no signup), multi-source stocks, indicators, signals,
local-AI analysis, journal.

## Features (Partner Center "Product features", up to 20)

- Live crypto prices with no signup (Coinbase)
- US stock quotes with your own free API key
- Offline demo mode
- VWAP, EMA, RSI and MACD on any symbol
- Automatic signal alerts
- Live candlestick chart with buy/sell signal markers
- Built-in private AI analyst (runs on your PC, no setup)
- Daily analysis journal
- Keys stored in Windows Credential Manager
- Read-only, never trades
- Free and open source (MIT)

## Search terms (up to 7)

stock market, crypto, technical analysis, trading dashboard, VWAP, RSI, AI analyst

## System requirements / notes

- Windows 10 1809+ or Windows 11, x64. Internet connection for live data.
- About 3.5 GB disk space (the built-in AI model is ~2.5 GB) and 8 GB RAM.
- AI is fastest with a graphics card (NVIDIA, AMD or Intel; ~80 tokens/s on an RTX 3060, an analysis in
  ~15 s). Without one it runs on the CPU (an analysis can take 1-2 minutes).

## Notes for certification (Partner Center → Submission options)

> QuantEcho is a read-only market dashboard. To test without any account: on launch choose
> "Coinbase (crypto, no key)" — prices, indicators and signals appear within a few seconds — or
> "Simulated (offline demo)", which needs no internet. Other sources require the user's own free
> API key from that provider. The AI works with no setup: after choosing a source, click
> "Analyze focus" (the first answer takes a few seconds while the built-in engine starts; on PCs without
> a graphics card it can take a minute or two). No sign-in, no in-app purchases.

## Screenshots

Desktop screenshots are in `packaging/store/` (1600×900). Suggested captions:
1. Live watchlist with indicators and the AI analysis panel
2. Pick a data source: crypto with no signup, stocks with a free key, or demo mode
3. Structured AI analysis: bias, signals, levels, invalidation, risk

## Age rating (IARC questionnaire)

No violence, sexual content, gambling, user-to-user communication, location sharing or purchases.
Expected rating: 3+ / Everyone.
