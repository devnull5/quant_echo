"""Daily markdown journal of AI analyses and signals: ~/.quanteco_gpt/journal/YYYY-MM-DD.md"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

JOURNAL_DIR = Path.home() / ".quanteco_gpt" / "journal"


def journal_path(day: Optional[str] = None, base: Path = JOURNAL_DIR) -> Path:
    return base / f"{day or time.strftime('%Y-%m-%d')}.md"


def append(title: str, body: str, *, context: str = "", base: Path = JOURNAL_DIR) -> Path:
    path = journal_path(base=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"\n## {time.strftime('%H:%M:%S')} — {title}\n", body.strip() + "\n"]
    if context:
        parts.append("\n<details><summary>telemetry</summary>\n\n```\n" + context.strip() + "\n```\n</details>\n")
    with path.open("a", encoding="utf-8") as f:
        if f.tell() == 0:
            f.write(f"# QuantEcho journal {time.strftime('%Y-%m-%d')}\n")
        f.write("".join(parts))
    return path
