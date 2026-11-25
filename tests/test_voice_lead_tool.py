import asyncio
import uuid

import pytest

import voice.lead_tool as lead_tool_module
from api.leads import SAVE_LEAD_TOOL
from voice.lead_tool import (
    PendingLeadSaves,
    VOICE_LEAD_RULES,
    build_save_lead_handler,
    build_save_lead_schema,
)


def test_voice_tool_schema_matches_the_shared_tool_definition():
    schema = build_save_lead_schema()
    function = SAVE_LEAD_TOOL["function"]

    assert schema.name == function["name"]
    assert schema.required == function["parameters"]["required"]
    assert set(schema.properties) == set(function["parameters"]["properties"])


def test_voice_rules_ask_the_visitor_to_spell_the_email():
    assert "spell" in VOICE_LEAD_RULES.lower()


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# VOICE-01: PendingLeadSaves tracking + disconnect-grace wiring
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pending_lead_saves_reports_idle_immediately_when_nothing_is_in_flight():
    pending = PendingLeadSaves()

    assert await pending.wait_idle(timeout=0) is True


@pytest.mark.anyio
async def test_pending_lead_saves_times_out_while_something_is_in_flight():
    pending = PendingLeadSaves()
    pending.enter()

    assert await pending.wait_idle(timeout=0.05) is False


@pytest.mark.anyio
async def test_pending_lead_saves_reports_idle_as_soon_as_exit_is_called():
    pending = PendingLeadSaves()
    pending.enter()
    pending.exit()

    assert await pending.wait_idle(timeout=0) is True


class _FakeContext:
    def get_messages(self):
        return [{"role": "user", "content": "Call me please"}]


class _FakeParams:
    def __init__(self, arguments):
        self.context = _FakeContext()
        self.arguments = arguments
        self.results = []

    async def result_callback(self, outcome):
        self.results.append(outcome)


class _FakeSessionFactory:
    """An async-context-manager session factory whose commit() blocks on a gate, so a
    test can observe PendingLeadSaves while the "DB write" is still in flight."""

    def __init__(self, commit_gate: asyncio.Event):
        self.commit_gate = commit_gate
        self.committed = False

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def commit(self):
        await self.commit_gate.wait()
        self.committed = True


@pytest.mark.anyio
async def test_handler_marks_the_save_in_flight_until_the_commit_actually_finishes(
    monkeypatch,
):
    """The exact scenario VOICE-01 is about: while save_lead_from_tool has already decided
    to save and the commit is still pending, PendingLeadSaves must show it as in-flight -
    and only report idle again once that commit genuinely completes, not before."""

    async def fake_save_lead_from_tool(session, organization_id, conversation_id, history, arguments):
        return {"status": "saved"}

    monkeypatch.setattr(lead_tool_module, "save_lead_from_tool", fake_save_lead_from_tool)

    commit_gate = asyncio.Event()
    session_factory = _FakeSessionFactory(commit_gate)
    pending = PendingLeadSaves()
    handler = build_save_lead_handler(
        session_factory=session_factory,
        organization_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        pending=pending,
    )
    params = _FakeParams(arguments={"name": "Rahul", "email": "rahul@example.com"})

    task = asyncio.create_task(handler(params))
    await asyncio.sleep(0.01)  # let the handler reach the blocked commit()

    # Still mid-commit: a disconnect arriving right now must see this as in-flight.
    assert await pending.wait_idle(timeout=0) is False
    assert session_factory.committed is False

    commit_gate.set()
    await task

    assert session_factory.committed is True
    assert params.results == [{"status": "saved"}]
    # And now that the commit is actually done, nothing is in flight any more.
    assert await pending.wait_idle(timeout=0) is True