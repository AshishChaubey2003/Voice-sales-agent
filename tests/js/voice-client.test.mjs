// Unit tests for voice/static/voice-client.js with fake fetch and RTCPeerConnection.
// Run from the repo root:  node --test tests/js/voice-client.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { startVoiceSession, VoiceAuthError } from "../../voice/static/voice-client.js";

const BASE = "https://voice.example.test";
const GOOD = "good-token";

class FakeRTC {
  static instances = [];
  constructor(config) {
    this.config = config;
    this.iceGatheringState = "complete";
    this.localDescription = null;
    this.remoteDescription = null;
    this.connectionState = "new";
    this.closed = false;
    this.transceivers = [];
    this.listeners = {};
    FakeRTC.instances.push(this);
  }
  addTransceiver(kind, options) { this.transceivers.push({ kind, options }); }
  addTrack(track, stream) { this.transceivers.push({ track, stream }); }
  async createOffer() { return { type: "offer", sdp: "v=0 fake-offer" }; }
  async setLocalDescription(description) { this.localDescription = { type: description.type, sdp: description.sdp }; }
  async setRemoteDescription(description) { this.remoteDescription = description; }
  addEventListener(name, fn) { (this.listeners[name] ??= []).push(fn); }
  removeEventListener(name, fn) { this.listeners[name] = (this.listeners[name] ?? []).filter((f) => f !== fn); }
  close() { this.closed = true; }
}

// `respond(url, init)` returns { status, body } for each request.
function fakeFetch(respond) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init, auth: init.headers.Authorization });
    const { status, body } = respond(url, init);
    return { ok: status >= 200 && status < 300, status, json: async () => body };
  };
  return { fetchImpl, calls };
}

// A runner that accepts only GOOD, like the real middleware.
function runner(sessionId = "sess-1") {
  return (url, init) => {
    const authorized = init.headers.Authorization === `Bearer ${GOOD}`;
    if (!authorized) return { status: 401, body: { detail: "A valid voice token is required" } };
    if (url.endsWith("/start")) {
      return { status: 200, body: { sessionId, iceConfig: { iceServers: [{ urls: ["stun:stun.example.test"] }] } } };
    }
    if (url.endsWith("/api/offer")) {
      return { status: 200, body: { sdp: "v=0 fake-answer", type: "answer", pc_id: "pc-1" } };
    }
    return { status: 404, body: {} };
  };
}

function tokens(...values) {
  let index = 0;
  return async () => values[Math.min(index++, values.length - 1)];
}

test("happy path: bearer token on /start and offer, session id in the offer path", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(runner());
  const session = await startVoiceSession({
    baseUrl: BASE + "/",
    getToken: tokens(GOOD),
    fetchImpl,
    RTCPeerConnectionImpl: FakeRTC,
    iceGatherTimeoutMs: 50,
  });

  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, `${BASE}/start`);
  assert.equal(calls[1].url, `${BASE}/sessions/sess-1/api/offer`);
  assert.equal(calls[0].auth, `Bearer ${GOOD}`);
  assert.equal(calls[1].auth, `Bearer ${GOOD}`);
  assert.deepEqual(JSON.parse(calls[0].init.body), { transport: "webrtc", enableDefaultIceServers: true });
  assert.deepEqual(Object.keys(JSON.parse(calls[1].init.body)).sort(), ["sdp", "type"]);

  const pc = FakeRTC.instances[0];
  assert.deepEqual(pc.config, { iceServers: [{ urls: ["stun:stun.example.test"] }] });
  assert.deepEqual(pc.remoteDescription, { type: "answer", sdp: "v=0 fake-answer" });
  assert.equal(session.sessionId, "sess-1");
  assert.equal(pc.transceivers[0].kind, "audio");

  session.close();
  assert.equal(pc.closed, true);
});

test("the token and the offer never appear in a URL", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(runner());
  await startVoiceSession({ baseUrl: BASE, getToken: tokens(GOOD), fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50 });
  for (const call of calls) assert.ok(!call.url.includes(GOOD));
});

test("no token: refused before any request is sent", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(runner());
  for (const value of ["", "   ", null, undefined]) {
    await assert.rejects(
      startVoiceSession({ baseUrl: BASE, getToken: async () => value, fetchImpl, RTCPeerConnectionImpl: FakeRTC }),
      VoiceAuthError,
    );
  }
  assert.equal(calls.length, 0);
  assert.equal(FakeRTC.instances.length, 0);
});

test("an invalid token is refused twice, and no SDP offer is ever sent", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(runner());
  const getToken = tokens("forged-token");
  await assert.rejects(
    startVoiceSession({ baseUrl: BASE, getToken, fetchImpl, RTCPeerConnectionImpl: FakeRTC }),
    (error) => error instanceof VoiceAuthError && !String(error.message).includes("forged-token"),
  );
  assert.ok(calls.every((call) => call.url.endsWith("/start")));
  assert.equal(calls.length, 2);
  assert.equal(FakeRTC.instances.length, 0);
});

