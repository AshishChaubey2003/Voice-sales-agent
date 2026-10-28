"""Private voice access: short-lived signed tokens checked before any session starts.

Voice sessions cost real money (LLM, STT, TTS, TURN), so a visitor must present a
valid token before the Pipecat runner negotiates a WebRTC connection or starts a
pipeline. Tokens are minted by the authenticated text API (POST /v1/voice/tokens),
which requires the organization's API key. The browser only ever receives the
short-lived token, never the signing secret.

The check is an ASGI middleware installed on the runner's FastAPI app. It runs
before Pipecat's route handlers, so `/api/offer` never returns an SDP answer to a
caller without a valid token.

Token format: ``v1.<base64url(payload json)>.<base64url(HMAC-SHA256 signature)>``.
Payload: ``{"sub", "scope": "voice", "iat", "exp"}``. Expiry is strict: a token is
refused at ``exp`` and after.

Fail-closed rules:
* No secret, or a secret shorter than MIN_SECRET_LENGTH: every token is refused.
* Any malformed, mis-signed, wrong-scope, or expired token is refused.
* Protected routes are default-deny. Only OPTIONS (CORS preflight, which creates no
  session) and GET/HEAD requests for the static assets under /client and
  /voice-client are public. Those assets contain no secrets.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Callable

TOKEN_VERSION = "v1"
TOKEN_SCOPE = "voice"
MIN_SECRET_LENGTH = 32
MAX_TOKEN_LENGTH = 2048
AUTH_FAILED_DETAIL = "A valid voice token is required"

_PUBLIC_EXACT_PATHS = ("/",)
# Only static, secret-free assets: the prebuilt UI and the browser client source.
_PUBLIC_PREFIXES = ("/client", "/voice-client")


def _now() -> int:
    return int(time.time())


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: bytes, signing_input: str) -> bytes:
    return hmac.new(secret, signing_input.encode("utf-8"), hashlib.sha256).digest()


def issue_voice_token(
    secret: bytes, subject: str, ttl_seconds: int, now: int | None = None
) -> str:
    """Returns a signed token valid for ``ttl_seconds`` from ``now``."""
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError("voice token secret is too short")
    issued_at = _now() if now is None else now
    payload = {
        "sub": subject,
        "scope": TOKEN_SCOPE,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{TOKEN_VERSION}.{body}"
    return f"{signing_input}.{_b64encode(_sign(secret, signing_input))}"


def verify_voice_token(
    token: str | None, secret: bytes | None, now: int | None = None
) -> bool:
    """True only for an untampered, unexpired voice token signed with ``secret``."""
    if not secret or len(secret) < MIN_SECRET_LENGTH:
        return False
    if not token or len(token) > MAX_TOKEN_LENGTH:
        return False

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        return False
    signing_input = f"{parts[0]}.{parts[1]}"

    try:
        given_signature = _b64decode(parts[2])
        payload_bytes = _b64decode(parts[1])
    except (binascii.Error, ValueError):
        return False

    # Compare signatures before reading any claim, in constant time.
    if not hmac.compare_digest(_sign(secret, signing_input), given_signature):
        return False

    try:
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("scope") != TOKEN_SCOPE:
        return False

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        return False
    current = _now() if now is None else now
    return current < expires_at


def production_token_errors(app_env: str, secret_value: str | None) -> list[str]:
    """Blocking messages for a production voice process without a usable secret."""
    if app_env != "production":
        return []
    if not secret_value or len(secret_value) < MIN_SECRET_LENGTH:
        return [
            f"VOICE_TOKEN_SECRET must be set to at least {MIN_SECRET_LENGTH} "
            "characters; voice calls are refused without it."
        ]
    return []


def _is_public(method: str, path: str) -> bool:
    if method not in ("GET", "HEAD"):
        return False
    if ".." in path.split("/"):
        return False
    if path in _PUBLIC_EXACT_PATHS:
        return True
    return any(path == prefix or path.startswith(prefix + "/") for prefix in _PUBLIC_PREFIXES)


def _bearer_token(scope) -> str | None:
    values = [value for name, value in scope.get("headers", []) if name.lower() == b"authorization"]
    if len(values) != 1:
        return None
    scheme, _, token = values[0].decode("latin-1").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def _reject(scope, send) -> None:
    if scope["type"] == "websocket":
        # Closing before accept refuses the handshake; nothing is started.
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": AUTH_FAILED_DETAIL}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class VoiceAuthMiddleware:
    """Refuses every request that is not public and lacks a valid voice token.

    Installed on the Pipecat runner app, so it wraps all of its routes: /start,
    /api/offer, /sessions/{id}/..., telephony webhooks, and websockets.
    """

    def __init__(
        self,
        app,
        secret: bytes | None,
        clock: Callable[[], int] = _now,
    ):
        self.app = app
        self._secret = secret
        self._clock = clock

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "http":
            if scope["method"] == "OPTIONS" or _is_public(scope["method"], scope["path"]):
                return await self.app(scope, receive, send)
        elif kind != "websocket":
            # Lifespan events carry no request, so they pass through.
            return await self.app(scope, receive, send)

        token = _bearer_token(scope)
        if verify_voice_token(token, self._secret, now=self._clock()):
            return await self.app(scope, receive, send)
        await _reject(scope, send)


def install_voice_auth(app, secret_value: str | None) -> None:
    """Adds the voice-token check to ``app``. Call before the app starts serving."""
    secret = None
    if secret_value and len(secret_value) >= MIN_SECRET_LENGTH:
        secret = secret_value.encode("utf-8")
    app.add_middleware(VoiceAuthMiddleware, secret=secret)
