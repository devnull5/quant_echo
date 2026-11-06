"""OpenAI Responses API client (stdlib only)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict

API_URL = "https://api.openai.com/v1/responses"

COACH = (
    "You are QuantEcho, a concise intraday market analyst.\n"
    "If telemetry is attached, analyze it for a 15-60 min bias; otherwise discuss setups and risk.\n"
    "Be concise and actionable. Never invent numbers that are not in the telemetry."
)

ANALYZE_TEMPLATE = (
    "Given the telemetry below, provide:\n"
    "1) One-sentence directional bias (bullish/bearish/neutral) with confidence %.\n"
    "2) 3-5 bullets on key signals (VWAP / RSI / MACD / momentum / volume).\n"
    "3) Key levels, one risk to the view, and what would invalidate it.\n\n"
    "{ctx}"
)


def _is_reasoning(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def build_request(prompt: str, model: str, max_tokens: int = 2500) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "instructions": COACH,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }
    if _is_reasoning(model):
        # Reasoning tokens count against max_output_tokens; keep effort low so
        # there is budget left for the visible answer.
        body["reasoning"] = {"effort": "low"}
    return body


def extract_text(resp: Dict[str, Any]) -> str:
    if isinstance(resp.get("output_text"), str) and resp["output_text"].strip():
        return resp["output_text"].strip()
    parts = []
    for item in resp.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for seg in item.get("content") or []:
            if isinstance(seg, dict) and seg.get("type") == "output_text" and seg.get("text"):
                parts.append(seg["text"].strip())
    if parts:
        return "\n".join(parts)
    if resp.get("status") == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason", "unknown")
        return f"(incomplete response: {reason} — try again or raise max_output_tokens)"
    return "(no answer)"


def ask(prompt: str, *, model: str, api_key: str, timeout: int = 90) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "QuantEcho",
    }
    if os.getenv("OPENAI_ORG"):
        headers["OpenAI-Organization"] = os.environ["OPENAI_ORG"]
    if os.getenv("OPENAI_PROJECT"):
        headers["OpenAI-Project"] = os.environ["OPENAI_PROJECT"]

    req = urllib.request.Request(
        API_URL, data=json.dumps(build_request(prompt, model)).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return extract_text(json.loads(r.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        try:
            msg = json.loads(detail).get("error", {}).get("message") or detail
        except (ValueError, AttributeError):
            msg = detail
        return f"[OpenAI HTTP {e.code}] {msg.strip() or e.reason}"
    except urllib.error.URLError as e:
        return f"[OpenAI network] {e.reason}"
    except (TimeoutError, ValueError) as e:
        return f"[OpenAI] {type(e).__name__}: {e}"
