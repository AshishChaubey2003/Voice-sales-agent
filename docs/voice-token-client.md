# Browser client for private voice access (token required)

Voice access is private. The runner refuses to create an SDP answer unless the request carries a valid short-lived voice token. `voice/static/voice-client.js` is the supported browser client. It uses the runner's public protocol only. Pipecat's installed prebuilt bundle is not patched.

## Why the bundled `/client` UI cannot be used as-is

The Pipecat prebuilt UI at `/client` calls `POST /start` and `POST /sessions/{id}/api/offer` with no way to add an `Authorization` header (the installed bundle has no URL or window configuration hook). With voice auth on, it gets `401` on `/start`. This is intended (fail closed). Use `voice-client.js`, or put a header-injecting reverse proxy in front of the runner.

## Flow

1. **Your backend (server side only)** holds the organization API key and calls the text API:

   ```
   POST /v1/voice/tokens
   Authorization: Bearer <organization API key>
   ```

   Response (`201`, `Cache-Control: no-store`): `{"token": "...", "token_type": "bearer", "expires_in": 300}`.
   The HMAC signing secret (`VOICE_TOKEN_SECRET`) and the organization API key never reach the browser.

2. **Your backend hands the short-lived token to your own page** over your own authenticated session. Do not put it in a URL, a log line, or long-lived storage.

3. **Your page calls the client:**

   ```js
   import { startVoiceSession, VoiceAuthError } from "./voice-client.js";

   const microphone = await navigator.mediaDevices.getUserMedia({ audio: true });
   const session = await startVoiceSession({
     baseUrl: "https://voice.example.com",
     getToken: () => fetch("/my-backend/voice-token").then((r) => r.text()), // your endpoint
     microphone,
     onTrack: (event) => { audio.srcObject = event.streams[0]; },
   });
   // later: session.close()
   ```

   The client sends, in order:
   - `POST {baseUrl}/start` with body `{"transport":"webrtc","enableDefaultIceServers":true}` and `Authorization: Bearer <token>`. Response: `{sessionId, iceConfig}`.
   - `POST {baseUrl}/sessions/{sessionId}/api/offer` with body `{"sdp": ..., "type": "offer"}` and the same header. Response: `{sdp, type: "answer", pc_id}`. The answer is applied with `setRemoteDescription`.

4. **Refusals.** A `401` on `/start` or on the offer makes the client ask `getToken` for a fresh token and restart once. A second `401`, or no token at all, throws `VoiceAuthError`. When no token is available, no request is sent. Other HTTP errors are thrown with the status only. They are not retried.

## Local demo

Outside `APP_ENV=production`, the runner serves `/voice-client/voice-client.js` and `/voice-client/demo.html` (static, no secrets, `GET` only). Open the demo page, paste a short-lived token, and connect. Never paste an organization API key. In production these files are not served. Copy `voice-client.js` into your own frontend instead.

## Supported versions

Developed and verified against `pipecat-ai==1.12.0`, `pipecat-ai-prebuilt==1.3.0`, and `aiortc==1.15.0`. `requirements.txt` allows newer Pipecat releases (an unconstrained dry run on 2026-10-28 resolved `pipecat-ai 1.14.0`, which has not been tested). CI therefore installs with `pip install -r requirements.txt -c constraints-ci.txt`, which pins exactly those three packages and changes no existing dependency file. To adopt a newer Pipecat, raise the pins in `constraints-ci.txt` and re-run every check below first.

## Verification

| Check | Command | What it proves |
| --- | --- | --- |
| Client logic (fake fetch and RTC) | `node --test tests/js/voice-client.test.mjs` | Header on both requests, no network call without a token, one retry on `401`, no offer sent after refusal, status-only errors, ICE-gather timeout. |
| Real runner, real SDP answer | `.\.venv313\Scripts\python.exe -m pytest tests\test_voice_runner_contract.py` | `bot.py` run as `__main__` with a real aiortc offer. Missing, malformed, wrong-secret, and expired tokens get `401` and no answer. A valid token gets `type: answer`, applied by aiortc, and the bot starts once. |
| Real Chromium signalling | start the runner, then `node tests/js/voice-client.browser.mjs` (see the file header) | The demo page in headless Chromium completes `/start` and the offer against the live server. A forged token is refused and no offer is sent. |
| Every entry point | included in `tests\test_voice_runner_contract.py` | The test reads all routes from the live runner app (HTTP, mounts, websockets) and requires a `401` (websocket close `1008`) for each without a token, except `GET /`. A valid token is not refused anywhere. A future Pipecat route cannot bypass the gate unnoticed. |
| Server gate | `.\.venv313\Scripts\python.exe -m pytest tests\test_voice_auth.py` | Token codec, middleware, issuance endpoint. |

## Entry points that stay closed to browsers

The runner also registers websocket routes (`/ws`, `/ws-client`, and their `/{token}` variants) and, in some configurations, telephony webhooks. All are refused without an `Authorization: Bearer` header. Browsers cannot set headers on a `WebSocket` handshake, and telephony providers do not send this token, so those transports are intentionally unavailable. This deployment uses WebRTC only. Opening a websocket or telephony path later needs its own design decision; do not add it to the public-path list.

Not verified: audio actually flowing in a call (the Chromium check ends at signalling, with the connection still `connecting` and a stub bot), a call through a real STUN or TURN server, other browsers, and cross-origin use. A cross-origin page sends an `Authorization` header, so the browser makes a CORS preflight. The runner's CORS default is `*` (existing behavior, unchanged here and recorded as an open item).
