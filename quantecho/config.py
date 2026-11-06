"""Settings: the user's saved settings file first, falling back to .env / environment.

Secrets (API keys) are only written to disk when "Remember keys" is ticked.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional
    pass

SETTINGS_PATH = Path.home() / ".quanteco_gpt" / "settings.json"
SECRET_FIELDS = ("schwab_app_key", "schwab_app_secret", "openai_key")


@dataclass
class Settings:
    symbols: str = "SPY QQQ NVDA"
    focus: str = "SPY"
    interval: int = 20
    model: str = "gpt-5"
    schwab_app_key: str = ""
    schwab_app_secret: str = ""
    schwab_callback: str = "https://127.0.0.1:8182"
    schwab_token_path: str = "tokens.json"
    openai_key: str = ""
    remember: bool = True


_ENV = {
    "symbols": "SYMBOLS",
    "focus": "FOCUS",
    "interval": "INTERVAL",
    "model": "AI_MODEL",
    "schwab_app_key": "SCHWAB_APP_KEY",
    "schwab_app_secret": "SCHWAB_APP_SECRET",
    "schwab_callback": "SCHWAB_CALLBACK_URL",
    "schwab_token_path": "SCHWAB_TOKEN_PATH",
    "openai_key": "OPENAI_API_KEY",
}


def _read_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    s = Settings()
    saved = _read_file(path)
    for f in fields(Settings):
        env_val = os.getenv(_ENV.get(f.name, ""), "") if f.name in _ENV else ""
        file_val = saved.get(f.name)
        val = file_val if file_val not in (None, "") else (env_val or None)
        if val is None:
            continue
        if f.type in ("int", int):
            try:
                val = int(val)
            except (TypeError, ValueError):
                continue
        elif f.type in ("bool", bool):
            val = bool(val)
        setattr(s, f.name, val)
    return s


def save_settings(s: Settings, path: Path = SETTINGS_PATH) -> None:
    data = asdict(s)
    if not s.remember:
        for k in SECRET_FIELDS:
            data[k] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
