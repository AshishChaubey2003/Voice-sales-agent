"""Private voice access: token codec, runner-level gate, and token issuance.

The gate tests use a small FastAPI app that mirrors the runner's route shapes
(/start, /api/offer, /sessions/{id}/api/offer, /status, /client, /ws) and records
whether the handler ran. A refused request must never reach the handler, which is
what "rejected before the SDP answer" means in practice.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.config import Settings
from api.deps import get_current_org
from api.voice_tokens import router as voice_tokens_router
from voice.voice_auth import (
    VoiceAuthMiddleware,
    _b64encode,
    _is_public,
    _now,
    _sign,
    install_voice_auth,
    issue_voice_token,
    production_token_errors,
    verify_voice_token,
)

SECRET = "s" * 32
OTHER_SECRET = "o" * 32
NOW = 1_800_000_000
TTL = 300


def valid_token(now: int = NOW, secret: str = SECRET) -> str:
    return issue_voice_token(secret.encode(), "org-1", TTL, now=now)


# ---------------------------------------------------------------- codec


def test_issued_token_verifies_before_expiry():
    token = valid_token()
    assert verify_voice_token(token, SECRET.encode(), now=NOW + 1)


def test_token_is_refused_at_and_after_expiry():
    token = valid_token()
    assert not verify_voice_token(token, SECRET.encode(), now=NOW + TTL)
    assert not verify_voice_token(token, SECRET.encode(), now=NOW + TTL + 1)


def test_token_signed_with_another_secret_is_refused():
    token = valid_token(secret=OTHER_SECRET)
    assert not verify_voice_token(token, SECRET.encode(), now=NOW)


def test_tampered_payload_is_refused():
    token = valid_token()
    version, body, signature = token.split(".")
    forged_body = _b64encode(
        b'{"exp":9999999999,"iat":1,"scope":"voice","sub":"someone-else"}'
    )
    assert not verify_voice_token(
        f"{version}.{forged_body}.{signature}", SECRET.encode(), now=NOW
    )


def test_token_without_voice_scope_is_refused_even_when_signed():
    body = _b64encode(b'{"sub":"org-1","scope":"admin","iat":1,"exp":9999999999}')
    signing_input = f"v1.{body}"
    signature = _b64encode(_sign(SECRET.encode(), signing_input))
    assert not verify_voice_token(f"{signing_input}.{signature}", SECRET.encode(), now=NOW)


def test_token_with_non_integer_expiry_is_refused():
    body = _b64encode(b'{"sub":"org-1","scope":"voice","exp":"forever"}')
    signing_input = f"v1.{body}"
    signature = _b64encode(_sign(SECRET.encode(), signing_input))
    assert not verify_voice_token(f"{signing_input}.{signature}", SECRET.encode(), now=NOW)


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "v1",
        "v1.only-two-parts",
        "v2.body.sig",
        "v1..",
        "v1.!!!.???",
        "v1.ä.ö",
        "x" * 3000,
    ],
)
def test_malformed_tokens_are_refused_without_raising(token):
    assert not verify_voice_token(token, SECRET.encode(), now=NOW)


def test_missing_or_short_secret_refuses_every_token():
    token = valid_token()
    assert not verify_voice_token(token, None, now=NOW)
    assert not verify_voice_token(token, b"", now=NOW)
    assert not verify_voice_token(token, b"short", now=NOW)


def test_issuing_with_a_short_secret_is_an_error():
    with pytest.raises(ValueError):
        issue_voice_token(b"short", "org-1", TTL, now=NOW)


def test_production_requires_a_usable_secret_and_development_does_not():
    assert production_token_errors("production", None)
    assert production_token_errors("production", "too-short")
    assert production_token_errors("production", SECRET) == []
    assert production_token_errors("development", None) == []


# ---------------------------------------------------------------- gate


def build_runner_like_app(calls: list[str], secret: str | None, clock=_now) -> FastAPI:
    app = FastAPI()

    @app.post("/start")
    async def start():
        calls.append("/start")
        return {"ok": True}

    @app.post("/api/offer")
    async def offer():
        calls.append("/api/offer")
        return {"sdp": "answer"}

    @app.post("/sessions/{session_id}/{path:path}")
    async def proxy(session_id: str, path: str):
        calls.append(f"/sessions/{path}")
        return {"ok": True}

    @app.get("/status")
    async def status_route():
        calls.append("/status")
        return {"sessions": 0}

    @app.get("/client/{name:path}")
    async def client_ui(name: str):
        calls.append("/client")
        return {"ui": name}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        calls.append("/ws")
        await websocket.accept()
        await websocket.close()

    app.add_middleware(
        VoiceAuthMiddleware,
        secret=secret.encode() if secret else None,
        clock=clock,
    )
    return app


@pytest.fixture
def calls():
    return []


@pytest.fixture
def client(calls):
    return TestClient(build_runner_like_app(calls, SECRET))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_offer_without_token_is_refused_before_the_handler_runs(client, calls):
    response = client.post("/api/offer", json={"sdp": "x"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer "},
        {"X-Voice-Token": "ignored-header"},
    ],
)
def test_invalid_or_wrong_scheme_tokens_are_refused(client, calls, headers):
    response = client.post("/api/offer", json={}, headers=headers)
    assert response.status_code == 401
    assert calls == []


def test_expired_token_is_refused(calls):
    app = build_runner_like_app(calls, SECRET, clock=lambda: NOW + TTL + 1)
    response = TestClient(app).post("/api/offer", json={}, headers=bearer(valid_token()))
    assert response.status_code == 401
    assert calls == []


def test_token_from_another_secret_is_refused(client, calls):
    response = client.post(
        "/api/offer", json={}, headers=bearer(valid_token(secret=OTHER_SECRET))
    )
    assert response.status_code == 401
    assert calls == []


def test_two_authorization_headers_are_refused(client, calls):
    token = valid_token()
    response = client.post(
        "/api/offer",
        json={},
        headers=[("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")],
    )
    assert response.status_code == 401
    assert calls == []


def test_valid_token_reaches_the_offer_handler(client, calls):
    # The gate uses the real clock, so mint a token that is valid now.
    token = issue_voice_token(SECRET.encode(), "org-1", TTL)
    response = client.post("/api/offer", json={}, headers=bearer(token))
    assert response.status_code == 200
    assert calls == ["/api/offer"]


def test_start_and_session_proxy_routes_are_protected_too(client, calls):
    assert client.post("/start", json={}).status_code == 401
    assert client.post("/sessions/abc/api/offer", json={}).status_code == 401
    assert calls == []

    token = issue_voice_token(SECRET.encode(), "org-1", TTL)
    assert client.post("/start", json={}, headers=bearer(token)).status_code == 200
    assert calls == ["/start"]


def test_status_is_private_and_the_client_ui_is_public(client, calls):
    assert client.get("/status").status_code == 401
    assert client.get("/client/index.html").status_code == 200
    assert calls == ["/client"]


def test_preflight_is_not_treated_as_a_session_request(client, calls):
    response = client.options("/api/offer")
    assert response.status_code != 401
    assert calls == []


def test_websocket_without_token_is_refused_before_accept(client, calls):
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws"):
            pass
    assert refused.value.code == 1008
    assert calls == []


def test_websocket_with_valid_token_is_admitted(client, calls):
    token = issue_voice_token(SECRET.encode(), "org-1", TTL)
    with client.websocket_connect("/ws", headers=bearer(token)):
        pass
    assert calls == ["/ws"]


def test_missing_secret_refuses_even_a_well_formed_token(calls):
    client = TestClient(build_runner_like_app(calls, secret=None))
    token = issue_voice_token(SECRET.encode(), "org-1", TTL)
    assert client.post("/api/offer", json={}, headers=bearer(token)).status_code == 401
    assert calls == []


def test_public_check_refuses_path_traversal_out_of_the_client_ui():
    assert _is_public("GET", "/client/index.html")
    assert not _is_public("GET", "/client/../status")
    assert not _is_public("POST", "/client/index.html")
    assert not _is_public("GET", "/api/offer")
    assert not _is_public("POST", "/")


def test_install_voice_auth_fails_closed_for_a_short_secret(calls):
    app = FastAPI()

    @app.post("/api/offer")
    async def offer():
        calls.append("/api/offer")
        return {}

    install_voice_auth(app, "short")
    client = TestClient(app)
    token = issue_voice_token(SECRET.encode(), "org-1", TTL)
    assert client.post("/api/offer", json={}, headers=bearer(token)).status_code == 401
    assert calls == []


# ---------------------------------------------------------------- issuance


def issuance_client(monkeypatch, secret: str | None, org_id):
    settings = Settings(
        _env_file=None,
        postgres_user="u",
        postgres_password="p",
        postgres_db="d",
        groq_api_key="k",
        groq_model="m",
        redis_enabled=False,
        voice_token_secret=secret,
        voice_token_ttl_secs=120,
    )
    monkeypatch.setattr("api.voice_tokens.get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(voice_tokens_router)
    app.dependency_overrides[get_current_org] = lambda: SimpleNamespace(id=org_id)
    return TestClient(app)


def test_issuance_returns_a_token_for_the_authenticated_org(monkeypatch):
    org_id = uuid4()
    client = issuance_client(monkeypatch, SECRET, org_id)
    response = client.post("/v1/voice/tokens")
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 120
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    assert verify_voice_token(body["token"], SECRET.encode())


def test_issuance_fails_closed_without_a_secret(monkeypatch):
    client = issuance_client(monkeypatch, None, uuid4())
    response = client.post("/v1/voice/tokens")
    assert response.status_code == 503
    assert "token" not in response.json()


def test_issuance_route_requires_the_organization_api_key_dependency():
    route = next(r for r in voice_tokens_router.routes if r.path == "/v1/voice/tokens")
    assert any(dep.call is get_current_org for dep in route.dependant.dependencies)
