/**
 * useLiveShare — REAL continuous live screen sharing for the Jarvis composer.
 *
 * This is the honest replacement for the previous "screenshare" button, which
 * only grabbed a single frame (and on mobile silently opened the image picker).
 * A live share is a genuine, ongoing getDisplayMedia session — never the image
 * picker, never a success-simulation:
 *
 *  - start() (from a user click, so browser user-activation survives) opens the
 *    getDisplayMedia stream and a backend session, shows a visible active state,
 *    and begins sampling the shared screen.
 *  - While active, the screen is sampled at ≤1 fps, downscaled to ≤1280px long
 *    edge, and the LATEST frame is streamed to the server with backpressure:
 *    exactly one upload in flight; a frame produced during an upload replaces
 *    any older pending frame ("latest wins", older frames dropped).
 *  - attachCurrentFrame() materialises the server's current frame into one
 *    normal upload asset so the existing image-turn pipeline lets Jarvis see the
 *    live screen — without persisting a growing pile of assets.
 *  - stop(), the browser's own "stop sharing" (track.onended) and unmount all
 *    tear the session down: tracks stopped, sampling cleared, backend notified.
 *
 * Where the browser cannot really capture a screen (Android Chrome / Samsung
 * Internet / iOS Safari — getDisplayMedia is absent or a mirage), supported is
 * false: the UI must declare it honestly, not fall back to an image picker.
 *
 * NATIVE PATH — inside the native Android hull (`android/hermes-voice/`), the
 * shell exposes a real MediaProjection capture bridge (`window.HermesNative`).
 * When that capability is present it is PREFERRED over getDisplayMedia: start()
 * asks native to capture (the only user-visible surface is the mandatory Android
 * system dialog), and native `screen_frame` JPEGs are decoded and streamed into
 * the SAME backend live-share session through the SAME backpressure/latest-wins
 * uploader — no second capture implementation, no second backend pipeline. A
 * capture generation guards against stale native frames after any stop.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { api } from "@/lib/api";
import {
  initNativeCaptureBridge,
  nativeScreenCaptureAvailable,
  startNativeScreenCapture,
  stopNativeScreenCapture,
  subscribeNativeCaptureAvailability,
} from "./nativeCaptureBridge";

/** Sampling cadence — 1 fps is plenty for an LLM "look at my screen" and keeps
 *  upload + CPU cost low (research handoff: start at ≤1 fps). */
export const LIVE_SHARE_SAMPLE_INTERVAL_MS = 1_000;
/** Downscale target so a 4K screen never streams multi-MiB frames. */
export const LIVE_SHARE_MAX_EDGE_PX = 1280;
const LIVE_SHARE_JPEG_QUALITY = 0.7;

export type LiveShareStatus = "idle" | "starting" | "sharing";

/** getDisplayMedia is exposed AND this is not a browser where it is known to be
 *  a mirage. Feature presence is the primary gate (never UA-only); the mobile
 *  guard catches Android Chrome, which historically exposed the symbol but
 *  always rejected the call (research handoff, MDN BCD). */
export function screenShareSupported(): boolean {
  if (typeof navigator === "undefined") return false;
  const md = navigator.mediaDevices as MediaDevices | undefined;
  if (typeof md?.getDisplayMedia !== "function") return false;
  return !/\b(Android|iPhone|iPad|iPod|Mobile)\b/i.test(navigator.userAgent ?? "");
}

function isCancel(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: string }).name === "NotAllowedError"
  );
}

/** Decode a base64 JPEG (no data: prefix, as the native bridge sends) into a
 *  Blob the existing uploader can post to /frame. Returns null on bad input. */
function base64ToJpegBlob(base64: string): Blob | null {
  try {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: "image/jpeg" });
  } catch {
    return null;
  }
}

export interface UseLiveShareResult {
  supported: boolean;
  status: LiveShareStatus;
  active: boolean;
  error: string | null;
  clearError: () => void;
  /** Must be invoked directly from a user gesture. */
  start: () => Promise<void>;
  stop: () => void;
  /** asset_id of the current live frame, or null if unavailable. */
  attachCurrentFrame: () => Promise<string | null>;
}

