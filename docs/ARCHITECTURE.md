# Architecture

> Voice Sales Agent — System Architecture

## Overview

The Voice Sales Agent is a multi-tenant, consent-gated AI sales assistant that handles both
text chat and real-time voice conversations. It is built on FastAPI with PostgreSQL/pgvector
for persistence and RAG, and Pipecat/WebRTC for voice.

## High-Level Components

```
┌──────────────────────────────────────────────────────────────┐
│                        Client Layer                          │
│  ┌─────────┐  ┌──────────────┐  ┌──────────┐  ┌──────────┐ │
│  │ Chat API │  │ Voice Widget │  │ MCP Tool │  │Dashboard │ │
│  └────┬─────┘  └──────┬───────┘  └────┬─────┘  └────┬─────┘ │
└───────┼───────────────┼──────────────┼──────────────┼────────┘
        │               │              │              │
┌───────┼───────────────┼──────────────┼──────────────┼────────┐
│       ▼               ▼              ▼              ▼        │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │                   FastAPI Application                   │ │
│  │  ┌──────────┐ ┌───────────┐ ┌─────────┐ ┌───────────┐ │ │
│  │  │ Chat     │ │ Voice     │ │ MCP     │ │ Eval      │ │ │
│  │  │ Router   │ │ Tokens    │ │ Server  │ │ Routes    │ │ │
│  │  └────┬─────┘ └─────┬─────┘ └────┬────┘ └─────┬─────┘ │ │
│  │       │             │            │             │       │ │
│  │  ┌────▼─────────────▼────────────▼─────────────▼────┐  │ │
│  │  │              Service Layer                       │  │ │
│  │  │  LangGraph · Knowledge · Leads · Handoff · Eval │  │ │
│  │  └────────────────────┬─────────────────────────────┘  │ │
│  │                       │                                │ │
│  │  ┌────────────────────▼─────────────────────────────┐  │ │
│  │  │            Data Layer (SQLAlchemy + pgvector)     │  │ │
│  │  │  Organizations · Conversations · Messages         │  │ │
│  │  │  Leads · Knowledge · Evaluations · SalesReps     │  │ │
│  │  │  HandoffRequests                                  │  │ │
│  │  └──────────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────┘ │
│                   Backend (api/)                              │
└──────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                   Voice Pipeline (voice/)                     │
│  Pipecat · WebRTC · Groq STT/LLM/TTS · Deepgram            │
│  Consent flow · Lead capture · Turn recording               │
└──────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

### Multi-Tenancy
- Every table includes `organization_id` as the first column of composite primary/foreign keys
- API key → org resolution via SHA-256 hash lookup (no plaintext storage)
- Cross-tenant access returns 404 (not 403) to avoid leaking existence

### Consent Gate
- Single pure function `check_lead_proposal()` in `api/leads.py`
- Called by REST, LangGraph, and MCP paths — no bypass route
- Requires: (1) valid email, (2) assistant read-back, (3) affirmative user reply
- `consent_prompt` and `consent_reply` are NOT NULL columns on `leads`

### Chat Orchestration (LangGraph)
- `api/chat_graph.py` — StateGraph with conditional routing
- Nodes: `detect_intent` → `retrieve` → `generate` → `consent_gate` → `capture_lead` → `request_handoff` → `followup`
- Voice pipeline intentionally independent (latency-sensitive)
- Fallback to legacy inline chat via `CHAT_ORCHESTRATOR=legacy` config

### MCP Integration
- `api/mcp_server.py` — 3 tools: `search_knowledge`, `get_sales_availability`, `create_lead`
- Mounted at `/mcp` via Streamable HTTP transport
- Same auth and consent gates as REST API
- No raw DB access exposed

### Human Handoff
- State machine: pending → assigned → resolved/cancelled
- Atomic claim via `UPDATE … WHERE status='pending'`
- Partial unique index prevents duplicate open requests per conversation
- Notification protocol (Null/SMTP) — notification failure never fails the handoff

## Directory Structure

```
api/              FastAPI backend
  chat.py         Chat route (LangGraph or legacy)
  chat_graph.py   LangGraph orchestration
  config.py       Pydantic settings
  deps.py         Dependency injection (auth, DB)
  handoff.py      Sales handoff routes + service
  knowledge.py    RAG (hybrid vector + keyword search)
  leads.py        Consent gate + lead persistence
  mcp_server.py   MCP server (3 tools)
  models.py       SQLAlchemy models
  notifications.py  Email notifier protocol
  schemas.py      Pydantic response schemas
  static/         Dashboard HTML files
voice/            Pipecat voice pipeline
  voice_auth.py   HMAC voice tokens
  static/         Widget demo page
evals/            Evaluation framework
tests/            Pytest suite (844+ tests)
migrations/       Alembic migration chain
knowledge/        NimbusCRM product knowledge docs
scripts/          Utility scripts
docs/             Project documentation
```

## Database (PostgreSQL 16 + pgvector)

Migration chain: Alembic, single-head linear history. Key tables:
- `organizations` — tenant registry with hashed API keys
- `conversations` — chat/voice sessions per org
- `messages` — conversation turns
- `leads` — consent-gated contact captures
- `knowledge_documents` / `knowledge_chunks` — RAG corpus with pgvector embeddings
- `turn_evaluations` — LLM-judged quality scores
- `sales_reps` — availability status
- `handoff_requests` — human handoff state machine

## External Dependencies

| Service | Purpose | Required |
|---|---|---|
| PostgreSQL 16 + pgvector | Persistence + vector search | Yes |
| Redis | Caching (optional) | No |
| Groq | LLM + STT + TTS | For voice and LLM chat |
| Deepgram | Alternative TTS | Optional |
| SMTP server | Handoff notifications | Optional |
| LangSmith | Tracing/observability | Optional |