test("an expired token on /start is replaced once and the session then proceeds", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(runner());
  const getToken = tokens("expired-token", GOOD);
  const session = await startVoiceSession({
    baseUrl: BASE, getToken, fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50,
  });
  assert.equal(session.sessionId, "sess-1");
  assert.equal(calls[0].auth, "Bearer expired-token");
  assert.equal(calls[1].auth, `Bearer ${GOOD}`);
  assert.equal(calls.filter((c) => c.url.endsWith("/api/offer")).length, 1);
});

test("a 401 on the offer restarts once with a fresh token and closes the stale connection", async () => {
  FakeRTC.instances = [];
  let offersSeen = 0;
  const { fetchImpl, calls } = fakeFetch((url, init) => {
    if (url.endsWith("/api/offer")) {
      offersSeen += 1;
      if (offersSeen === 1) return { status: 401, body: {} };
    }
    return runner()(url, init);
  });
  const getToken = tokens(GOOD, GOOD);
  const session = await startVoiceSession({
    baseUrl: BASE, getToken, fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50,
  });
  assert.equal(session.sessionId, "sess-1");
  assert.equal(FakeRTC.instances.length, 2);
  assert.equal(FakeRTC.instances[0].closed, true);
  assert.equal(FakeRTC.instances[1].closed, false);
  assert.equal(calls.filter((c) => c.url.endsWith("/start")).length, 2);
});

test("a 401 on the offer twice gives up, and every peer connection is closed", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch((url, init) => {
    if (url.endsWith("/start")) return runner()(url, init);
    return { status: 401, body: {} };
  });
  await assert.rejects(
    startVoiceSession({ baseUrl: BASE, getToken: tokens(GOOD), fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50 }),
    VoiceAuthError,
  );
  assert.equal(FakeRTC.instances.length, 2);
  assert.ok(FakeRTC.instances.every((pc) => pc.closed));
});

test("other HTTP errors are not retried and reveal only the status", async () => {
  FakeRTC.instances = [];
  const { fetchImpl, calls } = fakeFetch(() => ({ status: 500, body: { detail: "internal" } }));
  await assert.rejects(
    startVoiceSession({ baseUrl: BASE, getToken: tokens(GOOD), fetchImpl, RTCPeerConnectionImpl: FakeRTC }),
    (error) => error.message === "Voice start failed (HTTP 500).",
  );
  assert.equal(calls.length, 1);
});

test("a microphone's audio tracks are attached instead of a placeholder transceiver", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch(runner());
  const track = { kind: "audio" };
  const microphone = { getAudioTracks: () => [track] };
  await startVoiceSession({
    baseUrl: BASE, getToken: tokens(GOOD), microphone, fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50,
  });
  const pc = FakeRTC.instances[0];
  assert.equal(pc.transceivers.length, 1);
  assert.equal(pc.transceivers[0].track, track);
});

test("onTrack is attached before the answer is applied", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch(runner());
  const onTrack = () => {};
  await startVoiceSession({
    baseUrl: BASE, getToken: tokens(GOOD), onTrack, fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50,
  });
  assert.equal(FakeRTC.instances[0].listeners.track[0], onTrack);
});

test("a stalled ICE gatherer does not hang the call", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch(runner());
  class StalledRTC extends FakeRTC {
    constructor(config) { super(config); this.iceGatheringState = "gathering"; }
  }
  const session = await startVoiceSession({
    baseUrl: BASE, getToken: tokens(GOOD), fetchImpl, RTCPeerConnectionImpl: StalledRTC, iceGatherTimeoutMs: 20,
  });
  assert.equal(session.sessionId, "sess-1");
});

test("the token is acquired exactly once per attempt, never cached across sessions", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch(runner());
  let acquired = 0;
  const getToken = async () => { acquired += 1; return GOOD; };
  const options = { baseUrl: BASE, getToken, fetchImpl, RTCPeerConnectionImpl: FakeRTC, iceGatherTimeoutMs: 50 };
  await startVoiceSession(options);
  assert.equal(acquired, 1);
  await startVoiceSession(options);
  assert.equal(acquired, 2);
});

test("a failure while applying the answer closes the peer connection (cleanup)", async () => {
  FakeRTC.instances = [];
  const { fetchImpl } = fakeFetch(runner());
  class RejectingRTC extends FakeRTC {
    async setRemoteDescription() { throw new Error("bad answer"); }
  }
  await assert.rejects(
    startVoiceSession({ baseUrl: BASE, getToken: tokens(GOOD), fetchImpl, RTCPeerConnectionImpl: RejectingRTC, iceGatherTimeoutMs: 50 }),
    /bad answer/,
  );
  assert.equal(FakeRTC.instances.length, 1);
  assert.equal(FakeRTC.instances[0].closed, true);
});

test("invalid arguments fail fast", async () => {
  await assert.rejects(startVoiceSession({ getToken: tokens(GOOD) }), TypeError);
  await assert.rejects(startVoiceSession({ baseUrl: BASE, getToken: "not-a-function" }), TypeError);
});
