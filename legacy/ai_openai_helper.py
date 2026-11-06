from __future__ import annotations
import os, json
from typing import Any, Dict, List, Optional, Iterable, cast

# --- OpenAI client + typed errors (narrow ImportError handling)
try:
    from openai import OpenAI
    from openai import (
        APIError,
        APIConnectionError,
        RateLimitError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
    )
except ImportError:
    OpenAI = None  # type: ignore
    # Safe fallbacks so the module still imports without the SDK installed
    APIError = APIConnectionError = RateLimitError = AuthenticationError = BadRequestError = InternalServerError = Exception  # type: ignore[misc,assignment]

# --- Typed message param (SDK >= 1.0) with ImportError-only fallback
try:
    from openai.types.chat import ChatCompletionMessageParam as _ChatCompletionMessageParam
except ImportError:
    _ChatCompletionMessageParam = Dict[str, Any]  # type: ignore[assignment]
ChatCompletionMessageParam = _ChatCompletionMessageParam  # re-export

# --- Typed response_format (SDK >= 1.0) with ImportError-only fallback
try:
    from openai.types.chat.completion_create_params import ResponseFormat as _ResponseFormat
except ImportError:
    _ResponseFormat = Dict[str, Any]  # type: ignore[assignment]
ResponseFormat = _ResponseFormat  # re-export



class OpenAIHelper:
    """
    Minimal helper for Chat Completions (OpenAI SDK >= 1.0) with proper typing.

    Usage:
        client = OpenAIHelper(model="gpt-5-nano")
        text = client.ask("Say hi")
        data = client.chat_json([{"role":"user","content":"Return {\"ok\":true} only"}])
    """

    def __init__(self, model: str = "gpt-5-nano", base_url: Optional[str] = None, temperature: float = 0.2):
        self.model = model or "gpt-5-nano"
        self.temperature = float(temperature)
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL") or None
        self._client = self._get_client()

    def _get_client(self):
        if OpenAI is None:
            return None
        try:
            if self.base_url:
                return OpenAI(base_url=self.base_url)
            return OpenAI()
        except (AuthenticationError, APIConnectionError, APIError, BadRequestError, InternalServerError):
            return None

    @staticmethod
    def _ensure_messages(messages: Any) -> List[ChatCompletionMessageParam]:
        """
        Normalize arbitrary input into a List[ChatCompletionMessageParam].

        Accepts either:
          - already-formed list of chat messages (dicts with 'role'/'content')
          - a plain string/any object -> wrapped as a single user message
        """
        if isinstance(messages, list) and all(isinstance(m, dict) and "role" in m for m in messages):
            # Trust caller, and cast so type checkers see the correct union type.
            return cast(List[ChatCompletionMessageParam], messages)

        # Build a minimal valid list[ChatCompletionMessageParam]
        return cast(List[ChatCompletionMessageParam], [
            {"role": "user", "content": str(messages)}
        ])

    def chat(
            self,
            messages: Iterable[ChatCompletionMessageParam] | Any,
            model: Optional[str] = None,
            temperature: Optional[float] = None,
    ) -> Optional[str]:
        """
        Generic chat call -> returns plain text string or None on error.
        """
        if self._client is None:
            return None

        # Normalize and explicitly type messages for the SDK
        msgs: List[ChatCompletionMessageParam] = (
            list(messages)  # already Iterable[ChatCompletionMessageParam]
            if isinstance(messages, list) and all(isinstance(m, dict) and "role" in m for m in messages)
            else self._ensure_messages(messages)
        )

        try:
            resp = self._client.chat.completions.create(
                model=model or self.model,
                # Explicit cast to the precise union the SDK expects; silences IDE false positives
                messages=cast(List[ChatCompletionMessageParam], msgs),  # type: ignore[arg-type]
                temperature=float(temperature) if temperature is not None else self.temperature,
            )
            choice = (resp.choices or [None])[0]
            return (
                choice.message.content.strip()
                if choice and getattr(choice, "message", None) and getattr(choice.message, "content", None)
                else None
            )
        except (AuthenticationError, RateLimitError, APIConnectionError, BadRequestError, InternalServerError,
                APIError):
            return None

    def ask(self, prompt: str, system: Optional[str] = None, **kw) -> Optional[str]:
        """
        Convenience for one-off prompts. Optionally include a system prompt.
        """
        messages: List[ChatCompletionMessageParam] = []
        if system:
            messages.append(cast(ChatCompletionMessageParam, {"role": "system", "content": str(system)}))
        messages.append(cast(ChatCompletionMessageParam, {"role": "user", "content": str(prompt)}))
        return self.chat(messages, **kw)

    def chat_json(
            self,
            messages: Iterable[ChatCompletionMessageParam] | Any,
            model: Optional[str] = None,
            temperature: Optional[float] = 0.1,
    ) -> Optional[Dict[str, Any]]:
        """
        Chat call that requests JSON object output. Returns dict or None.
        """
        if self._client is None:
            return None

        # Normalize to a properly-typed list for the SDK
        if isinstance(messages, list) and all(isinstance(m, dict) and "role" in m for m in messages):
            msgs: List[ChatCompletionMessageParam] = messages  # already well-formed
        else:
            msgs = self._ensure_messages(messages)

        try:
            resp = self._client.chat.completions.create(
                model=model or self.model,
                messages=cast(List[ChatCompletionMessageParam], msgs),  # type: ignore[arg-type]
                temperature=float(temperature) if temperature is not None else 0.1,
                response_format=cast(ResponseFormat, {"type": "json_object"}),
            )
            choice = (resp.choices or [None])[0]
            txt = (
                choice.message.content.strip()
                if choice and getattr(choice, "message", None) and getattr(choice.message, "content", None)
                else ""
            )
            if not txt:
                return None

            # Be forgiving of stray text around JSON
            start, end = txt.find("{"), txt.rfind("}")
            if start != -1 and end != -1:
                txt = txt[start: end + 1]

            return json.loads(txt)
        except (AuthenticationError, RateLimitError, APIConnectionError, BadRequestError, InternalServerError,
                APIError):
            return None

