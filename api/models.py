import uuid
from datetime import datetime

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
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
