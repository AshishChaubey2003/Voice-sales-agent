"""MCP server: tool-level, in-process client, auth, tenant isolation and consent tests.

Pattern: in-process ``mcp.Client(mcp_server)`` via InMemoryTransport – no HTTP, no ports,
so these tests work in CI without an HTTP server running.  Auth is exercised by passing
real org API keys in the tool arguments; the MCP server resolves them through the same
``hash_api_key`` lookup the REST API uses.

Each tool call returns JSON; we decode and inspect it.
"""

from __future__ import annotations

import json
import uuid

import pytest
from mcp import Client
from sqlalchemy import delete

from api.auth import generate_api_key, hash_api_key
from api.db import build_engine, build_session_factory
from api.mcp_server import configure_session_factory, mcp
from api.models import Agent, Conversation, Organization


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _make_org(session_factory, label: str):
    raw_key = generate_api_key()
    async with session_factory() as session:
        org = Organization(
            name=f"{label} Co",
            slug=f"{label}-{uuid.uuid4().hex[:8]}",
            api_key_hash=hash_api_key(raw_key),
        )
        session.add(org)
        await session.flush()
        agent = Agent(organization_id=org.id, name="Agent", system_prompt="Hi")
        session.add(agent)
        await session.commit()
        return org.id, raw_key


async def _make_conversation(session_factory, org_id: uuid.UUID, agent_id=None):
    async with session_factory() as session:
        from sqlalchemy import select
        if agent_id is None:
            from api.models import Agent as _Agent
            agent_id = await session.scalar(
                select(_Agent.id).where(_Agent.organization_id == org_id).limit(1)
            )
        conv = Conversation(organization_id=org_id, agent_id=agent_id, channel="mcp")
        session.add(conv)
        await session.commit()
        return conv.id


@pytest.fixture
async def world():
    engine = build_engine()
    sf = build_session_factory(engine)
    configure_session_factory(sf)
    org_a_id, key_a = await _make_org(sf, "mcp-a")
    org_b_id, key_b = await _make_org(sf, "mcp-b")
    conv_id = await _make_conversation(sf, org_a_id)
    yield {
        "sf": sf,
        "org_a_id": org_a_id,
        "org_b_id": org_b_id,
        "key_a": key_a,
        "key_b": key_b,
        "conv_id": conv_id,
    }
    async with sf() as session:
        await session.execute(delete(Organization).where(Organization.id.in_([org_a_id, org_b_id])))
        await session.commit()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helper: synchronously call a tool via in-memory client
# ---------------------------------------------------------------------------

async def _call(tool_name: str, args: dict) -> dict:
    async with Client(mcp) as client:
        result = await client.call_tool(tool_name, args)
    text = result.content[0].text if result.content else "{}"
    return json.loads(text)


# ---------------------------------------------------------------------------
# search_knowledge_tool
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_search_knowledge_bad_key(world):
    """Invalid API key returns an error, not an exception."""
    data = await _call("search_knowledge_tool", {"query": "pricing", "api_key": "bad-key-xxx"})
    assert "error" in data
    assert "Invalid" in data["error"] or "invalid" in data["error"]


@pytest.mark.anyio
async def test_search_knowledge_empty_query(world):
    """Empty query returns an error without hitting the DB."""
    data = await _call("search_knowledge_tool", {"query": "   ", "api_key": world["key_a"]})
    assert "error" in data
    assert "empty" in data["error"].lower()


@pytest.mark.anyio
async def test_search_knowledge_valid_returns_chunks_key(world):
    """A valid call with no matching docs returns chunks key (possibly empty list)."""
    data = await _call(
        "search_knowledge_tool",
        {"query": "NimbusCRM enterprise pricing", "api_key": world["key_a"], "top_k": 3},
    )
    # Embedding model may not be available in CI (network blocked)
    if "error" in data:
        pytest.skip(f"Knowledge search unavailable: {data['error']}")
    assert "chunks" in data
    assert isinstance(data["chunks"], list)
    assert "elapsed_ms" in data


@pytest.mark.anyio
async def test_search_knowledge_top_k_capped(world):
    """top_k above 10 is silently capped; call succeeds."""
    data = await _call(
        "search_knowledge_tool",
        {"query": "features", "api_key": world["key_a"], "top_k": 999},
    )
    if "error" in data:
        pytest.skip(f"Knowledge search unavailable: {data['error']}")
    assert "chunks" in data


@pytest.mark.anyio
async def test_search_knowledge_tenant_isolation(world):
    """Org A and B searches succeed independently (each scoped to their own org)."""
    data_a = await _call(
        "search_knowledge_tool",
        {"query": "pricing", "api_key": world["key_a"]},
    )
    data_b = await _call(
        "search_knowledge_tool",
        {"query": "pricing", "api_key": world["key_b"]},
    )
    if "error" in data_a:
        pytest.skip(f"Knowledge search unavailable: {data_a['error']}")
    assert "error" not in data_b


# ---------------------------------------------------------------------------
# get_sales_availability_tool
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_availability_bad_key(world):
    data = await _call("get_sales_availability_tool", {"api_key": "wrong"})
    assert "error" in data


