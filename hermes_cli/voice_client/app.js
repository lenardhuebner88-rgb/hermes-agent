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
  spar: "Spar",
};

const statusElement = document.querySelector("#voice-status");
const statusDetailElement = document.querySelector("#status-detail");
const usageMeterElement = document.querySelector("#usage-meter");
const usageMeterFillElement = document.querySelector("#usage-meter-fill");
const usageLineElement = document.querySelector("#usage-line");
const sessionButton = document.querySelector("#session-button");
const transcriptElement = document.querySelector("#transcript");
const emptyTranscriptElement = document.querySelector("#transcript-empty");
const modeBadgeElement = document.querySelector("#mode-badge");
const modeExplanationElement = document.querySelector("#mode-explanation");
const connectionBannerElement = document.querySelector("#connection-banner");
const installCardElement = document.querySelector("#install-card");
const installButtonElement = document.querySelector("#install-button");
const installDismissElement = document.querySelector("#install-dismiss");
const composerForm = document.querySelector("#composer");
const composerInput = document.querySelector("#composer-input");
const composerSubmit = document.querySelector("#composer-submit");
const composerHintElement = document.querySelector("#composer-hint");
const cameraChipElement = document.querySelector("#camera-chip");
const screenChipElement = document.querySelector("#screen-chip");
const cameraShareStateElement = document.querySelector("#camera-share-state");
const screenShareStateElement = document.querySelector("#screen-share-state");
const screenShareHintElement = document.querySelector("#screen-share-hint");
const sharingIndicatorElement = document.querySelector("#sharing-indicator");
const sharingPreviewElement = document.querySelector("#sharing-preview");
const detailButtonElement = document.querySelector("#detail-frame-button");
const detailStateElement = document.querySelector("#detail-frame-state");
const modeLiveButton = document.querySelector('[data-mode-option="live"]');
const modeSparButton = document.querySelector('[data-mode-option="spar"]');
const talkButtonElement = document.querySelector("#talk-button");
const voiceTriggerElement = document.querySelector("#voice-trigger");
const phoneActionCardElement = document.querySelector("#phone-action-card");
const phoneActionImpactElement = document.querySelector("#phone-action-impact");
const phoneActionPreviewElement = document.querySelector("#phone-action-preview");
const phoneActionConfirmElement = document.querySelector("#phone-action-confirm");
const phoneActionCancelElement = document.querySelector("#phone-action-cancel");

const NO_SESSION_TEXT_HINT =
  "Starte zuerst eine Sitzung, dann kannst du auch schreiben.";

let activeSession = null;
let nextSessionId = 0;
let stashedInstallPrompt = null;
const INSTALL_CARD_DISMISSED_STORAGE_KEY = "hermesVoiceInstallCardDismissed";

function getInstallCardDismissed() {
  try {
    return window.localStorage?.getItem(INSTALL_CARD_DISMISSED_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function persistInstallCardDismissed() {
  try {
    window.localStorage?.setItem(INSTALL_CARD_DISMISSED_STORAGE_KEY, "1");
  } catch {
    // Storage may be blocked in private/embedded contexts. Dismissal still
    // applies for this page load through the in-memory flag below.
  }
}

let installCardDismissed = getInstallCardDismissed();
let connectionBannerTimer = null;
let nativePhoneActionsAvailable = false;

const PHONE_ACTION_COPY = {
  copy_text: "Text in die Zwischenablage kopieren",
  open_url: "Eine externe HTTPS-Adresse öffnen",
  share_text: "Androids Teilen-Menü mit diesem Text öffnen",
};

function clearPhoneAction(session) {
  const previousFocus = session?.phoneActionPreviousFocus;
  if (session) {
    session.pendingPhoneAction = null;
    session.phoneActionPreviousFocus = null;
  }
  document.body.dataset.phoneActionOpen = "false";
  phoneActionCardElement.hidden = true;
  phoneActionImpactElement.textContent = "";
  phoneActionPreviewElement.textContent = "";
  phoneActionConfirmElement.disabled = false;
  phoneActionCancelElement.disabled = false;
  if (previousFocus && previousFocus.isConnected !== false && typeof previousFocus.focus === "function") {
    previousFocus.focus();
  }
}

function invalidateNativePhoneActionSession(session) {
  const sessionId = session?.nativeActionSessionId;
  if (!sessionId) return;
  sendNativeBridgeMessage({
    v: 1, type: "invalidate_phone_action_session", session_id: sessionId,
  });
  session.nativeActionSessionId = null;
}

function beginNativePhoneActionSession(session) {
  invalidateNativePhoneActionSession(session);
  const randomUUID = window.crypto?.randomUUID;
  if (typeof randomUUID !== "function") return;
  const sessionId = randomUUID.call(window.crypto);
  session.nativeActionSessionId = sessionId;
  sendNativeBridgeMessage({
    v: 1, type: "begin_phone_action_session", session_id: sessionId,
  });
}

function sendPhoneActionResult(session, requestId, status) {
  if (!isCurrent(session) || !hasOpenWebSocket(session)) return;
  session.websocket.send(JSON.stringify({
    type: "phone_action_result", request_id: requestId, status,
  }));
}

function decidePhoneAction(decision) {
  const session = activeSession;
  const pending = session?.pendingPhoneAction;
  if (!pending || pending.decided || session.drainRequested || !hasOpenWebSocket(session)) return;
  pending.decided = true;
  phoneActionConfirmElement.disabled = true;
  phoneActionCancelElement.disabled = true;
  session.websocket.send(JSON.stringify({
    type: "phone_action_decision", request_id: pending.requestId, decision,
  }));
  if (decision === "cancelled") clearPhoneAction(session);
  else phoneActionImpactElement.textContent = "Bestätigt · sichere Ausführung wird vorbereitet …";
}

phoneActionConfirmElement?.addEventListener("click", () => decidePhoneAction("confirmed"));
phoneActionCancelElement?.addEventListener("click", () => decidePhoneAction("cancelled"));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && activeSession?.pendingPhoneAction) {
    event.preventDefault();
    decidePhoneAction("cancelled");
  }
});

