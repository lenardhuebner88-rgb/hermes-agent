"use strict";

const INPUT_SAMPLE_RATE = 16_000;
const INPUT_FRAME_SAMPLES = 320;
const OUTPUT_SAMPLE_RATE = 24_000;
const MAX_PLAYBACK_CHUNK_BYTES = OUTPUT_SAMPLE_RATE * 2 * 4;
const PLAYBACK_DRAIN_TIMEOUT_MS = 30_000;
const SERVER_DRAIN_TIMEOUT_MS = 180_000;

// Phone WS drops (screen lock, network handover, Gemini's 10 min cap) must
// not kill the conversation: five attempts with growing backoff before we
// give up and surface an error.
const RECONNECT_BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
const MAX_RECONNECT_ATTEMPTS = 5;
const NON_RETRYABLE_CLOSE_CODES = new Set([4401, 4403, 4408]);
const MIC_GATE_FAILSAFE_MS = 15_000;

// Speech at normal phone distance is typically well above 0.045 RMS. Three
// consecutive 20 ms chunks reject short clicks while keeping local barge-in
// detection near 60 ms and comfortably inside the <300 ms reaction target.
const BARGE_IN_RMS_THRESHOLD = 0.045;
const BARGE_IN_CONSECUTIVE_CHUNKS = 3;

const STATUS_COPY = {
  idle: "bereit",
  connecting: "verbindet",
  listening: "hört zu",
  thinking: "denkt",
  speaking: "spricht",
  error: "Fehler",
};

const MODE_BADGE_COPY = {
  live: "Live",
  fallback: "Fallback",
};

const statusElement = document.querySelector("#voice-status");
const statusDetailElement = document.querySelector("#status-detail");
const sessionButton = document.querySelector("#session-button");
const transcriptElement = document.querySelector("#transcript");
const emptyTranscriptElement = document.querySelector("#transcript-empty");
const modeBadgeElement = document.querySelector("#mode-badge");
const installChipElement = document.querySelector("#install-chip");
const composerForm = document.querySelector("#composer");
const composerInput = document.querySelector("#composer-input");

const NO_SESSION_TEXT_HINT =
  "Starte zuerst eine Sitzung, dann kannst du auch schreiben.";

let activeSession = null;
let nextSessionId = 0;
let stashedInstallPrompt = null;

function isCurrent(session) {
  return activeSession === session;
}

function setStatus(value, detail) {
  const normalized = Object.hasOwn(STATUS_COPY, value) ? value : "error";
  document.body.dataset.voiceState = normalized;
  statusElement.textContent = STATUS_COPY[normalized];
  if (detail) {
    statusDetailElement.textContent = detail;
  }
  if (normalized === "idle") {
    hideModeBadge();
  }
}

function hideModeBadge() {
  modeBadgeElement.hidden = true;
  delete modeBadgeElement.dataset.mode;
}

function renderModeBadge(value) {
  if (!Object.hasOwn(MODE_BADGE_COPY, value)) {
    return;
  }
  modeBadgeElement.textContent = MODE_BADGE_COPY[value];
  modeBadgeElement.dataset.mode = value;
  modeBadgeElement.hidden = false;
}

function setComposerEnabled(enabled) {
  composerInput.setAttribute("aria-disabled", enabled ? "false" : "true");
}

function setButton(mode) {
  if (mode === "start") {
    sessionButton.textContent = "Start";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung starten");
    setComposerEnabled(false);
    return;
  }
  if (mode === "stop") {
    sessionButton.textContent = "Stop";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung beenden");
    setComposerEnabled(true);
    return;
  }
  sessionButton.textContent = "Wird beendet …";
  sessionButton.disabled = true;
  sessionButton.setAttribute("aria-label", "Sprachsitzung wird beendet");
  // After "end" the server accepts only interrupt controls — a typed frame
  // during the drain would be rejected, so the composer closes with the mic.
  setComposerEnabled(false);
}

function createTranscriptEntry(role, text) {
  emptyTranscriptElement.hidden = true;
  const entry = document.createElement("article");
  entry.className = `transcript-entry transcript-entry--${role}`;

  const label = document.createElement("p");
  label.className = "transcript-role";
  label.textContent = role === "assistant" ? "Hermes" : "Du";

  const content = document.createElement("p");
  content.className = "transcript-text";
  content.textContent = text;

  entry.append(label, content);
  return entry;
}

