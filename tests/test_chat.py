import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from api.auth import generate_api_key, hash_api_key
from api.db import build_engine, build_session_factory
from api.deps import get_knowledge_search, get_llm
from api.knowledge import RetrievedChunk, SearchResult
from api.llm import LLMError, LLMReply, ToolCall
from api.main import app
from api.models import Agent, Lead, Message, Organization

PRO_PLAN_CHUNK = RetrievedChunk(
    chunk_id=uuid.uuid4(),
    title="NimbusCRM Pricing",
    heading="NimbusCRM Pricing > Pro plan",
    content="$29 per user per month.",
)


class FakeLLM:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = []
        self.prompts = []

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        self.prompts.append(system_prompt)
        self.calls.append(history)
        if self.fail:
            raise LLMError("fake_failure")
        return LLMReply(text="Hello! How can I help you today?", latency_ms=42)


class ScriptedLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        self.prompts.append(system_prompt)
        reply = self.replies.pop(0)
        return reply if isinstance(reply, LLMReply) else LLMReply(text=reply, latency_ms=10)


def fake_search(chunks=None):
    async def search(session, organization_id, query, top_k=4, max_distance=None):
        return SearchResult(chunks=list(chunks or []), elapsed_ms=1)

    return search


def lead_tool_call(**arguments):
    return LLMReply(
        text="",
        latency_ms=10,
        tool_calls=[ToolCall(id="call_1", name="save_lead", arguments=json.dumps(arguments))],
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def setup():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    app.state.session_factory = session_factory
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search()

    raw_key = generate_api_key()
    async with session_factory() as session:
        org = Organization(
            name="Test Co",
            slug=f"test-{uuid.uuid4().hex[:8]}",
            api_key_hash=hash_api_key(raw_key),
        )
        session.add(org)
        await session.flush()
        agent = Agent(organization_id=org.id, name="Test Agent", system_prompt="Be brief.")
        session.add(agent)
        await session.commit()
        org_id, agent_id = org.id, agent.id

    # Every request from this client is authenticated as the org above, so the
    # existing tests exercise the real auth path rather than bypassing it.
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {raw_key}"},
    ) as client:
        yield client, session_factory, agent_id

    async with session_factory() as session:
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()
    app.dependency_overrides.clear()
    await engine.dispose()


async def start_conversation(client, agent_id):
    response = await client.post("/v1/conversations", json={"agent_id": str(agent_id)})
    assert response.status_code == 201
    return response.json()["conversation_id"]


async def send(client, conversation_id, content):
    return await client.post(
        f"/v1/conversations/{conversation_id}/messages", json={"content": content}
    )


async def leads_for(session_factory, conversation_id):
    async with session_factory() as session:
        return (
            await session.scalars(
                select(Lead).where(Lead.conversation_id == uuid.UUID(conversation_id))
            )
        ).all()


@pytest.mark.anyio
async def test_chat_saves_user_and_assistant_messages(setup):
    client, session_factory, agent_id = setup
    fake = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: fake

    conversation_id = await start_conversation(client, agent_id)
    response = await send(client, conversation_id, "Hi, what do you sell?")

    assert response.status_code == 200
    assert response.json()["reply"] == "Hello! How can I help you today?"
    assert fake.calls[0][-1] == {"role": "user", "content": "Hi, what do you sell?"}

    async with session_factory() as session:
        roles = (
            await session.scalars(
                select(Message.role).where(
                    Message.conversation_id == uuid.UUID(conversation_id)
                )
            )
        ).all()
    assert sorted(roles) == ["assistant", "user"]


@pytest.mark.anyio
async def test_reply_prompt_is_grounded_in_retrieved_knowledge(setup):
    client, _, agent_id = setup
    fake = FakeLLM()
    app.dependency_overrides[get_llm] = lambda: fake
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search([PRO_PLAN_CHUNK])

    conversation_id = await start_conversation(client, agent_id)
    response = await send(client, conversation_id, "How much is the Pro plan?")

    assert response.status_code == 200
    assert response.json()["sources"] == ["NimbusCRM Pricing > Pro plan"]
    assert "$29 per user per month." in fake.prompts[0]


