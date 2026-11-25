"""Sales availability and live-chat handoff to a human.

Flow
  1. A visitor asks for a person (the chat graph detects it, or the client calls POST
     /v1/conversations/{id}/handoff). A HandoffRequest is created (status "pending"); available reps are told
     by email if that is configured (api/notifications.py).
  2. A rep accepts it in the sales dashboard (status "assigned"). From then on the AI stays silent in that
     conversation and the rep's messages are stored with role "human".
  3. The rep resolves it (status "resolved") and the AI answers again.

Rules enforced here
  * Everything is scoped to the organization behind the API key. Another organization's rep, conversation or
    handoff is a 404, never a 403, so existence is not revealed.
  * One open request per conversation (database partial unique index), so repeated asks are idempotent.
  * Accepting is an atomic "UPDATE ... WHERE status = 'pending'": two reps cannot both win.
  * A notification failure never fails the handoff; it is recorded as notification_status "failed".
  * No telephony and no call is ever placed.
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_org, get_session
from api.models import (
    HANDOFF_OPEN_STATUSES,
    Conversation,
    HandoffRequest,
    Message,
    Organization,
    SalesRep,
)
from api.notifications import HandoffNotice, Notifier, get_notifier

logger = logging.getLogger(__name__)

RepStatus = Literal["available", "busy", "offline"]
CONTEXT_MESSAGES = 10
CONTEXT_CHARS_PER_MESSAGE = 300
CONTEXT_MAX_CHARS = 3000
TRANSCRIPT_CAP = 200

router = APIRouter(prefix="/v1/sales", tags=["sales"])
conversation_router = APIRouter(prefix="/v1/conversations", tags=["handoff"])
page_router = APIRouter(tags=["sales"])

_DASHBOARD_HTML = Path(__file__).parent / "static" / "sales_dashboard.html"


# ---------------------------------------------------------------- schemas


class RepCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    status: RepStatus = "offline"


class RepStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: RepStatus


class RepOut(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    status: str
    status_changed_at: datetime


class HandoffCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str | None = Field(default=None, max_length=300)


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rep_id: uuid.UUID


class HumanMessageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rep_id: uuid.UUID
    content: str = Field(min_length=1, max_length=2000)


class HandoffOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    status: str
    reason: str | None
    context_summary: str
    assigned_rep_id: uuid.UUID | None
    notification_status: str
    created_at: datetime
    assigned_at: datetime | None
    resolved_at: datetime | None


class TranscriptMessage(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime


class HandoffDetail(HandoffOut):
    messages: list[TranscriptMessage]


class HandoffRequestResult(BaseModel):
    handoff: HandoffOut
    created: bool
    available_now: bool


class AvailabilityOut(BaseModel):
    available: bool
    available_count: int
    busy_count: int
    offline_count: int


class VisibleMessages(BaseModel):
    handoff_status: str | None
    messages: list[TranscriptMessage]


def _rep_out(rep: SalesRep) -> RepOut:
    return RepOut(id=rep.id, name=rep.name, email=rep.email, status=rep.status,
                  status_changed_at=rep.status_changed_at)


def _handoff_out(h: HandoffRequest) -> HandoffOut:
    return HandoffOut(
        id=h.id, conversation_id=h.conversation_id, status=h.status, reason=h.reason,
        context_summary=h.context_summary, assigned_rep_id=h.assigned_rep_id,
        notification_status=h.notification_status, created_at=h.created_at,
        assigned_at=h.assigned_at, resolved_at=h.resolved_at,
    )


def _transcript(m: Message) -> TranscriptMessage:
    return TranscriptMessage(id=m.id, role=m.role, content=m.content, created_at=m.created_at)


# ---------------------------------------------------------------- service functions (also used by chat and MCP)


async def availability(session: AsyncSession, organization_id: uuid.UUID) -> dict:
    rows = (
        await session.execute(
            select(SalesRep.status, func.count())
            .where(SalesRep.organization_id == organization_id)
            .group_by(SalesRep.status)
        )
    ).all()
    counts = {name: n for name, n in rows}
    available = counts.get("available", 0)
    return {
        "available": available > 0,
        "available_count": available,
        "busy_count": counts.get("busy", 0),
        "offline_count": counts.get("offline", 0),
    }


async def open_handoff(session: AsyncSession, organization_id: uuid.UUID,
                       conversation_id: uuid.UUID) -> HandoffRequest | None:
    return await session.scalar(
        select(HandoffRequest).where(
            HandoffRequest.organization_id == organization_id,
            HandoffRequest.conversation_id == conversation_id,
            HandoffRequest.status.in_(HANDOFF_OPEN_STATUSES),
        )
    )


async def _context_summary(session: AsyncSession, organization_id: uuid.UUID,
                           conversation_id: uuid.UUID) -> str:
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.organization_id == organization_id, Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(CONTEXT_MESSAGES)
        )
    ).all()
    lines = []
    for m in reversed(rows):
        who = {"user": "Visitor", "assistant": "Assistant", "human": "Sales"}.get(m.role, m.role)
        text = " ".join(m.content.split())
        if len(text) > CONTEXT_CHARS_PER_MESSAGE:
            text = text[: CONTEXT_CHARS_PER_MESSAGE - 1] + "…"
        lines.append(f"{who}: {text}")
    return "\n".join(lines)[:CONTEXT_MAX_CHARS]


async def _notification_recipients(session: AsyncSession, organization_id: uuid.UUID) -> tuple[str, ...]:
    """Available reps first; if nobody is available, busy reps. Offline reps are never emailed."""
    for wanted in ("available", "busy"):
        emails = (
            await session.scalars(
                select(SalesRep.email).where(SalesRep.organization_id == organization_id,
                                             SalesRep.status == wanted)
            )
        ).all()
        if emails:
            return tuple(sorted(emails))
    return ()


async def request_handoff(
    session: AsyncSession, org: Organization, conversation_id: uuid.UUID, reason: str | None,
    notifier: Notifier,
) -> dict:
    """Creates (or returns the existing) open handoff for a conversation of this organization."""
    existing = await open_handoff(session, org.id, conversation_id)
    if existing is not None:
        info = await availability(session, org.id)
        return {"handoff": existing, "created": False, "available_now": info["available"]}

    summary = await _context_summary(session, org.id, conversation_id)
    handoff = HandoffRequest(organization_id=org.id, conversation_id=conversation_id,
                             reason=reason, context_summary=summary)
    session.add(handoff)
    try:
        await session.commit()
    except IntegrityError:
        # A concurrent request created the open handoff first (partial unique index).
        await session.rollback()
        existing = await open_handoff(session, org.id, conversation_id)
        if existing is None:
            raise
        info = await availability(session, org.id)
        return {"handoff": existing, "created": False, "available_now": info["available"]}

    info = await availability(session, org.id)
    recipients = await _notification_recipients(session, org.id)
    notice = HandoffNotice(
        organization_name=org.name, handoff_id=str(handoff.id), conversation_id=str(conversation_id),
        reason=reason, context_summary=summary, recipients=recipients,
    )
    try:
        outcome = await notifier.notify(notice)
    except Exception:
        # Never include the exception text: SMTP errors can echo addresses or server banners.
        logger.warning("Handoff notification failed for handoff %s", handoff.id)
        outcome = "failed"
    if outcome != handoff.notification_status:
        handoff.notification_status = outcome
        await session.commit()
    return {"handoff": handoff, "created": True, "available_now": info["available"]}


async def _get_handoff(session: AsyncSession, org: Organization, handoff_id: uuid.UUID) -> HandoffRequest:
    handoff = await session.scalar(
        select(HandoffRequest).where(HandoffRequest.id == handoff_id,
                                     HandoffRequest.organization_id == org.id)
    )
    if handoff is None:
        raise HTTPException(status_code=404, detail="Handoff not found")
    return handoff


async def _get_rep(session: AsyncSession, org: Organization, rep_id: uuid.UUID) -> SalesRep:
    rep = await session.scalar(
        select(SalesRep).where(SalesRep.id == rep_id, SalesRep.organization_id == org.id)
    )
    if rep is None:
        raise HTTPException(status_code=404, detail="Sales rep not found")
    return rep


# ---------------------------------------------------------------- sales dashboard API


@router.get("/availability", response_model=AvailabilityOut)
async def get_availability(session: AsyncSession = Depends(get_session),
                           org: Organization = Depends(get_current_org)):
    return await availability(session, org.id)


@router.post("/reps", response_model=RepOut, status_code=status.HTTP_201_CREATED)
async def create_rep(payload: RepCreate, session: AsyncSession = Depends(get_session),
                     org: Organization = Depends(get_current_org)):
    rep = SalesRep(organization_id=org.id, name=payload.name, email=str(payload.email).lower(),
                   status=payload.status)
    session.add(rep)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status_code=409, detail="A sales rep with this email already exists")
    await session.refresh(rep)
    return _rep_out(rep)


@router.get("/reps", response_model=list[RepOut])
async def list_reps(session: AsyncSession = Depends(get_session),
                    org: Organization = Depends(get_current_org)):
    reps = (await session.scalars(
        select(SalesRep).where(SalesRep.organization_id == org.id).order_by(SalesRep.name, SalesRep.id)
    )).all()
    return [_rep_out(r) for r in reps]


@router.patch("/reps/{rep_id}/status", response_model=RepOut)
async def set_rep_status(rep_id: uuid.UUID, payload: RepStatusUpdate,
                         session: AsyncSession = Depends(get_session),
                         org: Organization = Depends(get_current_org)):
    rep = await _get_rep(session, org, rep_id)
    rep.status = payload.status
    rep.status_changed_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(rep)
    return _rep_out(rep)


@router.get("/handoffs", response_model=list[HandoffOut])
async def list_handoffs(
    status_filter: Literal["pending", "assigned", "resolved", "cancelled"] | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    org: Organization = Depends(get_current_org),
):
    query = select(HandoffRequest).where(HandoffRequest.organization_id == org.id)
    if status_filter:
        query = query.where(HandoffRequest.status == status_filter)
    rows = (await session.scalars(query.order_by(HandoffRequest.created_at.desc()).limit(limit))).all()
    return [_handoff_out(h) for h in rows]


@router.get("/handoffs/{handoff_id}", response_model=HandoffDetail)
async def handoff_detail(handoff_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                         org: Organization = Depends(get_current_org)):
    handoff = await _get_handoff(session, org, handoff_id)
    messages = (await session.scalars(
        select(Message)
        .where(Message.organization_id == org.id, Message.conversation_id == handoff.conversation_id)
        .order_by(Message.created_at.asc()).limit(TRANSCRIPT_CAP)
    )).all()
    return HandoffDetail(**_handoff_out(handoff).model_dump(), messages=[_transcript(m) for m in messages])


@router.post("/handoffs/{handoff_id}/accept", response_model=HandoffOut)
async def accept_handoff(handoff_id: uuid.UUID, payload: AcceptBody,
                         session: AsyncSession = Depends(get_session),
                         org: Organization = Depends(get_current_org)):
    handoff = await _get_handoff(session, org, handoff_id)
    rep = await _get_rep(session, org, payload.rep_id)
    if rep.status == "offline":
        raise HTTPException(status_code=409, detail="This rep is offline; set them Available or Busy first")
    # Atomic claim: only one caller can move pending -> assigned.
    claimed = await session.execute(
        update(HandoffRequest)
        .where(HandoffRequest.id == handoff.id, HandoffRequest.organization_id == org.id,
               HandoffRequest.status == "pending")
        .values(status="assigned", assigned_rep_id=rep.id, assigned_at=func.now())
    )
    await session.commit()
    if claimed.rowcount != 1:
        raise HTTPException(status_code=409, detail="This handoff is no longer pending")
    await session.refresh(handoff)
    return _handoff_out(handoff)


@router.post("/handoffs/{handoff_id}/messages", response_model=TranscriptMessage,
             status_code=status.HTTP_201_CREATED)
async def post_human_message(handoff_id: uuid.UUID, payload: HumanMessageBody,
                             session: AsyncSession = Depends(get_session),
                             org: Organization = Depends(get_current_org)):
    handoff = await _get_handoff(session, org, handoff_id)
    await _get_rep(session, org, payload.rep_id)
    if handoff.status != "assigned":
        raise HTTPException(status_code=409, detail="Accept the handoff before replying")
    if handoff.assigned_rep_id != payload.rep_id:
        raise HTTPException(status_code=409, detail="This handoff is assigned to another rep")
    message = Message(organization_id=org.id, conversation_id=handoff.conversation_id,
                      role="human", content=payload.content)
    session.add(message)
    await session.commit()
    await session.refresh(message)
    return _transcript(message)


@router.post("/handoffs/{handoff_id}/resolve", response_model=HandoffOut)
async def resolve_handoff(handoff_id: uuid.UUID, session: AsyncSession = Depends(get_session),
                          org: Organization = Depends(get_current_org)):
    handoff = await _get_handoff(session, org, handoff_id)
    if handoff.status not in HANDOFF_OPEN_STATUSES:
        raise HTTPException(status_code=409, detail="This handoff is already closed")
    handoff.status = "resolved"
    handoff.resolved_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(handoff)
    return _handoff_out(handoff)


# ---------------------------------------------------------------- visitor side


async def _owned_conversation(session: AsyncSession, org: Organization,
                              conversation_id: uuid.UUID) -> Conversation:
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == conversation_id,
                                   Conversation.organization_id == org.id,
                                   Conversation.status == "active")
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@conversation_router.post("/{conversation_id}/handoff", response_model=HandoffRequestResult)
async def request_human(conversation_id: uuid.UUID, payload: HandoffCreate,
                        session: AsyncSession = Depends(get_session),
                        org: Organization = Depends(get_current_org),
                        notifier: Notifier = Depends(get_notifier)):
    await _owned_conversation(session, org, conversation_id)
    result = await request_handoff(session, org, conversation_id, payload.reason, notifier)
    return HandoffRequestResult(handoff=_handoff_out(result["handoff"]), created=result["created"],
                                available_now=result["available_now"])


@conversation_router.get("/{conversation_id}/messages", response_model=VisibleMessages)
async def visible_messages(conversation_id: uuid.UUID,
                           after: uuid.UUID | None = Query(None, description="Return only messages after this message id"),
                           session: AsyncSession = Depends(get_session),
                           org: Organization = Depends(get_current_org)):
    """What the visitor's client polls: assistant and human replies, plus the handoff state."""
    await _owned_conversation(session, org, conversation_id)
    query = select(Message).where(Message.organization_id == org.id,
                                  Message.conversation_id == conversation_id,
                                  Message.role.in_(("assistant", "human")))
    if after is not None:
        marker = await session.scalar(
            select(Message.created_at).where(Message.id == after, Message.organization_id == org.id,
                                             Message.conversation_id == conversation_id)
        )
        if marker is None:
            raise HTTPException(status_code=404, detail="Message not found")
        query = query.where(Message.created_at > marker)
    rows = (await session.scalars(query.order_by(Message.created_at.asc()).limit(TRANSCRIPT_CAP))).all()
    handoff = await session.scalar(
        select(HandoffRequest).where(HandoffRequest.organization_id == org.id,
                                     HandoffRequest.conversation_id == conversation_id)
        .order_by(HandoffRequest.created_at.desc()).limit(1)
    )
    return VisibleMessages(handoff_status=handoff.status if handoff else None,
                           messages=[_transcript(m) for m in rows])


# ---------------------------------------------------------------- dashboard page


@page_router.get("/dashboard/sales", response_class=HTMLResponse, include_in_schema=False)
async def sales_dashboard():
    """The page carries no data; it calls the authenticated routes above with a key the user pastes."""
    nonce = secrets.token_urlsafe(16)
    html = _DASHBOARD_HTML.read_text(encoding="utf-8").replace("__NONCE__", nonce)
    headers = {
        "Content-Security-Policy": (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return HTMLResponse(html, headers=headers)
