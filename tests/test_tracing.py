import asyncio
import sys
import types

import pytest

from api import tracing


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    import os

    names = ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT")
    saved = {n: os.environ.pop(n, None) for n in names}
    tracing._reset_for_tests()
    yield
    tracing._reset_for_tests()
    # tracing.py exports these into the process environment; do not let that
    # leak into other tests (a leaked key would make the real SDK try to connect).
    for n in names:
        os.environ.pop(n, None)
        if saved[n] is not None:
            os.environ[n] = saved[n]


def settings(**overrides):
    from pydantic import SecretStr

    base = dict(langsmith_tracing=False, langsmith_api_key=None, langsmith_project="proj",
                langsmith_endpoint=None, langsmith_trace_content=False)
    base.update(overrides)
    if isinstance(base["langsmith_api_key"], str):
        base["langsmith_api_key"] = SecretStr(base["langsmith_api_key"])
    return types.SimpleNamespace(**base)


def use_settings(monkeypatch, value):
    import api.config

    monkeypatch.setattr(api.config, "get_settings", lambda: value)


def fake_langsmith(monkeypatch):
    calls = []

    def traceable(**kwargs):
        def wrap(fn):
            async def traced_fn(*a, **k):
                calls.append(kwargs)
                return await fn(*a, **k)

            return traced_fn

        return wrap

    module = types.ModuleType("langsmith")
    module.traceable = traceable
    monkeypatch.setitem(sys.modules, "langsmith", module)
    return calls


async def double(x):
    return x * 2


def test_disabled_by_default_and_function_behaves_identically(monkeypatch):
    use_settings(monkeypatch, settings())
    wrapped = tracing.traced("t", "chain")(double)
    assert asyncio.run(wrapped(4)) == 8
    assert tracing.tracing_enabled() is False
    assert wrapped.__name__ == "double"


def test_tracing_flag_without_a_key_stays_off(monkeypatch):
    calls = fake_langsmith(monkeypatch)
    use_settings(monkeypatch, settings(langsmith_tracing=True))
    assert asyncio.run(tracing.traced("t")(double)(2)) == 4
    assert tracing.tracing_enabled() is False and calls == []


def test_key_without_the_flag_stays_off(monkeypatch):
    calls = fake_langsmith(monkeypatch)
    use_settings(monkeypatch, settings(langsmith_api_key="lsv2_secretsecretsecret"))
    asyncio.run(tracing.traced("t")(double)(2))
    assert calls == [] and "LANGSMITH_API_KEY" not in __import__("os").environ


def test_enabled_exports_sdk_variables_and_wraps_with_redaction_hooks(monkeypatch):
    import os

    calls = fake_langsmith(monkeypatch)
    use_settings(monkeypatch, settings(langsmith_tracing=True, langsmith_api_key="lsv2_secretsecretsecret"))
    wrapped = tracing.traced("retrieval", "retriever", process_inputs=tracing.retrieval_inputs)(double)
    assert asyncio.run(wrapped(3)) == 6
    assert calls[0]["name"] == "retrieval" and calls[0]["run_type"] == "retriever"
    assert calls[0]["process_inputs"] is tracing.retrieval_inputs
    assert os.environ["LANGSMITH_TRACING"] == "true" and os.environ["LANGSMITH_PROJECT"] == "proj"
    assert os.environ["LANGSMITH_API_KEY"] == "lsv2_secretsecretsecret"


def test_missing_package_disables_tracing_without_breaking_calls(monkeypatch):
    monkeypatch.setitem(sys.modules, "langsmith", None)  # import raises ImportError
    use_settings(monkeypatch, settings(langsmith_tracing=True, langsmith_api_key="lsv2_secretsecretsecret"))
    assert asyncio.run(tracing.traced("t")(double)(5)) == 10
    assert tracing.tracing_enabled() is False


def test_unreadable_settings_never_break_the_request_path(monkeypatch):
    import api.config

    def boom():
        raise RuntimeError("bad config")

    monkeypatch.setattr(api.config, "get_settings", boom)
    assert asyncio.run(tracing.traced("t")(double)(1)) == 2


def test_visitor_text_is_redacted_unless_content_tracing_is_on(monkeypatch):
    use_settings(monkeypatch, settings())
    assert "query" not in tracing.retrieval_inputs({"query": "my secret question", "top_k": 4})
    assert tracing.retrieval_inputs({"query": "abcd"})["query_chars"] == 4

    llm = tracing.llm_inputs({"system_prompt": "SYS", "history": [{"role": "user", "content": "hi"}],
                              "tools": [{"function": {"name": "save_lead"}}]})
    assert "history" not in llm and "system_prompt" not in llm
    assert llm["tools"] == ["save_lead"] and llm["n_history_messages"] == 1

    reply = types.SimpleNamespace(text="hello", latency_ms=5, tool_calls=[types.SimpleNamespace(name="save_lead")])
    assert "text" not in tracing.llm_outputs(reply)
    assert tracing.llm_outputs(reply)["tool_calls"] == ["save_lead"]


def test_content_flag_includes_text_but_lead_details_never_appear(monkeypatch):
    fake_langsmith(monkeypatch)
    use_settings(monkeypatch, settings(langsmith_tracing=True, langsmith_api_key="lsv2_secretsecretsecret",
                                       langsmith_trace_content=True))
    assert tracing.retrieval_inputs({"query": "abcd"})["query"] == "abcd"
    inputs = tracing.tool_inputs({"history": [1, 2], "arguments": '{"name": "Rahul", "email": "r@example.com"}'})
    assert inputs == {"n_history_turns": 2}
    assert tracing.tool_outputs({"status": "saved", "reason": "x"}) == {"status": "saved"}


def test_retrieval_outputs_report_headings_not_chunk_text():
    chunk = types.SimpleNamespace(heading="H", content="SECRET BODY")
    out = tracing.retrieval_outputs(types.SimpleNamespace(chunks=[chunk], elapsed_ms=7))
    assert out == {"n_chunks": 1, "sources": ["H"], "elapsed_ms": 7}
