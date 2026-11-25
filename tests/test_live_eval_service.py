"""Persisted live-turn evaluation against a real PostgreSQL schema.

Opt in with TEST_DATABASE_URL (database name must contain "test"). Each test run creates a throwaway schema
with only the tables it needs and drops it afterwards, so the public schema and any real data are never
touched. Without the variable these tests are skipped, not passed.

    $env:TEST_DATABASE_URL = "postgresql+asyncpg://user:password@127.0.0.1:5433/vsa_test"
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, make_url, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api import eval_routes, live_eval
from api.auth import hash_api_key
from api.db import Base
from api.deps import get_session
from api.main import app
from api.models import Agent, Conversation, Message, Organization, TurnEvaluation
from evals import live

TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
def _test_database_name() -> str:
    try:
        return make_url(TEST_DB).database or ""
    except Exception:
        return ""


pytestmark = pytest.mark.skipif(
    "test" not in _test_database_name(),
    reason="set TEST_DATABASE_URL to a database whose name contains 'test'",
)
KEY_A, KEY_B = "vsa_test_key_org_a", "vsa_test_key_org_b"
# Relative to "now": the read APIs only look back `days` (default 30), so a hard-coded date turns these
# tests red once it ages out of the window. Turn timestamps stay a few seconds apart, in the recent past.
T0 = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
CASE = {"id": "pro-price", "kind": "rag_answer", "category": "factual",
        "messages": [{"role": "user", "content": "How much is the Pro plan?"}],
        "expected_sources": ["Pricing > Pro"], "required_facts": ["29", "per user"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def dataset_file(tmp_path):
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(CASE) + "\n", encoding="utf-8")
    return path


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DB)
    schema = "t_" + uuid.uuid4().hex[:10]
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = engine.execution_options(schema_translate_map={None: schema})
    tables = [t.__table__ for t in (Organization, Agent, Conversation, Message, TurnEvaluation)]
    async with scoped.begin() as conn:
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    yield async_sessionmaker(scoped, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()


class World:
    """Two organizations, each with an agent; helpers to add conversations and turns."""

    def __init__(self, factory):
        self.factory = factory
        self.clock = T0

    async def setup(self):
        async with self.factory() as s:
            self.a = Organization(name="A", slug="nimbus-crm-demo", api_key_hash=hash_api_key(KEY_A))
            self.b = Organization(name="B", slug="other-org", api_key_hash=hash_api_key(KEY_B))
            s.add_all([self.a, self.b])
            await s.flush()
            self.agent_a = Agent(organization_id=self.a.id, name="a", system_prompt="p")
            self.agent_b = Agent(organization_id=self.b.id, name="b", system_prompt="p")
            s.add_all([self.agent_a, self.agent_b])
            await s.commit()
        return self

    async def turn(self, org, agent, question, reply, latency=None, channel="text", conversation=None):
        async with self.factory() as s:
            if conversation is None:
                convo = Conversation(organization_id=org.id, agent_id=agent.id, channel=channel)
                s.add(convo)
                await s.flush()
                conversation = convo.id
            self.clock += timedelta(seconds=5)
            user = Message(organization_id=org.id, conversation_id=conversation, role="user", content=question,
                           created_at=self.clock)
            self.clock += timedelta(seconds=2)
            assistant = Message(organization_id=org.id, conversation_id=conversation, role="assistant",
                                content=reply, latency_ms=latency, created_at=self.clock)
            s.add_all([user, assistant])
            await s.commit()
            return conversation, user.id, assistant.id


@pytest.fixture
async def world(factory):
    return await World(factory).setup()


async def count(factory, model=TurnEvaluation):
    async with factory() as s:
        return await s.scalar(select(func.count()).select_from(model))


GOOD = ("Pro is 29 dollars per user.", {"rag_sources": ["Pricing > Pro"], "rag": 12})


# ---- persistence, references and statuses ----

@pytest.mark.anyio
async def test_each_assistant_message_gets_one_persisted_evaluation(world, factory, dataset_file):
    c1, u1, a1 = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", GOOD[0], GOOD[1])
    await world.turn(world.a, world.agent_a, "Do you integrate with Slack?", "Yes, we do.", {"rag_sources": []})
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
    assert out["selected"] == 2 and out["written"] == 2 and out["remaining_pending"] == 0
    assert out["by_status"] == {"passed": 1, "ungraded": 1}
    async with factory() as s:
        row = (await s.scalars(select(TurnEvaluation).where(TurnEvaluation.message_id == a1))).one()
    assert (row.status, row.correctness, row.reference_case_id) == ("passed", "correct", "pro-price")
    assert row.conversation_id == c1 and row.user_message_id == u1 and row.organization_id == world.a.id
    assert row.reference_quality == "author_unreviewed" and row.correctness_provisional is True
    assert row.dataset_sha256 and row.evaluator_version == live.EVALUATOR_VERSION


@pytest.mark.anyio
async def test_unmatched_question_stays_ungraded_with_an_explicit_reason(world, factory, dataset_file):
    await world.turn(world.a, world.agent_a, "Do you integrate with Slack?", "Yes.", {"rag_sources": []})
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        row = (await s.scalars(select(TurnEvaluation))).one()
    assert row.status == "ungraded" and row.correctness is None and row.reference_quality == "none"
    assert "no trusted reference" in row.ungraded_reason and row.reference_case_id is None


@pytest.mark.anyio
async def test_an_organization_without_a_reference_dataset_is_never_graded(world, factory, dataset_file):
    # the same golden question, asked to another organization, must NOT be graded against NimbusCRM's facts
    await world.turn(world.b, world.agent_b, "How much is the Pro plan?", "Pro is 99.", GOOD[1])
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.b, dataset_path=dataset_file)
        row = (await s.scalars(select(TurnEvaluation))).one()
    assert out["reference_available"] is False
    assert row.status == "ungraded" and row.correctness is None
    assert row.ungraded_reason == "no reference dataset is configured for this organization"


@pytest.mark.anyio
async def test_missing_dataset_file_degrades_to_ungraded_not_a_crash(world, factory, tmp_path):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", GOOD[0], GOOD[1])
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.a, dataset_path=tmp_path / "missing.jsonl")
        row = (await s.scalars(select(TurnEvaluation))).one()
    assert out["dataset"]["error"] == "FileNotFoundError" and row.status == "ungraded"


@pytest.mark.anyio
async def test_incorrect_and_partial_answers_are_failures_and_interrupted_or_empty_are_skipped(world, factory, dataset_file):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "Pro is 99 a seat.", GOOD[1])
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "It starts at 29.", GOOD[1])
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "Pro is 29 per", {**GOOD[1], "interrupted": True},
                     channel="voice")
    await world.turn(world.a, world.agent_a, "hello", "   ")
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        rows = (await s.scalars(select(TurnEvaluation).order_by(TurnEvaluation.created_at))).all()
    assert out["by_status"] == {"failed": 2, "skipped": 2}
    assert [r.correctness for r in rows] == ["incorrect", "partial", None, None]
    assert "interrupted" in rows[2].ungraded_reason


# ---- idempotence and duplicate prevention ----

@pytest.mark.anyio
async def test_repeat_runs_create_no_duplicates_and_pending_goes_to_zero(world, factory, dataset_file):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    async with factory() as s:
        first = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        second = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
    assert first["written"] == 1 and second["selected"] == 0 and second["written"] == 0
    assert await count(factory) == 1


@pytest.mark.anyio
async def test_the_database_itself_rejects_a_duplicate_evaluation(world, factory, dataset_file):
    _, _, a1 = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        existing = (await s.scalars(select(TurnEvaluation))).one()
        s.add(TurnEvaluation(organization_id=existing.organization_id, conversation_id=existing.conversation_id,
                             message_id=a1, evaluator_version=existing.evaluator_version, status="passed",
                             result={}))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.anyio
async def test_force_replaces_the_same_row_instead_of_adding_one(world, factory, dataset_file, tmp_path):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "Pro is 29 per user.", GOOD[1])
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
    reviewed = dict(CASE, review_status="reviewed", reviewer="Asha", facts_review_status="reviewed", facts_reviewer="Asha")
    new_file = tmp_path / "reviewed.jsonl"
    new_file.write_text(json.dumps(reviewed) + "\n", encoding="utf-8")
    async with factory() as s:
        again = await live_eval.evaluate_turns(s, world.a, dataset_path=new_file, force=True)
        row = (await s.scalars(select(TurnEvaluation))).one()
    assert again["written"] == 1 and await count(factory) == 1
    assert row.reference_quality == "reviewed" and row.correctness_provisional is False


@pytest.mark.anyio
async def test_a_new_evaluator_version_adds_rows_and_the_summary_reads_only_the_current_one(world, factory, dataset_file, monkeypatch):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
    monkeypatch.setattr(live, "EVALUATOR_VERSION", "live-next")
    async with factory() as s:
        pending = await live_eval.summarize_live(s, world.a, dataset_path=dataset_file)
        assert pending["turns"] == 0 and pending["pending"] == 1
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
    assert await count(factory) == 2


# ---- failures must not damage conversation data ----

@pytest.mark.anyio
async def test_an_evaluator_bug_is_recorded_as_error_and_the_conversation_is_untouched(world, factory, dataset_file, monkeypatch):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    monkeypatch.setattr(live, "_correctness", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        row = (await s.scalars(select(TurnEvaluation))).one()
        reply = (await s.scalars(select(Message).where(Message.role == "assistant"))).one()
    assert out["by_status"] == {"error": 1} and row.status == "error" and row.correctness is None
    assert row.result["error"] == "RuntimeError" and reply.content == GOOD[0]


@pytest.mark.anyio
async def test_deleting_a_message_removes_only_its_evaluation(world, factory, dataset_file):
    _, _, a1 = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    await world.turn(world.a, world.agent_a, "Do you integrate with Slack?", "Yes.", {"rag_sources": []})
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        await s.execute(delete(Message).where(Message.id == a1))
        await s.commit()
    assert await count(factory) == 1


# ---- organization isolation ----

@pytest.mark.anyio
async def test_one_organization_never_evaluates_reads_or_counts_anothers_turns(world, factory, dataset_file):
    conv_a, _, _ = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    conv_b, _, _ = await world.turn(world.b, world.agent_b, "How much is the Pro plan?", "Pro is 5.", GOOD[1])
    async with factory() as s:
        out = await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        assert out["selected"] == 1
        # org B cannot trigger evaluation of org A's conversation, and vice versa
        cross = await live_eval.evaluate_turns(s, world.b, conversation_id=conv_a, dataset_path=dataset_file)
        assert cross["selected"] == 0
        await live_eval.evaluate_turns(s, world.b, dataset_path=dataset_file)
        summary_a = await live_eval.summarize_live(s, world.a, dataset_path=dataset_file)
        turns_a = await live_eval.list_turns(s, world.a)
        turns_b = await live_eval.list_turns(s, world.b)
        peek = await live_eval.list_turns(s, world.b, conversation_id=conv_a)
    assert summary_a["turns"] == 1 and summary_a["counts"]["passed"] == 1
    assert {t["conversation_id"] for t in turns_a["turns"]} == {str(conv_a)}
    assert {t["conversation_id"] for t in turns_b["turns"]} == {str(conv_b)}
    assert peek["total"] == 0 and peek["turns"] == []


# ---- aggregation, evidence and dashboard data ----

@pytest.mark.anyio
async def test_summary_has_distinct_units_graded_denominators_and_honest_voice_timings(world, factory, dataset_file):
    voice_lat = {"rag_sources": ["Pricing > Pro"], "rag_ms": 40, "llm_ttfb_ms": 500, "tts_ttfb_ms": 300}
    conv, *_ = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "Pro is 29 per user.", voice_lat, channel="voice")
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", "Pro is 99.", {"rag_sources": []}, conversation=conv)
    await world.turn(world.a, world.agent_a, "Do you integrate with Slack?", "Yes.", {"llm": 100}, channel="text")
    await world.turn(world.a, world.agent_a, "hi", "", channel="text")
    async with factory() as s:
        before = await live_eval.summarize_live(s, world.a, dataset_path=dataset_file)
        assert before["turns"] == 0 and before["pending"] == 4 and before["pass_rate"]["value"] is None
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        out = await live_eval.summarize_live(s, world.a, dataset_path=dataset_file)
        voice_only = await live_eval.summarize_live(s, world.a, channel="voice", dataset_path=dataset_file)
    assert out["kind"] == "live_turns" and out["turns"] == 4 and out["conversations"] == 3
    assert out["counts"] == {"passed": 1, "failed": 1, "ungraded": 1, "error": 0, "skipped": 1, "graded": 2}
    assert out["counts_consistent"] is True and out["pending"] == 0
    assert out["pass_rate"] == {"value": 0.5, "n": 2, "n_unit": "turns"}
    assert out["ungraded_rate"]["n"] == 3 and out["ungraded_rate"]["value"] == pytest.approx(1 / 3)
    assert out["correctness"]["correct"]["count"] == 1 and out["correctness"]["incorrect"]["count"] == 1
    assert out["correctness_n"]["provisional_turns"] == 2  # unreviewed references: every verdict is provisional
    assert out["conversation_correctness"] == {"graded_conversations": 1, "all_correct": 0, "rate": 0.0, "n_unit": "conversations"}
    # RAG: expected sources exist for the 2 graded turns; retrieval was recorded for 3 turns (the chat one had none)
    assert out["rag"]["recall_at_4"]["n"] == 2 and out["rag"]["recall_at_4"]["n_unit"] == "graded turns with expected sources"
    assert out["rag"]["empty_retrieval_rate"] == {"value": 0.5, "n": 2, "n_unit": "turns with recorded retrieval"}
    assert out["rag"]["retrieval_not_recorded"]["not_instrumented"] == 1
    # voice timings: only stages that were recorded; others are None (never zero)
    stages = out["voice_timings"]["stages"]
    assert out["voice_timings"]["voice_turns"] == 2
    assert stages["llm_ttfb_ms"]["n"] == 1 and stages["voice_to_voice_ms"] is None and stages["first_speech_ms"] is None
    assert voice_only["turns"] == 2 and voice_only["channel"] == "voice"


@pytest.mark.anyio
async def test_turn_list_shows_evidence_masks_contact_details_and_filters(world, factory, dataset_file):
    await world.turn(world.a, world.agent_a, "How much is the Pro plan? my mail is rahul@example.com",
                     "Pro is 99.", GOOD[1])
    await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    async with factory() as s:
        await live_eval.evaluate_turns(s, world.a, dataset_path=dataset_file)
        failed = await live_eval.list_turns(s, world.a, status="failed")
        correct = await live_eval.list_turns(s, world.a, correctness="correct")
        everything = await live_eval.list_turns(s, world.a)
    assert everything["total"] == 2 and failed["total"] == 0  # the first question is not an exact match: ungraded
    turn = correct["turns"][0]
    assert turn["status"] == "passed" and turn["correctness_provisional"] is True
    assert {c["name"] for c in turn["checks"]} >= {"required_facts", "retrieval_hit"}
    assert turn["scores"]["mrr"] == 1.0 and turn["retrieval"]["sources"] == ["Pricing > Pro"]
    assert turn["trace_id"] is None and turn["timings"] == {"rag": 12}
    masked = [t for t in everything["turns"] if "rahul" in (t["question"] or "") or "[EMAIL]" in (t["question"] or "")]
    assert masked and "rahul@example.com" not in masked[0]["question"] and "[EMAIL]" in masked[0]["question"]


# ---- HTTP routes: authentication, organization isolation, validation ----

@pytest.fixture
async def client(factory, dataset_file, monkeypatch):
    async def session_override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_session] = session_override
    monkeypatch.setattr(eval_routes, "_reference_dataset_path", lambda: dataset_file)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def auth(key):
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.anyio
async def test_live_routes_require_a_valid_organization_key(world, client):
    for method, path in (("get", "/v1/evals/live/summary"), ("get", "/v1/evals/live/turns"), ("post", "/v1/evals/live/refresh")):
        missing = await getattr(client, method)(path)
        wrong = await getattr(client, method)(path, headers=auth("vsa_wrong"))
        assert missing.status_code == 401 and wrong.status_code == 401, path


@pytest.mark.anyio
async def test_refresh_then_summary_then_turns_end_to_end_over_http(world, client):
    conv, *_ = await world.turn(world.a, world.agent_a, "How much is the Pro plan?", *GOOD)
    await world.turn(world.b, world.agent_b, "How much is the Pro plan?", "Pro is 5.", GOOD[1])
    refreshed = await client.post("/v1/evals/live/refresh", headers=auth(KEY_A))
    assert refreshed.status_code == 200 and refreshed.json()["written"] == 1
    again = await client.post("/v1/evals/live/refresh", headers=auth(KEY_A), json={"force": False})
    assert again.json()["written"] == 0
    summary = (await client.get("/v1/evals/live/summary", headers=auth(KEY_A))).json()
    assert summary["turns"] == 1 and summary["counts"]["passed"] == 1 and summary["pending"] == 0
    other = (await client.get("/v1/evals/live/summary", headers=auth(KEY_B))).json()
    assert other["turns"] == 0 and other["pending"] == 1  # B's own turn is still pending, A's is invisible
    turns = (await client.get(f"/v1/evals/live/turns?conversation_id={conv}", headers=auth(KEY_A))).json()
    assert turns["total"] == 1
    spy = (await client.get(f"/v1/evals/live/turns?conversation_id={conv}", headers=auth(KEY_B))).json()
    assert spy["total"] == 0 and spy["turns"] == []
    forced = await client.post("/v1/evals/live/refresh", headers=auth(KEY_B), json={"conversation_id": str(conv)})
    assert forced.json()["selected"] == 0  # B cannot evaluate A's conversation


@pytest.mark.anyio
async def test_invalid_filters_are_rejected(world, client):
    for query in ("status=bogus", "correctness=wrong", "channel=sms", "days=0", "days=9999", "limit=0", "limit=500",
                  "conversation_id=not-a-uuid"):
        response = await client.get(f"/v1/evals/live/turns?{query}", headers=auth(KEY_A))
        assert response.status_code == 422, query
    assert (await client.post("/v1/evals/live/refresh", headers=auth(KEY_A), json={"limit": 9999})).status_code == 422


# ---- latency telemetry: its own dataset, never mixed into evaluation ----

VOICE_USER = {"stt_ttfb_ms": 310, "stt_processing_ms": 280}
VOICE_BOT = {"reasoning_effort": "low", "tts_provider": "groq", "rag": "on", "llm_ttfb_ms": 420,
             "llm_ttfat_ms": 610, "tts_ttfb_ms": 150, "tts_ttfa_ms": 190, "voice_to_voice_ms": 1800,
             "rag_ms": 35, "trace_run_id": "11111111-1111-1111-1111-111111111111", "interrupted": False,
             "latency_breakdown": [{"key": "llm", "ms": 900}], "breakdown_from": "user_silence"}


async def _voice_turn(world, org, agent, user_latency, bot_latency, question="How much is the Pro plan?"):
    conv, user_id, bot_id = await world.turn(org, agent, question, "Pro is 29 dollars per user.", bot_latency, channel="voice")
    async with world.factory() as s:
        message = await s.get(Message, user_id)
        message.latency_ms = user_latency
        await s.commit()
    return conv, user_id, bot_id


def _metric(body, channel, key):
    return next(m for m in body["channels"][channel]["metrics"] if m["key"] == key)


@pytest.mark.anyio
async def test_latency_route_requires_a_valid_organization_key(world, client):
    assert (await client.get("/v1/evals/live/latency")).status_code == 401
    assert (await client.get("/v1/evals/live/latency", headers=auth("vsa_wrong"))).status_code == 401


@pytest.mark.anyio
async def test_latency_reads_user_and_assistant_timings_of_this_organization_only(world, client):
    await _voice_turn(world, world.a, world.agent_a, VOICE_USER, VOICE_BOT)
    await _voice_turn(world, world.b, world.agent_b, {"stt_ttfb_ms": 9999}, {**VOICE_BOT, "llm_ttfb_ms": 9999})
    body = (await client.get("/v1/evals/live/latency", headers=auth(KEY_A))).json()
    assert body["kind"] == "latency_telemetry" and body["window_days"] == 30 and body["truncated"] is False
    assert _metric(body, "voice", "stt_ttfb_ms")["n"] == 1 and _metric(body, "voice", "stt_ttfb_ms")["stats"]["max"] == 310
    assert _metric(body, "voice", "llm_ttfb_ms")["stats"]["max"] == 420  # B's 9999 never leaks in
    assert _metric(body, "voice", "llm_ttfat_ms")["n"] == 1 and _metric(body, "voice", "tts_ttfa_ms")["n"] == 1
    assert body["channels"]["voice"]["outcomes"]["with_langsmith_run_id"] == 1
    other = (await client.get("/v1/evals/live/latency", headers=auth(KEY_B))).json()
    assert _metric(other, "voice", "llm_ttfb_ms")["stats"]["max"] == 9999


@pytest.mark.anyio
async def test_latency_without_data_is_not_instrumented_and_old_rows_still_work(world, client):
    empty = (await client.get("/v1/evals/live/latency", headers=auth(KEY_A))).json()
    assert all(m["state"] == "not_instrumented" and m["stats"] is None for m in empty["channels"]["voice"]["metrics"])
    # a voice row written before Stage 3: only the old keys exist
    await _voice_turn(world, world.a, world.agent_a, None, {"reasoning_effort": "low", "llm_ttfb_ms": 500, "rag": "on"})
    body = (await client.get("/v1/evals/live/latency?channel=voice", headers=auth(KEY_A))).json()
    assert _metric(body, "voice", "llm_ttfb_ms")["n"] == 1
    assert _metric(body, "voice", "llm_ttfat_ms")["state"] == "not_instrumented"
    assert _metric(body, "voice", "stt_ttfb_ms")["state"] == "not_instrumented"


@pytest.mark.anyio
async def test_latency_channel_filter_accepts_chat_as_an_alias_for_text(world, client):
    await world.turn(world.a, world.agent_a, "q", "a", {"llm": 800, "rag": 20, "rag_sources": []}, channel="text")
    await _voice_turn(world, world.a, world.agent_a, VOICE_USER, VOICE_BOT)
    chat = (await client.get("/v1/evals/live/latency?channel=chat", headers=auth(KEY_A))).json()
    text = (await client.get("/v1/evals/live/latency?channel=text", headers=auth(KEY_A))).json()
    assert chat["channels"]["text"]["outcomes"]["assistant_turns"] == 1 == text["channels"]["text"]["outcomes"]["assistant_turns"]
    assert chat["channels"]["voice"]["outcomes"]["assistant_turns"] == 0  # filtered out
    assert _metric(chat, "text", "llm")["stats"]["max"] == 800
    assert (await client.get("/v1/evals/live/latency?channel=sms", headers=auth(KEY_A))).status_code == 422
    assert (await client.get("/v1/evals/live/latency?days=0", headers=auth(KEY_A))).status_code == 422


@pytest.mark.anyio
async def test_latency_telemetry_does_not_change_evaluation_results(world, client):
    conv, *_ = await _voice_turn(world, world.a, world.agent_a, VOICE_USER, {**VOICE_BOT, "rag_sources": ["Pricing > Pro"]})
    before = (await client.post("/v1/evals/live/refresh", headers=auth(KEY_A))).json()
    await client.get("/v1/evals/live/latency", headers=auth(KEY_A))
    after = (await client.post("/v1/evals/live/refresh", headers=auth(KEY_A))).json()
    assert before["written"] == 1 and after["written"] == 0  # reading telemetry writes nothing and stays idempotent
    summary = (await client.get("/v1/evals/live/summary", headers=auth(KEY_A))).json()
    assert summary["turns"] == 1
    turns = (await client.get(f"/v1/evals/live/turns?conversation_id={conv}", headers=auth(KEY_A))).json()["turns"]
    assert turns[0]["trace_id"] == "11111111-1111-1111-1111-111111111111"
    assert turns[0]["timings"]["llm_ttfat_ms"] == 610 and turns[0]["timings"]["tts_ttfa_ms"] == 190
    assert "rag" not in turns[0]["timings"]  # the voice "rag": "on" flag is configuration, not a timing