function setTranscriptEntryText(entry, text) {
  const content = entry.querySelector(".transcript-text");
  if (content) {
    content.textContent = text;
  }
}

// Live captions stream in as fragments (partial: true) and are replaced in
// place until the turn closes (partial: false). The cascade fallback never
// sets "partial" at all, which `handleJsonMessage` normalizes to `false`, so
// those transcripts still create-and-finalize an entry in one call, exactly
// like the pre-C3 appendTranscript behavior.
function upsertTranscript(session, role, text, partial) {
  if (typeof text !== "string") {
    return;
  }
  const key = role === "assistant" ? "assistant" : "user";
  const pending = session.pendingTranscript[key];

  if (!pending) {
    if (text.trim() === "") {
      return;
    }
    const entry = createTranscriptEntry(role, text);
    transcriptElement.append(entry);
    while (transcriptElement.children.length > 100) {
      transcriptElement.firstElementChild.remove();
    }
    entry.scrollIntoView({ behavior: "smooth", block: "nearest" });
    if (partial) {
      session.pendingTranscript[key] = entry;
    }
    return;
  }

  setTranscriptEntryText(pending, text);
  if (!partial) {
    session.pendingTranscript[key] = null;
    pending.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function safeErrorMessage(error) {
  if (error && error.name === "NotAllowedError") {
    return "Der Mikrofonzugriff wurde nicht erlaubt. Bitte gib Hermes Voice die Berechtigung.";
  }
  if (error && error.name === "NotFoundError") {
    return "Es wurde kein Mikrofon gefunden.";
  }
  if (error && error.name === "NotReadableError") {
    return "Das Mikrofon wird bereits von einer anderen Anwendung verwendet.";
  }
  if (error && error.name === "AbortError") {
    return "Der Start wurde abgebrochen.";
  }
  if (error instanceof VoiceClientError) {
    return error.message;
  }
  return "Hermes Voice konnte nicht gestartet werden. Bitte versuche es erneut.";
}

class VoiceClientError extends Error {
  constructor(message) {
    super(message);
    this.name = "VoiceClientError";
  }
}

async function mintWebSocketTicket(session) {
  const headers = new Headers();
  const loopbackToken = window.__HERMES_SESSION_TOKEN__;
  if (typeof loopbackToken === "string" && loopbackToken.length > 0) {
    headers.set("X-Hermes-Session-Token", loopbackToken);
  }

  const response = await fetch("/api/auth/ws-ticket", {
    method: "POST",
    credentials: "same-origin",
    headers,
    signal: session.abortController.signal,
  });
  if (!response.ok) {
    throw new VoiceClientError(
      `Die Anmeldung für die Sprachverbindung ist fehlgeschlagen (HTTP ${response.status}).`,
    );
  }
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.toLowerCase().includes("application/json")) {
    throw new VoiceClientError(
      "Der Server hat kein gültiges Verbindungsticket geliefert.",
    );
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new VoiceClientError(
      "Der Server hat kein gültiges Verbindungsticket geliefert.",
    );
  }

  if (
    !payload ||
    typeof payload.ticket !== "string" ||
    payload.ticket.length < 16 ||
    payload.ticket.length > 4096 ||
    typeof payload.ttl_seconds !== "number" ||
    !Number.isFinite(payload.ttl_seconds) ||
    payload.ttl_seconds <= 0
  ) {
    throw new VoiceClientError(
      "Der Server hat ein ungültiges Verbindungsticket geliefert.",
    );
  }
  return payload.ticket;
}

let inMemorySessionId = null;

// Per-TAB id (sessionStorage, not localStorage): two tabs must never share a
// resumption key — they would overwrite each other's Gemini handle and one
// tab's reconnect could resume the other's conversation. Residual: within ONE
// tab a later login could resume the previous login's context until the
// server-side 60-min registry TTL expires — acceptable for this
// single-operator dashboard.
function getClientSessionId() {
  const storageKey = "hermesVoiceSessionId";
  try {
    const stored = sessionStorage.getItem(storageKey);
    if (stored) {
      return stored;
    }
  } catch {
    // Storage can be blocked (private mode, embedded webview) — fall through.
  }
  if (inMemorySessionId) {
    return inMemorySessionId;
  }
  const id =
    typeof crypto.randomUUID === "function"
      ? crypto.randomUUID()
      : Array.from(crypto.getRandomValues(new Uint8Array(16)))
          .map((byte) => byte.toString(16).padStart(2, "0"))
          .join("");
  inMemorySessionId = id;
  try {
    sessionStorage.setItem(storageKey, id);
  } catch {
    // Keep the in-memory id for this page's lifetime instead.
  }
  return id;
}

function createWebSocket(ticket) {
  const websocketUrl = new URL("/api/voice/live", window.location.origin);
  websocketUrl.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  websocketUrl.searchParams.set("ticket", ticket);
  websocketUrl.searchParams.set("session", getClientSessionId());
  const websocket = new WebSocket(websocketUrl);
  websocket.binaryType = "arraybuffer";
  return websocket;
}

function waitForWebSocket(session) {
  return new Promise((resolve, reject) => {
    const websocket = session.websocket;
    const handleOpen = () => {
      websocket.removeEventListener("error", handleError);
      websocket.removeEventListener("close", handleCloseBeforeOpen);
      session.everOpen = true;
      resolve();
    };
    const handleError = () => {
      websocket.removeEventListener("open", handleOpen);
      websocket.removeEventListener("close", handleCloseBeforeOpen);
      reject(new VoiceClientError("Die Sprachverbindung konnte nicht geöffnet werden."));
    };
    const handleCloseBeforeOpen = () => {
      websocket.removeEventListener("open", handleOpen);
      websocket.removeEventListener("error", handleError);
      reject(new VoiceClientError("Der Server hat die Sprachverbindung abgelehnt."));
    };
    websocket.addEventListener("open", handleOpen, { once: true });
    websocket.addEventListener("error", handleError, { once: true });
    websocket.addEventListener("close", handleCloseBeforeOpen, { once: true });
  });
}

function stopPlayback(session) {
  for (const source of session.playbackSources) {
    source.onended = null;
    try {
      source.stop();
    } catch {
      // A source may already have ended between iteration and stop().
    }
    source.disconnect();
  }
  session.playbackSources.clear();
  session.playbackCursor = session.audioContext
    ? session.audioContext.currentTime
    : 0;
}

async function waitForPlaybackDrain(session) {
  const deadline = performance.now() + PLAYBACK_DRAIN_TIMEOUT_MS;
  while (
    isCurrent(session) &&
    session.playbackSources.size > 0 &&
    performance.now() < deadline
  ) {
    await new Promise((resolve) => window.setTimeout(resolve, 50));
  }
}

function schedulePlayback(session, arrayBuffer) {
  if (
    session.suppressIncomingAudio ||
    session.voiceState !== "speaking" ||
    !(arrayBuffer instanceof ArrayBuffer)
  ) {
    return;
  }
  if (
    arrayBuffer.byteLength === 0 ||
    arrayBuffer.byteLength % 2 !== 0 ||
    arrayBuffer.byteLength > MAX_PLAYBACK_CHUNK_BYTES
  ) {
    throw new VoiceClientError(
      "Der Server hat ein ungültiges oder zu großes Audiofragment gesendet.",
    );
  }

  const sampleCount = arrayBuffer.byteLength / 2;
  const audioBuffer = session.audioContext.createBuffer(
    1,
    sampleCount,
    OUTPUT_SAMPLE_RATE,
  );
  const output = audioBuffer.getChannelData(0);
  const view = new DataView(arrayBuffer);
  for (let index = 0; index < sampleCount; index += 1) {
    output[index] = view.getInt16(index * 2, true) / 0x8000;
  }

  const source = session.audioContext.createBufferSource();
  source.buffer = audioBuffer;
  source.connect(session.audioContext.destination);
  const startAt = Math.max(
    session.audioContext.currentTime + 0.01,
    session.playbackCursor,
  );
  session.playbackCursor = startAt + audioBuffer.duration;
  session.playbackSources.add(source);
  source.onended = () => {
    session.playbackSources.delete(source);
    source.disconnect();
    if (
      session.playbackSources.size === 0 &&
      (session.voiceState === "listening" || session.voiceState === "thinking")
    ) {
      session.suppressIncomingAudio = false;
      resetBargeIn(session);
    }
  };
  source.start(startAt);
}

function resetBargeIn(session) {
  session.loudChunks = 0;
  session.bargeStartedAt = null;
  session.bargeTriggered = false;
}

function hasOpenWebSocket(session) {
  return session.websocket?.readyState === WebSocket.OPEN;
}

function handleMicFrame(session, message) {
  if (!isCurrent(session) || session.microphoneStopped) {
    return;
  }

  const rms = Number(message.rms);
  if (
    session.playbackSources.size > 0 &&
    !session.bargeTriggered &&
    Number.isFinite(rms)
  ) {
    if (rms >= BARGE_IN_RMS_THRESHOLD) {
      if (session.loudChunks === 0) {
        session.bargeStartedAt = performance.now();
      }
      session.loudChunks += 1;
      if (session.loudChunks >= BARGE_IN_CONSECUTIVE_CHUNKS) {
        session.bargeTriggered = true;
        session.suppressIncomingAudio = true;
        stopPlayback(session);
        if (hasOpenWebSocket(session)) {
          session.websocket.send(JSON.stringify({ type: "interrupt" }));
        }
        const latency = performance.now() - session.bargeStartedAt;
        console.info(
          `[Hermes Voice] Barge-in lokal in ${latency.toFixed(1)} ms (Ziel < 300 ms).`,
        );
        statusDetailElement.textContent = "Unterbrochen – ich höre dir weiter zu.";
      }
    } else {
      session.loudChunks = 0;
      session.bargeStartedAt = null;
    }
  }

  if (
    message.pcm instanceof ArrayBuffer &&
    hasOpenWebSocket(session) &&
    !session.drainRequested &&
    // The SDK contract forbids interleaving realtime (mic) input with a
    // client-content text turn — hold PCM until the server reacts to the
    // typed turn (state/audio/error all clear the gate; 15 s failsafe).
    !session.muteMicUntilResponse
  ) {
    session.websocket.send(message.pcm);
  }
}

function clearMicGate(session) {
  session.muteMicUntilResponse = false;
  if (session.micGateTimer !== null) {
    window.clearTimeout(session.micGateTimer);
    session.micGateTimer = null;
  }
}

function applyServerState(session, value) {
  if (!Object.hasOwn(STATUS_COPY, value) || value === "idle" || value === "error") {
    return;
  }
  clearMicGate(session);
  session.voiceState = value;
  if (
    (value === "listening" || value === "thinking") &&
    session.playbackSources.size === 0
  ) {
    session.suppressIncomingAudio = false;
    resetBargeIn(session);
  }
  const detail = {
    listening: "Sag einfach, was Hermes tun soll.",
    thinking: "Hermes verarbeitet deine Anfrage.",
    speaking: "Du kannst Hermes jederzeit unterbrechen.",
  }[value];
  setStatus(value, detail);
}

function handleJsonMessage(session, raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    throw new VoiceClientError("Der Server hat eine ungültige Nachricht gesendet.");
  }
  if (!message || typeof message.type !== "string") {
    throw new VoiceClientError("Der Server hat eine unvollständige Nachricht gesendet.");
  }

  if (message.type === "state") {
    applyServerState(session, message.value);
    return;
  }
  if (message.type === "mode") {
    session.mode = message.value;
    renderModeBadge(message.value);
    return;
  }
  if (message.type === "transcript") {
    upsertTranscript(
      session,
      message.role === "assistant" ? "assistant" : "user",
      message.text,
      message.partial === true,
    );
    return;
  }
  if (message.type === "interrupted") {
    session.suppressIncomingAudio = true;
    stopPlayback(session);
    // The server already finalized (or never started) the assistant's
    // transcript before sending this; the entry itself stays as-is, only
    // the pending ref is cleared client-side as a belt-and-braces guard.
    session.pendingTranscript.assistant = null;
    statusDetailElement.textContent = "Antwort unterbrochen – Hermes hört weiter zu.";
    return;
  }
  if (message.type === "error") {
    const text =
      message.error && typeof message.error.message === "string"
        ? message.error.message
        : "Die Sprachverbindung hat einen Fehler gemeldet.";
    // Error EVENTS are advisory: the server keeps the socket open after
    // recoverable rejections (text_busy, invalid frames). Show the message
    // and keep the session — the websocket close event is the one true
    // fatal signal and has its own handler.
    statusDetailElement.textContent = text;
    clearMicGate(session);
    return;
  }
}