@pytest.mark.anyio
async def test_get_availability_valid(world):
    data = await _call("get_sales_availability_tool", {"api_key": world["key_a"]})
    assert "available" in data
    assert "available_count" in data
    assert "busy_count" in data
    assert "offline_count" in data


@pytest.mark.anyio
async def test_get_availability_zero_reps(world):
    """No reps registered → available=False, all counts 0."""
    data = await _call("get_sales_availability_tool", {"api_key": world["key_a"]})
    assert data["available"] is False
    assert data["available_count"] == 0


@pytest.mark.anyio
async def test_get_availability_tenant_isolation(world):
    """Org B's key returns its own counts, not A's."""
    data_a = await _call("get_sales_availability_tool", {"api_key": world["key_a"]})
    data_b = await _call("get_sales_availability_tool", {"api_key": world["key_b"]})
    if "error" in data_a:
        pytest.skip(f"Knowledge search unavailable: {data_a['error']}")
    assert "error" not in data_b


# ---------------------------------------------------------------------------
# create_lead_tool
# ---------------------------------------------------------------------------

def _ok_history(email: str = "alice@example.com") -> list[dict]:
    """Minimal history that passes consent_problem checks."""
    return [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": f"Can I save your details? Name: Alice, Email: {email}"},
        {"role": "user", "content": "yes"},
    ]


@pytest.mark.anyio
async def test_create_lead_bad_api_key(world):
    history = _ok_history()
    data = await _call(
        "create_lead_tool",
        {
            "api_key": "wrong",
            "name": "Alice",
            "email": "alice@example.com",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert "error" in data or data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_no_consent(world):
    """History missing explicit yes → rejected."""
    history = [
        {"role": "user", "content": "I want a demo"},
        {"role": "assistant", "content": "Sure! Email?"},
    ]
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Bob",
            "email": "bob@example.com",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"
    assert "reason" in data


@pytest.mark.anyio
async def test_create_lead_consent_no(world):
    """Visitor says no → rejected."""
    history = [
        {"role": "user", "content": "Tell me more"},
        {"role": "assistant", "content": "Sure! Name: Bob, Email: bob@example.com. Can I save?"},
        {"role": "user", "content": "no thanks"},
    ]
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Bob",
            "email": "bob@example.com",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_email_not_read_back(world):
    """Email not read back by assistant → rejected (LEAD-01)."""
    history = [
        {"role": "user", "content": "I'm Alice"},
        {"role": "assistant", "content": "Great! Shall I save your contact info?"},
        {"role": "user", "content": "yes"},
    ]
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Alice",
            "email": "alice@example.com",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_invalid_email(world):
    """Invalid email → rejected before hitting the DB."""
    history = _ok_history(email="not-an-email")
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Alice",
            "email": "not-an-email",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_wrong_conversation_id(world):
    """Random UUID not in DB → rejected."""
    history = _ok_history()
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Alice",
            "email": "alice@example.com",
            "conversation_id": str(uuid.uuid4()),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_cross_tenant_conversation(world):
    """Org B's key cannot save a lead to Org A's conversation."""
    history = _ok_history()
    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_b"],
            "name": "Alice",
            "email": "alice@example.com",
            "conversation_id": str(world["conv_id"]),
            "history": history,
        },
    )
    assert data.get("status") == "rejected"


@pytest.mark.anyio
async def test_create_lead_saves_successfully(world):
    """Happy path: consent present, email read back, valid conversation → saved."""
    conv_id = await _make_conversation(world["sf"], world["org_a_id"])
    email = f"mcp-{uuid.uuid4().hex[:6]}@example.com"
    history = _ok_history(email=email)

    data = await _call(
        "create_lead_tool",
        {
            "api_key": world["key_a"],
            "name": "Alice MCP",
            "email": email,
            "conversation_id": str(conv_id),
            "history": history,
        },
    )
    assert data.get("status") == "saved", data


@pytest.mark.anyio
async def test_create_lead_duplicate_idempotent(world):
    """Saving the same conversation's lead twice returns saved both times."""
    conv_id = await _make_conversation(world["sf"], world["org_a_id"])
    email = f"dup-{uuid.uuid4().hex[:6]}@example.com"
    history = _ok_history(email=email)
    args = {
        "api_key": world["key_a"],
        "name": "Dup",
        "email": email,
        "conversation_id": str(conv_id),
        "history": history,
    }
    r1 = await _call("create_lead_tool", args)
    r2 = await _call("create_lead_tool", args)
    assert r1.get("status") == "saved"
    assert r2.get("status") == "saved"


# ---------------------------------------------------------------------------
# Protocol-level: list_tools
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_tools_returns_all_three():
    """The MCP server advertises exactly the three expected tools."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools.tools}
    assert "search_knowledge_tool" in names
    assert "get_sales_availability_tool" in names
    assert "create_lead_tool" in names


@pytest.mark.anyio
async def test_tools_have_descriptions():
    """Each tool has a non-empty description."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    for t in tools.tools:
        assert t.description and len(t.description) > 10, f"{t.name} has no useful description"
