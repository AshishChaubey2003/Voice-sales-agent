from api import redact, tracing


def test_scrub_masks_emails_phones_keys_and_database_urls():
    text = ("mail jane@example.com, call +353 87 123 4567, key gsk_abcdefghijklmnop1234, "
            "Authorization: Bearer abcdefghijklmnopqrstuv, db postgresql://user:pw@host:5432/app")
    out = redact.scrub(text)
    for leaked in ("jane@", "123 4567", "abcdefghijklmnop", "user:pw"):
        assert leaked not in out
    assert {"EMAIL", "PHONE", "SECRET", "DBURL"} <= set(redact.find_sensitive(text))


def test_scrub_leaves_ordinary_prices_and_numbers_alone():
    text = "Pro is $29 per user per month; 10,000 contacts; call between 9 and 5."
    assert redact.scrub(text) == text and redact.find_sensitive(text) == []


def test_content_mode_trace_payloads_are_scrubbed_too(monkeypatch):
    monkeypatch.setattr(tracing, "content_allowed", lambda: True)
    inputs = tracing.llm_inputs({"system_prompt": "key sk-abcdefghijklmnop1234", "history": [{"role": "user", "content": "I am jane@example.com"}]})
    assert "abcdefghijklmnop" not in repr(inputs) and "jane@" not in repr(inputs)
    assert "jane@" not in repr(tracing.retrieval_inputs({"query": "jane@example.com plan?"}))
    assert "jane@" not in repr(tracing.turn_inputs("jane@example.com"))
    reply = type("R", (), {"text": "reach us at jane@example.com", "latency_ms": 1, "tool_calls": [], "usage": {"total_tokens": 7, "x": None}})()
    out = tracing.llm_outputs(reply)
    assert "jane@" not in repr(out) and out["usage"] == {"total_tokens": 7}


def test_default_mode_sends_no_text_at_all(monkeypatch):
    monkeypatch.setattr(tracing, "content_allowed", lambda: False)
    inputs = tracing.llm_inputs({"system_prompt": "secret prompt", "history": [{"role": "user", "content": "hello"}]})
    assert "secret prompt" not in repr(inputs) and "hello" not in repr(inputs)
    assert tracing.tool_inputs({"history": [1, 2], "arguments": '{"email":"a@b.com"}'}) == {"n_history_turns": 2}
