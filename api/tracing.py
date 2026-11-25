"""Optional LangSmith tracing for the retrieval, model and tool boundaries.

Off unless BOTH are set: LANGSMITH_TRACING=true and LANGSMITH_API_KEY. With
either missing, or with the `langsmith` package not installed, every decorated
function runs exactly as before (one cached boolean check per call).

What is sent when enabled
-------------------------
Counts, timings, chunk headings, tool names and the status of a tool call. The
visitor's message, the model's text and the system prompt are NOT sent unless
LANGSMITH_TRACE_CONTENT=true. Lead details (name, email, company) are never sent
in any setting. If a redaction hook raises, the SDK drops the payload instead of
sending it unprocessed.

Not covered: the Pipecat voice pipeline's own STT/LLM/TTS services (voice latency
is recorded in the database by voice/turn_recorder.py instead), and the HTTP
route itself.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
from collections.abc import Callable
from typing import Any

from api.redact import scrub, scrub_messages

logger = logging.getLogger(__name__)

_resolved = False
_enabled = False
_trace_content = False


def _resolve() -> bool:
    """Reads settings once and exports the SDK's variables. Never raises."""
    global _resolved, _enabled, _trace_content
    if _resolved:
        return _enabled
    _resolved = True
    try:
        from api.config import get_settings

        settings = get_settings()
        if not (settings.langsmith_tracing and settings.langsmith_api_key):
            return False
        import langsmith  # noqa: F401  (optional dependency)

        # The SDK reads these from the process environment, not from .env.
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key.get_secret_value())
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        if settings.langsmith_endpoint:
            os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
        _trace_content = bool(settings.langsmith_trace_content)
        _enabled = True
        logger.info("LangSmith tracing enabled (project %s)", settings.langsmith_project)
    except ImportError:
        logger.warning("LANGSMITH_TRACING is set but the 'langsmith' package is not installed")
    except Exception:
        # Misconfiguration must never take the request path down.
        logger.warning("LangSmith tracing disabled: configuration could not be read")
    return _enabled


def tracing_enabled() -> bool:
    return _resolve()


def content_allowed() -> bool:
    return _resolve() and _trace_content


def _reset_for_tests() -> None:
    global _resolved, _enabled, _trace_content
    _resolved = _enabled = _trace_content = False


def traced(
    name: str,
    run_type: str = "chain",
    *,
    process_inputs: Callable[[dict], dict] | None = None,
    process_outputs: Callable[[Any], dict] | None = None,
):
    """Decorator for async functions. A no-op wrapper unless tracing is enabled."""

    def decorator(fn):
        built: dict[str, Any] = {}

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            target = built.get("fn")
            if target is None:
                target = fn
                if _resolve():
                    try:
                        from langsmith import traceable

                        target = traceable(
                            name=name,
                            run_type=run_type,
                            process_inputs=process_inputs,
                            process_outputs=process_outputs,
                        )(fn)
                    except Exception:
                        logger.warning("Could not enable tracing for %s", name)
                built["fn"] = target
            return await target(*args, **kwargs)

        return wrapper

    return decorator


@contextlib.contextmanager
def untraced():
    """Disables LangSmith tracing inside the block (evaluators must not trace themselves)."""
    if not _resolve():
        yield
        return
    try:
        from langsmith import tracing_context

        manager = tracing_context(enabled=False)
    except Exception:
        yield
        return
    with manager:
        yield


class TurnTrace:
    """Handle yielded by turn_trace. Safe to use whether or not tracing is on."""

    def __init__(self) -> None:
        self._run = None
        self._outputs: dict[str, Any] = {}

    @property
    def active(self) -> bool:
        return self._run is not None

    @property
    def run_id(self) -> str | None:
        """The LangSmith run id of this parent run, or None when tracing is off or no run was started.

        Never invented: it is read from the run object the SDK returned.
        """
        try:
            value = getattr(self._run, "id", None) if self._run is not None else None
            return str(value) if value else None
        except Exception:
            return None

    def set_outputs(self, **outputs: Any) -> None:
        """Adds redacted summary values (counts, statuses) to the parent run."""
        self._outputs.update(outputs)


