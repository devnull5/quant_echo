"""Rotating log file + crash capture.

Log: ~/.quanteco_gpt/logs/quantecho.log (1 MB x 5). Messages from providers are already
key-redacted; this module adds a last-line redaction pass for anything that looks like a key.
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

LOG_DIR = Path.home() / ".quanteco_gpt" / "logs"
LOG_PATH = LOG_DIR / "quantecho.log"
log = logging.getLogger("quantecho")

_KEYISH = re.compile(r"((?:api[_-]?key|apikey|token|secret|authorization)[\"'=:\s]+(?:bearer\s+|token\s+|apikey\s+)?)"
                     r"[A-Za-z0-9._\-]{8,}", re.I)


class _Redact(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _KEYISH.sub(r"\1***", str(record.getMessage()))
        record.args = ()
        return True


def setup(path: Path = LOG_PATH, level: int = logging.INFO) -> Optional[Path]:
    """Idempotent. Returns the log path, or None if the file can't be opened (logging stays in-memory)."""
    if any(isinstance(h, RotatingFileHandler) for h in log.handlers):
        return path
    log.setLevel(level)
    log.propagate = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    except OSError:
        return None
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s: %(message)s"))
    h.addFilter(_Redact())
    log.addHandler(h)

    def _hook(exc_type, exc, tb):
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args):
        log.critical(f"Uncaught exception in thread {args.thread.name if args.thread else '?'}",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
    return path
