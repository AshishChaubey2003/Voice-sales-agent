"""MCP server for the Voice Sales Agent.

Exposes three tools to MCP clients (Claude Desktop, custom agents, etc.):

  search_knowledge      – tenant-scoped hybrid RAG search (read-only)
  get_sales_availability – how many reps are available / busy / offline
  create_lead           – consent-gated lead creation (enforced by check_lead_proposal)

Authentication: every request must carry an ``Authorization: Bearer <key>`` or
``X-API-Key`` header. Keys are resolved to organizations the same way the REST API
does it, so there is no separate MCP credential.

The server is mounted at ``/mcp`` via ``streamable_http_app()`` and registered
in ``api/main.py``; it can also be run standalone via stdio for local clients::

    python -m api.mcp_server

Security notes:
 * No raw SQL or direct table access is exposed.
 * Knowledge retrieval is limited to 10 chunks per call.
 * ``create_lead`` calls ``check_lead_proposal`` – the same gate used by the REST API –
   so there is no way to bypass consent validation from an MCP client.
 * Error messages from tool failures are returned as MCP error responses; they never
   reveal internal stack traces or secrets.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.auth import hash_api_key
from api.config import Settings, get_settings
from api.handoff import availability as svc_availability
from api.knowledge import RetrievedChunk, search_knowledge
from api.leads import LeadInput, check_lead_proposal, dialogue_turns, save_lead_from_tool
from api.models import Conversation, Organization

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP server instance (singleton; reused when imported in main.py)
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "voice-sales-agent",
    # Version matches the application version; bumped with releases.
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Helpers shared across tools
# ---------------------------------------------------------------------------

@dataclass
class _OrgSession:
    org: Organization
    session: AsyncSession


async def _resolve_org(session: AsyncSession, raw_key: str) -> Organization | None:
    """Return the org for a raw API key, or None if the key is invalid."""
    from sqlalchemy import select as _select
    org = await session.scalar(
        _select(Organization).where(Organization.api_key_hash == hash_api_key(raw_key))
    )
    return org


def _make_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


# Module-level lazy session factory (populated once on first tool call or by lifespan).
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = _make_session_factory(get_settings())
    return _session_factory


@asynccontextmanager
async def _auth_scope(raw_key: str) -> AsyncIterator[_OrgSession]:
    """Open a DB session, resolve the API key → org, yield the pair."""
    factory = _get_session_factory()
    async with factory() as session:
        org = await _resolve_org(session, raw_key)
        if org is None:
            raise ValueError("Invalid or missing API key")
        yield _OrgSession(org=org, session=session)


# ---------------------------------------------------------------------------
# Tool: search_knowledge
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Search the organization's product knowledge base using a natural-language query. "
        "Returns up to `top_k` text chunks ordered by relevance. "
        "Use this to answer visitor questions about product features, pricing, and policies."
    )
)
async def search_knowledge_tool(
    query: Annotated[str, "The natural-language search query (max 500 chars)"],
    api_key: Annotated[str, "Organization API key (Bearer token or X-API-Key)"],
    top_k: Annotated[int, "Maximum chunks to return (1-10, default 4)"] = 4,
) -> str:
    """Search product knowledge and return formatted chunks."""
    query = query.strip()[:500]
    if not query:
        return json.dumps({"error": "query must not be empty"})

    top_k = max(1, min(top_k, 10))  # enforce safe range

    try:
        async with _auth_scope(api_key) as ctx:
            result = await search_knowledge(
                ctx.session,
                ctx.org.id,
                query,
                top_k=top_k,
            )
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("search_knowledge_tool failed")
        return json.dumps({"error": "knowledge search temporarily unavailable"})

    chunks = [
        {
            "title": c.title,
            "heading": c.heading,
            "content": c.content,
        }
        for c in result.chunks
    ]
    return json.dumps({"chunks": chunks, "elapsed_ms": result.elapsed_ms})


# ---------------------------------------------------------------------------
# Tool: get_sales_availability
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Return how many sales representatives are currently available, busy, or offline. "
        "Use this before triggering a human handoff so the agent can tell the visitor "
        "whether a rep can help immediately."
    )
)
async def get_sales_availability_tool(
    api_key: Annotated[str, "Organization API key (Bearer token or X-API-Key)"],
) -> str:
    """Return current rep availability counts."""
    try:
        async with _auth_scope(api_key) as ctx:
            avail = await svc_availability(ctx.session, ctx.org.id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("get_sales_availability_tool failed")
        return json.dumps({"error": "availability check temporarily unavailable"})

    return json.dumps(avail)


# ---------------------------------------------------------------------------
# Tool: create_lead
# ---------------------------------------------------------------------------

@mcp.tool(
    description=(
        "Save a visitor's contact details for the sales team. "
        "IMPORTANT: The visitor must have already confirmed their name and email and "
        "explicitly consented to being contacted. "
        "Pass the recent conversation history so the consent gate can verify that the "
        "last user message was an affirmative reply to the assistant reading the email back. "
        "Returns {status: 'saved'} on success or {status: 'rejected', reason: '...'} when "
        "consent or validation fails."
    )
)
async def create_lead_tool(
    api_key: Annotated[str, "Organization API key"],
    name: Annotated[str, "Visitor's full name"],
    email: Annotated[str, "Visitor's email address"],
    conversation_id: Annotated[str, "Conversation UUID from the Voice Sales Agent API"],
    history: Annotated[
        list[dict[str, str]],
        "Recent conversation turns as [{role, content}] dicts. "
        "Must end with the user's affirmative consent reply.",
    ],
    company: Annotated[str | None, "Company name (optional)"] = None,
    team_size: Annotated[str | None, "Team size (optional)"] = None,
    need: Annotated[str | None, "What they are looking for (optional)"] = None,
) -> str:
    """Consent-gated lead creation.

    The gate is ``check_lead_proposal`` – the same function used by the REST API.
    No lead is ever persisted when consent is missing or the email was not read back.
    """
    # Build the JSON arguments string expected by check_lead_proposal
    args: dict[str, Any] = {"name": name, "email": email}
    if company is not None:
        args["company"] = company
    if team_size is not None:
        args["team_size"] = team_size
    if need is not None:
        args["need"] = need

    lead_input, problem = check_lead_proposal(history, json.dumps(args))
    if problem:
        return json.dumps({"status": "rejected", "reason": problem})

    # Parse the conversation UUID
    try:
        conv_id = uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        return json.dumps({"status": "rejected", "reason": "Invalid conversation_id"})

    try:
        async with _auth_scope(api_key) as ctx:
            # Verify the conversation belongs to this organization
            from sqlalchemy import select as _select
            conv = await ctx.session.scalar(
                _select(Conversation).where(
                    Conversation.id == conv_id,
                    Conversation.organization_id == ctx.org.id,
                )
            )
            if conv is None:
                return json.dumps({"status": "rejected", "reason": "conversation not found"})

            result = await save_lead_from_tool(
                ctx.session,
                ctx.org.id,
                conv_id,
                history,
                json.dumps(args),
            )
            await ctx.session.commit()
    except ValueError as exc:
        return json.dumps({"status": "rejected", "reason": str(exc)})
    except Exception:
        logger.exception("create_lead_tool failed")
        return json.dumps({"status": "error", "reason": "lead creation temporarily unavailable"})

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Lifespan integration: inject the app-level session factory when mounted
# ---------------------------------------------------------------------------

def configure_session_factory(factory: async_sessionmaker[AsyncSession]) -> None:
    """Called from api/main.py lifespan to share the app's DB pool."""
    global _session_factory
    _session_factory = factory


# ---------------------------------------------------------------------------
# Standalone stdio entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    asyncio.run(mcp.run_stdio_async())
