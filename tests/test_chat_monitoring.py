"""The chat route hands only counts to the monitor, and monitoring faults never reach a visitor."""
import types
import uuid

from api import chat


def conversation():
    return types.SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())


def test_record_turn_sends_counts_and_never_the_reply_text(monkeypatch):
    seen = []
    monitor = types.SimpleNamespace(submit=seen.append)
    monkeypatch.setattr(chat, "get_monitor", lambda: monitor)
    chat._record_turn(conversation(), ok=True, llm_ms=120, rag_ms=9, n_sources=1, lead_status="saved",
                      reply_text="Thanks jane@example.com, key sk-abcdefghijklmnop1234 was saved")
    (event,) = seen
    assert event.secret_kinds == ("SECRET",)          # emails are expected in lead read-backs, so not flagged
    assert event.reply_chars == len("Thanks jane@example.com, key sk-abcdefghijklmnop1234 was saved")
    assert "jane" not in repr(event) and "abcdefghij" not in repr(event)


def test_record_turn_is_a_noop_when_monitoring_is_off_and_swallows_faults(monkeypatch):
    monkeypatch.setattr(chat, "get_monitor", lambda: None)
    chat._record_turn(conversation(), ok=True)

    def broken():
        raise RuntimeError("monitor exploded")

    monkeypatch.setattr(chat, "get_monitor", broken)
    chat._record_turn(conversation(), ok=False, error_type="LLMError")  # must not raise