async function stopMicrophone(session) {
  if (session.microphoneStopped) {
    return;
  }
  session.microphoneStopped = true;
  if (session.workletNode) {
    session.workletNode.port.onmessage = null;
    session.workletNode.port.postMessage({ type: "stop" });
    session.workletNode.disconnect();
    session.workletNode.port.close();
  }
  if (session.microphoneSource) {
    session.microphoneSource.disconnect();
  }
  if (session.silentGain) {
    session.silentGain.disconnect();
  }
  if (session.stream) {
    for (const track of session.stream.getTracks()) {
      track.stop();
    }
  }
}

async function cleanupSession(session, { closeSocket = true } = {}) {
  if (session.serverDrainTimer !== null) {
    window.clearTimeout(session.serverDrainTimer);
    session.serverDrainTimer = null;
  }
  session.abortController.abort();
  await stopMicrophone(session);
  stopPlayback(session);
  clearMicGate(session);
  session.wakeLock?.release?.().catch(() => {});
  if (
    closeSocket &&
    session.websocket &&
    (session.websocket.readyState === WebSocket.OPEN ||
      session.websocket.readyState === WebSocket.CONNECTING)
  ) {
    session.websocket.close(1000, "client cleanup");
  }
  if (session.audioContext && session.audioContext.state !== "closed") {
    await session.audioContext.close().catch(() => {});
  }
}

