"""Contract tests against the real Pipecat runner app, with a real aiortc SDP offer.

bot.py is executed as __main__ exactly as production runs it, with uvicorn.run
replaced by a capture so the configured FastAPI app can be driven in-process.
The bot itself is a stub that records each start. Everything before the bot
(the auth middleware, /start, the offer handler, and aiortc's SDP answer) is real.

These tests prove, at the protocol level, that no SDP answer is produced for a
missing, malformed, wrong-secret, or expired token, and that a valid token gets a
real answer and starts the bot once. They do not prove media flow or a browser
connection; see tests/js/voice-client.browser.mjs for the Chromium check.
"""

import asyncio
import os
import re
import runpy
import sys
from pathlib import Path

import httpx
import pytest
import uvicorn
from aiortc import RTCPeerConnection, RTCSessionDescription
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.config import Settings, get_settings
from voice.voice_auth import issue_voice_token

ROOT = Path(__file__).resolve().parents[1]
SECRET = "c" * 40
OTHER_SECRET = "d" * 40


@pytest.fixture(scope="module", autouse=True)
def _restore_process_environment():
    """Whatever the runner import or startup does to os.environ is undone afterwards."""
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture(scope="module")
def runner():
    """Runs voice/bot.py as __main__ once and captures the configured app."""
    recorded_starts: list = []
    with pytest.MonkeyPatch.context() as mp:
        # pipecat.runner.run calls load_dotenv(override=True) when it is first imported, and
        # load_dotenv searches upward from that file. With a virtualenv inside the repository it
        # finds the developer's real .env and copies it over os.environ for the rest of the
        # suite, including LANGSMITH_TRACING=true and provider keys. So: neutralise load_dotenv
        # BEFORE the first import, import lazily here (never at module level), and keep the
        # settings object from reading .env as well.
        import dotenv

        mp.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
        mp.setitem(Settings.model_config, "env_file", None)
        import pipecat.runner.run as pipecat_run

        for key, value in {
            "GROQ_API_KEY": "ci-placeholder",
            "GROQ_MODEL": "openai/gpt-oss-20b",
            "TTS_PROVIDER": "groq",
            "REDIS_ENABLED": "false",
            "POSTGRES_USER": "contract",
            "POSTGRES_PASSWORD": "contract-password",
            "POSTGRES_DB": "contract",
            "POSTGRES_HOST": "127.0.0.1",
            "APP_ENV": "development",
            "VOICE_TOKEN_SECRET": SECRET,
        }.items():
            mp.setenv(key, value)
        mp.delenv("DATABASE_URL", raising=False)
        get_settings.cache_clear()

        captured: dict = {}
        mp.setattr(uvicorn, "run", lambda app, **kwargs: captured.setdefault("app", app))

        async def stub_bot(runner_args):
            recorded_starts.append(runner_args)

        class StubBotModule:
            bot = staticmethod(stub_bot)

        mp.setattr(pipecat_run, "_get_bot_module", lambda: StubBotModule)
        mp.setattr(sys, "argv", ["bot.py", "--host", "127.0.0.1", "--port", "7860"])

        runpy.run_path(str(ROOT / "voice" / "bot.py"), run_name="__main__")
        get_settings.cache_clear()
        yield {"app": captured["app"], "starts": recorded_starts}
        get_settings.cache_clear()


@pytest.fixture
def starts(runner):
    runner["starts"].clear()
    return runner["starts"]


def bearer(value: str) -> dict:
    return {"Authorization": f"Bearer {value}"}


def valid(now=None) -> str:
    return issue_voice_token(SECRET.encode(), "org-contract", 300, now=now)