function updateAppViewport() {
  const viewport = window.visualViewport;
  const height = viewport?.height || window.innerHeight;
  const offsetTop = viewport?.offsetTop || 0;
  if (Number.isFinite(height) && height > 0) {
    document.documentElement?.style.setProperty("--app-height", `${height}px`);
    document.documentElement?.style.setProperty("--app-top", `${offsetTop}px`);
  }
  const composerFocused = document.activeElement === composerInput;
  const keyboardOpen = Boolean(
    viewport &&
      (composerFocused ||
        (Number.isFinite(window.innerHeight) && viewport.height < window.innerHeight - 120)),
  );
  document.body.dataset.keyboardOpen = keyboardOpen ? "true" : "false";
}

window.visualViewport?.addEventListener("resize", updateAppViewport);
window.visualViewport?.addEventListener("scroll", updateAppViewport);
window.addEventListener("resize", updateAppViewport);
updateAppViewport();
composerInput.addEventListener("focus", updateAppViewport);
composerInput.addEventListener("blur", updateAppViewport);

function haptic(pattern) {
  if (
    typeof navigator.vibrate !== "function" ||
    window.matchMedia?.("(prefers-reduced-motion: reduce)").matches
  ) {
    return;
  }
  try {
    navigator.vibrate(pattern);
  } catch {
    // Some embedded WebViews expose vibrate but reject it at runtime.
  }
}

function setConnectionBanner(text, tone = "neutral", hideAfterMs = 0) {
  if (!connectionBannerElement) {
    return;
  }
  if (connectionBannerTimer !== null) {
    window.clearTimeout(connectionBannerTimer);
    connectionBannerTimer = null;
  }
  connectionBannerElement.hidden = !text;
  connectionBannerElement.textContent = text || "";
  connectionBannerElement.setAttribute("data-tone", tone);
  if (text && hideAfterMs > 0) {
    connectionBannerTimer = window.setTimeout(() => {
      connectionBannerElement.hidden = true;
      connectionBannerTimer = null;
    }, hideAfterMs);
  }
}

// Sparmodus (cascade, $0 marginal cost): chosen before Start, persisted
// across reconnects (AC). Live stays the default — Sparmodus is opt-in.
const VOICE_MODE_STORAGE_KEY = "hermesVoiceModePreference";

function getStoredVoiceMode() {
  try {
    const stored = window.localStorage?.getItem(VOICE_MODE_STORAGE_KEY);
    return stored === "spar" ? "spar" : "live";
  } catch {
    return "live";
  }
}

function setStoredVoiceMode(mode) {
  try {
    window.localStorage?.setItem(VOICE_MODE_STORAGE_KEY, mode);
  } catch {
    // Storage can be blocked (private mode) — the in-memory selection still
    // works for the rest of this page load.
  }
}

let selectedVoiceMode = getStoredVoiceMode();

function renderModeToggle() {
  const locked = Boolean(activeSession);
  for (const [button, mode] of [
    [modeLiveButton, "live"],
    [modeSparButton, "spar"],
  ]) {
    if (!button) {
      continue;
    }
    const checked = selectedVoiceMode === mode;
    button.setAttribute("aria-checked", checked ? "true" : "false");
    button.disabled = locked;
  }
  if (modeExplanationElement) {
    modeExplanationElement.hidden = locked;
    modeExplanationElement.textContent =
      selectedVoiceMode === "spar"
        ? "Spar · Push-to-talk, etwas langsamer, praktisch kostenlos"
        : "Live · natürliches Echtzeitgespräch";
  }
}

// Spar-Warmup (turn-1-latency): fire-and-forget hint to the server to
// prespawn a persistent claude-lane child + load the whisper model before
// the next Sparmodus session actually starts. Best-effort only — a failed
// or throttled warmup just costs the next session its usual cold-start
// latency, nothing else depends on it. Throttled client-side so page load
// plus a toggle click within the same minute can't fire it twice.
const SPAR_WARMUP_THROTTLE_MS = 60_000;
let lastSparWarmupAt = -Infinity;

function warmupSparMode() {
  const now = Date.now();
  if (now - lastSparWarmupAt < SPAR_WARMUP_THROTTLE_MS) {
    return;
  }
  lastSparWarmupAt = now;
  const headers = new Headers();
  const loopbackToken = window.__HERMES_SESSION_TOKEN__;
  if (typeof loopbackToken === "string" && loopbackToken.length > 0) {
    headers.set("X-Hermes-Session-Token", loopbackToken);
  }
  fetch("/api/voice/spar/warmup", {
    method: "POST",
    credentials: "same-origin",
    headers,
  }).catch(() => {
    // Ignored — see comment above.
  });
}

function selectVoiceMode(mode) {
  if (activeSession || (mode !== "live" && mode !== "spar")) {
    return;
  }
  selectedVoiceMode = mode;
  setStoredVoiceMode(mode);
  renderModeToggle();
  if (mode === "spar") {
    warmupSparMode();
  }
}

modeLiveButton?.addEventListener("click", () => selectVoiceMode("live"));
modeSparButton?.addEventListener("click", () => selectVoiceMode("spar"));
renderModeToggle();
if (selectedVoiceMode === "spar") {
  warmupSparMode();
}

// "Sehen" (camera/screen sharing): state lives outside any voice session
// object because the toggle chips are always visible in the header — a
// share can be started before a session exists and must keep running
// across a mic-session reconnect (see canSendVideoFrame, which reads
// activeSession fresh on every capture tick).
let sharingSource = null;
let sharingStream = null;
let sharingIntervalId = null;
let sharingStartGeneration = 0;
let sharingCanvasElement = null;
let sharingCanvasContext = null;
let pendingDetailRequestId = null;
const MAX_DETAIL_FRAME_BYTES = 512 * 1024;

// Native Android screen-share bridge (Bridge Protocol v1, JSON strings both
// ways). `nativeScreen.generation` snapshots `sharingStartGeneration` at
// request time so a stale native reply that lands after a stop/restart is
// ignored the same way a stray getDisplayMedia() resolution already is.
let nativeScreen = { available: false, state: "idle", generation: 0 };

