/**
 * nativeCaptureBridge — Jarvis-side client for the native Android screen-capture
 * bridge (`window.HermesNative`, WebViewCompat.addWebMessageListener).
 *
 * The native Hermes shell (`android/hermes-voice/`) already owns a full
 * MediaProjection capture pipeline: it advertises a `screen_capture` capability,
 * emits ~1 fps downscaled JPEG frames (`screen_frame`) and honest lifecycle
 * messages (`screen_capture_started` / `screen_capture_stopped` /
 * `screen_capture_error`). On Android, browser `getDisplayMedia` is a mirage, so
 * this bridge is the ONLY real screen capture path in the native hull.
 *
 * This module is the small, framework-free counterpart to the voice page's
 * bridge glue in `hermes_cli/voice_client/app.js`: same Bridge Protocol v1 (JSON
 * strings both ways, unknown/invalid input ignored), reused here so Jarvis can
 * drive native capture WITHOUT a second capture implementation. It never logs
 * frame payloads or message contents.
 *
 * Capability is discovered asynchronously: a one-time `bridge_ready` handshake
 * triggers `native_capabilities`. Consumers subscribe to availability changes so
 * the UI can reveal the native path reactively (mirroring the voice client).
 */

/** Bridge Protocol version — must match `BRIDGE_PROTOCOL_VERSION` in Kotlin. */
const BRIDGE_PROTOCOL_VERSION = 1;

/** The origin-scoped native bridge object injected by addWebMessageListener. */
interface NativeBridgeObject {
  postMessage(message: string): void;
  addEventListener?(type: "message", listener: (event: { data?: unknown }) => void): void;
  onmessage?: ((event: { data?: unknown }) => void) | null;
}

/** Callbacks for a single active native capture session. */
export interface NativeCaptureHandlers {
  /** Native confirmed MediaProjection is running (after the system dialog). */
  onStarted: () => void;
  /** A downscaled JPEG frame arrived, base64-encoded (no data: prefix). */
  onFrame: (base64Jpeg: string) => void;
  /** Native ended the capture (system stop, user cancel, lifecycle teardown). */
  onStopped: (reason: string) => void;
  /** Native failed to start/continue the capture. */
  onError: (code: string, message: string) => void;
}

type AvailabilityListener = (available: boolean) => void;

let initialized = false;
let screenCaptureAvailable = false;
const availabilityListeners = new Set<AvailabilityListener>();
// At most one capture consumer at a time — the live Jarvis share.
let captureHandlers: NativeCaptureHandlers | null = null;

function getBridge(): NativeBridgeObject | null {
  if (typeof window === "undefined") return null;
  const bridge = (window as unknown as { HermesNative?: NativeBridgeObject }).HermesNative;
  if (!bridge || typeof bridge.postMessage !== "function") return null;
  return bridge;
}

function sendToNative(message: Record<string, unknown>): boolean {
  const bridge = getBridge();
  if (!bridge) return false;
  try {
    bridge.postMessage(JSON.stringify(message));
    return true;
  } catch {
    // The bridge object can vanish or throw across WebView lifecycle
    // transitions (activity recreation, process death) — never fatal here.
    return false;
  }
}

function setAvailable(available: boolean): void {
  if (screenCaptureAvailable === available) return;
  screenCaptureAvailable = available;
  for (const listener of availabilityListeners) {
    try {
      listener(available);
    } catch {
      /* a listener throwing must not stop the others */
    }
  }
}

// Bridge Protocol v1: JSON strings both ways, never trust the shape. Bad JSON,
// an unknown type, or a mismatched version are ignored silently — the native
// shell may be running a newer/older bridge build than this client.
function handleNativeMessage(raw: string): void {
  let message: Record<string, unknown>;
  try {
    message = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return;
  }
  if (!message || message.v !== BRIDGE_PROTOCOL_VERSION || typeof message.type !== "string") {
    return;
  }

  switch (message.type) {
    case "native_capabilities":
      setAvailable(message.screen_capture === true);
      return;
    case "screen_capture_started":
      captureHandlers?.onStarted();
      return;
    case "screen_frame":
      if (typeof message.data === "string") {
        captureHandlers?.onFrame(message.data);
      }
      return;
    case "screen_capture_stopped":
      captureHandlers?.onStopped(typeof message.reason === "string" ? message.reason : "");
      return;
    case "screen_capture_error":
      captureHandlers?.onError(
        typeof message.code === "string" ? message.code : "",
        typeof message.message === "string" ? message.message : "",
      );
      return;
    default:
      // dictation_draft / phone_action_result etc. belong to the voice page.
      return;
  }
}

/**
 * Attaches the message listener and performs the one-time `bridge_ready`
 * handshake. Idempotent and safe to call on every hook mount; a no-op when the
 * native bridge is absent (desktop / plain browser).
 */
export function initNativeCaptureBridge(): void {
  if (initialized) return;
  initialized = true;
  const bridge = getBridge();
  if (!bridge) return;
  const listener = (event: { data?: unknown }): void => {
    handleNativeMessage(typeof event?.data === "string" ? event.data : "");
  };
  if (typeof bridge.addEventListener === "function") {
    bridge.addEventListener("message", listener);
  } else {
    bridge.onmessage = listener;
  }
  sendToNative({ v: BRIDGE_PROTOCOL_VERSION, type: "bridge_ready" });
}

/** Whether the native shell has confirmed a real `screen_capture` capability. */
export function nativeScreenCaptureAvailable(): boolean {
  return screenCaptureAvailable;
}

/** Subscribe to availability changes; returns an unsubscribe function. */
export function subscribeNativeCaptureAvailability(listener: AvailabilityListener): () => void {
  availabilityListeners.add(listener);
  return () => {
    availabilityListeners.delete(listener);
  };
}

/**
 * Requests a native capture start. The handlers replace any previous consumer
 * (only one live share at a time). The caller MUST guard stale callbacks by its
 * own capture generation — this bridge forwards native events verbatim.
 */
export function startNativeScreenCapture(handlers: NativeCaptureHandlers): void {
  captureHandlers = handlers;
  sendToNative({ v: BRIDGE_PROTOCOL_VERSION, type: "start_screen_capture" });
}

/**
 * Detaches the current capture handlers so no further native frames/events are
 * delivered, and (unless `notifyNative` is false) tells native to stop. Pass
 * `notifyNative: false` when native already reported stopped/errored, to avoid a
 * stop ping-pong with the shell. Idempotent.
 */
export function stopNativeScreenCapture(options?: { notifyNative?: boolean }): void {
  captureHandlers = null;
  if (options?.notifyNative !== false) {
    sendToNative({ v: BRIDGE_PROTOCOL_VERSION, type: "stop_screen_capture" });
  }
}

/** Test-only: reset all singleton state between test cases. */
export function __resetNativeCaptureBridgeForTest(): void {
  initialized = false;
  screenCaptureAvailable = false;
  availabilityListeners.clear();
  captureHandlers = null;
}
