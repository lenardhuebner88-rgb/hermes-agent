"use strict";

const INPUT_SAMPLE_RATE = 16_000;
const INPUT_FRAME_SAMPLES = 320;
const OUTPUT_SAMPLE_RATE = 24_000;
const MAX_PLAYBACK_CHUNK_BYTES = OUTPUT_SAMPLE_RATE * 2 * 4;
const PLAYBACK_DRAIN_TIMEOUT_MS = 30_000;
const SERVER_DRAIN_TIMEOUT_MS = 180_000;

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

const statusElement = document.querySelector("#voice-status");
const statusDetailElement = document.querySelector("#status-detail");
const sessionButton = document.querySelector("#session-button");
const transcriptElement = document.querySelector("#transcript");
const emptyTranscriptElement = document.querySelector("#transcript-empty");

let activeSession = null;
let nextSessionId = 0;

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
}

function setButton(mode) {
  if (mode === "start") {
    sessionButton.textContent = "Start";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung starten");
    return;
  }
  if (mode === "stop") {
    sessionButton.textContent = "Stop";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung beenden");
    return;
  }
  sessionButton.textContent = "Wird beendet …";
  sessionButton.disabled = true;
  sessionButton.setAttribute("aria-label", "Sprachsitzung wird beendet");
}

function appendTranscript(role, text) {
  if (typeof text !== "string" || text.trim() === "") {
    return;
  }

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
  transcriptElement.append(entry);
  while (transcriptElement.children.length > 100) {
    transcriptElement.firstElementChild.remove();
  }
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });
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

function createWebSocket(ticket) {
  const websocketUrl = new URL("/api/voice/live", window.location.origin);
  websocketUrl.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  websocketUrl.searchParams.set("ticket", ticket);
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
        if (session.websocket.readyState === WebSocket.OPEN) {
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
    session.websocket.readyState === WebSocket.OPEN &&
    !session.drainRequested
  ) {
    session.websocket.send(message.pcm);
  }
}

function applyServerState(session, value) {
  if (!Object.hasOwn(STATUS_COPY, value) || value === "idle" || value === "error") {
    return;
  }
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
  if (message.type === "transcript") {
    appendTranscript(message.role === "assistant" ? "assistant" : "user", message.text);
    return;
  }
  if (message.type === "interrupted") {
    session.suppressIncomingAudio = true;
    stopPlayback(session);
    statusDetailElement.textContent = "Antwort unterbrochen – Hermes hört weiter zu.";
    return;
  }
  if (message.type === "error") {
    const text =
      message.error && typeof message.error.message === "string"
        ? message.error.message
        : "Die Sprachverbindung hat einen Fehler gemeldet.";
    throw new VoiceClientError(text);
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

function attachWebSocketHandlers(session) {
  session.websocket.addEventListener("message", (event) => {
    if (!isCurrent(session)) {
      return;
    }
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
  session.websocket.addEventListener("error", () => {
    if (isCurrent(session) && !session.drainRequested) {
      setStatus("error", "Die Sprachverbindung wurde unterbrochen.");
    }
  });
  session.websocket.addEventListener("close", (event) => {
    if (isCurrent(session)) {
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

window.addEventListener("pagehide", () => {
  if (activeSession) {
    void cleanupSession(activeSession);
  }
});
