import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from groq import AsyncGroq

from api.tracing import llm_inputs, llm_outputs, traced


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str

    def to_message_part(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class LLMReply:
    text: str
    latency_ms: int
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int | None] | None = None  # provider token counts when reported


class LLMError(Exception):
    """LLM provider se jawab nahi mila."""


class LLMClient(Protocol):
    async def generate(
        self,
        system_prompt: str,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMReply: ...


class GroqLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
    ) -> None:
        self._client = AsyncGroq(
            api_key=api_key, timeout=timeout_seconds, max_retries=1
        )
        self._model = model
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort

    @traced("llm_call", "llm", process_inputs=llm_inputs, process_outputs=llm_outputs)
    async def generate(
        self,
        system_prompt: str,
        history: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> LLMReply:
        messages = [{"role": "system", "content": system_prompt}, *history]
        extra_body = (
            {"reasoning_effort": self._reasoning_effort}
            if self._reasoning_effort
            else None
        )
        tool_kwargs: dict[str, Any] = {}
        if tools:
            tool_kwargs["tools"] = tools
            tool_kwargs["tool_choice"] = tool_choice or "auto"

        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=self._max_output_tokens,
                temperature=0.3,
                extra_body=extra_body,
                **tool_kwargs,
            )
        except Exception as exc:
            raise LLMError(type(exc).__name__) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=call.function.arguments or "{}",
            )
            for call in (choice.message.tool_calls or [])
        ]
        if not text and not tool_calls:
            raise LLMError(f"empty_response finish_reason={choice.finish_reason}")
        usage = getattr(response, "usage", None)
        counts = (
            {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
            if usage is not None
            else None
        )
        return LLMReply(text=text, latency_ms=latency_ms, tool_calls=tool_calls, usage=counts)

    async def close(self) -> None:
        await self._client.close()