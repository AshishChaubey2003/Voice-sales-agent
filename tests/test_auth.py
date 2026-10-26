"""P0 authentication and tenant-isolation tests.

These are real-DB tests: they run against the PostgreSQL that `build_engine()`
resolves from configuration (the same engine the app uses), create two
organizations each with its own API key, and exercise the HTTP routes through
the ASGI app. Only the LLM and the knowledge search are stubbed, because they
are external/non-deterministic; the auth path, the DB and tenant scoping are
all real.

Covered:
  1. Missing API key           -> 401
  2. Invalid API key           -> 401
  3. Valid key -> correct org  (conversation is owned by that org)
  4. Org A cannot READ org B's conversation   -> 404
  5. Org A cannot WRITE to org B's conversation (no message is persisted)
  6. Lead capture still enforces the server-side consent gate
"""
import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from api.auth import generate_api_key, hash_api_key
from api.db import build_engine, build_session_factory
from api.deps import get_knowledge_search, get_llm
from api.knowledge import RetrievedChunk, SearchResult
from api.llm import LLMReply, ToolCall
from api.main import app
from api.models import Agent, Conversation, Lead, Message, Organization

PRO_PLAN_CHUNK = RetrievedChunk(
    chunk_id=uuid.uuid4(),
    title="NimbusCRM Pricing",
    heading="NimbusCRM Pricing > Pro plan",
    content="$29 per user per month.",
)


class FakeLLM:
    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        return LLMReply(text="Hello!", latency_ms=1)


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        reply = self.replies.pop(0)
        return reply if isinstance(reply, LLMReply) else LLMReply(text=reply, latency_ms=1)


def fake_search(chunks=None):
    async def search(session, organization_id, query, top_k=4, max_distance=None):
        return SearchResult(chunks=list(chunks or []), elapsed_ms=1)

    return search


def lead_tool_call(**arguments):
    return LLMReply(
        text="",
        latency_ms=1,
        tool_calls=[ToolCall(id="call_1", name="save_lead", arguments=json.dumps(arguments))],
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Tenant:
    """One organization with its API key, agent id and auth header."""

    def __init__(self, org_id, agent_id, raw_key):
        self.org_id = org_id
        self.agent_id = agent_id
        self.raw_key = raw_key
        self.headers = {"Authorization": f"Bearer {raw_key}"}


@pytest.fixture
async def two_tenants():
    """Creates two isolated organizations (A and B) in the real database."""
    engine = build_engine()
    session_factory = build_session_factory(engine)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search()

    created = []

    async def make_org(name):
        raw_key = generate_api_key()
        async with session_factory() as session:
            org = Organization(
                name=name,
                slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}",
                api_key_hash=hash_api_key(raw_key),
            )
            session.add(org)
            await session.flush()
            agent = Agent(organization_id=org.id, name=f"{name} Agent", system_prompt="Be brief.")
            session.add(agent)
            await session.commit()
            created.append(org.id)
            return Tenant(org.id, agent.id, raw_key)

    a = await make_org("OrgA")
    b = await make_org("OrgB")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, session_factory, a, b

    async with session_factory() as session:
        await session.execute(delete(Organization).where(Organization.id.in_(created)))
        await session.commit()
    app.dependency_overrides.clear()
    await engine.dispose()


async def start_conversation(client, tenant):
    response = await client.post(
        "/v1/conversations",
        json={"agent_id": str(tenant.agent_id)},
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["conversation_id"]


# --- 1. Missing API key -------------------------------------------------------
@pytest.mark.anyio
async def test_missing_api_key_is_rejected(two_tenants):
    client, _, a, _ = two_tenants
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    response = await client.post("/v1/conversations", json={"agent_id": str(a.agent_id)})

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


# --- 2. Invalid API key -------------------------------------------------------
@pytest.mark.anyio
async def test_invalid_api_key_is_rejected(two_tenants):
    client, _, a, _ = two_tenants
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    response = await client.post(
        "/v1/conversations",
        json={"agent_id": str(a.agent_id)},
        headers={"Authorization": "Bearer vsa_not-a-real-key"},
    )

    assert response.status_code == 401


# --- 3. Valid key authenticates the correct org -------------------------------
@pytest.mark.anyio
async def test_valid_key_binds_conversation_to_its_org(two_tenants):
    client, session_factory, a, b = two_tenants
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    conversation_id = await start_conversation(client, a)

    async with session_factory() as session:
        conversation = await session.get(Conversation, uuid.UUID(conversation_id))
    assert conversation is not None
    assert conversation.organization_id == a.org_id
    assert conversation.organization_id != b.org_id


# --- 4. Org A cannot READ org B's conversation --------------------------------
@pytest.mark.anyio
async def test_org_cannot_read_another_orgs_conversation(two_tenants):
    client, _, a, b = two_tenants
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    # B owns the conversation.
    b_conversation = await start_conversation(client, b)

    # A sends B's conversation id with A's key -> must not resolve.
    response = await client.post(
        f"/v1/conversations/{b_conversation}/messages",
        json={"content": "Show me the history"},
        headers=a.headers,
    )

    assert response.status_code == 404


# --- 5. Org A cannot WRITE to org B's conversation ----------------------------
@pytest.mark.anyio
async def test_org_cannot_write_to_another_orgs_conversation(two_tenants):
    client, session_factory, a, b = two_tenants
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    b_conversation = await start_conversation(client, b)

    response = await client.post(
        f"/v1/conversations/{b_conversation}/messages",
        json={"content": "Injected message from org A"},
        headers=a.headers,
    )

    assert response.status_code == 404
    # Nothing from A's attempt may have been persisted on B's conversation.
    async with session_factory() as session:
        messages = (
            await session.scalars(
                select(Message).where(Message.conversation_id == uuid.UUID(b_conversation))
            )
        ).all()
    assert messages == []


# --- 6. Lead capture still enforces the consent gate --------------------------
@pytest.mark.anyio
async def test_lead_capture_still_enforces_consent(two_tenants):
    client, session_factory, a, _ = two_tenants
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search([PRO_PLAN_CHUNK])
    # The LLM proposes save_lead on the very first turn, before any read-back or
    # consent. The server-side gate must reject it.
    app.dependency_overrides[get_llm] = lambda: ScriptedLLM(
        [
            lead_tool_call(name="Rahul", email="rahul@example.com"),
            "Just to confirm: Rahul, rahul@example.com. May our sales team contact you?",
        ]
    )

    conversation_id = await start_conversation(client, a)
    response = await client.post(
        f"/v1/conversations/{conversation_id}/messages",
        json={"content": "I'm Rahul, rahul@example.com, call me"},
        headers=a.headers,
    )

    assert response.status_code == 200
    assert response.json()["lead_status"] == "rejected"

    # The gate's decision must also mean nothing was persisted.
    async with session_factory() as session:
        leads = (
            await session.scalars(
                select(Lead).where(Lead.conversation_id == uuid.UUID(conversation_id))
            )
        ).all()
    assert leads == []
