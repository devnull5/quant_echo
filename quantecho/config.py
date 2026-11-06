"""Settings: the user's saved settings file first, falling back to .env / environment.

Provider credentials live in `creds` (keys defined by each provider's Field list).
Secret fields are only written to disk when "Remember keys" is ticked.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # optional
    pass

from .llm import DEFAULT_HOST, DEFAULT_MODEL
from .providers import DEFAULT_PROVIDER, PROVIDERS, all_fields

SETTINGS_PATH = Path.home() / ".quanteco_gpt" / "settings.json"


@dataclass
class Settings:
    provider: str = DEFAULT_PROVIDER
    symbols: str = "SPY QQQ NVDA"
    focus: str = "SPY"
    interval: int = 20
    model: str = DEFAULT_MODEL
    ollama_host: str = DEFAULT_HOST
    remember: bool = True
    creds: Dict[str, str] = field(default_factory=dict)


_ENV = {
    "provider": "DATA_PROVIDER",
    "symbols": "SYMBOLS",
    "focus": "FOCUS",
    "interval": "INTERVAL",
    "model": "OLLAMA_MODEL",
    "ollama_host": "OLLAMA_HOST",
}


def _read_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def normalize_host(h: str) -> str:
    """Ollama's own OLLAMA_HOST often lacks a scheme or is a bind address like 0.0.0.0."""
    h = (h or DEFAULT_HOST).strip().rstrip("/")
    if not h.startswith(("http://", "https://")):
        h = "http://" + h
    h = h.replace("://0.0.0.0", "://127.0.0.1")
    if h.count(":") == 1:  # no port
        h += ":11434"
    return h


def _pick(saved, env_name: str):
    if saved not in (None, ""):
        return saved
    return os.getenv(env_name) or None


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    s = Settings()
    saved = _read_file(path)
    for name, env in _ENV.items():
        val = _pick(saved.get(name), env)
        if val is None:
            continue
        if name == "interval":
            try:
                val = max(1, int(val))
            except (TypeError, ValueError):
                continue
        setattr(s, name, str(val) if name != "interval" else val)
    if "remember" in saved:
        s.remember = bool(saved["remember"])
    if s.provider not in PROVIDERS:
        s.provider = DEFAULT_PROVIDER
    s.ollama_host = normalize_host(s.ollama_host)

    saved_creds = saved.get("creds") if isinstance(saved.get("creds"), dict) else {}
    for f in all_fields():
        # also accept old flat keys (schwab_app_key etc.) from earlier versions
        val = _pick(saved_creds.get(f.key) or saved.get(f.key), f.env)
        s.creds[f.key] = str(val) if val is not None else f.default
    return s


def save_settings(s: Settings, path: Path = SETTINGS_PATH) -> None:
    data = asdict(s)
    if not s.remember:
        secret = {f.key for f in all_fields() if f.secret}
        data["creds"] = {k: ("" if k in secret else v) for k, v in s.creds.items()}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
