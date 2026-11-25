/**
 * Token-aware WebRTC client for the private Pipecat voice runner.
 *
 * Uses only the runner's public protocol, with no bundle patching:
 *   1. POST {base}/start                          -> { sessionId, iceConfig }
 *   2. POST {base}/sessions/{sessionId}/api/offer -> { sdp, type, pc_id }
 * Both requests carry `Authorization: Bearer <voice token>`.
 *
 * The host page supplies `getToken`. It must fetch the token from ITS OWN backend,
 * which calls POST /v1/voice/tokens with the organization API key. This module never
 * receives the organization key or the HMAC signing secret, and it never puts the
 * token in a URL or a log line.
 *
 * Dependency-free ES module. Browsers use the global fetch and RTCPeerConnection;
 * tests inject fakes through the options.
 */

export class VoiceAuthError extends Error {
  constructor(message) {
    super(message);
    this.name = "VoiceAuthError";
  }
}

const DEFAULT_ICE_GATHER_TIMEOUT_MS = 5000;
// One fresh token per attempt. A 401 on /start or on the offer restarts the whole
// flow once with a new token (for example after expiry), and then gives up.
const MAX_AUTH_ATTEMPTS = 2;

async function requireToken(getToken) {
  const token = await getToken();
  if (typeof token !== "string" || token.trim() === "") {
    throw new VoiceAuthError("No voice token was provided.");
  }
  return token.trim();
}

function postJson(fetchImpl, url, token, body) {
  return fetchImpl(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(body),
  });
}

// Non-trickle negotiation: the offer must carry its candidates, because the runner
// does not use PATCH requests in this client. Gathering is bounded so that a stalled
// gatherer cannot hang the call.
function waitForIceGathering(pc, timeoutMs) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    let timer = null;
    const finish = () => {
      clearTimeout(timer);
      pc.removeEventListener("icegatheringstatechange", onChange);
      resolve();
    };
    const onChange = () => {
      if (pc.iceGatheringState === "complete") finish();
    };
    timer = setTimeout(finish, timeoutMs);
    pc.addEventListener("icegatheringstatechange", onChange);
  });
}

/**
 * Starts a voice session and resolves once the SDP answer is applied.
 *
 * @param {object} options
 * @param {string} options.baseUrl       Runner origin, for example "https://voice.example.com".
 * @param {() => Promise<string>} options.getToken  Returns a fresh short-lived voice token.
 * @param {MediaStream} [options.microphone]        Local audio. Without it, a recv/send audio transceiver is added.
 * @param {(event: RTCTrackEvent) => void} [options.onTrack]  Receives the bot's audio track.
 * @param {Function} [options.fetchImpl]
 * @param {Function} [options.RTCPeerConnectionImpl]
 * @param {number} [options.iceGatherTimeoutMs]
 * @returns {Promise<{sessionId: string, pc: RTCPeerConnection, close: () => void}>}
 * @throws {VoiceAuthError} When no token is available or the runner refuses it twice.
 * @throws {Error} For any other non-success HTTP status. The message has the status only.
 */
export async function startVoiceSession({
  baseUrl,
  getToken,
  microphone = null,
  onTrack = null,
  fetchImpl = globalThis.fetch,
  RTCPeerConnectionImpl = globalThis.RTCPeerConnection,
  iceGatherTimeoutMs = DEFAULT_ICE_GATHER_TIMEOUT_MS,
}) {
  if (typeof baseUrl !== "string" || baseUrl === "") {
    throw new TypeError("baseUrl is required");
  }
  if (typeof getToken !== "function") {
    throw new TypeError("getToken must be a function that returns a voice token");
  }
  const base = baseUrl.replace(/\/+$/, "");

  for (let attempt = 1; attempt <= MAX_AUTH_ATTEMPTS; attempt += 1) {
    const token = await requireToken(getToken);

    const start = await postJson(fetchImpl, `${base}/start`, token, {
      transport: "webrtc",
      enableDefaultIceServers: true,
    });
    if (start.status === 401) continue;
    if (!start.ok) throw new Error(`Voice start failed (HTTP ${start.status}).`);

    const { sessionId, iceConfig } = await start.json();
    const pc = new RTCPeerConnectionImpl(iceConfig ?? {});
    try {
      if (onTrack) pc.addEventListener("track", onTrack);
      const tracks = microphone ? microphone.getAudioTracks() : [];
      if (tracks.length > 0) {
        for (const track of tracks) pc.addTrack(track, microphone);
      } else {
        pc.addTransceiver("audio", { direction: "sendrecv" });
      }

      await pc.setLocalDescription(await pc.createOffer());
      await waitForIceGathering(pc, iceGatherTimeoutMs);

      const offer = await postJson(
        fetchImpl,
        `${base}/sessions/${encodeURIComponent(sessionId)}/api/offer`,
        token,
        { sdp: pc.localDescription.sdp, type: pc.localDescription.type },
      );
      if (offer.status === 401) {
        pc.close();
        continue;
      }
      if (!offer.ok) throw new Error(`Voice offer failed (HTTP ${offer.status}).`);

      const answer = await offer.json();
      await pc.setRemoteDescription({ type: answer.type, sdp: answer.sdp });
      return { sessionId, pc, close: () => pc.close() };
    } catch (error) {
      pc.close();
      throw error;
    }
  }

  throw new VoiceAuthError("Voice access was refused. The token is missing, invalid, or expired.");
}