function renderSharingControls() {
  const cameraActive = sharingSource === "camera";
  const screenActive =
    sharingSource === "screen" ||
    nativeScreen.state === "requesting" ||
    nativeScreen.state === "active";
  cameraShareStateElement.textContent = cameraActive ? "Wird geteilt · Stoppen" : "Aus";
  screenShareStateElement.textContent =
    nativeScreen.state === "requesting"
      ? "Wird gestartet …"
      : screenActive
        ? "Wird geteilt · Stoppen"
        : "Aus";
  cameraChipElement.setAttribute(
    "aria-label",
    cameraActive ? "Kamerafreigabe stoppen" : "Kamera für Hermes freigeben",
  );
  screenChipElement.setAttribute(
    "aria-label",
    screenActive ? "Bildschirmfreigabe stoppen" : "Bildschirm für Hermes freigeben",
  );
  if (detailButtonElement) {
    const shortcutAvailable = activeSession?.voiceMode !== "spar";
    detailButtonElement.disabled =
      !(cameraActive || screenActive) ||
      !hasOpenWebSocket(activeSession) ||
      !shortcutAvailable ||
      Boolean(activeSession?.drainRequested) ||
      Boolean(activeSession?.muteMicUntilResponse);
    detailButtonElement.title = shortcutAvailable
      ? "Ein frisches hochauflösendes Einzelbild analysieren"
      : "Im Sparmodus bitte „Genau ansehen“ sagen";
  }
}

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
  // Mirrors onto <body data-mode="…"> so the CSS accent (bronze for "live",
  // teal reserved for the upcoming "spar" budget mode) can key off the root
  // element instead of a header chip that scrolls out of view.
  delete document.body.dataset.mode;
}

function renderModeBadge(value) {
  if (!Object.hasOwn(MODE_BADGE_COPY, value)) {
    return;
  }
  modeBadgeElement.textContent = MODE_BADGE_COPY[value];
  modeBadgeElement.dataset.mode = value;
  modeBadgeElement.hidden = false;
  document.body.dataset.mode = value;
}

function setComposerEnabled(enabled, lockedCopy = "") {
  composerInput.setAttribute("aria-disabled", enabled ? "false" : "true");
  composerInput.disabled = !enabled;
  composerSubmit.disabled = !enabled;
  composerInput.placeholder = enabled ? "Nachricht an Hermes …" : lockedCopy;
  composerHintElement.textContent = lockedCopy;
  composerHintElement.hidden = enabled;
}

function updateTalkButtonVisibility() {
  if (!talkButtonElement) {
    return;
  }
  const isSpar = Boolean(activeSession) && activeSession.voiceMode === "spar";
  talkButtonElement.hidden = !isSpar;
  if (!isSpar) {
    talkButtonElement.disabled = true;
  }
}

function setButton(mode) {
  renderModeToggle();
  updateTalkButtonVisibility();
  if (mode === "start") {
    sessionButton.textContent = "Sitzung starten";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung starten");
    voiceTriggerElement?.setAttribute("aria-label", "Sprachsitzung starten");
    setComposerEnabled(false, "Nach Sitzungsstart verfügbar.");
    document.documentElement?.style.setProperty("--mic-lift", "0");
    return;
  }
  if (mode === "stop") {
    sessionButton.textContent = "Sitzung beenden";
    sessionButton.disabled = false;
    sessionButton.setAttribute("aria-label", "Sprachsitzung beenden");
    voiceTriggerElement?.setAttribute("aria-label", "Sprachsitzung beenden");
    // Sparmodus has no typed-turn control frame server-side (walkie-talkie
    // only) — the composer stays closed for the whole session.
    setComposerEnabled(
      activeSession?.voiceMode !== "spar",
      "Im Sparmodus sprichst du über die Sprechtaste.",
    );
    if (talkButtonElement) {
      talkButtonElement.disabled = false;
    }
    return;
  }
  sessionButton.textContent = "Wird beendet …";
  sessionButton.disabled = true;
  sessionButton.setAttribute("aria-label", "Sprachsitzung wird beendet");
  voiceTriggerElement?.setAttribute("aria-label", "Sprachsitzung wird beendet");
  // After "end" the server accepts only interrupt controls — a typed frame
  // during the drain would be rejected, so the composer closes with the mic.
  setComposerEnabled(false, "Sitzung wird beendet …");
  if (talkButtonElement) {
    talkButtonElement.disabled = true;
  }
}

function activateVoiceTrigger() {
  if (!sessionButton.disabled) {
    sessionButton.click();
  }
}

voiceTriggerElement?.addEventListener("click", activateVoiceTrigger);
voiceTriggerElement?.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    activateVoiceTrigger();
  }
});

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
    scrollTranscriptIntoView(entry);
    if (partial) {
      session.pendingTranscript[key] = entry;
    }
    return;
  }

  setTranscriptEntryText(pending, text);
  if (!partial) {
    session.pendingTranscript[key] = null;
    scrollTranscriptIntoView(pending);
  }
}

// Auto-scroll follows new turns only while the operator is already at (or
// near) the bottom — once they scroll up to re-read earlier context, new
// messages must not yank the view back down. `transcriptUserScrolledUp`
// tracks both scroll surfaces the CSS can put the transcript on: the
// internal `#transcript` scrollbox on the desktop two-pane layout, and the
// page scroll on mobile where the whole `<main>` scrolls instead.
let transcriptUserScrolledUp = false;

function isNearBottom(element, threshold) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= threshold;
}

