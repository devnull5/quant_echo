"""Local LLM via Ollama (https://ollama.com). No API key; runs on this machine.

Default model: Qwen3 14B (Apache-2.0 — free for commercial use). Fits a 12 GB GPU.
    ollama pull qwen3:14b      # or qwen3:8b for speed, qwen3:30b-a3b, mistral-small3.2, granite3.3 ...
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:14b"

# Open-weight models whose licenses allow commercial use (shown first in the model picker).
RECOMMENDED = {
    "qwen3:14b": "Apache-2.0 · best quality on 12 GB GPUs",
    "qwen3:8b": "Apache-2.0 · ~2x faster, slightly weaker",
    "qwen3:30b-a3b": "Apache-2.0 · MoE, needs ~20 GB (GPU+RAM)",
    "mistral-small3.2": "Apache-2.0 · 24B, needs ~16 GB",
    "granite3.3:8b": "Apache-2.0 · IBM",
    "gpt-oss:20b": "Apache-2.0 · OpenAI open-weight, needs ~14 GB",
}

SYSTEM = (
    "You are QuantEcho, a disciplined intraday market analyst.\n"
    "Rules:\n"
    "- Base every claim on the telemetry provided. Quote the actual numbers. Never invent prices or news.\n"
    "- The FACTS section is computed by code and is correct; build on it, never contradict it.\n"
    "- Key levels must be prices that appear in the telemetry (VWAP, EMAs, session high/low, 5-min highs/lows).\n"
    "- If data is missing or stale (pre/post-market, prior session), say so and lower confidence.\n"
    "- Separate observation (what the data shows) from interpretation (what it might mean).\n"
    "- Be concise: short bullets, no filler, no disclaimers beyond one line."
)

ANALYZE_TEMPLATE = (
    "Analyze the telemetry for a 15-60 minute horizon. Reply in exactly this format:\n\n"
    "**Bias:** bullish / bearish / neutral — confidence NN%\n"
    "**Signals:**\n- (3-5 bullets citing VWAP, EMA9/20, RSI, MACD, 5-min structure, relative volume)\n"
    "**Levels:** support … / resistance … (from session high/low, VWAP, recent 5-min pivots)\n"
    "**Invalidation:** the specific price/condition that would flip the view\n"
    "**Risk:** one key risk to this read\n\n"
    "{ctx}"
)

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


@dataclass
class Stats:
    model: str = ""
    eval_tokens: int = 0
    tok_per_s: float = 0.0
    think_tokens: int = 0  # chars-based estimate of hidden reasoning


def build_request(prompt: str, model: str, *, think: bool = False, stream: bool = False,
                  max_tokens: Optional[int] = None) -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "stream": stream,
        "think": think,
        "keep_alive": "30m",
        "options": {"temperature": 0.3 if not think else 0.6, "num_ctx": 8192,
                    "num_predict": max_tokens or (4096 if think else 1024)},
    }


def extract_text(resp: Dict[str, Any]) -> str:
    msg = (resp.get("message") or {}).get("content") or resp.get("response") or ""
    msg = _THINK.sub("", msg).strip()  # models that ignore `think` emit inline <think> blocks
    return msg or "(no answer)"


def _friendly(e: Exception, host: str, model: str) -> str:
    if isinstance(e, urllib.error.HTTPError):
        detail = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except (ValueError, AttributeError):
            pass
        if e.code == 404 or "not found" in str(detail).lower():
            return f"[Ollama] Model '{model}' not installed. Run:  ollama pull {model}"
        if "does not support thinking" in str(detail):
            return f"[Ollama] '{model}' has no thinking mode — untick 'Deep think'."
        return f"[Ollama HTTP {e.code}] {detail}"
    if isinstance(e, urllib.error.URLError):
        return f"[Ollama] Can't reach {host} ({e.reason}). Is Ollama running?"
    if "not found" in str(e).lower() and "model" in str(e).lower():
        return f"[Ollama] Model '{model}' not installed. Run:  ollama pull {model}"
    return f"[Ollama] {type(e).__name__}: {e}"


def _open(host: str, body: Dict[str, Any], timeout: float):
    req = urllib.request.Request(f"{host}/api/chat", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _post(url: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ask(prompt: str, *, model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST, think: bool = False,
        timeout: float = 600) -> str:
    """Blocking, non-streaming call. Returns the answer text or a friendly error string."""
    host = (host or DEFAULT_HOST).rstrip("/")
    try:
        return extract_text(_post(f"{host}/api/chat", build_request(prompt, model, think=think), timeout))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return _friendly(e, host, model)


def ask_stream(prompt: str, on_token: Callable[[str], None], *, model: str = DEFAULT_MODEL,
               host: str = DEFAULT_HOST, think: bool = False, on_thinking: Optional[Callable[[int], None]] = None,
               timeout: float = 600) -> tuple[str, Stats]:
    """Stream the answer. Calls on_token(text_chunk) for visible output and on_thinking(chars_so_far)
    while the model reasons. Returns (full_text, stats); on failure full_text is a friendly error."""
    host = (host or DEFAULT_HOST).rstrip("/")
    out: List[str] = []
    stats = Stats(model=model)
    thinking_chars = 0
    in_inline_think = False
    try:
        with _open(host, build_request(prompt, model, think=think, stream=True), timeout) as r:
            for raw in r:
                if not raw.strip():
                    continue
                chunk = json.loads(raw.decode("utf-8"))
                if chunk.get("error"):
                    raise ValueError(chunk["error"])
                msg = chunk.get("message") or {}
                th = msg.get("thinking") or ""
                if th:
                    thinking_chars += len(th)
                    if on_thinking:
                        on_thinking(thinking_chars)
                text = msg.get("content") or ""
                # handle inline <think>…</think> from models/servers without a separate field
                if "<think>" in text:
                    in_inline_think = True
                    text = text.split("<think>", 1)[0]
                if in_inline_think:
                    if "</think>" in (msg.get("content") or ""):
                        in_inline_think = False
                        text = (msg.get("content") or "").split("</think>", 1)[1]
                    else:
                        thinking_chars += len(msg.get("content") or "")
                        if on_thinking:
                            on_thinking(thinking_chars)
                        text = ""
                if text:
                    if not out:
                        text = text.lstrip()
                    out.append(text)
                    on_token(text)
                if chunk.get("done"):
                    n, ns = chunk.get("eval_count") or 0, chunk.get("eval_duration") or 0
                    stats.eval_tokens = int(n)
                    stats.tok_per_s = n / (ns / 1e9) if ns else 0.0
                    stats.think_tokens = thinking_chars // 4
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        return _friendly(e, host, model), stats
    return ("".join(out).strip() or "(no answer)"), stats


def preload(model: str = DEFAULT_MODEL, host: str = DEFAULT_HOST, timeout: float = 120) -> bool:
    """Load the model into GPU memory now so the first real question isn't slow."""
    host = (host or DEFAULT_HOST).rstrip("/")
    body = {"model": model, "messages": [], "keep_alive": "30m",
            "options": {"num_ctx": build_request("", model)["options"]["num_ctx"]}}
    try:
        _post(f"{host}/api/chat", body, timeout)
        return True
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return False


def list_models(host: str = DEFAULT_HOST, timeout: float = 3) -> List[str]:
    """Installed model names, or [] if Ollama isn't reachable."""
    try:
        with urllib.request.urlopen(f"{(host or DEFAULT_HOST).rstrip('/')}/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return sorted(m["name"] for m in data.get("models", []) if m.get("name"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError, KeyError):
        return []
