"""Session/thread metadata and parent runs for conversation turns.

Fake SDK tests run everywhere. The real-SDK test records runs in memory through a
patched client, never touching the network, and skips when langsmith is absent.
"""
import asyncio
import os
import sys
import types
import uuid

import pytest

from api import tracing
from tests.test_tracing import fresh_state, settings, use_settings  # noqa: F401  (fixture reuse)

SESSION = uuid.uuid4()
ORG = uuid.uuid4()


def fake_trace_module(monkeypatch, fail_on_enter=False):
    record = {"ends": [], "exit": None}

    class FakeRun:
        def end(self, **kw):
            record["ends"].append(kw)

    class FakeTrace:
        def __init__(self, **kw):
            record["kwargs"] = kw

        def __enter__(self):
            if fail_on_enter:
                raise RuntimeError("sdk down")
            return FakeRun()

        def __exit__(self, *exc):
            record["exit"] = exc

    module = types.ModuleType("langsmith")
    module.trace = FakeTrace
    monkeypatch.setitem(sys.modules, "langsmith", module)
    return record


def enable(monkeypatch, **overrides):
    use_settings(monkeypatch, settings(langsmith_tracing=True, langsmith_api_key="lsv2_x", **overrides))


def test_disabled_tracing_is_a_pure_passthrough(monkeypatch):
    use_settings(monkeypatch, settings())
    monkeypatch.setitem(sys.modules, "langsmith", None)  # importing would fail
    with tracing.turn_trace("t", session_id=SESSION, channel="chat") as turn:
        turn.set_outputs(a=1)
        assert turn.active is False
    with pytest.raises(ValueError):
        with tracing.turn_trace("t", session_id=SESSION, channel="chat"):
            raise ValueError("app error")


def test_enabled_turn_carries_thread_metadata_and_redacted_inputs(monkeypatch):
    enable(monkeypatch)
    record = fake_trace_module(monkeypatch)
    with tracing.turn_trace(
        "chat_turn", session_id=SESSION, channel="chat", organization_id=ORG,
        inputs=tracing.turn_inputs("my email is a@b.com"),
    ) as turn:
        assert turn.active
        turn.set_outputs(n_sources=2, lead_status=None)
    kw = record["kwargs"]
    assert kw["metadata"]["session_id"] == str(SESSION)
    assert kw["metadata"]["channel"] == "chat" and kw["metadata"]["organization_id"] == str(ORG)
    assert kw["inputs"] == {"message_chars": 19}
    assert "a@b.com" not in repr(kw)
    assert record["ends"] == [{"outputs": {"n_sources": 2, "lead_status": None}}]
    assert record["exit"] == (None, None, None)


def test_message_text_only_with_explicit_content_opt_in(monkeypatch):
    enable(monkeypatch, langsmith_trace_content=True)
    record = fake_trace_module(monkeypatch)
    with tracing.turn_trace("t", session_id=SESSION, channel="chat", inputs=tracing.turn_inputs("hello")):
        pass
    assert record["kwargs"]["inputs"]["message"] == "hello"


def test_errors_record_only_the_exception_type_and_still_propagate(monkeypatch):
    enable(monkeypatch)
    record = fake_trace_module(monkeypatch)
    secret = ValueError("provider said: api key sk-SECRET rejected")
    with pytest.raises(ValueError) as info:
        with tracing.turn_trace("t", session_id=SESSION, channel="chat"):
            raise secret
    assert info.value is secret
    exc_type, exc_value, _ = record["exit"]
    assert exc_type is RuntimeError and str(exc_value) == "ValueError"
    assert "SECRET" not in repr(record)


def test_sdk_failure_never_breaks_the_request(monkeypatch):
    enable(monkeypatch)
    fake_trace_module(monkeypatch, fail_on_enter=True)
    ran = []
    with tracing.turn_trace("t", session_id=SESSION, channel="chat") as turn:
        ran.append(turn.active)
    assert ran == [False]


def test_return_inside_the_block_still_closes_the_run(monkeypatch):
    enable(monkeypatch)
    record = fake_trace_module(monkeypatch)

    def handler():
        with tracing.turn_trace("t", session_id=SESSION, channel="chat") as turn:
            turn.set_outputs(done=True)
            return "reply"

    assert handler() == "reply"
    assert record["ends"] == [{"outputs": {"done": True}}] and record["exit"] == (None, None, None)




def test_real_sdk_nests_child_runs_under_one_session_without_network(monkeypatch):
    pytest.importorskip("langsmith")
    import langsmith
    from langsmith import Client

    created = []

    class RecordingClient(Client):
        def create_run(self, **kw):
            created.append(kw)

        def update_run(self, *a, **kw):
            return None

    client = RecordingClient(api_url="http://127.0.0.1:9", api_key="not-a-real-key", auto_batch_tracing=False)

    # Force the module on without reading real settings or a real key.
    monkeypatch.setattr(tracing, "_resolved", True)
    monkeypatch.setattr(tracing, "_enabled", True)
    use_settings(monkeypatch, settings(langsmith_tracing=True, langsmith_api_key="x"))

    @tracing.traced("retrieval", "retriever")
    async def retrieve(query):
        return {"n": 1}

    async def go():
        with langsmith.tracing_context(client=client, enabled=True, project_name="unit-test"):
            with tracing.turn_trace("chat_turn", session_id=SESSION, channel="chat", organization_id=ORG):
                await retrieve("x")

    asyncio.run(go())
    by_name = {c["name"]: c for c in created}
    assert {"chat_turn", "retrieval"} <= set(by_name)
    parent, child = by_name["chat_turn"], by_name["retrieval"]
    assert parent["extra"]["metadata"]["session_id"] == str(SESSION)
    assert child["parent_run_id"] == parent["id"]
    assert child["trace_id"] == parent["trace_id"]