async function finishSession(
  session,
  detail,
  { closeSocket = false, drainPlayback = false } = {},
) {
  if (!isCurrent(session) || session.finishing) {
    return;
  }
  session.finishing = true;
  session.pendingTranscript = { user: null, assistant: null };
  hideModeBadge();
  if (session.serverDrainTimer !== null) {
    window.clearTimeout(session.serverDrainTimer);
    session.serverDrainTimer = null;
  }
  if (drainPlayback) {
    statusDetailElement.textContent = "Die letzte Antwort wird noch abgespielt.";
    await waitForPlaybackDrain(session);
  }
  await cleanupSession(session, { closeSocket });
  if (isCurrent(session)) {
    activeSession = null;
    setButton("start");
    if (document.body.dataset.voiceState !== "error") {
      setStatus("idle", detail || "Sitzung beendet. Du kannst neu starten.");
    }
  }
}

function finalizeWebSocketClose(session, event) {
  if (NON_RETRYABLE_CLOSE_CODES.has(event.code)) {
    setStatus("error", "Die Anmeldung ist abgelaufen. Bitte lade die Seite neu.");
    void finishSession(session, undefined);
    return;
  }
  void finishSession(
    session,
    session.drainRequested
      ? "Sitzung vollständig beendet."
      : "Verbindung beendet. Du kannst neu starten.",
    {
      drainPlayback:
        session.drainRequested && (event.wasClean || event.code === 1000),
    },
  );
}

