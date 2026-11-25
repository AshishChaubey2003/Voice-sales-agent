"""Admission-order regression tests for the voice session (finding A).

A call must be admitted by the concurrency limiter BEFORE anything is persisted.
Before the fix, `run_session` inserted a `conversations` row first and only
consulted the limiter at the very end, so every refused call still wrote a row.

Labels: tests marked [mocked] use a session factory that fails if touched;
tests marked [real-DB] use the PostgreSQL that `build_engine()` resolves, with a
throwaway organization that is deleted afterwards. No network, no provider
calls, no API keys: the Pipecat objects are constructed with a dummy key and
never run.
"""

import uuid
from types import SimpleNamespace

import pytest
from pipecat.processors.frame_processor import FrameProcessor
from sqlalchemy import delete, func, select

from api.config import Settings
from api.db import build_engine, build_session_factory
from api.models import Agent, Conversation, Organization
from voice import bot as bot_module
from voice.limits import CallLimiter


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_settings(agent_id):
    # Explicit values only (no .env), mirroring tests/test_config.py.
    return Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        groq_api_key="dummy-not-a-real-key",
        groq_model="dummy-model",
        redis_password="p",
        voice_agent_id=agent_id,
        voice_max_concurrent_calls=1,
    )


class FakeTransport:
    """Just enough transport for pipeline construction; records any use."""

    def __init__(self):
        self.touched = []

    def input(self):
        self.touched.append("input")
        return FrameProcessor()

    def output(self):
        self.touched.append("output")
        return FrameProcessor()

    def event_handler(self, _name):
        def decorator(fn):
            return fn

        return decorator


RUNNER_ARGS = SimpleNamespace(pipeline_idle_timeout_secs=None, handle_sigint=False)


def saturated_limiter():
    limiter = CallLimiter(max_concurrent=1)
    assert limiter.try_acquire() is True  # another call is already in progress
    return limiter


async def count_conversations(session_factory, agent_id):
    async with session_factory() as session:
        return await session.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.agent_id == agent_id)
        )


@pytest.fixture
async def voice_agent():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        org = Organization(name="Voice Test Co", slug=f"voice-test-{uuid.uuid4().hex[:8]}")
        session.add(org)
        await session.flush()
        agent = Agent(organization_id=org.id, name="Voice Test Agent", system_prompt="Be brief.")
        session.add(agent)
        await session.commit()
        org_id, agent_id = org.id, agent.id

    yield session_factory, agent_id

    async with session_factory() as session:
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()
    await engine.dispose()


# --- [mocked] a refused call does no database work at all ---------------------
@pytest.mark.anyio
async def test_refused_call_never_touches_the_database(monkeypatch):
    limiter = saturated_limiter()
    monkeypatch.setattr(bot_module, "_call_limiter", limiter)
    touched = []

    def exploding_factory():
        touched.append("session_factory")
        raise AssertionError("database touched before the call was admitted")

    await bot_module.run_session(
        FakeTransport(), RUNNER_ARGS, make_settings(uuid.uuid4()), exploding_factory
    )

    assert touched == []
    assert limiter.active == 1  # the in-progress call's slot is untouched


# --- [real-DB] a refused call creates no conversation row ---------------------
@pytest.mark.anyio
async def test_refused_call_creates_no_conversation_row(monkeypatch, voice_agent):
    session_factory, agent_id = voice_agent
    limiter = saturated_limiter()
    monkeypatch.setattr(bot_module, "_call_limiter", limiter)
    monkeypatch.setattr(bot_module, "get_embedding_model", lambda: None)
    transport = FakeTransport()

    await bot_module.run_session(transport, RUNNER_ARGS, make_settings(agent_id), session_factory)

    assert await count_conversations(session_factory, agent_id) == 0
    assert transport.touched == []  # no pipeline was built for the refused call
    assert limiter.active == 1


# --- [real-DB] repeated refused calls cannot grow the table -------------------
@pytest.mark.anyio
async def test_many_refused_calls_still_create_no_rows(monkeypatch, voice_agent):
    session_factory, agent_id = voice_agent
    monkeypatch.setattr(bot_module, "_call_limiter", saturated_limiter())
    monkeypatch.setattr(bot_module, "get_embedding_model", lambda: None)

    for _ in range(5):
        await bot_module.run_session(
            FakeTransport(), RUNNER_ARGS, make_settings(agent_id), session_factory
        )

    assert await count_conversations(session_factory, agent_id) == 0


# --- [real-DB] reordering must not leak the slot when setup fails -------------
@pytest.mark.anyio
async def test_slot_is_released_when_the_agent_is_missing(monkeypatch, voice_agent):
    session_factory, _ = voice_agent
    limiter = CallLimiter(max_concurrent=1)
    monkeypatch.setattr(bot_module, "_call_limiter", limiter)

    with pytest.raises(RuntimeError, match="Voice agent not found"):
        await bot_module.run_session(
            FakeTransport(), RUNNER_ARGS, make_settings(uuid.uuid4()), session_factory
        )

    assert limiter.active == 0  # a failed setup must not hold a call slot


# --- [real-DB] partial initialisation is cleaned up ---------------------------
@pytest.mark.anyio
async def test_failure_after_the_row_is_created_ends_it_and_frees_the_slot(
    monkeypatch, voice_agent
):
    session_factory, agent_id = voice_agent
    limiter = CallLimiter(max_concurrent=1)
    monkeypatch.setattr(bot_module, "_call_limiter", limiter)

    def model_unavailable():
        raise RuntimeError("embedding model unavailable")

    monkeypatch.setattr(bot_module, "get_embedding_model", model_unavailable)

    with pytest.raises(RuntimeError, match="embedding model unavailable"):
        await bot_module.run_session(
            FakeTransport(), RUNNER_ARGS, make_settings(agent_id), session_factory
        )

    assert limiter.active == 0
    async with session_factory() as session:
        rows = (
            await session.scalars(select(Conversation).where(Conversation.agent_id == agent_id))
        ).all()
    assert len(rows) == 1  # the call was admitted, so a row was created ...
    assert rows[0].status == "ended"  # ... and is not left "active" forever
    assert rows[0].ended_at is not None


# --- [real-DB] a valid call still behaves as before ---------------------------
@pytest.mark.anyio
async def test_admitted_call_creates_one_row_and_hands_its_slot_to_the_guards(
    monkeypatch, voice_agent
):
    session_factory, agent_id = voice_agent
    limiter = CallLimiter(max_concurrent=1)
    monkeypatch.setattr(bot_module, "_call_limiter", limiter)
    monkeypatch.setattr(bot_module, "get_embedding_model", lambda: None)
    seen = {}

    async def recording_guards(*, runner, limiter, max_call_seconds, cleanup, **kwargs):
        # Stands in for the live call: the pipeline is not run.
        seen["kwargs"] = kwargs
        seen["active_during_call"] = limiter.active
        limiter.release()  # the real guards release the slot in their finally
        await cleanup()
        return "ran"

    monkeypatch.setattr(bot_module, "run_with_call_guards", recording_guards)

    await bot_module.run_session(
        FakeTransport(), RUNNER_ARGS, make_settings(agent_id), session_factory
    )

    assert seen["kwargs"] == {"admitted": True}   # the guards must not acquire a second slot
    assert seen["active_during_call"] == 1        # the call held exactly one slot
    assert limiter.active == 0
    async with session_factory() as session:
        rows = (
            await session.scalars(select(Conversation).where(Conversation.agent_id == agent_id))
        ).all()
    assert len(rows) == 1
    assert rows[0].channel == "voice"
    assert rows[0].status == "ended"  # cleanup ran at the end of the call
