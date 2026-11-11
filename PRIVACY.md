# QuantEcho Privacy Policy

_Last updated: 2026-11-06_

QuantEcho is a free, open-source desktop app ([source code](https://github.com/devnull5/quant_echo)).
This policy explains what it does with information. **Short version: QuantEcho collects nothing about
you. There are no accounts, analytics, ads or tracking, and the developer receives no data.**

## What the app sends over the internet

- **Market-data requests** go **directly from your computer** to the data source *you* select
  (for example Coinbase, Alpaca, Twelve Data). They contain the ticker symbols you entered and,
  for sources that need one, **your own API key**. Those providers' own privacy policies and terms
  apply to those requests. QuantEcho's developer has no servers and never sees this traffic.
- **AI features run on your own computer.** The built-in AI is a local engine that QuantEcho starts
  on `127.0.0.1` (not reachable from other machines) and stops when the app closes. If you choose the
  optional Ollama backend, requests go to your Ollama (`127.0.0.1` by default, or the address you set).
  Your questions and market context are never sent to a cloud AI service.
- Links you click (for example "get a key ↗") open in your web browser.

## What the app stores on your computer

| What | Where | Notes |
|---|---|---|
| API keys (only if "Remember keys" is ticked) | Windows Credential Manager | Encrypted by Windows; never written to plain files |
| Settings (source, symbols, model…) | `%USERPROFILE%\.quanteco_gpt\settings.json` | No secrets |
| AI journal | `%USERPROFILE%\.quanteco_gpt\journal\` | Your questions, answers and the market data shown to the AI |
| Log files | `%USERPROFILE%\.quanteco_gpt\logs\` | App + AI-engine diagnostics; API keys are redacted |

All of it stays on your computer. Delete the `.quanteco_gpt` folder and the "quantecho" entries in
Credential Manager to remove everything. Uninstalling the app doesn't delete your journal, so you keep it
unless you remove that folder.

## Children

QuantEcho is not directed at children and collects no personal information from anyone.

## Changes and contact

Changes to this policy will be published in this file in the app's public repository, with a new date.
Questions: open an issue at <https://github.com/devnull5/quant_echo/issues>.
