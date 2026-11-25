from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import hash_api_key
from api.knowledge import search_knowledge
from api.llm import LLMClient
from api.models import Organization


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm


def get_knowledge_search():
    return search_knowledge


def _extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Reads the API key from Authorization: Bearer <key>, or X-API-Key."""
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    return None


async def get_current_org(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Organization:
    """Resolves the organization from the request's API key, server-side.

    401 when the key is missing or does not match any organization. The error
    never says which of the two it was, and never echoes the key.
    """
    raw_key = _extract_api_key(authorization, x_api_key)
    if raw_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    org = await session.scalar(
        select(Organization).where(
            Organization.api_key_hash == hash_api_key(raw_key)
        )
    )
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return org
