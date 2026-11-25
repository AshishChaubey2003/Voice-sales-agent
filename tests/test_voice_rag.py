import uuid

import pytest

from voice.rag import (
    KNOWLEDGE_MARKER,
    KnowledgeInjector,
    is_knowledge_message,
    is_tool_result,
    latest_user_text,
    with_knowledge,
)


def test_latest_user_text_ignores_knowledge_messages():
    messages = [
        {"role": "assistant", "content": "Hi, how can I help?"},
        {"role": "user", "content": "How much is the Pro plan?"},
        {"role": "developer", "content": f"{KNOWLEDGE_MARKER}.\n\nold facts"},
    ]

    assert latest_user_text(messages) == "How much is the Pro plan?"


def test_latest_user_text_is_none_when_the_bot_spoke_last():
    messages = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
    ]

    assert latest_user_text(messages) is None


def test_with_knowledge_replaces_the_previous_knowledge_message():
    messages = [
        {"role": "user", "content": "Pro plan price?"},
        {"role": "developer", "content": f"{KNOWLEDGE_MARKER}.\n\nold facts"},
        {"role": "assistant", "content": "$29."},
        {"role": "user", "content": "And Business?"},
    ]

    updated = with_knowledge(messages, "Knowledge base:\n\n[1] Business plan\n$49")
    knowledge = [message for message in updated if is_knowledge_message(message)]

    assert len(knowledge) == 1
    assert "$49" in knowledge[0]["content"]
    assert updated[-1] is knowledge[0]


def test_tool_results_do_not_trigger_a_new_search():
    messages = [
        {"role": "user", "content": "Call me please"},
        {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "saved"}'},
    ]

    assert is_tool_result(messages)
    assert not is_tool_result([{"role": "user", "content": "Hi"}])


class _FakeSettings:
    rag_top_k = 4
    rag_max_distance = 0.3


def _make_injector(session_factory):
    return KnowledgeInjector(
        session_factory=session_factory,
        organization_id=uuid.uuid4(),
        settings=_FakeSettings(),
        rewriter=None,
        on_retrieval=lambda *args: None,
    )


@pytest.mark.anyio
async def test_voice_search_marks_infrastructure_failures_distinctly_from_empty_results():
    """RAG-02/VOICE-02 mirror of the chat-path fix: the voice pipeline's own retrieval
    wrapper must flag an exception as error=True, not silently coerce it into the same
    SearchResult shape as 'nothing relevant found'."""

    def raising_session_factory():
        raise RuntimeError("pgvector connection lost")

    injector = _make_injector(raising_session_factory)
    result = await injector._search("How much is the Pro plan?")

    assert result.chunks == []
    assert result.error is True


@pytest.fixture
def anyio_backend():
    return "asyncio"