def _close_turn(cm, turn: TurnTrace, error: BaseException | None) -> None:
    if cm is None:
        return
    try:
        if error is not None:
            # Only the exception TYPE is recorded; messages can contain provider text.
            sanitized = RuntimeError(type(error).__name__)
            cm.__exit__(RuntimeError, sanitized, None)
        else:
            turn._run.end(outputs=turn._outputs)
            cm.__exit__(None, None, None)
    except Exception:
        logger.warning("Could not finish the trace for a conversation turn")


@contextlib.contextmanager
def turn_trace(
    name: str,
    *,
    session_id: Any,
    channel: str,
    organization_id: Any = None,
    inputs: dict | None = None,
):
    """One parent run per conversation turn, so child runs nest and group into a thread.

    `session_id` is the conversation id; LangSmith groups runs that share a
    `session_id` metadata value into one thread. A no-op yielding an inactive handle
    when tracing is off or the SDK call fails, and it never changes the exception
    the wrapped code raises.
    """
    turn = TurnTrace()
    cm = None
    if _resolve():
        try:
            from langsmith import trace

            metadata = {"session_id": str(session_id), "channel": channel}
            if organization_id is not None:
                metadata["organization_id"] = str(organization_id)
            try:
                from api.config import get_settings

                metadata["app_env"] = get_settings().app_env
            except Exception:
                pass
            cm = trace(name=name, run_type="chain", inputs=inputs or {}, metadata=metadata, tags=[channel])
            turn._run = cm.__enter__()
        except Exception:
            cm = None
            turn._run = None
            logger.warning("Could not start a trace for %s", name)
    try:
        yield turn
    except BaseException as error:
        _close_turn(cm, turn, error)
        raise
    else:
        _close_turn(cm, turn, None)


# ---- redaction hooks -------------------------------------------------------


def retrieval_inputs(inputs: dict) -> dict:
    query = inputs.get("query")
    out = {
        "query_chars": len(query) if isinstance(query, str) else None,
        "top_k": inputs.get("top_k"),
        "max_distance": inputs.get("max_distance"),
    }
    if content_allowed():
        out["query"] = scrub(query)
    return out


def retrieval_outputs(result: Any) -> dict:
    chunks = getattr(result, "chunks", None) or []
    return {
        "n_chunks": len(chunks),
        "sources": [getattr(chunk, "heading", None) for chunk in chunks],
        "elapsed_ms": getattr(result, "elapsed_ms", None),
    }


def llm_inputs(inputs: dict) -> dict:
    history = inputs.get("history") or []
    tools = inputs.get("tools") or []
    out = {
        "n_history_messages": len(history),
        "system_prompt_chars": len(inputs.get("system_prompt") or ""),
        "tools": [t.get("function", {}).get("name") for t in tools if isinstance(t, dict)],
        "tool_choice": inputs.get("tool_choice"),
    }
    if content_allowed():
        out["system_prompt"] = scrub(inputs.get("system_prompt"))
        out["history"] = scrub_messages(history)
    return out


def llm_outputs(result: Any) -> dict:
    tool_calls = getattr(result, "tool_calls", None) or []
    text = getattr(result, "text", "") or ""
    out = {
        "latency_ms": getattr(result, "latency_ms", None),
        "text_chars": len(text),
        "tool_calls": [getattr(call, "name", None) for call in tool_calls],
    }
    usage = getattr(result, "usage", None)
    if isinstance(usage, dict):
        out["usage"] = {k: v for k, v in usage.items() if isinstance(v, int)}
    if content_allowed():
        out["text"] = scrub(text)
    return out


def tool_inputs(inputs: dict) -> dict:
    # Never the arguments: they are the visitor's name and email.
    return {"n_history_turns": len(inputs.get("history") or [])}


def tool_outputs(result: Any) -> dict:
    status = result.get("status") if isinstance(result, dict) else None
    return {"status": status}


def turn_inputs(message: str | None, **extra: Any) -> dict:
    out = {"message_chars": len(message) if isinstance(message, str) else None, **extra}
    if content_allowed():
        out["message"] = scrub(message)
    return out