@pytest.mark.anyio
async def test_follow_up_question_is_rewritten_when_first_search_finds_nothing(setup):
    client, _, agent_id = setup
    business_plan = RetrievedChunk(
        chunk_id=uuid.uuid4(),
        title="NimbusCRM Pricing",
        heading="NimbusCRM Pricing > Business plan",
        content="$49 per user per month.",
    )
    searched_queries = []

    async def search(session, organization_id, query, top_k=4, max_distance=None):
        searched_queries.append(query)
        chunks = [business_plan] if query == "Business plan price" else []
        return SearchResult(chunks=chunks, elapsed_ms=1)

    llm = ScriptedLLM(["The Pro plan is $29.", "Business plan price", "The Business plan is $49."])
    app.dependency_overrides[get_knowledge_search] = lambda: search
    app.dependency_overrides[get_llm] = lambda: llm

    conversation_id = await start_conversation(client, agent_id)
    await send(client, conversation_id, "How much is the Pro plan?")
    response = await send(client, conversation_id, "And what about the Business plan?")

    assert response.status_code == 200
    assert response.json()["sources"] == ["NimbusCRM Pricing > Business plan"]
    assert searched_queries[-1] == "Business plan price"
    assert "$49 per user per month." in llm.prompts[-1]


@pytest.mark.anyio
async def test_lead_is_saved_after_details_are_read_back_and_visitor_says_yes(setup):
    client, session_factory, agent_id = setup
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search([PRO_PLAN_CHUNK])
    llm = ScriptedLLM(
        [
            "Just to confirm: Rahul, rahul@example.com. May our sales team contact you?",
            lead_tool_call(name="Rahul", email="rahul@example.com", team_size=20),
            "Thanks Rahul, I've passed your details to the sales team.",
        ]
    )
    app.dependency_overrides[get_llm] = lambda: llm

    conversation_id = await start_conversation(client, agent_id)
    await send(client, conversation_id, "Please ask sales to call me. I'm Rahul, rahul@example.com")
    response = await send(client, conversation_id, "Yes")

    assert response.status_code == 200
    assert response.json()["lead_status"] == "saved"
    leads = await leads_for(session_factory, conversation_id)
    assert len(leads) == 1
    assert leads[0].email == "rahul@example.com"
    assert leads[0].team_size == 20
    assert leads[0].consent_reply == "Yes"


@pytest.mark.anyio
async def test_lead_is_not_saved_without_read_back_and_confirmation(setup):
    client, session_factory, agent_id = setup
    app.dependency_overrides[get_knowledge_search] = lambda: fake_search([PRO_PLAN_CHUNK])
    llm = ScriptedLLM(
        [
            lead_tool_call(name="Rahul", email="rahul@example.com"),
            "Just to confirm: Rahul, rahul@example.com. May our sales team contact you?",
        ]
    )
    app.dependency_overrides[get_llm] = lambda: llm

    conversation_id = await start_conversation(client, agent_id)
    response = await send(client, conversation_id, "I'm Rahul, rahul@example.com, call me")

    assert response.status_code == 200
    assert response.json()["lead_status"] == "rejected"
    assert await leads_for(session_factory, conversation_id) == []


@pytest.mark.anyio
async def test_llm_failure_returns_503_without_leaking_details(setup):
    client, _, agent_id = setup
    app.dependency_overrides[get_llm] = lambda: FakeLLM(fail=True)

    conversation_id = await start_conversation(client, agent_id)
    response = await send(client, conversation_id, "Hello")

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service temporarily unavailable"}


@pytest.mark.anyio
async def test_blank_message_is_rejected(setup):
    client, _, agent_id = setup
    app.dependency_overrides[get_llm] = lambda: FakeLLM()

    conversation_id = await start_conversation(client, agent_id)
    response = await send(client, conversation_id, "    ")

    assert response.status_code == 422


@pytest.mark.anyio
async def test_unknown_agent_returns_404(setup):
    client, _, _ = setup

    response = await client.post(
        "/v1/conversations", json={"agent_id": str(uuid.uuid4())}
    )

    assert response.status_code == 404