async def _offer():
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    await pc.setLocalDescription(await pc.createOffer())
    return pc, {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


def run(coro):
    return asyncio.run(coro)


def test_start_and_offer_without_a_token_are_refused(runner, starts):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            assert (await client.post("/start", json={"transport": "webrtc"})).status_code == 401
            pc, body = await _offer()
            try:
                response = await client.post("/sessions/not-a-session/api/offer", json=body)
                assert response.status_code == 401
                assert "sdp" not in response.text
            finally:
                await pc.close()

    run(scenario())
    assert starts == []


@pytest.mark.parametrize(
    "credential",
    [
        pytest.param(lambda: "not-a-token", id="malformed"),
        pytest.param(lambda: issue_voice_token(OTHER_SECRET.encode(), "org", 300), id="wrong-secret"),
        pytest.param(lambda: issue_voice_token(SECRET.encode(), "org", 300, now=0), id="expired"),
        pytest.param(lambda: "", id="empty"),
    ],
)
def test_bad_credentials_get_no_sdp_answer_on_a_live_session(runner, starts, credential):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            # A valid start creates a live session, so the offer is refused for auth alone.
            start = await client.post("/start", json={"transport": "webrtc"}, headers=bearer(valid()))
            assert start.status_code == 200
            session_id = start.json()["sessionId"]
            pc, body = await _offer()
            try:
                response = await client.post(
                    f"/sessions/{session_id}/api/offer", json=body, headers=bearer(credential())
                )
                assert response.status_code == 401
                assert "answer" not in response.text
            finally:
                await pc.close()

    run(scenario())
    assert starts == []


def test_a_valid_token_gets_a_real_sdp_answer_and_starts_the_bot_once(runner, starts):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            start = await client.post("/start", json={"transport": "webrtc", "enableDefaultIceServers": True}, headers=bearer(valid()))
            assert start.status_code == 200
            session_id = start.json()["sessionId"]

            pc, body = await _offer()
            try:
                response = await client.post(
                    f"/sessions/{session_id}/api/offer", json=body, headers=bearer(valid())
                )
                assert response.status_code == 200
                answer = response.json()
                assert answer["type"] == "answer"
                # aiortc accepts the answer as a valid remote description, so the negotiation is real.
                await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
            finally:
                await pc.close()

    run(scenario())
    assert len(starts) == 1


def test_loading_the_runner_does_not_pull_a_dotenv_file_into_the_environment(runner):
    # conftest.py forces tracing off for the whole suite; a leaked .env would flip it back.
    assert os.environ.get("LANGSMITH_TRACING") == "false"


def _registered_entry_points(app):
    """Every (kind, method, concrete path) the runner registered, read from the live app."""
    entries = []
    for route in app.routes:
        kind = type(route).__name__
        path = re.sub(r"\{[^}]+\}", "x", route.path)
        if kind in ("Route", "APIRoute"):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                entries.append(("http", method, path))
        elif kind in ("WebSocketRoute", "APIWebSocketRoute"):
            entries.append(("ws", "WS", path))
        elif kind == "Mount":
            entries.append(("mount", "POST", route.path + "/x"))
    return entries


def test_every_registered_entry_point_refuses_a_caller_without_a_token(runner, starts):
    entries = _registered_entry_points(runner["app"])
    # Guard the guard: the critical routes must actually be in the enumeration.
    seen = {(method, path) for _, method, path in entries}
    for expected in [("POST", "/start"), ("POST", "/api/offer"), ("PATCH", "/api/offer"),
                     ("POST", "/sessions/x/x"), ("WS", "/ws"), ("GET", "/status")]:
        assert expected in seen, f"{expected} is no longer a registered route; update this test"

    public_get = {("GET", "/")}
    unexpectedly_open: list[str] = []

    async def check_http():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            for kind, method, path in entries:
                if kind == "ws" or (method, path) in public_get:
                    continue
                body = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
                response = await client.request(method, path, **body)
                if response.status_code != 401:
                    unexpectedly_open.append(f"{method} {path} -> {response.status_code}")

    run(check_http())

    client = TestClient(runner["app"])
    for kind, _, path in entries:
        if kind != "ws":
            continue
        try:
            with client.websocket_connect(path):
                unexpectedly_open.append(f"WS {path} -> accepted")
        except WebSocketDisconnect as closed:
            if closed.code != 1008:
                unexpectedly_open.append(f"WS {path} -> closed with {closed.code}")

    assert unexpectedly_open == []
    assert starts == []


def test_the_gate_is_not_a_blanket_deny_for_a_valid_token(runner, starts):
    entries = _registered_entry_points(runner["app"])

    async def check():
        refused: list[str] = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            for kind, method, path in entries:
                if kind != "http":
                    continue
                body = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
                response = await client.request(method, path, headers=bearer(valid()), **body)
                if response.status_code == 401:
                    refused.append(f"{method} {path}")
        return refused

    assert run(check()) == []


def test_status_and_the_session_proxy_are_private(runner):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            assert (await client.get("/status")).status_code == 401
            assert (await client.get("/status", headers=bearer(valid()))).status_code == 200

    run(scenario())


def test_the_browser_client_is_served_publicly_and_contains_no_secret(runner):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runner["app"]), base_url="http://test") as client:
            module = await client.get("/voice-client/voice-client.js")
            demo = await client.get("/voice-client/demo.html")
            return module, demo

    module, demo = run(scenario())
    assert module.status_code == 200
    assert "startVoiceSession" in module.text
    assert demo.status_code == 200
    for response in (module, demo):
        assert SECRET not in response.text
        assert "VOICE_TOKEN_SECRET" not in response.text
