"""Persisted evaluation of live turns (chat and voice).

Reads assistant messages that were already saved, evaluates them with evals.live, and stores one
TurnEvaluation per (message, evaluator version). It never touches the request path of a call: nothing in
api/chat.py or voice/bot.py imports this module, so a slow or failing evaluation cannot delay or fail a
reply. Every query is scoped to one organization.

References come only from the golden dataset, and only for the organizations it was written for
(REFERENCE_ORG_SLUGS). For any other organization every turn is ungraded with an explicit reason.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.models import Conversation, Message, Organization, TurnEvaluation
from api.redact import scrub
from evals import latency_telemetry, live
from evals.metrics import latency_summary, mean
from evals.schema import DatasetError, load_dataset

logger = logging.getLogger(__name__)

DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "nimbuscrm_golden_v1.jsonl"
# The golden dataset is NimbusCRM's knowledge base. Its answers are only a reference for that organization.
REFERENCE_ORG_SLUGS = ("nimbus-crm-demo",)
ROW_CAP = 20_000
MAX_BATCH = 500
TEXT_CAP = 1_500
UNIQUE_CONSTRAINT = "uq_turn_evaluations_message_version"
VOICE_STAGES = ("rag_ms", "llm_ttfb_ms", "tts_ttfb_ms", "voice_to_voice_ms", "first_speech_ms")


class ReferenceSet:
    """The golden-dataset references available for one organization."""

    def __init__(self, org: Organization, dataset_path: Path | str | None = None,
                 org_slugs: tuple[str, ...] = REFERENCE_ORG_SLUGS):
        self.available = getattr(org, "slug", None) in org_slugs
        self.index: dict = {}
        self.dataset: dict = {"name": None, "sha256": None, "reviewed_cases": None, "error": None}
        if not self.available:
            return
        try:
            dataset = load_dataset(dataset_path or DEFAULT_DATASET)
        except (DatasetError, OSError) as error:
            # A missing or broken dataset must not break evaluation: every turn is then ungraded.
            self.available = False
            self.dataset["error"] = type(error).__name__
            logger.warning("Live evaluation: reference dataset unavailable (%s)", type(error).__name__)
            return
        self.index = live.build_case_index(dataset)
        self.dataset.update(name=dataset.name, sha256=dataset.sha256, reviewed_cases=dataset.reviewed_count,
                            usable_references=len(self.index))


def _is_ordered_before(earlier: Message, later: Message) -> bool:
    if earlier.created_at != later.created_at:
        return earlier.created_at < later.created_at
    return earlier.role == "user" and later.role != "user"


def _row_values(org: Organization, assistant: Message, user: Message | None, result: dict, refs: ReferenceSet) -> dict:
    return {
        "organization_id": org.id,
        "conversation_id": assistant.conversation_id,
        "message_id": assistant.id,
        "user_message_id": user.id if user else None,
        "evaluator_version": result["evaluator_version"],
        "status": result["status"],
        "correctness": result.get("correctness"),
        "correctness_provisional": bool(result.get("correctness_provisional", True)),
        "reference_quality": result["reference"]["quality"],
        "reference_case_id": result["reference"].get("case_id"),
        "dataset_sha256": refs.dataset.get("sha256") if result["reference"]["quality"] != "none" else None,
        "ungraded_reason": result.get("ungraded_reason") or result.get("skipped_reason"),
        "result": result,
        "evaluated_at": datetime.now(timezone.utc),
    }


async def evaluate_turns(
    session: AsyncSession,
    org: Organization,
    *,
    conversation_id=None,
    days: int | None = None,
    force: bool = False,
    limit: int = MAX_BATCH,
    dataset_path: Path | str | None = None,
) -> dict:
    """Evaluate assistant messages that have no evaluation for the current evaluator version.

    Idempotent: the unique (message_id, evaluator_version) constraint means a repeat call, a retry or two
    concurrent callers can never create a duplicate. force=True re-evaluates and replaces the stored result
    for the same version (for example after the dataset was reviewed); it never adds a second row.
    """
    limit = max(1, min(limit, MAX_BATCH))
    refs = ReferenceSet(org, dataset_path)
    version = live.EVALUATOR_VERSION

    query = select(Message).where(Message.organization_id == org.id, Message.role == "assistant")
    if conversation_id is not None:
        query = query.where(Message.conversation_id == conversation_id)
    if days is not None:
        query = query.where(Message.created_at >= datetime.now(timezone.utc) - timedelta(days=days))
    if not force:
        query = query.where(~exists().where(TurnEvaluation.message_id == Message.id,
                                            TurnEvaluation.evaluator_version == version))
    assistants = list((await session.scalars(query.order_by(Message.created_at).limit(limit))).all())
    summary = {"selected": len(assistants), "written": 0, "by_status": {}, "evaluator_version": version,
               "force": force, "dataset": refs.dataset, "reference_available": refs.available}
    if not assistants:
        summary["remaining_pending"] = await count_pending(session, org, days=days, conversation_id=conversation_id)
        return summary

    conversation_ids = {m.conversation_id for m in assistants}
    history = list((await session.scalars(
        select(Message).where(Message.organization_id == org.id, Message.conversation_id.in_(conversation_ids))
        .order_by(Message.created_at)
    )).all())
    by_conversation: dict = {}
    for message in history:
        by_conversation.setdefault(message.conversation_id, []).append(message)

    counts: Counter = Counter()
    for assistant in assistants:
        earlier_users = [m for m in by_conversation.get(assistant.conversation_id, [])
                         if m.role == "user" and _is_ordered_before(m, assistant)]
        user = earlier_users[-1] if earlier_users else None
        if user is None:
            result = {"evaluator_version": version, "status": "skipped", "correctness": None,
                      "correctness_provisional": False, "ungraded_reason": None,
                      "skipped_reason": "no visitor message precedes this reply",
                      "reference": {"quality": "none", "case_id": None, "match": None},
                      "checks": [], "scores": {}, "flags": [], "retrieval": {"state": "not_evaluated"}}
        else:
            result = live.evaluate_turn(
                user_text=user.content, reply=assistant.content, latency=assistant.latency_ms,
                case_index=refs.index, visitor_texts=[m.content for m in earlier_users],
                reference_available=refs.available,
            )
        values = _row_values(org, assistant, user, result, refs)
        statement = pg_insert(TurnEvaluation).values(**values)
        if force:
            update = {k: v for k, v in values.items()
                      if k not in ("organization_id", "conversation_id", "message_id", "evaluator_version")}
            statement = statement.on_conflict_do_update(constraint=UNIQUE_CONSTRAINT, set_=update)
        else:
            statement = statement.on_conflict_do_nothing(constraint=UNIQUE_CONSTRAINT)
        written = await session.execute(statement.returning(TurnEvaluation.id))
        if written.first() is not None:
            counts[result["status"]] += 1
    await session.commit()

    summary["written"] = sum(counts.values())
    summary["by_status"] = dict(counts)
    summary["remaining_pending"] = await count_pending(session, org, days=days, conversation_id=conversation_id)
    return summary


async def count_pending(session: AsyncSession, org: Organization, *, days: int | None = None,
                        conversation_id=None) -> int:
    query = select(func.count()).select_from(Message).where(
        Message.organization_id == org.id, Message.role == "assistant",
        ~exists().where(TurnEvaluation.message_id == Message.id,
                        TurnEvaluation.evaluator_version == live.EVALUATOR_VERSION),
    )
    if conversation_id is not None:
        query = query.where(Message.conversation_id == conversation_id)
    if days is not None:
        query = query.where(Message.created_at >= datetime.now(timezone.utc) - timedelta(days=days))
    return int(await session.scalar(query) or 0)


def normalize_channel(channel: str | None) -> str | None:
    """Conversations store their channel as "text" (chat) or "voice". "chat" is accepted as an alias for "text"."""
    return "text" if channel == "chat" else channel


def _filters(org: Organization, *, days: int, channel: str | None, conversation_id=None, status: str | None = None,
             correctness: str | None = None) -> list:
    channel = normalize_channel(channel)
    clauses = [
        TurnEvaluation.organization_id == org.id,
        TurnEvaluation.evaluator_version == live.EVALUATOR_VERSION,
        Message.created_at >= datetime.now(timezone.utc) - timedelta(days=days),
    ]
    if channel:
        clauses.append(Conversation.channel == channel)
    if conversation_id is not None:
        clauses.append(TurnEvaluation.conversation_id == conversation_id)
    if status:
        clauses.append(TurnEvaluation.status == status)
    if correctness:
        clauses.append(TurnEvaluation.correctness == correctness)
    return clauses


def _joined(statement):
    return (statement
            .join(Message, Message.id == TurnEvaluation.message_id)
            .join(Conversation, and_(Conversation.id == TurnEvaluation.conversation_id,
                                     Conversation.organization_id == TurnEvaluation.organization_id)))


def _summary_row(evaluation: TurnEvaluation, channel: str, latency: dict | None) -> dict:
    result = evaluation.result or {}
    return {"conversation_id": str(evaluation.conversation_id), "status": evaluation.status,
            "correctness": evaluation.correctness, "correctness_provisional": evaluation.correctness_provisional,
            "reference_quality": evaluation.reference_quality, "flags": result.get("flags") or [],
            "scores": result.get("scores") or {}, "retrieval": result.get("retrieval") or {},
            "channel": channel, "latency": latency or {}}


def rag_metrics(rows: list[dict]) -> dict:
    """Retrieval quality of live turns. Each metric states its own unit and n; nothing is padded with zeros.

    Recall/MRR need expected sources, which exist only for turns that matched a dataset case. The empty
    retrieval rate needs only that retrieval was recorded.
    """
    out: dict = {}
    for key in ("recall_at_1", "recall_at_3", "recall_at_4", "precision_at_4", "mrr"):
        values = [r["scores"][key] for r in rows if r["scores"].get(key) is not None]
        out[key] = {"value": mean(values), "n": len(values), "n_unit": "graded turns with expected sources"}
    measured = [r for r in rows if r["retrieval"].get("state") == "measured"]
    empty = sum(1 for r in measured if r["retrieval"].get("empty"))
    out["empty_retrieval_rate"] = {"value": (empty / len(measured)) if measured else None, "n": len(measured),
                                   "n_unit": "turns with recorded retrieval"}
    out["retrieval_not_recorded"] = {
        "not_instrumented": sum(1 for r in rows if r["retrieval"].get("state") == "not_instrumented"),
        "error": sum(1 for r in rows if r["retrieval"].get("state") == "error"), "n_unit": "turns"}
    return out


_TIMING_KEYS = frozenset(m["key"] for m in latency_telemetry.METRICS if m["role"] == "assistant")


def _turn_timings(latency: dict | None) -> dict:
    """Numeric timings of one assistant message, plus the named breakdown parts when Pipecat reported them."""
    out = {}
    for key, value in (latency or {}).items():
        if key in _TIMING_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value  # voice also stores a "rag": "on" config flag, which is not a timing
    parts = (latency or {}).get("latency_breakdown")
    if isinstance(parts, list) and parts:
        out["latency_breakdown"] = parts
        out["breakdown_from"] = (latency or {}).get("breakdown_from")
    return out


def _trace_id(latency: dict | None) -> str | None:
    value = (latency or {}).get("trace_run_id")
    return value if isinstance(value, str) and value else None


async def latency_telemetry_for(session: AsyncSession, org: Organization, *, days: int = 30,
                                channel: str | None = None) -> dict:
    """Latency telemetry of this organization's persisted messages (see evals/latency_telemetry.py).

    A dataset of its own: it counts messages that carry a timing, not evaluated turns, and it does not
    depend on whether a turn was evaluated.
    """
    clauses = [
        Message.organization_id == org.id,
        Message.role.in_(("user", "assistant")),
        Message.created_at >= datetime.now(timezone.utc) - timedelta(days=days),
    ]
    channel = normalize_channel(channel)
    if channel:
        clauses.append(Conversation.channel == channel)
    fetched = (await session.execute(
        select(Conversation.channel, Message.role, Message.latency_ms)
        .join(Conversation, and_(Conversation.id == Message.conversation_id,
                                 Conversation.organization_id == Message.organization_id))
        .where(*clauses).order_by(Message.created_at.desc()).limit(ROW_CAP)
    )).all()
    out = latency_telemetry.summarize((ch, role, lat) for ch, role, lat in fetched)
    out.update(window_days=days, channel=channel, truncated=len(fetched) >= ROW_CAP,
               generated_at=datetime.now(timezone.utc).isoformat())
    return out


def voice_timings(rows: list[dict]) -> dict:
    """Timings of the evaluated VOICE turns, only where the stage was actually recorded."""
    voice = [r for r in rows if r["channel"] == "voice"]
    stages = {}
    for stage in VOICE_STAGES:
        values = [r["latency"][stage] for r in voice
                  if isinstance(r["latency"].get(stage), (int, float)) and not isinstance(r["latency"].get(stage), bool)]
        stages[stage] = latency_summary(values) if values else None  # None = never recorded: not zero
    return {"voice_turns": len(voice), "stages": stages, "n_unit": "voice turns",
            "note": "Time to first byte per service, not stage duration. STT is recorded on the visitor's message, not here. "
                    "Latency across ALL persisted messages, with definitions, is a separate dataset: /v1/evals/live/latency."}


async def summarize_live(session: AsyncSession, org: Organization, *, days: int = 30, channel: str | None = None,
                         conversation_id=None, dataset_path: Path | str | None = None) -> dict:
    refs = ReferenceSet(org, dataset_path)
    result = await session.execute(
        _joined(select(TurnEvaluation, Conversation.channel, Message.latency_ms))
        .where(*_filters(org, days=days, channel=channel, conversation_id=conversation_id))
        .order_by(Message.created_at.desc()).limit(ROW_CAP)
    )
    fetched = result.all()
    rows = [_summary_row(e, ch, lat) for e, ch, lat in fetched]
    summary = live.summarize(rows)
    pending = await count_pending(session, org, days=days, conversation_id=conversation_id)
    return {
        "kind": "live_turns",  # offline runs are a different population: never add these numbers to a run's
        "unit_note": "Counts are assistant turns from persisted conversations. A conversation has many turns.",
        "window_days": days, "channel": channel, "evaluator_version": live.EVALUATOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "truncated": len(fetched) >= ROW_CAP,
        "pending": pending, "pending_n_unit": "assistant turns without an evaluation yet",
        **summary,
        "rag": rag_metrics(rows),
        "voice_timings": voice_timings(rows),
        "reference": {"available": refs.available, "org_slugs": list(REFERENCE_ORG_SLUGS), "dataset": refs.dataset,
                      "quality_counts": dict(Counter(r["reference_quality"] for r in rows)),
                      "provisional_note": "Every verdict is provisional until the reference facts are reviewed by a named human (facts_review_status: reviewed)."},
    }


async def list_turns(session: AsyncSession, org: Organization, *, days: int = 30, channel: str | None = None,
                     conversation_id=None, status: str | None = None, correctness: str | None = None,
                     limit: int = 50, offset: int = 0) -> dict:
    where = _filters(org, days=days, channel=channel, conversation_id=conversation_id, status=status,
                     correctness=correctness)
    total = int(await session.scalar(_joined(select(func.count()).select_from(TurnEvaluation)).where(*where)) or 0)
    fetched = (await session.execute(
        _joined(select(TurnEvaluation, Conversation.channel, Message))
        .where(*where).order_by(Message.created_at.desc()).limit(limit).offset(offset)
    )).all()
    user_ids = [e.user_message_id for e, _, _ in fetched if e.user_message_id]
    users = {}
    if user_ids:
        users = {m.id: m for m in (await session.scalars(
            select(Message).where(Message.organization_id == org.id, Message.id.in_(user_ids)))).all()}
    turns = []
    for evaluation, channel_name, assistant in fetched:
        result = evaluation.result or {}
        user = users.get(evaluation.user_message_id)
        turns.append({
            "evaluation_id": str(evaluation.id), "message_id": str(evaluation.message_id),
            "conversation_id": str(evaluation.conversation_id), "channel": channel_name,
            "created_at": assistant.created_at.isoformat(), "evaluated_at": evaluation.evaluated_at.isoformat(),
            "status": evaluation.status, "correctness": evaluation.correctness,
            "correctness_provisional": evaluation.correctness_provisional,
            "correctness_reason": result.get("correctness_reason"),
            "reference": {"quality": evaluation.reference_quality, "case_id": evaluation.reference_case_id},
            "ungraded_reason": evaluation.ungraded_reason,
            # Stored transcripts can contain what the visitor typed; secrets and contact details are masked.
            "question": scrub(user.content)[:TEXT_CAP] if user else None,
            "reply": scrub(assistant.content)[:TEXT_CAP],
            "checks": result.get("checks") or [], "scores": result.get("scores") or {},
            "flags": result.get("flags") or [], "retrieval": result.get("retrieval") or {},
            "timings": _turn_timings(assistant.latency_ms),
            # Only a LangSmith run id that was actually read from a started run (voice retrieval step).
            # Chat does not store one, so it is None there rather than made up.
            "trace_id": _trace_id(assistant.latency_ms),
            "error": result.get("error"),
        })
    return {"total": total, "limit": limit, "offset": offset, "turns": turns}
