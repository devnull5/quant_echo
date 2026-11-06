"""Settings: the user's saved settings file first, falling back to .env / environment.

Provider credentials live in `creds` (keys defined by each provider's Field list).
Secret fields are stored in the OS keyring (Windows Credential Manager / macOS Keychain /
Secret Service) only when "Remember keys" is ticked, and never in the JSON settings file.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

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
        # precedence: OS keyring -> settings file (older versions stored secrets there) -> env/.env -> default
        stored = secret_get(f.key) if f.secret else None
        val = _pick(stored or saved_creds.get(f.key) or saved.get(f.key), f.env)
        s.creds[f.key] = str(val) if val is not None else f.default
    return s


# ---------------------------------------------------------------- OS keyring for secrets

KEYRING_SERVICE = "quantecho"
_keyring: Any = None  # tests may inject a fake


def _kr() -> Any:
    global _keyring
    if _keyring is None:
        try:
            import keyring  # Windows Credential Manager / macOS Keychain / Secret Service

            _keyring = keyring
        except ImportError:
            _keyring = False
    return _keyring or None


def keyring_available() -> bool:
    kr = _kr()
    if not kr:
        return False
    try:
        from keyring.backends import fail  # type: ignore

        return not isinstance(kr.get_keyring(), fail.Keyring)
    except Exception:
        return True  # injected fake or unknown backend: assume usable


def secret_get(name: str) -> Optional[str]:
    kr = _kr()
    if not kr:
        return None
    try:
        return kr.get_password(KEYRING_SERVICE, name) or None
    except Exception:
        return None


def secret_set(name: str, value: str) -> bool:
    kr = _kr()
    if not kr:
        return False
    try:
        if value:
            kr.set_password(KEYRING_SERVICE, name, value)
        else:
            try:
                kr.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass  # nothing stored
        return True
    except Exception:
        return False


def save_settings(s: Settings, path: Path = SETTINGS_PATH) -> List[str]:
    """Write settings. Secret fields go to the OS keyring (when 'remember' is on) and are NEVER written
    to the JSON file. Returns the names of secrets that could not be stored securely."""
    data = asdict(s)
    secret_keys = {f.key for f in all_fields() if f.secret}
    data["creds"] = {k: ("" if k in secret_keys else v) for k, v in s.creds.items()}
    for k in secret_keys:  # scrub legacy flat keys from older versions
        data.pop(k, None)
    failed: List[str] = []
    for k in secret_keys:
        val = s.creds.get(k, "") if s.remember else ""
        if not secret_set(k, val) and val:
            failed.append(k)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return failed
