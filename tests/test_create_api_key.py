"""Safety tests for scripts/create_api_key.py.

The raw key must only ever be shown on an interactive terminal, and a refusal
must happen BEFORE any database change (otherwise a piped run would rotate the
stored hash and the new key would be lost).

Labels: [mocked] needs no database; [real-DB] uses the PostgreSQL that
`build_engine()` resolves, with a throwaway organization deleted afterwards.
No real key is generated or shown: key generation is patched to a fixed fake.
"""

import io
import uuid

import pytest
from sqlalchemy import delete, select

from api.auth import hash_api_key
from api.db import build_engine, build_session_factory
from api.models import Organization
from scripts import create_api_key

FAKE_KEY = "vsa_unit-test-key-not-a-real-secret"
OLD_KEY = "vsa_previous-fake-key-not-real"


class Terminal(io.StringIO):
    """A stream that claims to be an interactive terminal."""

    def isatty(self):
        return True


class Pipe(io.StringIO):
    """A redirected/captured stream (StringIO.isatty() is False)."""


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def temp_org():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    slug = f"keyscript-test-{uuid.uuid4().hex[:8]}"
    async with session_factory() as session:
        org = Organization(name="Key Script Test Co", slug=slug)
        session.add(org)
        await session.commit()
        org_id = org.id

    async def stored_hash():
        async with session_factory() as session:
            return await session.scalar(
                select(Organization.api_key_hash).where(Organization.id == org_id)
            )

    async def set_hash(value):
        async with session_factory() as session:
            org = await session.get(Organization, org_id)
            org.api_key_hash = value
            await session.commit()

    yield slug, stored_hash, set_hash

    async with session_factory() as session:
        await session.execute(delete(Organization).where(Organization.id == org_id))
        await session.commit()
    await engine.dispose()


@pytest.fixture
def fake_key(monkeypatch):
    monkeypatch.setattr(create_api_key, "generate_api_key", lambda: FAKE_KEY)


def no_database(monkeypatch):
    def forbidden():
        raise AssertionError("database touched before the interactive check")

    monkeypatch.setattr(create_api_key, "build_engine", forbidden)


# --- [mocked] refuses when output is captured --------------------------------
@pytest.mark.anyio
async def test_refuses_when_stdout_is_not_a_terminal(monkeypatch, fake_key):
    no_database(monkeypatch)
    stdout = Pipe()

    with pytest.raises(SystemExit) as refusal:
        await create_api_key.run("any-org", False, stdin=Terminal(), stdout=stdout)

    assert refusal.value.code not in (0, None)
    assert FAKE_KEY not in stdout.getvalue()
    assert FAKE_KEY not in str(refusal.value)


# --- [mocked] refuses when input is not a terminal ----------------------------
@pytest.mark.anyio
async def test_refuses_when_stdin_is_not_a_terminal(monkeypatch, fake_key):
    no_database(monkeypatch)
    stdout = Terminal()

    with pytest.raises(SystemExit) as refusal:
        await create_api_key.run("any-org", True, stdin=Pipe(), stdout=stdout)

    assert refusal.value.code not in (0, None)
    assert FAKE_KEY not in stdout.getvalue()


# --- [real-DB] a refused rotate must not change the stored hash ---------------
@pytest.mark.anyio
async def test_refused_rotation_leaves_the_existing_key_working(temp_org, fake_key):
    slug, stored_hash, set_hash = temp_org
    await set_hash(hash_api_key(OLD_KEY))

    with pytest.raises(SystemExit):
        await create_api_key.run(slug, True, stdin=Pipe(), stdout=Pipe())

    assert await stored_hash() == hash_api_key(OLD_KEY)


# --- [real-DB] interactive creation: shown once, then cleared -----------------
@pytest.mark.anyio
async def test_interactive_creation_shows_the_key_once_then_clears_the_screen(
    temp_org, fake_key
):
    slug, stored_hash, _ = temp_org
    stdout = Terminal()
    events = []

    def wait(_prompt=""):
        # By the time the user is asked to confirm, the key is already on screen.
        events.append(("waited", FAKE_KEY in stdout.getvalue()))
        return ""

    await create_api_key.run(slug, False, stdin=Terminal(), stdout=stdout, wait=wait)

    out = stdout.getvalue()
    assert out.count(FAKE_KEY) == 1
    assert events == [("waited", True)]
    assert create_api_key.CLEAR_SCREEN in out
    assert out.index(create_api_key.CLEAR_SCREEN) > out.index(FAKE_KEY)  # cleared after
    # Only the hash is stored, never the raw key.
    saved = await stored_hash()
    assert saved == hash_api_key(FAKE_KEY)
    assert FAKE_KEY not in saved


# --- [real-DB] closed stdin at the confirmation prompt still clears -----------
@pytest.mark.anyio
async def test_screen_is_cleared_even_if_the_confirmation_prompt_fails(temp_org, fake_key):
    slug, _, _ = temp_org
    stdout = Terminal()

    def closed_stdin(_prompt=""):
        raise EOFError

    await create_api_key.run(slug, False, stdin=Terminal(), stdout=stdout, wait=closed_stdin)

    assert create_api_key.CLEAR_SCREEN in stdout.getvalue()


# --- [real-DB] existing behaviour preserved: no silent overwrite --------------
@pytest.mark.anyio
async def test_existing_key_is_not_overwritten_without_rotate(temp_org, fake_key):
    slug, stored_hash, set_hash = temp_org
    await set_hash(hash_api_key(OLD_KEY))
    stdout = Terminal()

    with pytest.raises(SystemExit) as refusal:
        await create_api_key.run(slug, False, stdin=Terminal(), stdout=stdout, wait=lambda _="": "")

    assert "--rotate" in str(refusal.value)
    assert FAKE_KEY not in stdout.getvalue()
    assert await stored_hash() == hash_api_key(OLD_KEY)


# --- [real-DB] existing behaviour preserved: intentional rotation works -------
@pytest.mark.anyio
async def test_interactive_rotation_replaces_the_key(temp_org, fake_key):
    slug, stored_hash, set_hash = temp_org
    await set_hash(hash_api_key(OLD_KEY))
    stdout = Terminal()

    await create_api_key.run(slug, True, stdin=Terminal(), stdout=stdout, wait=lambda _="": "")

    assert await stored_hash() == hash_api_key(FAKE_KEY)
    assert stdout.getvalue().count(FAKE_KEY) == 1


# --- [real-DB] unknown organisation: nothing is created or shown --------------
@pytest.mark.anyio
async def test_unknown_organisation_creates_nothing(fake_key):
    stdout = Terminal()

    with pytest.raises(SystemExit) as refusal:
        await create_api_key.run(
            f"no-such-org-{uuid.uuid4().hex[:8]}",
            False,
            stdin=Terminal(),
            stdout=stdout,
            wait=lambda _="": "",
        )

    assert "not found" in str(refusal.value)
    assert FAKE_KEY not in stdout.getvalue()