function canReconnect(session, event) {
  return (
    isCurrent(session) &&
    !session.drainRequested &&
    !session.finishing &&
    session.everOpen &&
    event.code !== 1000 &&
    !NON_RETRYABLE_CLOSE_CODES.has(event.code) &&
    session.reconnectAttempts < MAX_RECONNECT_ATTEMPTS
  );
}

async function attemptReconnect(session, event) {
  if (!canReconnect(session, event)) {
    finalizeWebSocketClose(session, event);
    return;
  }

  session.reconnectAttempts += 1;
  setStatus(
    "connecting",
    `Verbindung unterbrochen – verbindet neu (Versuch ${session.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}) …`,
  );
  stopPlayback(session);
  session.suppressIncomingAudio = false;
  resetBargeIn(session);

  await new Promise((resolve) => {
    window.setTimeout(resolve, RECONNECT_BACKOFF_MS[session.reconnectAttempts - 1]);
  });
  if (!isCurrent(session) || session.drainRequested || session.finishing) {
    return;
  }

  let ticket;
  try {
    ticket = await mintWebSocketTicket(session);
  } catch {
    if (!isCurrent(session) || session.drainRequested || session.finishing) {
      return;
    }
    // A mint failure creates no socket, so no close event will re-drive the
    // loop — recurse directly (with the original close event) instead.
    await attemptReconnect(session, event);
    return;
  }
  if (!isCurrent(session) || session.drainRequested || session.finishing) {
    return;
  }
  // From here the new socket's own events own the outcome: a failed connect
  // fires "close", which re-enters attemptReconnect exactly once. Never ALSO
  // await the socket here — a second waiter would double-drive the loop.
  session.websocket = createWebSocket(ticket);
  attachWebSocketHandlers(session);
}