export interface UseLiveShareOptions {
  /** German error copy, injected by the composer (i18n). */
  errorText: string;
}

export function useLiveShare({ errorText }: UseLiveShareOptions): UseLiveShareResult {
  const [status, setStatus] = useState<LiveShareStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const displaySupported = screenShareSupported();
  // Native capability is discovered asynchronously (bridge_ready → capabilities).
  // The bridge is an external store; subscribe with the tear-safe primitive so the
  // button reveals the native path the moment capability is known, with no
  // setState-in-effect. Server snapshot is false (no native bridge off-device).
  const nativeAvailable = useSyncExternalStore(
    subscribeNativeCaptureAvailability,
    nativeScreenCaptureAvailable,
    () => false,
  );
  const supported = displaySupported || nativeAvailable;

  const streamRef = useRef<MediaStream | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);
  // Backpressure: at most one frame upload in flight; a frame produced while an
  // upload is running overwrites any older pending frame (latest wins).
  const uploadingRef = useRef(false);
  const pendingRef = useRef<Blob | null>(null);
  // Native capture: bumped on every start/teardown so a native frame or lifecycle
  // callback from a superseded capture is ignored (no upload after a visible stop).
  const captureGenRef = useRef(0);
  // True while a native capture is engaged, so teardown knows to tell native to
  // stop. Distinguishes the native path from the getDisplayMedia path.
  const nativeCaptureRef = useRef(false);

  const teardown = useCallback((notifyNative = true) => {
    // Invalidate any in-flight native capture: a late started/frame callback must
    // not resurrect the share (its generation check below now fails).
    captureGenRef.current += 1;
    if (nativeCaptureRef.current) {
      nativeCaptureRef.current = false;
      stopNativeScreenCapture({ notifyNative });
    }
    if (timerRef.current !== null) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    const stream = streamRef.current;
    streamRef.current = null;
    stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    const video = videoRef.current;
    if (video) {
      try {
        video.pause();
      } catch {
        /* jsdom / detached video */
      }
      video.srcObject = null;
    }
    videoRef.current = null;
    pendingRef.current = null;
    const sid = sessionIdRef.current;
    sessionIdRef.current = null;
    if (sid) void api.stopLiveShare(sid).catch(() => {});
  }, []);

  const stop = useCallback(() => {
    teardown();
    if (mountedRef.current) setStatus("idle");
  }, [teardown]);

  const drainUpload = useCallback(async (first: Blob) => {
    if (uploadingRef.current) {
      pendingRef.current = first; // latest wins — drop the older pending frame
      return;
    }
    uploadingRef.current = true;
    let current: Blob | null = first;
    try {
      while (current && sessionIdRef.current && mountedRef.current) {
        try {
          await api.uploadLiveShareFrame(sessionIdRef.current, current);
        } catch {
          // A dropped frame must not kill the session — keep sharing.
        }
        current = pendingRef.current;
        pendingRef.current = null;
      }
    } finally {
      uploadingRef.current = false;
    }
  }, []);

  const sampleTick = useCallback(() => {
    const video = videoRef.current;
    if (!video || !video.videoWidth || !video.videoHeight) return;
    // A fresh canvas per sample (1/s) keeps the encode self-contained and avoids
    // mutating a ref-cached element.
    const canvas = document.createElement("canvas");
    const scale = Math.min(
      1,
      LIVE_SHARE_MAX_EDGE_PX / Math.max(video.videoWidth, video.videoHeight),
    );
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(
      (blob) => {
        if (blob) void drainUpload(blob);
      },
      "image/jpeg",
      LIVE_SHARE_JPEG_QUALITY,
    );
  }, [drainUpload]);

  // Native capture path: no browser prompt, no <video>, no sampling timer —
  // native owns the pixels and pushes ready JPEGs. We only open the backend
  // session AFTER native confirms capture (screen_capture_started), so a
  // cancelled Android permission dialog never leaks a server session.
  const startNative = useCallback(() => {
    const gen = (captureGenRef.current += 1);
    nativeCaptureRef.current = true;
    startNativeScreenCapture({
      onStarted: () => {
        void (async () => {
          if (gen !== captureGenRef.current || !mountedRef.current) {
            // Superseded / cancelled before native confirmed — kill the orphan.
            stopNativeScreenCapture();
            return;
          }
          try {
            const session = await api.startLiveShare();
            if (gen !== captureGenRef.current || !mountedRef.current) {
              // The share ended while startLiveShare() was in flight — close the
              // orphaned server session best-effort (mirrors the display path).
              void api.stopLiveShare(session.session_id).catch(() => {});
              return;
            }
            sessionIdRef.current = session.session_id;
            setStatus("sharing");
          } catch {
            // No backend session → do not pretend we are sharing; stop native.
            teardown();
            if (mountedRef.current) {
              setStatus("idle");
              setError(errorText);
            }
          }
        })();
      },
      onFrame: (base64) => {
        // Discard stale frames (superseded generation) and any frame arriving
        // before the session exists or after teardown — nothing uploads post-stop.
        if (gen !== captureGenRef.current || !sessionIdRef.current || !mountedRef.current) {
          return;
        }
        const blob = base64ToJpegBlob(base64);
        if (blob) void drainUpload(blob);
      },
      onStopped: () => {
        // Native already stopped (system stop / user cancel) — tear down without
        // echoing stop_screen_capture back (avoids a ping-pong). Silent, no error.
        if (gen !== captureGenRef.current) return;
        teardown(false);
        if (mountedRef.current) setStatus("idle");
      },
      onError: () => {
        if (gen !== captureGenRef.current) return;
        teardown(false);
        if (mountedRef.current) {
          setStatus("idle");
          setError(errorText);
        }
      },
    });
  }, [drainUpload, teardown, errorText]);

  const start = useCallback(async () => {
    if (!supported || sessionIdRef.current || status !== "idle") return;
    setError(null);
    setStatus("starting");
    if (nativeAvailable) {
      startNative();
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: true,
        audio: false,
      });
    } catch (err) {
      if (mountedRef.current) setStatus("idle");
      // User cancelled the OS picker → silent, no error banner.
      if (!isCancel(err) && mountedRef.current) setError(errorText);
      return;
    }
    if (!mountedRef.current) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    streamRef.current = stream;
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.srcObject = stream;
    videoRef.current = video;
    try {
      await video.play();
    } catch {
      /* autoplay of a muted stream may still reject in jsdom — sampling copes */
    }
    // The user (or the OS) can end the share from the browser chrome; mirror it.
    stream.getVideoTracks().forEach((track) => {
      track.onended = () => stop();
    });
    try {
      const session = await api.startLiveShare();
      if (!mountedRef.current || streamRef.current !== stream) {
        // The share ended (track.onended / stop / unmount) while startLiveShare()
        // was still in flight: teardown() already ran but could not know this
        // session id yet, so the server session would leak. Close it best-effort.
        void api.stopLiveShare(session.session_id).catch(() => {});
        return;
      }
      sessionIdRef.current = session.session_id;
    } catch {
      // No backend session → do not pretend we are sharing.
      teardown();
      if (mountedRef.current) {
        setStatus("idle");
        setError(errorText);
      }
      return;
    }
    timerRef.current = setInterval(sampleTick, LIVE_SHARE_SAMPLE_INTERVAL_MS);
    sampleTick(); // first frame immediately, don't wait a full interval
    if (mountedRef.current) setStatus("sharing");
  }, [supported, nativeAvailable, startNative, status, errorText, sampleTick, stop, teardown]);

  const attachCurrentFrame = useCallback(async (): Promise<string | null> => {
    const sid = sessionIdRef.current;
    if (!sid) return null;
    try {
      const result = await api.attachLiveShareFrame(sid);
      return result.asset_id;
    } catch {
      return null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    // Attach the native bridge + perform the bridge_ready handshake (no-op
    // off-device). Capability updates flow through useSyncExternalStore above.
    initNativeCaptureBridge();
    return () => {
      mountedRef.current = false;
      teardown();
    };
  }, [teardown]);

  return {
    supported,
    status,
    active: status === "sharing",
    error,
    clearError: () => setError(null),
    start,
    stop,
    attachCurrentFrame,
  };
}
