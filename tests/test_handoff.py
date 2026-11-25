"""Sales availability and live-chat handoff: API behaviour, tenant isolation and notification safety.

Uses the same real-PostgreSQL pattern as tests/test_chat.py (throwaway organizations, deleted afterwards).
No email is ever sent: the notifier is a fake, and the SMTP class is tested against a patched smtplib.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from api import notifications
from api.auth import generate_api_key, hash_api_key
from api.config import Settings
from api.db import build_engine, build_session_factory
from api.handoff import _DASHBOARD_HTML
from api.main import app
from api.models import Agent, HandoffRequest, Message, Organization


class FakeNotifier:
    def __init__(self, outcome="sent", error=None):
        self.outcome, self.error, self.notices = outcome, error, []

    async def notify(self, notice):
        self.notices.append(notice)
        if self.error:
            raise self.error
        return self.outcome


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _make_org(session_factory, label):
    raw_key = generate_api_key()
    async with session_factory() as session:
        org = Organization(name=f"{label} Co", slug=f"{label}-{uuid.uuid4().hex[:8]}",
                           api_key_hash=hash_api_key(raw_key))
        session.add(org)
        await session.flush()
        agent = Agent(organization_id=org.id, name="Agent", system_prompt="Be brief.")
        session.add(agent)
        await session.commit()
        return org.id, agent.id, raw_key


@pytest.fixture
async def world():
    engine = build_engine()
    session_factory = build_session_factory(engine)
    app.state.session_factory = session_factory
    notifier = FakeNotifier()
    app.dependency_overrides[notifications.get_notifier] = lambda: notifier
    org_a, agent_a, key_a = await _make_org(session_factory, "a")
    org_b, agent_b, key_b = await _make_org(session_factory, "b")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": f"Bearer {key_a}"}) as a, \
               AsyncClient(transport=transport, base_url="http://test",
                           headers={"Authorization": f"Bearer {key_b}"}) as b:
        yield {"a": a, "b": b, "agent_a": agent_a, "agent_b": agent_b, "sf": session_factory,
               "notifier": notifier, "org_a": org_a, "org_b": org_b}
    async with session_factory() as session:
        await session.execute(delete(Organization).where(Organization.id.in_([org_a, org_b])))
        await session.commit()
    app.dependency_overrides.clear()
    await engine.dispose()


async def _conversation(client, agent_id):
    r = await client.post("/v1/conversations", json={"agent_id": str(agent_id)})
    assert r.status_code == 201
    return r.json()["conversation_id"]


async def _rep(client, name="Asha", email=None, status="available"):
    r = await client.post("/v1/sales/reps", json={
        "name": name, "email": email or f"{name.lower()}-{uuid.uuid4().hex[:6]}@example.com", "status": status})
    assert r.status_code == 201, r.text
    return r.json()


async def _add_messages(sf, org_id, conversation_id, items):
    # One commit per message, like the chat route: created_at defaults to the transaction start, so
    # messages written in a single transaction would tie on time and have no defined order.
    for role, content in items:
        async with sf() as session:
            session.add(Message(organization_id=org_id, conversation_id=uuid.UUID(conversation_id),
                                role=role, content=content))
            await session.commit()


# ------------------------------------------------------------------ auth and availability


@pytest.mark.anyio
async def test_sales_routes_require_a_valid_api_key(world):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        for method, path in [("GET", "/v1/sales/availability"), ("GET", "/v1/sales/reps"),
                             ("GET", "/v1/sales/handoffs"), ("POST", f"/v1/conversations/{uuid.uuid4()}/handoff")]:
            r = await anon.request(method, path, json={} if method == "POST" else None)
            assert r.status_code == 401, (method, path)
        bad = await anon.get("/v1/sales/availability", headers={"Authorization": "Bearer vsa_wrong"})
        assert bad.status_code == 401


@pytest.mark.anyio
async def test_availability_reflects_rep_status_and_is_per_organization(world):
    a, b = world["a"], world["b"]
    empty = (await a.get("/v1/sales/availability")).json()
    assert empty == {"available": False, "available_count": 0, "busy_count": 0, "offline_count": 0}

    asha = await _rep(a, "Asha", status="available")
    await _rep(a, "Ben", status="busy")
    await _rep(a, "Cara", status="offline")
    now = (await a.get("/v1/sales/availability")).json()
    assert now == {"available": True, "available_count": 1, "busy_count": 1, "offline_count": 1}

    toggled = await a.patch(f"/v1/sales/reps/{asha['id']}/status", json={"status": "busy"})
    assert toggled.status_code == 200 and toggled.json()["status"] == "busy"
    assert (await a.get("/v1/sales/availability")).json()["available"] is False

    # Organization B sees nothing of A's team.
    assert (await b.get("/v1/sales/availability")).json()["available_count"] == 0
    assert (await b.get("/v1/sales/reps")).json() == []


@pytest.mark.anyio
async def test_rep_validation_duplicates_and_cross_org_status_change(world):
    a, b = world["a"], world["b"]
    assert (await a.post("/v1/sales/reps", json={"name": "", "email": "x@example.com"})).status_code == 422
    assert (await a.post("/v1/sales/reps", json={"name": "N", "email": "not-an-email"})).status_code == 422
    assert (await a.post("/v1/sales/reps", json={"name": "N", "email": "n@example.com", "status": "away"})).status_code == 422
    assert (await a.post("/v1/sales/reps", json={"name": "N", "email": "n@example.com", "extra": 1})).status_code == 422
    first = await _rep(a, "Dup", "dup@example.com")
    assert (await a.post("/v1/sales/reps", json={"name": "Dup2", "email": "DUP@example.com"})).status_code == 409
    # The same email in another organization is fine.
    assert (await b.post("/v1/sales/reps", json={"name": "Dup", "email": "dup@example.com"})).status_code == 201
    # B cannot change A's rep, and gets a 404 that does not reveal it exists.
    r = await b.patch(f"/v1/sales/reps/{first['id']}/status", json={"status": "offline"})
    assert r.status_code == 404
    assert [r["status"] for r in (await a.get("/v1/sales/reps")).json()] == ["available"]


# ------------------------------------------------------------------ requesting a handoff


@pytest.mark.anyio
async def test_requesting_a_human_creates_one_pending_handoff_with_context_and_is_idempotent(world):
    a, sf = world["a"], world["sf"]
    await _rep(a, "Asha", "asha@example.com", "available")
    conv = await _conversation(a, world["agent_a"])
    await _add_messages(sf, world["org_a"], conv, [("user", "Can I talk to someone about pricing?"),
                                                   ("assistant", "Sure, what would you like to know?")])
    first = await a.post(f"/v1/conversations/{conv}/handoff", json={"reason": "Pricing question"})
    assert first.status_code == 200
    body = first.json()
    assert body["created"] is True and body["available_now"] is True
    assert body["handoff"]["status"] == "pending"
    assert "Visitor: Can I talk to someone about pricing?" in body["handoff"]["context_summary"]

    again = await a.post(f"/v1/conversations/{conv}/handoff", json={})
    assert again.json()["created"] is False
    assert again.json()["handoff"]["id"] == body["handoff"]["id"]

    async with sf() as session:
        rows = (await session.scalars(select(HandoffRequest).where(
            HandoffRequest.conversation_id == uuid.UUID(conv)))).all()
    assert len(rows) == 1
    assert len(world["notifier"].notices) == 1  # no second email for the repeated ask
    assert world["notifier"].notices[0].recipients == ("asha@example.com",)


@pytest.mark.anyio
async def test_nobody_available_still_records_the_request_and_reports_it(world):
    a = world["a"]
    await _rep(a, "Off", "off@example.com", "offline")
    conv = await _conversation(a, world["agent_a"])
    r = await a.post(f"/v1/conversations/{conv}/handoff", json={})
    assert r.status_code == 200
    assert r.json()["available_now"] is False and r.json()["handoff"]["status"] == "pending"
    # Offline reps are never emailed.
    assert world["notifier"].notices[0].recipients == ()


@pytest.mark.anyio
async def test_busy_reps_are_notified_only_when_nobody_is_available(world):
    a = world["a"]
    await _rep(a, "Busy", "busy@example.com", "busy")
    conv = await _conversation(a, world["agent_a"])
    await a.post(f"/v1/conversations/{conv}/handoff", json={})
    assert world["notifier"].notices[0].recipients == ("busy@example.com",)


@pytest.mark.anyio
async def test_a_notification_failure_never_fails_the_handoff(world):
    a, sf = world["a"], world["sf"]
    world["notifier"].error = RuntimeError("smtp password=hunter2 refused")
    await _rep(a, "Asha", "asha@example.com", "available")
    conv = await _conversation(a, world["agent_a"])
    r = await a.post(f"/v1/conversations/{conv}/handoff", json={})
    assert r.status_code == 200
    assert r.json()["handoff"]["notification_status"] == "failed"
    assert "hunter2" not in r.text
    async with sf() as session:
        row = await session.scalar(select(HandoffRequest).where(HandoffRequest.conversation_id == uuid.UUID(conv)))
    assert row.status == "pending" and row.notification_status == "failed"


@pytest.mark.anyio
async def test_unconfigured_email_is_recorded_honestly(world):
    a = world["a"]
    world["notifier"].outcome = "not_configured"
    conv = await _conversation(a, world["agent_a"])
    r = await a.post(f"/v1/conversations/{conv}/handoff", json={})
    assert r.json()["handoff"]["notification_status"] == "not_configured"


@pytest.mark.anyio
async def test_other_organizations_conversation_cannot_be_handed_off(world):
    a, b = world["a"], world["b"]
    conv_a = await _conversation(a, world["agent_a"])
    assert (await b.post(f"/v1/conversations/{conv_a}/handoff", json={})).status_code == 404
    assert (await b.get(f"/v1/conversations/{conv_a}/messages")).status_code == 404
    assert (await a.post(f"/v1/conversations/{uuid.uuid4()}/handoff", json={})).status_code == 404


@pytest.mark.anyio
async def test_database_allows_only_one_open_handoff_per_conversation(world):
    a, sf = world["a"], world["sf"]
    conv = await _conversation(a, world["agent_a"])
    await a.post(f"/v1/conversations/{conv}/handoff", json={})
    async with sf() as session:
        session.add(HandoffRequest(organization_id=world["org_a"], conversation_id=uuid.UUID(conv)))
        with pytest.raises(IntegrityError):
            await session.commit()


# ------------------------------------------------------------------ accept, reply, resolve


async def _pending(world, rep_status="available"):
    a = world["a"]
    rep = await _rep(a, "Asha", status=rep_status)
    conv = await _conversation(a, world["agent_a"])
    await _add_messages(world["sf"], world["org_a"], conv, [("user", "Hi"), ("assistant", "Hello")])
    handoff = (await a.post(f"/v1/conversations/{conv}/handoff", json={})).json()["handoff"]
    return rep, conv, handoff


@pytest.mark.anyio
async def test_accept_is_atomic_so_a_second_rep_cannot_also_take_it(world):
    a = world["a"]
    rep1, conv, handoff = await _pending(world)
    rep2 = await _rep(a, "Ben")
    first = await a.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep1["id"]})
    assert first.status_code == 200
    assert first.json()["status"] == "assigned" and first.json()["assigned_rep_id"] == rep1["id"]
    second = await a.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep2["id"]})
    assert second.status_code == 409
    detail = (await a.get(f"/v1/sales/handoffs/{handoff['id']}")).json()
    assert detail["assigned_rep_id"] == rep1["id"]


@pytest.mark.anyio
async def test_an_offline_rep_cannot_accept_and_other_orgs_cannot_touch_it(world):
    a, b = world["a"], world["b"]
    rep, conv, handoff = await _pending(world)
    await a.patch(f"/v1/sales/reps/{rep['id']}/status", json={"status": "offline"})
    assert (await a.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep["id"]})).status_code == 409
    rep_b = await _rep(b, "Bea")
    # B can neither see, accept, reply to nor resolve A's handoff.
    assert (await b.get(f"/v1/sales/handoffs/{handoff['id']}")).status_code == 404
    assert (await b.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep_b["id"]})).status_code == 404
    assert (await b.post(f"/v1/sales/handoffs/{handoff['id']}/resolve")).status_code == 404
    assert (await b.get("/v1/sales/handoffs")).json() == []
    # A cannot assign B's rep to A's handoff.
    assert (await a.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep_b["id"]})).status_code == 404


@pytest.mark.anyio
async def test_live_chat_roundtrip_human_reply_is_stored_and_visible_then_resolved(world):
    a, sf = world["a"], world["sf"]
    rep, conv, handoff = await _pending(world)

    # Replying before accepting is refused.
    early = await a.post(f"/v1/sales/handoffs/{handoff['id']}/messages", json={"rep_id": rep["id"], "content": "hi"})
    assert early.status_code == 409
    await a.post(f"/v1/sales/handoffs/{handoff['id']}/accept", json={"rep_id": rep["id"]})

    # Another rep cannot speak in this chat.
    other = await _rep(a, "Ben")
    assert (await a.post(f"/v1/sales/handoffs/{handoff['id']}/messages",
                         json={"rep_id": other["id"], "content": "mine"})).status_code == 409

    sent = await a.post(f"/v1/sales/handoffs/{handoff['id']}/messages",
                        json={"rep_id": rep["id"], "content": "Hi, this is Asha from sales."})
    assert sent.status_code == 201 and sent.json()["role"] == "human"

    visible = (await a.get(f"/v1/conversations/{conv}/messages")).json()
    assert visible["handoff_status"] == "assigned"
    assert [m["role"] for m in visible["messages"]][-1] == "human"
    last_id = visible["messages"][-1]["id"]
    assert (await a.get(f"/v1/conversations/{conv}/messages", params={"after": last_id})).json()["messages"] == []
    assert (await a.get(f"/v1/conversations/{conv}/messages", params={"after": str(uuid.uuid4())})).status_code == 404

    resolved = await a.post(f"/v1/sales/handoffs/{handoff['id']}/resolve")
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_at"] is not None
    assert (await a.post(f"/v1/sales/handoffs/{handoff['id']}/resolve")).status_code == 409
    assert (await a.post(f"/v1/sales/handoffs/{handoff['id']}/messages",
                         json={"rep_id": rep["id"], "content": "late"})).status_code == 409

    # After resolution the visitor can ask again and gets a fresh handoff.
    again = (await a.post(f"/v1/conversations/{conv}/handoff", json={})).json()
    assert again["created"] is True and again["handoff"]["id"] != handoff["id"]


@pytest.mark.anyio
async def test_handoff_detail_transfers_the_conversation_context(world):
    a = world["a"]
    rep, conv, handoff = await _pending(world)
    detail = (await a.get(f"/v1/sales/handoffs/{handoff['id']}")).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["context_summary"].startswith("Visitor: Hi")


@pytest.mark.anyio
async def test_list_filters_by_status(world):
    a = world["a"]
    rep, conv, handoff = await _pending(world)
    assert len((await a.get("/v1/sales/handoffs", params={"status": "pending"})).json()) == 1
    assert (await a.get("/v1/sales/handoffs", params={"status": "assigned"})).json() == []
    assert (await a.get("/v1/sales/handoffs", params={"status": "bogus"})).status_code == 422


# ------------------------------------------------------------------ dashboard page


@pytest.mark.anyio
async def test_sales_dashboard_page_is_served_with_a_strict_csp_and_no_innerhtml(world):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        r = await anon.get("/dashboard/sales")
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    nonce = csp.split("script-src 'nonce-")[1].split("'")[0]
    assert f'nonce="{nonce}"' in r.text and "__NONCE__" not in r.text
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["cache-control"] == "no-store"
    source = _DASHBOARD_HTML.read_text(encoding="utf-8")
    assert "innerHTML" not in source and "localStorage" not in source and "sessionStorage" not in source


# ------------------------------------------------------------------ notifier unit tests (no network)


def _settings(**kw):
    base = dict(_env_file=None, groq_api_key="x", groq_model="m", postgres_user="u", postgres_password="p", postgres_db="d", redis_enabled=False)
    base.update(kw)
    return Settings(**base)


def test_email_is_off_unless_fully_configured():
    assert isinstance(notifications.notifier_from_settings(_settings()), notifications.NullNotifier)
    assert isinstance(notifications.notifier_from_settings(_settings(handoff_email_enabled=True)),
                      notifications.NullNotifier)
    ready = _settings(handoff_email_enabled=True, smtp_host="smtp.test", smtp_from="alerts@example.com")
    assert isinstance(notifications.notifier_from_settings(ready), notifications.SmtpNotifier)


@pytest.mark.anyio
async def test_smtp_notifier_sends_one_plain_text_mail_without_secrets_in_the_body(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["conn"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr(notifications.smtplib, "SMTP", FakeSMTP)
    s = _settings(handoff_email_enabled=True, smtp_host="smtp.test", smtp_from="alerts@example.com",
                  smtp_username="user", smtp_password="s3cret")
    notice = notifications.HandoffNotice("Acme", "h1", "c1", "Pricing", "Visitor: hi", ("a@example.com", "b@example.com"))
    assert await notifications.SmtpNotifier(s).notify(notice) == "sent"
    assert sent["conn"] == ("smtp.test", 587, notifications.SMTP_TIMEOUT_SECONDS) and sent["tls"] is True
    assert sent["login"] == ("user", "s3cret")
    msg = sent["message"]
    assert msg["To"] == "a@example.com, b@example.com" and "s3cret" not in msg.as_string()
    assert "Visitor: hi" in msg.get_content()


@pytest.mark.anyio
async def test_smtp_notifier_with_no_recipients_sends_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not connect")
    monkeypatch.setattr(notifications.smtplib, "SMTP", boom)
    s = _settings(handoff_email_enabled=True, smtp_host="smtp.test", smtp_from="alerts@example.com")
    notice = notifications.HandoffNotice("Acme", "h1", "c1", None, "", ())
    assert await notifications.SmtpNotifier(s).notify(notice) == "not_configured"