function attachWebSocketHandlers(session) {
  const socket = session.websocket;
  socket.addEventListener("open", () => {
    if (!isCurrent(session) || session.websocket !== socket) {
      return;
    }
    session.everOpen = true;
    if (session.reconnectAttempts > 0 && !session.drainRequested) {
      session.voiceState = "listening";
      setStatus(
        "listening",
        "Verbindung wiederhergestellt. Sag einfach, was Hermes tun soll.",
      );
    }
  });
  socket.addEventListener("message", (event) => {
    if (!isCurrent(session) || session.websocket !== socket) {
      return;
    }
    // Only real application traffic proves the connection: a proxy that
    // accepts the handshake and then drops would otherwise reset the
    // counter on every "open" and defeat the five-attempt bound.
    session.reconnectAttempts = 0;
    try {
      if (typeof event.data === "string") {
        handleJsonMessage(session, event.data);
      } else {
        schedulePlayback(session, event.data);
      }
    } catch (error) {
      setStatus("error", safeErrorMessage(error));
      void finishSession(session, undefined, { closeSocket: true });
    }
  });
  socket.addEventListener("error", () => {
    if (isCurrent(session) && session.websocket === socket && !session.drainRequested) {
      setStatus("error", "Die Sprachverbindung wurde unterbrochen.");
    }
  });
  socket.addEventListener("close", (event) => {
    if (!isCurrent(session) || session.websocket !== socket) {
      return;
    }
    void attemptReconnect(session, event);
  });
}

async function startSession() {
  if (activeSession) {
    return;
  }
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    setStatus(
      "error",
      "Das Mikrofon benötigt eine sichere HTTPS-Verbindung (localhost ist ebenfalls erlaubt).",
    );
    return;
  }
  if (!window.AudioContext || !window.AudioWorkletNode) {
    setStatus("error", "Dieser Browser unterstützt die benötigte Audio-Technik nicht.");
    return;
  }

  const session = {
    id: ++nextSessionId,
    abortController: new AbortController(),
    audioContext: null,
    stream: null,
    microphoneSource: null,
    workletNode: null,
    silentGain: null,
    websocket: null,
    playbackSources: new Set(),
    playbackCursor: 0,
    voiceState: "connecting",
    suppressIncomingAudio: false,
    loudChunks: 0,
    bargeStartedAt: null,
    bargeTriggered: false,
    drainRequested: false,
    serverDrainTimer: null,
    microphoneStopped: false,
    finishing: false,
    everOpen: false,
    reconnectAttempts: 0,
    mode: null,
    wakeLock: null,
    muteMicUntilResponse: false,
    micGateTimer: null,
    pendingTranscript: { user: null, assistant: null },
  };
  activeSession = session;
  setButton("stop");
  setStatus("connecting", "Mikrofon und sichere Sprachverbindung werden vorbereitet.");

  try {
    session.audioContext = new AudioContext({ latencyHint: "interactive" });
    await session.audioContext.resume();
    await session.audioContext.audioWorklet.addModule("/voice/worklet.js");
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }
    session.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }

    session.microphoneSource = session.audioContext.createMediaStreamSource(
      session.stream,
    );
    session.workletNode = new AudioWorkletNode(
      session.audioContext,
      "hermes-mic-processor",
      {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: {
          targetSampleRate: INPUT_SAMPLE_RATE,
          frameSamples: INPUT_FRAME_SAMPLES,
        },
      },
    );
    session.silentGain = session.audioContext.createGain();
    session.silentGain.gain.value = 0;
    session.microphoneSource
      .connect(session.workletNode)
      .connect(session.silentGain)
      .connect(session.audioContext.destination);
    session.workletNode.port.onmessage = (event) => {
      handleMicFrame(session, event.data || {});
    };

    session.wakeLock = await navigator.wakeLock?.request?.("screen").catch(() => null);

    const ticket = await mintWebSocketTicket(session);
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }
    session.websocket = createWebSocket(ticket);
    attachWebSocketHandlers(session);
    await waitForWebSocket(session);
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }
    session.voiceState = "listening";
    setStatus("listening", "Sag einfach, was Hermes tun soll.");
  } catch (error) {
    if (!isCurrent(session)) {
      return;
    }
    setStatus("error", safeErrorMessage(error));
    await cleanupSession(session);
    if (isCurrent(session)) {
      activeSession = null;
      setButton("start");
    }
  }
}

