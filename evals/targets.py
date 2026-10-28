"""Systems under test.

RetrievalTarget  calls the real retriever (api.knowledge.search_knowledge) against
                 the organization's ingested knowledge. Needs Postgres and the
                 local embedding model. No LLM, no cost.
ApiTarget        drives the real FastAPI routes in-process (ASGI, no network),
                 with the real retriever and a real LLM client. It writes
                 conversations, messages and leads for the evaluation
                 organization and deletes them again in cleanup().
ReplayTarget     re-scores outputs recorded by an earlier run. No services needed.

Imports of api.* happen inside the classes so that `import evals` stays light.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from evals.schema import EvalCase


class EvalTargetError(RuntimeError):
    """The system under test did not produce a usable answer."""


class CountingLLM:
    """Wraps an LLM client and enforces a hard cap on provider calls."""

    def __init__(self, inner, max_calls: int) -> None:
        self._inner = inner
        self.max_calls = max_calls
        self.calls = 0
        # Provider-reported token counts only. Calls without a report are counted, never estimated.
        self.usage = {"calls_with_usage": 0, "calls_without_usage": 0,
                      "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _record_usage(self, reply) -> None:
        reported = getattr(reply, "usage", None)
        counts = {k: v for k, v in (reported or {}).items()
                  if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                  and isinstance(v, int) and not isinstance(v, bool)}
        if not counts:
            self.usage["calls_without_usage"] += 1
            return
        self.usage["calls_with_usage"] += 1
        for key, value in counts.items():
            self.usage[key] += value

    @property
    def budget_exhausted(self) -> bool:
        return self.calls >= self.max_calls

    async def generate(self, system_prompt, history, tools=None, tool_choice=None):
        from api.llm import LLMError

        if self.calls >= self.max_calls:
            raise LLMError("eval_call_budget_exceeded")
        self.calls += 1
        reply = await self._inner.generate(
            system_prompt, history, tools=tools, tool_choice=tool_choice
        )
        self._record_usage(reply)
        return reply


class RetrievalTarget:
    def __init__(self, *, session_factory, organization_id: uuid.UUID, settings) -> None:
        self._session_factory = session_factory
        self._organization_id = organization_id
        self._settings = settings

    async def run_case(self, case: EvalCase) -> dict:
        from api.knowledge import search_knowledge

        started = time.perf_counter()
        async with self._session_factory() as session:
            result = await search_knowledge(
                session,
                self._organization_id,
                case.query,
                top_k=self._settings.rag_top_k,
                max_distance=self._settings.rag_max_distance,
            )
        return {
            "retrieved_sources": [chunk.heading for chunk in result.chunks],
            "retrieval_ms": result.elapsed_ms,
            "wall_ms": int((time.perf_counter() - started) * 1000),
        }


class ApiTarget:
    def __init__(self, *, app, session_factory, organization, agent_id: uuid.UUID, llm: CountingLLM):
        self._app = app
        self._session_factory = session_factory
        self._organization = organization
        self._agent_id = agent_id
        self._llm = llm
        self._conversation_ids: list[uuid.UUID] = []
        self._client = None

    @property
    def budget_exhausted(self) -> bool:
        return self._llm.budget_exhausted

    async def __aenter__(self) -> "ApiTarget":
        from httpx import ASGITransport, AsyncClient

        from api.deps import get_current_org, get_llm

        self._app.state.session_factory = self._session_factory
        # Authentication is covered by tests/test_auth.py. The evaluation acts
        # as its own organization in-process, so no API key is needed or stored.
        self._app.dependency_overrides[get_current_org] = lambda: self._organization
        self._app.dependency_overrides[get_llm] = lambda: self._llm
        self._client = AsyncClient(transport=ASGITransport(app=self._app), base_url="http://eval")
        return self

    async def __aexit__(self, *exc) -> None:
        await self.cleanup()
        if self._client is not None:
            await self._client.aclose()
        from api.deps import get_current_org, get_llm

        self._app.dependency_overrides.pop(get_current_org, None)
        self._app.dependency_overrides.pop(get_llm, None)

    async def cleanup(self) -> int:
        """Deletes every conversation this run created (messages and leads cascade)."""
        from sqlalchemy import delete

        from api.models import Conversation

        if not self._conversation_ids:
            return 0
        async with self._session_factory() as session:
            await session.execute(
                delete(Conversation).where(
                    Conversation.id.in_(self._conversation_ids),
                    Conversation.organization_id == self._organization.id,
                )
            )
            await session.commit()
        removed = len(self._conversation_ids)
        self._conversation_ids.clear()
        return removed

    async def _seed_history(self, conversation_id: uuid.UUID, prior: list[dict]) -> None:
        from api.models import Message

        # Explicit, increasing timestamps: rows inserted in one transaction share
        # the same server-side now(), which would make their order undefined.
        base = datetime.now(timezone.utc) - timedelta(minutes=10)
        async with self._session_factory() as session:
            for offset, message in enumerate(prior):
                session.add(
                    Message(
                        organization_id=self._organization.id,
                        conversation_id=conversation_id,
                        role=message["role"],
                        content=message["content"],
                        created_at=base + timedelta(seconds=offset),
                    )
                )
            await session.commit()

    async def run_case(self, case: EvalCase) -> dict:
        from sqlalchemy import select

        from api.models import KnowledgeChunk, Lead, Message

        created = await self._client.post("/v1/conversations", json={"agent_id": str(self._agent_id)})
        if created.status_code != 201:
            raise EvalTargetError(f"create conversation: HTTP {created.status_code}")
        conversation_id = uuid.UUID(created.json()["conversation_id"])
        self._conversation_ids.append(conversation_id)

        if len(case.messages) > 1:
            await self._seed_history(conversation_id, case.messages[:-1])

        started = time.perf_counter()
        response = await self._client.post(
            f"/v1/conversations/{conversation_id}/messages", json={"content": case.query}
        )
        wall_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise EvalTargetError(f"send message: HTTP {response.status_code}")
        body = response.json()

        async with self._session_factory() as session:
            assistant = await session.scalar(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.organization_id == self._organization.id,
                    Message.role == "assistant",
                )
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            timings = (assistant.latency_ms or {}) if assistant else {}

            lead = await session.scalar(
                select(Lead).where(
                    Lead.conversation_id == conversation_id,
                    Lead.organization_id == self._organization.id,
                )
            )
            lead_row = {"email": lead.email} if lead else None

            sources = list(body.get("sources", []))
            context: list[str] = []
            if sources:
                rows = (
                    await session.execute(
                        select(KnowledgeChunk.heading, KnowledgeChunk.content).where(
                            KnowledgeChunk.organization_id == self._organization.id,
                            KnowledgeChunk.heading.in_(sources),
                        )
                    )
                ).all()
                by_heading = {heading: content for heading, content in rows}
                context = [f"{h}\n{by_heading[h]}" for h in sources if h in by_heading]

        return {
            "conversation_id": str(conversation_id),
            "reply": body["reply"],
            "retrieved_sources": sources,
            "context": context,
            "retrieval_ms": timings.get("rag"),
            "retrieval_rewritten_query": timings.get("rag_rewritten_query"),
            "llm_ms": body.get("llm_latency_ms"),
            "wall_ms": wall_ms,
            "lead_status": body.get("lead_status"),
            "lead_row": lead_row,
        }


class ReplayTarget:
    """Re-scores outputs saved in an earlier run's cases.jsonl."""

    def __init__(self, cases_path: str | Path) -> None:
        self.outputs: dict[str, dict] = {}
        for line in Path(cases_path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("status") == "ok" and isinstance(record.get("output"), dict):
                self.outputs[record["case_id"]] = record["output"]

    @property
    def case_ids(self) -> set[str]:
        return set(self.outputs)

    async def run_case(self, case: EvalCase) -> dict:
        output = dict(self.outputs[case.id])
        output.pop("context_chunks", None)
        return output
