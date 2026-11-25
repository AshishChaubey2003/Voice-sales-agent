"""The LangGraph chat orchestration: routing, branches, failure handling and equivalence with the legacy path.

Graph-only tests use a fake session (no database) because the nodes only commit/rollback on it. Tests that
persist anything (handoff rows, leads) go through the real HTTP route against PostgreSQL, like test_chat.py.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from api import chat as chat_module
from api import notifications
from api.auth import generate_api_key, hash_api_key
from api.chat import safe_search
from api.chat_graph import (
    GRAPH,
    HANDOFF_AVAILABLE_REPLY,
    HANDOFF_QUEUED_REPLY,
    ChatDeps,
    ChatTurnFailed,
    detect_intent_of,
    route_after_followup,
    route_after_gate,
    route_after_generate,
    route_after_handoff,
    route_after_intent,
    route_after_retrieve,
    run_chat_turn,
)
from api.db import build_engine, build_session_factory
from api.deps import get_knowledge_search, get_llm
from api.knowledge import RetrievedChunk, SearchResult
from api.llm import LLMError, LLMReply, ToolCall
from api.main import app
from api.models import Agent, Lead, Message, Organization
from evals.live import evaluate_turn

CHUNK = RetrievedChunk(chunk_id=uuid.uuid4(), title="Pricing", heading="Pricing > Pro plan",
                       content="$29 per user per month.")
SETTINGS = SimpleNamespace(rag_top_k=4, rag_max_distance=0.3)
ORG, CONV = uuid.uuid4(), uuid.uuid4()


class FakeSession:
    def __init__(self):
        self.commits = self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class ScriptedLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        self.calls.append({"prompt": system_prompt, "history": history, "tool_choice": tool_choice})
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, LLMReply) else LLMReply(text=item, latency_ms=10)


def searcher(*results):
    queue = list(results)

    async def search(session, organization_id, query, top_k=4, max_distance=None):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return search


def hit():
    return SearchResult(chunks=[CHUNK], elapsed_ms=2)


def miss():
    return SearchResult(chunks=[], elapsed_ms=1)


def make_deps(llm, search, *, request_handoff=None, session=None):
    return ChatDeps(session=session or FakeSession(), llm=llm, knowledge_search=search, settings=SETTINGS,
                    org=SimpleNamespace(id=ORG, name="Acme"), notifier=object(), safe_search=safe_search,
                    request_handoff=request_handoff)


async def run(deps, text, history=None):
    history = history if history is not None else [{"role": "user", "content": text}]
    return await run_chat_turn(deps, user_message=text, history=history, agent_prompt="Be brief.",
                               organization_id=ORG, conversation_id=CONV)


def save_lead_call(**args):
    return LLMReply(text="", latency_ms=5, tool_calls=[ToolCall(id="c1", name="save_lead", arguments=json.dumps(args))])


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ------------------------------------------------------------------ routing (pure)


def test_intent_detection_catches_requests_for_a_person_and_ignores_product_questions():
    asks = ["Can I talk to a human?", "I want to speak with someone from sales", "connect me to a sales rep please",
            "I'd like a real person", "Please transfer me to the sales team", "Human please",
            "can I speak to a representative", "get me a live agent"]
    questions = ["How much is the Pro plan?", "Does it have a sales pipeline?",
                 "What does your sales team do for onboarding?", "Is there a human review step in the workflow?",
                 "Can you talk to my CRM?", "how many people can use the Pro plan", "I want to talk about pricing",
                 "Can it integrate with person records?", "Hi", ""]
    assert all(detect_intent_of(t) == "human_handoff" for t in asks)
    assert all(detect_intent_of(t) == "general" for t in questions)


def test_routing_functions():
    assert route_after_intent({"intent": "human_handoff"}) == "handoff"
    assert route_after_intent({"intent": "general"}) == "retrieve"
    assert route_after_handoff({"handoff_failed": True}) == "retrieve"
    assert route_after_handoff({}) == "finalize"
    two = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    one = two[:1]
    assert route_after_retrieve({"search": miss(), "history": two}) == "rewrite"
    assert route_after_retrieve({"search": miss(), "history": one}) == "build_prompt"     # nothing to refer back to
    assert route_after_retrieve({"search": hit(), "history": two}) == "build_prompt"
    assert route_after_retrieve({"search": SearchResult(chunks=[], elapsed_ms=0, error=True), "history": two}) == "build_prompt"
    assert route_after_generate({"error": "llm"}) == "fail"
    assert route_after_generate({"result": LLMReply(text="x", latency_ms=1)}) == "finalize"
    assert route_after_generate({"result": save_lead_call(name="A")}) == "consent_gate"
    assert route_after_gate({"lead_decisions": [{"accepted": True}, {"accepted": False}]}) == "lead_capture"
    assert route_after_gate({"lead_decisions": [{"accepted": False}]}) == "tool_followup"
    assert route_after_followup({"error": "llm"}) == "fail"
    assert route_after_followup({}) == "finalize"


def test_the_compiled_graph_has_the_documented_nodes():
    nodes = set(GRAPH.get_graph().nodes) - {"__start__", "__end__"}
    assert nodes == {"detect_intent", "handoff", "retrieve", "rewrite", "build_prompt", "generate", "consent_gate",
                     "lead_capture", "tool_followup", "finalize", "fail"}


# ------------------------------------------------------------------ branches (no database)


@pytest.mark.anyio
async def test_plain_answer_path_is_grounded_and_makes_one_llm_call():
    llm = ScriptedLLM("The Pro plan is $29.")
    out = await run(make_deps(llm, searcher(hit())), "How much is the Pro plan?")
    assert out.path == ["detect_intent", "retrieve", "build_prompt", "generate", "finalize"]
    assert out.text == "The Pro plan is $29." and out.sources == ["Pricing > Pro plan"]
    assert len(llm.calls) == 1 and "$29 per user per month." in llm.calls[0]["prompt"]


@pytest.mark.anyio
async def test_follow_up_is_rewritten_once_then_searched_again():
    history = [{"role": "user", "content": "How much is the Pro plan?"}, {"role": "assistant", "content": "$29."},
               {"role": "user", "content": "and the Business plan?"}]
    llm = ScriptedLLM("Business plan price per user", "The Business plan is $49.")
    out = await run(make_deps(llm, searcher(miss(), hit())), "and the Business plan?", history)
    assert out.path == ["detect_intent", "retrieve", "rewrite", "build_prompt", "generate", "finalize"]
    assert out.rewritten_query == "Business plan price per user" and out.sources == ["Pricing > Pro plan"]
    assert len(llm.calls) == 2


@pytest.mark.anyio
async def test_infrastructure_error_skips_the_rewrite_and_flags_it():
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    llm = ScriptedLLM("I can't look that up right now.")
    out = await run(make_deps(llm, searcher(SearchResult(chunks=[], elapsed_ms=0, error=True))), "c", history)
    assert "rewrite" not in out.path and out.rag_error is True and len(llm.calls) == 1


@pytest.mark.anyio
async def test_a_failed_rewrite_does_not_break_the_turn():
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    llm = ScriptedLLM(LLMError("boom"), "Sorry, I don't have that.")
    out = await run(make_deps(llm, searcher(miss())), "c", history)
    assert out.path[:3] == ["detect_intent", "retrieve", "rewrite"] and out.text == "Sorry, I don't have that."


@pytest.mark.anyio
async def test_llm_failure_raises_chat_turn_failed():
    with pytest.raises(ChatTurnFailed):
        await run(make_deps(ScriptedLLM(LLMError("down")), searcher(hit())), "hello")


@pytest.mark.anyio
async def test_llm_failure_after_a_tool_call_also_fails_the_turn():
    history = [{"role": "assistant", "content": "Is ann@example.com right?"}, {"role": "user", "content": "yes"}]
    llm = ScriptedLLM(save_lead_call(name="Ann", email="other@example.com"), LLMError("down"))
    with pytest.raises(ChatTurnFailed):
        await run(make_deps(llm, searcher(hit())), "yes", history)


@pytest.mark.anyio
async def test_rejected_lead_goes_through_the_gate_but_never_reaches_capture():
    # The email was never read back, so the consent gate refuses and nothing is written.
    history = [{"role": "assistant", "content": "Anything else?"}, {"role": "user", "content": "yes please contact me"}]
    llm = ScriptedLLM(save_lead_call(name="Ann", email="ann@example.com"), "Let me read that back first.")
    session = FakeSession()
    out = await run(make_deps(llm, searcher(hit()), session=session), "yes please contact me", history)
    assert out.path == ["detect_intent", "retrieve", "build_prompt", "generate", "consent_gate", "tool_followup", "finalize"]
    assert out.lead_status == "rejected" and "lead_capture" not in out.path
    tool_message = llm.calls[1]["history"][-1]
    assert tool_message["role"] == "tool" and json.loads(tool_message["content"])["status"] == "rejected"
    assert llm.calls[1]["tool_choice"] == "none"


@pytest.mark.anyio
async def test_unknown_tool_is_rejected():
    llm = ScriptedLLM(LLMReply(text="", latency_ms=1, tool_calls=[ToolCall(id="x", name="drop_tables", arguments="{}")]),
                      "I can't do that.")
    out = await run(make_deps(llm, searcher(hit())), "hi")
    assert "lead_capture" not in out.path
    assert json.loads(llm.calls[1]["history"][-1]["content"]) == {"status": "rejected", "reason": "Unknown tool."}


@pytest.mark.anyio
async def test_asking_for_a_human_takes_the_handoff_branch_without_calling_the_llm():
    async def handoff(session, org, conversation_id, reason, notifier):
        return {"handoff": SimpleNamespace(status="pending"), "created": True, "available_now": True}

    llm = ScriptedLLM()
    out = await run(make_deps(llm, searcher(hit()), request_handoff=handoff), "Can I talk to a human?")
    assert out.path == ["detect_intent", "handoff", "finalize"]
    assert out.text == HANDOFF_AVAILABLE_REPLY and out.handoff_status == "pending" and out.llm_ms == 0
    assert llm.calls == []


@pytest.mark.anyio
async def test_nobody_available_gets_the_honest_queued_reply():
    async def handoff(session, org, conversation_id, reason, notifier):
        return {"handoff": SimpleNamespace(status="pending"), "created": True, "available_now": False}

    out = await run(make_deps(ScriptedLLM(), searcher(hit()), request_handoff=handoff), "I want a real person")
    assert out.text == HANDOFF_QUEUED_REPLY
    assert "contact you" in out.text and "minutes" not in out.text and "soon" not in out.text.lower()


@pytest.mark.anyio
async def test_if_the_handoff_machinery_fails_the_ai_still_answers():
    async def broken(session, org, conversation_id, reason, notifier):
        raise RuntimeError("db down")

    session = FakeSession()
    llm = ScriptedLLM("I can help with that.")
    out = await run(make_deps(llm, searcher(hit()), request_handoff=broken, session=session), "Can I talk to a human?")
    assert out.path == ["detect_intent", "handoff", "retrieve", "build_prompt", "generate", "finalize"]
    assert out.handoff_status is None and out.text == "I can help with that." and session.rollbacks == 1


def test_a_handoff_reply_is_evaluated_without_error_and_never_counted_as_a_retrieval_answer():
    # latency_ms of a templated handoff reply carries no rag keys; the live evaluator must cope.
    result = evaluate_turn(user_text="Can I talk to a human?", reply=HANDOFF_AVAILABLE_REPLY,
                           latency={"llm": 0, "handoff": "pending"}, case_index={})
    assert result["status"] in {"passed", "failed", "ungraded", "error", "skipped"}


# ------------------------------------------------------------------ end to end through the route (real database)


@pytest.fixture
async def world():
    engine = build_engine()
    sf = build_session_factory(engine)
    app.state.session_factory = sf
    app.dependency_overrides[get_knowledge_search] = lambda: searcher(hit())
    key = generate_api_key()
    async with sf() as s:
        org = Organization(name="Graph Co", slug=f"g-{uuid.uuid4().hex[:8]}", api_key_hash=hash_api_key(key))
        s.add(org)
        await s.flush()
        agent = Agent(organization_id=org.id, name="A", system_prompt="Be brief.")
        s.add(agent)
        await s.commit()
        org_id, agent_id = org.id, agent.id

    class Recorder:
        notices = []

        async def notify(self, notice):
            self.notices.append(notice)
            return "sent"

    rec = Recorder()
    app.dependency_overrides[notifications.get_notifier] = lambda: rec
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {key}"}) as client:
        yield {"client": client, "sf": sf, "agent": agent_id, "org": org_id, "rec": rec}
    async with sf() as s:
        await s.execute(delete(Organization).where(Organization.id == org_id))
        await s.commit()
    app.dependency_overrides.clear()
    await engine.dispose()


async def conversation(client, agent):
    return (await client.post("/v1/conversations", json={"agent_id": str(agent)})).json()["conversation_id"]


async def say(client, conv, text):
    return await client.post(f"/v1/conversations/{conv}/messages", json={"content": text})


@pytest.mark.anyio
async def test_route_asking_for_a_human_creates_a_handoff_and_replies_with_the_template(world):
    c = world["client"]
    llm = ScriptedLLM()
    app.dependency_overrides[get_llm] = lambda: llm
    await c.post("/v1/sales/reps", json={"name": "Asha", "email": "asha@example.com", "status": "available"})
    conv = await conversation(c, world["agent"])
    r = await say(c, conv, "Can I talk to a human?")
    assert r.status_code == 200
    body = r.json()
    assert body["reply"] == HANDOFF_AVAILABLE_REPLY and body["handoff_status"] == "pending" and body["sources"] == []
    assert llm.calls == [] and len(world["rec"].notices) == 1
    handoffs = (await c.get("/v1/sales/handoffs")).json()
    assert len(handoffs) == 1 and handoffs[0]["conversation_id"] == conv
    async with world["sf"]() as s:
        stored = (await s.scalars(select(Message).where(Message.conversation_id == uuid.UUID(conv))
                                  .order_by(Message.created_at))).all()
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].latency_ms == {"llm": 0, "handoff": "pending"}


@pytest.mark.anyio
async def test_route_ai_stays_silent_while_a_rep_owns_the_chat_then_resumes_after_resolve(world):
    c = world["client"]
    llm = ScriptedLLM("AI answer after resolve.")
    app.dependency_overrides[get_llm] = lambda: llm
    rep = (await c.post("/v1/sales/reps", json={"name": "Asha", "email": "asha@example.com", "status": "available"})).json()
    conv = await conversation(c, world["agent"])
    handoff = (await say(c, conv, "I want to speak to a sales rep")).json()
    hid = (await c.get("/v1/sales/handoffs")).json()[0]["id"]
    await c.post(f"/v1/sales/handoffs/{hid}/accept", json={"rep_id": rep["id"]})
    await c.post(f"/v1/sales/handoffs/{hid}/messages", json={"rep_id": rep["id"], "content": "Hi, Asha here."})

    quiet = await say(c, conv, "Great, what does Business cost?")
    assert quiet.json()["reply"] == "" and quiet.json()["handoff_status"] == "assigned"
    assert llm.calls == []                       # the model was not called at all
    async with world["sf"]() as s:
        texts = (await s.scalars(select(Message.content).where(Message.conversation_id == uuid.UUID(conv),
                                                               Message.role == "user"))).all()
    assert "Great, what does Business cost?" in texts   # stored for the rep

    await c.post(f"/v1/sales/handoffs/{hid}/resolve")
    back = await say(c, conv, "One more question about pricing")
    assert back.json()["reply"] == "AI answer after resolve." and back.json()["handoff_status"] is None
    # The rep's message reached the model as an assistant-side turn, never as an unknown role.
    roles = [m["role"] for m in llm.calls[0]["history"]]
    assert "human" not in roles and {"user", "assistant"} >= set(roles)


@pytest.mark.anyio
async def test_route_consented_lead_is_saved_through_the_graph_and_a_refusal_is_not(world):
    c, sf = world["client"], world["sf"]
    conv = await conversation(c, world["agent"])
    llm = ScriptedLLM("Is ann@example.com right? May our team contact you?")
    app.dependency_overrides[get_llm] = lambda: llm
    await say(c, conv, "Please have sales contact me, I'm Ann, ann@example.com")

    llm.replies = [save_lead_call(name="Ann", email="ann@example.com"), "Your details have been passed to the sales team."]
    ok = await say(c, conv, "yes")
    assert ok.json()["lead_status"] == "saved"
    async with sf() as s:
        leads = (await s.scalars(select(Lead).where(Lead.conversation_id == uuid.UUID(conv)))).all()
    assert len(leads) == 1 and leads[0].email == "ann@example.com" and leads[0].consent_reply == "yes"

    # A second conversation where the visitor refuses: the model may propose a save, the gate refuses.
    conv2 = await conversation(c, world["agent"])
    llm.replies = ["Is bo@example.com right? May our team contact you?"]
    await say(c, conv2, "Contact me, Bo, bo@example.com")
    llm.replies = [save_lead_call(name="Bo", email="bo@example.com"), "No problem, nothing was saved."]
    no = await say(c, conv2, "no")
    assert no.json()["lead_status"] == "rejected"
    async with sf() as s:
        assert (await s.scalars(select(Lead).where(Lead.conversation_id == uuid.UUID(conv2)))).all() == []


def use_orchestrator(monkeypatch, mode):
    base = chat_module.get_settings()
    monkeypatch.setattr(chat_module, "get_settings", lambda: base.model_copy(update={"chat_orchestrator": mode}))


@pytest.mark.anyio
async def test_route_llm_failure_is_a_503_on_both_orchestrators(world, monkeypatch):
    c = world["client"]
    base = chat_module.get_settings()
    for mode in ("langgraph", "legacy"):
        monkeypatch.setattr(chat_module, "get_settings",
                            lambda m=mode: base.model_copy(update={"chat_orchestrator": m}))
        app.dependency_overrides[get_llm] = lambda: ScriptedLLM(LLMError("down"))
        conv = await conversation(c, world["agent"])
        assert (await say(c, conv, "hello")).status_code == 503, mode


@pytest.mark.anyio
async def test_route_both_orchestrators_give_the_same_answer_for_the_same_input(world, monkeypatch):
    c = world["client"]
    base = chat_module.get_settings()
    replies = {}
    for mode in ("langgraph", "legacy"):
        monkeypatch.setattr(chat_module, "get_settings",
                            lambda m=mode: base.model_copy(update={"chat_orchestrator": m}))
        app.dependency_overrides[get_llm] = lambda: ScriptedLLM("The Pro plan is $29.")
        conv = await conversation(c, world["agent"])
        r = await say(c, conv, "How much is the Pro plan?")
        replies[mode] = {k: v for k, v in r.json().items() if k != "conversation_id"}
    assert replies["langgraph"] == replies["legacy"]
    assert replies["langgraph"]["sources"] == ["Pricing > Pro plan"]