async function requestStop(session) {
  if (!isCurrent(session) || session.drainRequested) {
    return;
  }
  session.drainRequested = true;
  setButton("draining");
  statusDetailElement.textContent = "Mikrofon aus – die letzte Antwort wird noch abgespielt.";
  await stopMicrophone(session);

  if (session.websocket?.readyState === WebSocket.OPEN) {
    session.websocket.send(JSON.stringify({ type: "end" }));
    session.serverDrainTimer = window.setTimeout(() => {
      if (!isCurrent(session) || session.finishing) {
        return;
      }
      setStatus(
        "error",
        "Der Server hat die Sitzung nicht rechtzeitig beendet. Die Verbindung wurde geschlossen.",
      );
      void finishSession(session, undefined, { closeSocket: true });
    }, SERVER_DRAIN_TIMEOUT_MS);
    return;
  }

  session.abortController.abort();
  await cleanupSession(session);
  if (isCurrent(session)) {
    activeSession = null;
    setButton("start");
    setStatus("idle", "Start abgebrochen. Du kannst neu starten.");
  }
}

sessionButton.addEventListener("click", () => {
  if (activeSession) {
    void requestStop(activeSession);
  } else {
    void startSession();
  }
});

function submitComposerText() {
  const text = composerInput.value.trim();
  if (!text) {
    return;
  }
  if (
    !activeSession ||
    !hasOpenWebSocket(activeSession) ||
    activeSession.drainRequested
  ) {
    statusDetailElement.textContent = NO_SESSION_TEXT_HINT;
    return;
  }
  const session = activeSession;
  session.websocket.send(JSON.stringify({ type: "text", text }));
  // Hold mic PCM until the server reacts to the typed turn (SDK contract:
  // realtime input and client-content turns must not interleave).
  session.muteMicUntilResponse = true;
  if (session.micGateTimer !== null) {
    window.clearTimeout(session.micGateTimer);
  }
  session.micGateTimer = window.setTimeout(() => {
    if (isCurrent(session)) {
      session.muteMicUntilResponse = false;
      session.micGateTimer = null;
    }
  }, MIC_GATE_FAILSAFE_MS);
  composerInput.value = "";
}

composerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitComposerText();
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  stashedInstallPrompt = event;
  installChipElement.hidden = false;
});

async function handleInstallChipClick() {
  if (!stashedInstallPrompt) {
    return;
  }
  const promptEvent = stashedInstallPrompt;
  stashedInstallPrompt = null;
  promptEvent.prompt();
  await promptEvent.userChoice;
  installChipElement.hidden = true;
}

installChipElement.addEventListener("click", () => {
  void handleInstallChipClick();
});

window.addEventListener("appinstalled", () => {
  stashedInstallPrompt = null;
  installChipElement.hidden = true;
});

window.addEventListener("pagehide", () => {
  if (activeSession) {
    void cleanupSession(activeSession);
  }
});

document.addEventListener("visibilitychange", () => {
  if (
    document.visibilityState !== "visible" ||
    !activeSession ||
    activeSession.finishing
  ) {
    return;
  }
  const session = activeSession;
  if (!session.wakeLock || session.wakeLock.released) {
    void navigator.wakeLock
      ?.request?.("screen")
      .catch(() => null)
      .then((lock) => {
        if (isCurrent(session) && !session.finishing) {
          session.wakeLock = lock;
        } else {
          // The session ended while the request was in flight — releasing
          // here prevents an idle page from keeping the phone awake.
          void lock?.release?.().catch(() => {});
        }
      });
  }
  if (session.audioContext?.state === "suspended") {
    void session.audioContext.resume();
  }
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/voice/sw.js", { scope: "/voice" }).catch(() => {});
}
