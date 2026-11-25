"""Gemini access for the LLM judge, over the official REST API with httpx.

No new dependency: httpx is already required. The API key travels only in the
`x-goog-api-key` header; it is never put in a URL, logged or echoed, and errors carry
only a status code or exception type name, never the response body.

A Gemini *subscription* does not by itself grant API quota; the API needs its own key
and billing in Google AI Studio / Google Cloud. This module has not been called live
in this repository: see docs/GEMINI_JUDGE.md.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx

from api.llm import LLMError, LLMReply

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# The judge call is not streamed: the whole answer (including any model "thinking") must arrive
# before the first byte, so the read timeout has to cover the full generation. 20 s (the chat
# default, LLM_TIMEOUT_SECONDS) proved too short in practice; 60 s is the default for judge calls
# only. Override with GEMINI_JUDGE_TIMEOUT_SECONDS. Not tied to the voice agent in any way.
DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_TIMEOUT_SECONDS = 600.0
_RETRY_STATUSES = {429, 500, 502, 503, 504}
# Timeouts after the request was sent: never retried (see generate()).
_NOT_RETRIED = (httpx.ReadTimeout, httpx.WriteTimeout)


_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,99}$")
_PLACEHOLDER_WORDS = ("model-id", "model_id", "modelid", "your-model", "your_model", "placeholder",
                      "changeme", "change-me", "replace-me", "example", "from-ai-studio", "todo", "xxx")
SETUP_HINT = ("set GEMINI_JUDGE_MODEL to a model id your API key can use "
              "(look it up in Google AI Studio), e.g. a line `GEMINI_JUDGE_MODEL=<id>` in .env "
              "with the angle brackets removed")


def model_id_problem(model: str | None) -> str | None:
    """Why this value cannot be a model id, or None when it looks like one.

    This catches obvious mistakes only (empty, unfilled placeholder, characters that cannot be
    part of an id). It does not prove the id exists or that the key may use it; only the API can.
    The id is never altered: a valid-looking value is passed through unchanged.
    """
    if model is None or not str(model).strip():
        return "GEMINI_JUDGE_MODEL is empty"
    value = str(model)
    if value != value.strip() or not _MODEL_ID.match(value):
        return "GEMINI_JUDGE_MODEL is not a valid model id (letters, digits, '.', '_', '-' only; remove angle brackets, quotes and spaces)"
    lowered = value.lower()
    if any(word in lowered for word in _PLACEHOLDER_WORDS):
        return "GEMINI_JUDGE_MODEL looks like an unfilled placeholder"
    return None


class GeminiJudgeLLM:
    """Matches the `generate(system_prompt, history)` shape the existing Judge expects."""

    provider = "gemini"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = 1024,
        max_retries: int = 2,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("a Gemini API key is required")
        problem = model_id_problem(model)
        if problem:
            raise ValueError(f"{problem}; {SETUP_HINT}")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._max_retries = max_retries
        self._url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"x-goog-api-key": api_key, "content-type": "application/json"},
            transport=transport,
        )

    async def generate(self, system_prompt, history, tools=None, tool_choice=None) -> LLMReply:
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "model" if m.get("role") == "assistant" else "user",
                    "parts": [{"text": str(m.get("content") or "")}],
                }
                for m in history
            ],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self._max_output_tokens,
                "responseMimeType": "application/json",
            },
        }
        start = time.perf_counter()
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._url, json=body)
            except _NOT_RETRIED as error:
                # The request reached Google and may already be running (and billed). Sending it
                # again would only double the cost of a slow answer, so a read/write timeout is final.
                raise LLMError(f"gemini_timeout_{self.timeout_seconds:g}s_{attempt + 1}_attempt_no_retry") from None
            except httpx.HTTPError as error:
                # Connection-level failures (the request never got an answer) are safe to retry.
                if attempt >= self._max_retries:
                    raise LLMError(f"{type(error).__name__}_after_{attempt + 1}_attempts") from None
                await self._sleep(2**attempt)
                continue
            if response.status_code in _RETRY_STATUSES and attempt < self._max_retries:
                await self._sleep(2**attempt)
                continue
            if response.status_code != 200:
                suffix = f"_after_{attempt + 1}_attempts" if attempt else ""
                raise LLMError(f"gemini_http_{response.status_code}{suffix}")
            break
        try:
            data = response.json()
        except ValueError:
            raise LLMError("gemini_bad_json_envelope") from None
        if (data.get("promptFeedback") or {}).get("blockReason"):
            raise LLMError("gemini_blocked_prompt")
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts).strip()
        except (KeyError, IndexError, TypeError):
            raise LLMError("gemini_empty_response") from None
        if not text:
            raise LLMError("gemini_empty_response")
        usage = data.get("usageMetadata") or {}
        return LLMReply(
            text=text,
            latency_ms=int((time.perf_counter() - start) * 1000),
            usage={
                "prompt_tokens": usage.get("promptTokenCount"),
                "completion_tokens": usage.get("candidatesTokenCount"),
                "total_tokens": usage.get("totalTokenCount"),
            },
        )

    async def close(self) -> None:
        await self._client.aclose()


def build_gemini_judge_llm(settings, model_override: str | None = None) -> GeminiJudgeLLM:
    """From project settings. Raises ValueError (no secrets in the message) when unconfigured."""
    key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else ""
    return GeminiJudgeLLM(
        api_key=key,
        model=model_override or settings.gemini_judge_model or "",
        # Judge-only setting. The chat timeout (LLM_TIMEOUT_SECONDS) is deliberately not used here.
        timeout_seconds=getattr(settings, "gemini_judge_timeout_seconds", None) or DEFAULT_TIMEOUT_SECONDS,
    )