function scrollTranscriptIntoView(entry) {
  if (transcriptUserScrolledUp) {
    return;
  }
  entry.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

transcriptElement.addEventListener("scroll", () => {
  transcriptUserScrolledUp = !isNearBottom(transcriptElement, 24);
});

window.addEventListener("scroll", () => {
  const doc = document.documentElement;
  if (!doc) {
    return;
  }
  transcriptUserScrolledUp = !isNearBottom(doc, 48);
});

async function acquireMicrophoneStream() {
  // Preferred path: hardware echo cancellation / noise suppression. On some Android
  // WebView builds this forces the mic into communication-mode capture that the OS
  // refuses to open (surfaces as NotReadableError even with a free mic). If that
  // happens, retry once with a plain audio request, which avoids that capture mode.
  try {
    return await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
      video: false,
    });
  } catch (error) {
    if (error && (error.name === "NotReadableError" || error.name === "OverconstrainedError")) {
      return await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    }
    throw error;
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

function createWebSocket(ticket, voiceMode) {
  const path = voiceMode === "spar" ? "/api/voice/spar" : "/api/voice/live";
  const websocketUrl = new URL(path, window.location.origin);
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

function canSendVideoFrame(session) {
  return (
    Boolean(session) &&
    hasOpenWebSocket(session) &&
    session.mode !== "fallback" &&
    !session.muteMicUntilResponse
  );
}

function getSharingCanvas() {
  if (!sharingCanvasElement) {
    sharingCanvasElement = document.createElement("canvas");
    sharingCanvasContext = sharingCanvasElement.getContext("2d");
  }
  return sharingCanvasElement;
}

function encodeBlobAsBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const commaIndex = result.indexOf(",");
      resolve(commaIndex === -1 ? "" : result.slice(commaIndex + 1));
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

async function captureAndSendFrame() {
  if (!sharingStream || !canSendVideoFrame(activeSession)) {
    return;
  }
  const videoWidth = sharingPreviewElement.videoWidth;
  const videoHeight = sharingPreviewElement.videoHeight;
  if (!videoWidth || !videoHeight) {
    return;
  }
  const longestEdge = Math.max(videoWidth, videoHeight);
  const scale = longestEdge > 1024 ? 1024 / longestEdge : 1;
  const canvas = getSharingCanvas();
  canvas.width = Math.round(videoWidth * scale);
  canvas.height = Math.round(videoHeight * scale);
  sharingCanvasContext.drawImage(sharingPreviewElement, 0, 0, canvas.width, canvas.height);

  const blob = await new Promise((resolve) => {
    canvas.toBlob(resolve, "image/jpeg", 0.7);
  });
  // Re-check after each await: the share (or the session's WS/mode/mic-gate)
  // may have changed while the canvas encode was in flight.
  if (!blob || !sharingStream || !canSendVideoFrame(activeSession)) {
    return;
  }
  const base64Data = await encodeBlobAsBase64(blob);
  if (!base64Data || !sharingStream || !canSendVideoFrame(activeSession)) {
    return;
  }
  activeSession.websocket.send(
    JSON.stringify({ type: "video_frame", data: base64Data, source: sharingSource }),
  );
}

async function encodeDetailFrame(maxEdge, quality) {
  if (!sharingStream) return "";
  const videoWidth = sharingPreviewElement.videoWidth;
  const videoHeight = sharingPreviewElement.videoHeight;
  if (!videoWidth || !videoHeight) return "";
  let edge = Math.max(1024, Math.min(2048, Number(maxEdge) || 2048));
  let jpegQuality = Math.max(0.65, Math.min(0.92, Number(quality) || 0.9));
  const canvas = getSharingCanvas();
  for (let attempt = 0; attempt < 6; attempt += 1) {
    const scale = Math.min(1, edge / Math.max(videoWidth, videoHeight));
    canvas.width = Math.max(1, Math.round(videoWidth * scale));
    canvas.height = Math.max(1, Math.round(videoHeight * scale));
    sharingCanvasContext.drawImage(sharingPreviewElement, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", jpegQuality));
    if (!blob) return "";
    if (blob.size <= MAX_DETAIL_FRAME_BYTES) return encodeBlobAsBase64(blob);
    if (attempt < 2) jpegQuality -= 0.12;
    else edge = Math.round(edge * 0.82);
  }
  return "";
}

function sendDetailUnavailable(session, requestId) {
  if (isCurrent(session) && hasOpenWebSocket(session)) {
    session.websocket.send(JSON.stringify({ type: "detail_frame_unavailable", request_id: requestId }));
  }
  if (pendingDetailRequestId === requestId) pendingDetailRequestId = null;
  if (detailStateElement) detailStateElement.textContent = "Kein Detailbild verfügbar";
  renderSharingControls();
}

async function handleDetailFrameRequest(session, message) {
  const requestId = typeof message.request_id === "string" ? message.request_id : "";
  if (!/^[a-f0-9]{32}$/.test(requestId) || !isCurrent(session)) return;
  pendingDetailRequestId = requestId;
  if (detailStateElement) detailStateElement.textContent = "Detailbild wird aufgenommen …";
  if (detailButtonElement) detailButtonElement.disabled = true;

  if (nativeScreen.state === "active") {
    if (!sendNativeBridgeMessage({
      v: 1,
      type: "capture_detail_frame",
      request_id: requestId,
      max_edge: message.max_edge,
      quality: message.quality,
    })) {
      sendDetailUnavailable(session, requestId);
    }
    return;
  }
  if (!sharingStream || !canSendVideoFrame(session)) {
    sendDetailUnavailable(session, requestId);
    return;
  }
  const data = await encodeDetailFrame(message.max_edge, message.quality);
  if (!data || pendingDetailRequestId !== requestId || !isCurrent(session) || !hasOpenWebSocket(session)) {
    sendDetailUnavailable(session, requestId);
    return;
  }
  session.websocket.send(JSON.stringify({
    type: "detail_frame", request_id: requestId, data, source: sharingSource,
  }));
  pendingDetailRequestId = null;
  if (detailStateElement) detailStateElement.textContent = "Detailbild gesendet";
  renderSharingControls();
}

function requestLookClosely() {
  if (!sharingStream && nativeScreen.state !== "active") return;
  if (
    sendTypedTurn(
      "Nutze look_closely und sieh dir die aktuell geteilte Ansicht genau an. Lies kleine Schrift und UI-Details präzise.",
    )
  ) {
    if (detailStateElement) detailStateElement.textContent = "Genaues Ansehen angefordert …";
  }
}

detailButtonElement?.addEventListener("click", requestLookClosely);

// notifyNative=false is for the path where NATIVE told us the share already
// stopped/errored (screen_capture_stopped/screen_capture_error): echoing
// stop_screen_capture back in that case would risk a ping-pong loop with the
// shell. stopSharing() itself keeps its existing zero-arg shape (an existing
// tripwire test pins `"function stopSharing()"`) and always notifies.
function resetSharing(notifyNative) {
  // Invalidate any in-flight startSharing: a getUserMedia/getDisplayMedia
  // await that resolves after this point must not resurrect a share (its
  // late stream gets stopped in startSharing's generation check).
  sharingStartGeneration += 1;
  const wasNativeSharing = nativeScreen.state !== "idle";
  const wasSharing = Boolean(sharingStream) || wasNativeSharing;
  if (wasNativeSharing) {
    // Reset state BEFORE notifying native: an incoming screen_capture_stopped
    // calls this with notifyNative=false, but any other caller (chip click,
    // requestStop, mode fallback, mutual exclusion) may still be racing a
    // native event — resetting first means a stray reply lands on "idle" and
    // is ignored instead of re-entering this branch.
    nativeScreen.state = "idle";
    if (notifyNative) {
      sendNativeBridgeMessage({ v: 1, type: "stop_screen_capture" });
    }
  }
  if (wasSharing && hasOpenWebSocket(activeSession)) {
    activeSession.websocket.send(JSON.stringify({ type: "sharing_stopped" }));
  }
  if (sharingIntervalId !== null) {
    window.clearInterval(sharingIntervalId);
    sharingIntervalId = null;
  }
  if (sharingStream) {
    // track.stop() intentionally never fires "ended" (spec), so no listener
    // teardown is needed here — only external cessation (device unplugged,
    // browser's native "Stop sharing" UI) reaches the handler below.
    for (const track of sharingStream.getTracks()) {
      track.stop();
    }
  }
  sharingStream = null;
  sharingSource = null;
  sharingPreviewElement.srcObject = null;
  sharingPreviewElement.hidden = true;
  sharingIndicatorElement.hidden = true;
  cameraChipElement.setAttribute("aria-pressed", "false");
  screenChipElement.setAttribute("aria-pressed", "false");
  renderSharingControls();
}

function stopSharing() {
  resetSharing(true);
}

async function startSharing(source) {
  // Mutually exclusive: activating one source stops the other first.
  stopSharing();
  const generation = ++sharingStartGeneration;
  if (source === "screen" && nativeScreen.available) {
    // Native wins in the shell regardless of whether getDisplayMedia exists —
    // no browser capture prompt, no local <video> preview, no capture timer.
    nativeScreen.state = "requesting";
    nativeScreen.generation = generation;
    renderSharingControls();
    sendNativeBridgeMessage({ v: 1, type: "start_screen_capture" });
    return;
  }
  try {
    const stream =
      source === "camera"
        ? await navigator.mediaDevices.getUserMedia({
            video: { facingMode: "environment" },
          })
        : await navigator.mediaDevices.getDisplayMedia({ video: true });
    if (generation !== sharingStartGeneration) {
      // A newer start/stop superseded this one while the permission prompt
      // was open (double-click, cross-click): a stray live stream here would
      // keep the camera on with the UI showing "off" — stop it immediately.
      for (const track of stream.getTracks()) {
        track.stop();
      }
      return;
    }
    sharingStream = stream;
    sharingSource = source;
    sharingPreviewElement.srcObject = stream;
    sharingPreviewElement.hidden = false;
    sharingIndicatorElement.hidden = false;
    (source === "camera" ? cameraChipElement : screenChipElement).setAttribute(
      "aria-pressed",
      "true",
    );
    renderSharingControls();
    for (const track of stream.getVideoTracks()) {
      // The user can stop a screen share via the browser's own "Stop
      // sharing" UI, bypassing our chip entirely — that fires "ended".
      track.addEventListener("ended", () => {
        if (sharingStream === stream) {
          stopSharing();
        }
      });
    }
    sharingIntervalId = window.setInterval(() => {
      void captureAndSendFrame();
    }, 1000);
  } catch {
    if (generation === sharingStartGeneration) {
      stopSharing();
    }
  }
}

function toggleSharing(source) {
  if (
    sharingSource === source ||
    (source === "screen" && nativeScreen.state !== "idle")
  ) {
    stopSharing();
    return;
  }
  void startSharing(source);
}

function featureDetectScreenShare() {
  if (typeof navigator.mediaDevices?.getDisplayMedia !== "function") {
    screenChipElement.disabled = true;
    screenShareHintElement.hidden = false;
    screenChipElement.title =
      "Bildschirmfreigabe wird von diesem Browser nicht unterstützt.";
  }
}
featureDetectScreenShare();
renderSharingControls();

cameraChipElement.addEventListener("click", () => {
  toggleSharing("camera");
});
screenChipElement.addEventListener("click", () => {
  toggleSharing("screen");
});

function sendNativeBridgeMessage(message) {
  try {
    const bridge = window.HermesNative;
    if (!bridge || typeof bridge.postMessage !== "function") return false;
    bridge.postMessage(JSON.stringify(message));
    return true;
  } catch {
    // The bridge object can vanish or throw across WebView lifecycle
    // transitions (activity recreation, process death) — never fatal here.
    return false;
  }
}

function sumTokenGroup(group) {
  if (!group || typeof group !== "object") {
    return 0;
  }
  return Object.values(group).reduce((total, value) => {
    const numeric = Number(value);
    return total + (Number.isFinite(numeric) ? numeric : 0);
  }, 0);
}

function formatUsageDetail(message) {
  // Sparmodus turns are $0 marginal cost by construction (local STT/TTS +
  // a subscription-lane LLM CLI) — show that instead of a dollar amount
  // computed from token pricing that doesn't apply to this mode.
  if (message.mode === "spar" && typeof message.label === "string") {
    return message.label;
  }
  const totalTokens =
    sumTokenGroup(message.tokens?.input) + sumTokenGroup(message.tokens?.output);
  if (
    typeof message.estimated_usd !== "number" ||
    !Number.isFinite(message.estimated_usd)
  ) {
    return `~${Math.round(totalTokens)} Tokens (keine Preisdaten)`;
  }
  const amount = `$${message.estimated_usd.toFixed(3)}`;
  if (message.estimate_incomplete === true) {
    return `Kosten ≥ ${amount} (unvollständig)`;
  }
  const minutes = Math.round((Number(message.session_seconds) || 0) / 60);
  return `Kosten ≈ ${amount} · ${minutes} Min`;
}

// Fill percentage for the budget meter, derived from the same
// estimated_usd/soft_budget_usd pair the text label already renders — never
// a second source of truth. `usageMeterElement.style` is absent in the
// node:vm test harness (its fake elements only model what the tests assert
// on), so the guard also doubles as the harness compatibility check.
function updateUsageMeter(message, isWarn) {
  if (!usageMeterElement) {
    return;
  }
  usageMeterElement.hidden = false;
  usageMeterElement.classList.toggle("usage-meter--warn", isWarn);
  if (!usageMeterFillElement || !usageMeterFillElement.style) {
    return;
  }
  const budget = Number(message.soft_budget_usd);
  const spent = Number(message.estimated_usd);
  const pct =
    Number.isFinite(budget) && budget > 0 && Number.isFinite(spent)
      ? Math.max(0, Math.min(100, (spent / budget) * 100))
      : 0;
  usageMeterFillElement.style.setProperty("--usage-pct", `${pct}%`);
}

function speakUsageWarningOnce(session, text) {
  if (session.usageWarningSpoken || typeof window.speechSynthesis === "undefined") {
    return;
  }
  session.usageWarningSpoken = true;
  try {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "de-DE";
    window.speechSynthesis.speak(utterance);
  } catch {
    // speechSynthesis can be unavailable or throw in some WebViews — the
    // visible status text already carries the warning either way.
  }
}

// Bridge Protocol v1: JSON strings both ways, never trust the shape. Bad
// JSON, an unknown type, or a mismatched version are ignored silently — the
// native shell may be running a newer/older bridge build than this client.
function handleNativeBridgeMessage(raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return;
  }
  if (!message || message.v !== 1 || typeof message.type !== "string") {
    return;
  }

  if (message.type === "native_capabilities") {
    nativePhoneActionsAvailable = message.phone_action === true;
    if (message.screen_capture === true) {
      nativeScreen.available = true;
      // Undo the getDisplayMedia-based disable from featureDetectScreenShare:
      // the native shell can capture the screen even where the WebView's own
      // getDisplayMedia is absent.
      screenChipElement.disabled = false;
      screenShareHintElement.hidden = true;
      screenChipElement.title = "Bildschirm für Hermes freigeben";
    }
    return;
  }
  if (message.type === "phone_action_result") {
    const session = activeSession;
    const pending = session?.pendingPhoneAction;
    if (
      pending && pending.requestId === message.request_id && pending.executing &&
      ["executed", "unsupported", "failed"].includes(message.status)
    ) {
      sendPhoneActionResult(session, pending.requestId, message.status);
      clearPhoneAction(session);
    }
    return;
  }
  if (message.type === "screen_capture_started") {
    if (nativeScreen.state === "requesting" && nativeScreen.generation === sharingStartGeneration) {
      nativeScreen.state = "active";
      sharingSource = "screen";
      sharingIndicatorElement.hidden = false;
      screenChipElement.setAttribute("aria-pressed", "true");
      renderSharingControls();
    } else {
      // Stale reply: the client already moved on (canceled, superseded by a
      // newer start/stop) before native confirmed this one — kill the
      // orphaned capture instead of leaving it running headless.
      sendNativeBridgeMessage({ v: 1, type: "stop_screen_capture" });
    }
    return;
  }
  if (message.type === "screen_frame") {
    if (
      nativeScreen.state === "active" &&
      canSendVideoFrame(activeSession) &&
      typeof message.data === "string"
    ) {
      activeSession.websocket.send(
        JSON.stringify({ type: "video_frame", data: message.data, source: "screen" }),
      );
    }
    return;
  }
  if (message.type === "detail_screen_frame") {
    const requestId = typeof message.request_id === "string" ? message.request_id : "";
    if (
      requestId === pendingDetailRequestId &&
      nativeScreen.state === "active" &&
      canSendVideoFrame(activeSession) &&
      typeof message.data === "string"
    ) {
      activeSession.websocket.send(JSON.stringify({
        type: "detail_frame", request_id: requestId, data: message.data, source: "screen",
      }));
      pendingDetailRequestId = null;
      if (detailStateElement) detailStateElement.textContent = "Detailbild gesendet";
      renderSharingControls();
    }
    return;
  }
  if (message.type === "detail_screen_frame_unavailable") {
    if (message.request_id === pendingDetailRequestId) {
      sendDetailUnavailable(activeSession, message.request_id);
    }
    return;
  }
  if (message.type === "screen_capture_stopped" || message.type === "screen_capture_error") {
    if (nativeScreen.state !== "idle") {
      if (message.type === "screen_capture_error" && typeof message.message === "string") {
        statusDetailElement.textContent = message.message;
      }
      // The native side already stopped/errored — reset our UI without
      // echoing stop_screen_capture back (avoids a ping-pong loop).
      resetSharing(false);
    }
    return;
  }
}

function initNativeBridge() {
  const bridge = window.HermesNative;
  if (!bridge || typeof bridge.postMessage !== "function") {
    return;
  }
  const handleMessage = (event) => {
    handleNativeBridgeMessage(event && typeof event.data === "string" ? event.data : "");
  };
  if (typeof bridge.addEventListener === "function") {
    bridge.addEventListener("message", handleMessage);
  } else {
    bridge.onmessage = handleMessage;
  }
  sendNativeBridgeMessage({ v: 1, type: "bridge_ready" });
}
initNativeBridge();

function handleMicFrame(session, message) {
  if (!isCurrent(session) || session.microphoneStopped) {
    return;
  }

  const measuredRms = Number(message.rms);
  if (Number.isFinite(measuredRms)) {
    const targetLevel = Math.min(1, Math.max(0, measuredRms / 0.12));
    session.micLevel = (session.micLevel || 0) * 0.7 + targetLevel * 0.3;
    document.documentElement?.style.setProperty(
      "--mic-lift",
      (session.micLevel * 0.14).toFixed(3),
    );
  }

  if (session.voiceMode === "spar") {
    // Walkie-talkie contract: no barge-in, no continuous streaming — only
    // forward PCM while the talk button is actively held, and only once
    // the server has confirmed it's listening for the next turn.
    if (
      session.sparRecording &&
      message.pcm instanceof ArrayBuffer &&
      hasOpenWebSocket(session) &&
      !session.drainRequested
    ) {
      session.websocket.send(message.pcm);
    }
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
  renderSharingControls();
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
  if (message.type === "detail_frame_request") {
    void handleDetailFrameRequest(session, message);
    return;
  }
  if (message.type === "detail_frame_timeout") {
    if (message.request_id === pendingDetailRequestId) {
      pendingDetailRequestId = null;
      if (detailStateElement) detailStateElement.textContent = "Detailaufnahme hat zu lange gedauert";
      renderSharingControls();
    }
    return;
  }
  if (message.type === "phone_action_confirmation") {
    if (
      typeof message.request_id !== "string" ||
      typeof message.preview !== "string" ||
      !PHONE_ACTION_COPY[message.action] ||
      session.pendingPhoneAction
    ) return;
    session.pendingPhoneAction = {
      requestId: message.request_id, action: message.action, decided: false, executing: false,
    };
    session.phoneActionPreviousFocus = document.activeElement;
    document.body.dataset.phoneActionOpen = "true";
    phoneActionImpactElement.textContent = `${PHONE_ACTION_COPY[message.action]}. Diese Aktion kann außerhalb von Hermes wirken.`;
    phoneActionPreviewElement.textContent = message.preview;
    phoneActionCardElement.hidden = false;
    phoneActionConfirmElement.disabled = false;
    phoneActionCancelElement.disabled = false;
    phoneActionConfirmElement.focus();
    return;
  }
  if (message.type === "phone_action_execute") {
    const pending = session.pendingPhoneAction;
    if (!pending || !pending.decided || pending.executing || pending.requestId !== message.request_id) return;
    if (session.drainRequested) {
      sendPhoneActionResult(session, pending.requestId, "cancelled");
      clearPhoneAction(session);
      return;
    }
    if (message.action !== pending.action) {
      sendPhoneActionResult(session, pending.requestId, "failed");
      clearPhoneAction(session);
      return;
    }
    pending.executing = true;
    if (!nativePhoneActionsAvailable || !session.nativeActionSessionId || !sendNativeBridgeMessage({
      v: 1, type: "execute_phone_action", request_id: pending.requestId,
      action: message.action, text: message.text, url: message.url,
      expires_at_ms: message.expires_at_ms,
      session_id: session.nativeActionSessionId,
    })) {
      sendPhoneActionResult(session, pending.requestId, "unsupported");
      clearPhoneAction(session);
    }
    return;
  }
  if (message.type === "phone_action_closed") {
    if (session.pendingPhoneAction?.requestId === message.request_id) {
      invalidateNativePhoneActionSession(session);
      clearPhoneAction(session);
      if (!session.drainRequested && hasOpenWebSocket(session)) {
        beginNativePhoneActionSession(session);
      }
    }
    return;
  }
  if (message.type === "mode") {
    session.mode = message.value;
    if (typeof message.video_mode === "string") {
      // Told once, on the same event that already announces live/fallback:
      // informational only (e.g. for a future UI indicator) — capture
      // itself always runs at a fixed 1fps regardless of mode; gating
      // on_demand vs. stream happens server-side at the relay.
      session.videoMode = message.video_mode;
    }
    renderModeBadge(message.value);
    if (message.value === "fallback") {
      // The capture tick gates on mode !== "fallback", so after this event
      // no frame ever reaches the server and its video_unavailable_fallback
      // advisory (our other auto-stop) can never fire — stop here instead of
      // leaving the camera on with a pulsing "teilt" that sends nothing.
      stopSharing();
    }
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
    if (message.error && message.error.code === "video_unavailable_fallback") {
      // The server won't accept frames again until the session leaves
      // fallback mode — stop hammering it and reset the toggle UI.
      stopSharing();
    }
    return;
  }
  if (message.type === "usage_update") {
    usageLineElement.textContent = formatUsageDetail(message);
    usageLineElement.hidden = false;
    const isWarn = message.soft_budget_exceeded === true;
    usageLineElement.classList.toggle("usage-line--warn", isWarn);
    updateUsageMeter(message, isWarn);
    return;
  }
  if (message.type === "usage_warning") {
    const minutes = Math.round(Number(message.minutes) || 0);
    statusDetailElement.textContent = `Kostenwarnung: Sitzung läuft seit ${minutes} Minuten.`;
    speakUsageWarningOnce(session, statusDetailElement.textContent);
    return;
  }
  if (message.type === "session_ended") {
    // The server closes the websocket right after this (code 1000), which
    // drives finishSession with its own generic idle text — stash this
    // reason on the session so the close/finish path can use it instead of
    // overwriting it.
    session.terminalDetail =
      message.reason === "hard_budget"
        ? "Sitzung wegen Budgetlimit beendet. Starte bei Bedarf neu."
        : "Sitzung wegen Zeitlimit beendet. Starte bei Bedarf neu.";
    statusDetailElement.textContent = session.terminalDetail;
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
  invalidateNativePhoneActionSession(session);
  if (session.pendingPhoneAction && !session.pendingPhoneAction.decided && hasOpenWebSocket(session)) {
    session.websocket.send(JSON.stringify({
      type: "phone_action_decision",
      request_id: session.pendingPhoneAction.requestId,
      decision: "cancelled",
    }));
  }
  clearPhoneAction(session);
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
  stopSharing();
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
    setConnectionBanner("");
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
    session.terminalDetail ||
      (session.drainRequested
        ? "Sitzung vollständig beendet."
        : "Verbindung beendet. Du kannst neu starten."),
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
  invalidateNativePhoneActionSession(session);
  clearPhoneAction(session);
  if (!canReconnect(session, event)) {
    finalizeWebSocketClose(session, event);
    return;
  }

  session.reconnectAttempts += 1;
  setStatus(
    "connecting",
    `Verbindung unterbrochen – verbindet neu (Versuch ${session.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}) …`,
  );
  setConnectionBanner(
    `Verbindung unterbrochen · neuer Versuch ${session.reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}`,
    "warn",
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
    beginNativePhoneActionSession(session);
    renderSharingControls();
    haptic(session.reconnectAttempts > 0 ? [8, 30, 8] : 10);
    if (session.reconnectAttempts > 0 && !session.drainRequested) {
      session.voiceState = "listening";
      setStatus(
        "listening",
        "Verbindung wiederhergestellt. Sag einfach, was Hermes tun soll.",
      );
      setConnectionBanner("Verbindung wiederhergestellt.", "ok", 3200);
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
      setConnectionBanner("Verbindung unterbrochen – Wiederverbindung läuft …", "warn");
    }
  });
  socket.addEventListener("close", (event) => {
    if (!isCurrent(session) || session.websocket !== socket) {
      return;
    }
    invalidateNativePhoneActionSession(session);
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
    videoMode: null,
    wakeLock: null,
    muteMicUntilResponse: false,
    micGateTimer: null,
    pendingTranscript: { user: null, assistant: null },
    usageWarningSpoken: false,
    terminalDetail: null,
    voiceMode: selectedVoiceMode,
    sparRecording: false,
    micLevel: 0,
    pendingPhoneAction: null,
    phoneActionPreviousFocus: null,
    nativeActionSessionId: null,
  };
  activeSession = session;
  haptic(12);
  transcriptUserScrolledUp = false;
  setButton("stop");
  setStatus("connecting", "Mikrofon und sichere Sprachverbindung werden vorbereitet.");
  setConnectionBanner("Sichere Verbindung wird aufgebaut …");

  try {
    session.audioContext = new AudioContext({ latencyHint: "interactive" });
    await session.audioContext.resume();
    await session.audioContext.audioWorklet.addModule("/voice/worklet.js");
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }
    session.stream = await acquireMicrophoneStream();
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
    session.websocket = createWebSocket(ticket, session.voiceMode);
    attachWebSocketHandlers(session);
    await waitForWebSocket(session);
    if (!isCurrent(session) || session.drainRequested) {
      await cleanupSession(session);
      return;
    }
    session.voiceState = "listening";
    setConnectionBanner("Verbunden · verschlüsselte Sitzung aktiv", "ok", 2600);
    if (session.voiceMode === "spar") {
      // No server "mode" event exists for the cascade (that event is
      // Live-only) — the badge/accent switch to teal right here instead.
      renderModeBadge("spar");
      setStatus("listening", "Halte den Knopf gedrückt und sprich.");
    } else {
      setStatus("listening", "Sag einfach, was Hermes tun soll.");
    }
  } catch (error) {
    if (!isCurrent(session)) {
      return;
    }
    const technical = error && error.name ? ` [${error.name}]` : "";
    setStatus("error", safeErrorMessage(error) + technical);
    // This is a terminal start failure (permission/device/ticket/socket), not
    // a reconnect attempt. Clear the optimistic banner before awaiting
    // cleanup so it can never contradict the visible error state.
    setConnectionBanner("");
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
  invalidateNativePhoneActionSession(session);
  haptic(16);
  setConnectionBanner("");
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

  stopSharing();
  session.abortController.abort();
  await cleanupSession(session);
  if (isCurrent(session)) {
    activeSession = null;
    setButton("start");
    setConnectionBanner("");
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

// Sparmodus push-to-talk: hold to record one turn, release to send it —
// the walkie-talkie contract has no server-side barge-in/VAD endpoint, so
// the turn boundary is entirely this button.
function canStartSparTurn(session) {
  return (
    Boolean(session) &&
    session.voiceMode === "spar" &&
    hasOpenWebSocket(session) &&
    !session.drainRequested &&
    !session.sparRecording
  );
}

function startSparTurn() {
  const session = activeSession;
  if (!canStartSparTurn(session)) {
    return;
  }
  session.sparRecording = true;
  talkButtonElement?.setAttribute("data-recording", "true");
  statusDetailElement.textContent = "Ich höre zu …";
}

function endSparTurn() {
  const session = activeSession;
  if (!session || !session.sparRecording) {
    return;
  }
  session.sparRecording = false;
  talkButtonElement?.setAttribute("data-recording", "false");
  if (hasOpenWebSocket(session)) {
    session.websocket.send(JSON.stringify({ type: "turn_end" }));
    statusDetailElement.textContent = "Hermes denkt nach …";
  }
}

if (talkButtonElement) {
  talkButtonElement.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    startSparTurn();
  });
  talkButtonElement.addEventListener("pointerup", endSparTurn);
  talkButtonElement.addEventListener("pointercancel", endSparTurn);
  talkButtonElement.addEventListener("pointerleave", endSparTurn);
}

function sendTypedTurn(text) {
  if (!text) {
    return false;
  }
  if (
    !activeSession ||
    !hasOpenWebSocket(activeSession) ||
    activeSession.drainRequested ||
    activeSession.muteMicUntilResponse ||
    activeSession.voiceMode === "spar"
  ) {
    statusDetailElement.textContent = NO_SESSION_TEXT_HINT;
    return false;
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
      renderSharingControls();
    }
  }, MIC_GATE_FAILSAFE_MS);
  renderSharingControls();
  return true;
}

function submitComposerText() {
  const text = composerInput.value.trim();
  if (!sendTypedTurn(text)) return;
  composerInput.value = "";
}

composerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitComposerText();
});

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  stashedInstallPrompt = event;
  if (!installCardDismissed && !isStandaloneApp()) {
    installCardElement.hidden = false;
  }
});

function isStandaloneApp() {
  return (
    window.matchMedia?.("(display-mode: standalone)").matches === true ||
    window.navigator?.standalone === true
  );
}

async function handleInstallButtonClick() {
  if (!stashedInstallPrompt) {
    return;
  }
  const promptEvent = stashedInstallPrompt;
  stashedInstallPrompt = null;
  promptEvent.prompt();
  await promptEvent.userChoice;
  installCardElement.hidden = true;
}

installButtonElement.addEventListener("click", () => {
  void handleInstallButtonClick();
});

installDismissElement.addEventListener("click", () => {
  installCardDismissed = true;
  persistInstallCardDismissed();
  installCardElement.hidden = true;
});

window.addEventListener("appinstalled", () => {
  stashedInstallPrompt = null;
  installCardElement.hidden = true;
});

if (isStandaloneApp()) {
  installCardElement.hidden = true;
}

window.addEventListener("pagehide", () => {
  stopSharing();
  if (activeSession) {
    invalidateNativePhoneActionSession(activeSession);
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
  navigator.serviceWorker.register("/voice/sw.js", { scope: "/voice/" }).catch(() => {});
}
