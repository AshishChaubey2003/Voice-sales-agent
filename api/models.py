import uuid
from datetime import datetime

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from api.db import Base

EMBEDDING_DIM = 384


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    # SHA-256 hex digest of the org's API key. Nullable so existing rows remain
    # valid after migration; an org with no hash simply cannot authenticate.
    # The raw key is never stored here.
    api_key_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True
    )


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    system_prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")

    __table_args__ = (UniqueConstraint("id", "organization_id"),)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    agent_id: Mapped[uuid.UUID]
    channel: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="active")
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id", "organization_id"],
            ["agents.id", "agents.organization_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "organization_id"),
        Index("ix_conversations_org_created", "organization_id", "created_at"),
    )


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[uuid.UUID]
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )
    
class KnowledgeDocument(TimestampMixin, Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(300))
    content_hash: Mapped[str] = mapped_column(String(64))

    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "source"),
    )


class KnowledgeChunk(TimestampMixin, Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    heading: Mapped[str] = mapped_column(String(300))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', heading || ' ' || content)", persisted=True),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "organization_id"],
            ["knowledge_documents.id", "knowledge_documents.organization_id"],
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_chunks_org_document", "organization_id", "document_id"),
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_knowledge_chunks_search_vector", "search_vector", postgresql_using="gin"),
    ) 
    
class Lead(TimestampMixin, Base):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[uuid.UUID]
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    company: Mapped[str | None] = mapped_column(String(200))
    team_size: Mapped[int | None]
    need: Mapped[str | None] = mapped_column(Text)
    consent_prompt: Mapped[str] = mapped_column(Text)
    consent_reply: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="new")

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("organization_id", "conversation_id"),
        Index("ix_leads_org_created", "organization_id", "created_at"),
    )


TURN_EVALUATION_STATUSES = ("passed", "failed", "ungraded", "error", "skipped")


class TurnEvaluation(TimestampMixin, Base):
    """One evaluation of one assistant message by one evaluator version (see evals/live.py).

    (message_id, evaluator_version) is unique, so re-running the evaluator can never create a duplicate.
    A different evaluator version adds a new row and keeps the old one. `pending` is not stored: a turn
    with no row yet is pending. There is no trace id column on purpose: neither channel stores a LangSmith
    run id with the message today, so there is nothing genuine to link.
    """

    __tablename__ = "turn_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[uuid.UUID]
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    user_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    evaluator_version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20))
    correctness: Mapped[str | None] = mapped_column(String(20))
    correctness_provisional: Mapped[bool] = mapped_column(Boolean, server_default="true")
    reference_quality: Mapped[str] = mapped_column(String(20), server_default="none")
    reference_case_id: Mapped[str | None] = mapped_column(String(100))
    dataset_sha256: Mapped[str | None] = mapped_column(String(64))
    ungraded_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict] = mapped_column(JSONB)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("message_id", "evaluator_version", name="uq_turn_evaluations_message_version"),
        CheckConstraint(
            "status IN ('passed', 'failed', 'ungraded', 'error', 'skipped')", name="status_valid"
        ),
        CheckConstraint(
            "correctness IS NULL OR correctness IN ('correct', 'partial', 'incorrect')",
            name="correctness_valid",
        ),
        Index("ix_turn_evaluations_org_created", "organization_id", "created_at"),
        Index("ix_turn_evaluations_org_status", "organization_id", "status"),
        Index("ix_turn_evaluations_conversation", "conversation_id"),
    )


SALES_REP_STATUSES = ("available", "busy", "offline")
HANDOFF_STATUSES = ("pending", "assigned", "resolved", "cancelled")
HANDOFF_OPEN_STATUSES = ("pending", "assigned")
NOTIFICATION_STATUSES = ("not_configured", "sent", "failed")


class SalesRep(TimestampMixin, Base):
    """A human on the organization's sales team who can take over a live chat."""

    __tablename__ = "sales_reps"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20), server_default="offline", default="offline")
    status_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("id", "organization_id"),
        UniqueConstraint("organization_id", "email", name="uq_sales_reps_org_email"),
        CheckConstraint("status IN ('available', 'busy', 'offline')", name="status_valid"),
        Index("ix_sales_reps_org_status", "organization_id", "status"),
    )


class HandoffRequest(TimestampMixin, Base):
    """A visitor asked for a human. At most one open request per conversation (partial unique index)."""

    __tablename__ = "handoff_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    conversation_id: Mapped[uuid.UUID]
    status: Mapped[str] = mapped_column(String(20), server_default="pending", default="pending")
    reason: Mapped[str | None] = mapped_column(String(300))
    # Short transcript snippet handed to the rep; the full conversation is read through the API.
    context_summary: Mapped[str] = mapped_column(Text, server_default="", default="")
    assigned_rep_id: Mapped[uuid.UUID | None]
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_status: Mapped[str] = mapped_column(
        String(20), server_default="not_configured", default="not_configured"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "organization_id"],
            ["conversations.id", "conversations.organization_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["assigned_rep_id", "organization_id"],
            ["sales_reps.id", "sales_reps.organization_id"],
        ),
        CheckConstraint(
            "status IN ('pending', 'assigned', 'resolved', 'cancelled')", name="status_valid"
        ),
        CheckConstraint(
            "notification_status IN ('not_configured', 'sent', 'failed')",
            name="notification_status_valid",
        ),
        Index(
            "uq_handoff_one_open_per_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'assigned')"),
        ),
        Index("ix_handoff_requests_org_status", "organization_id", "status", "created_at"),
    )
