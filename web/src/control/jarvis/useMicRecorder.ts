/**
 * useMicRecorder — schlanker Browser-Port des Desktop-Musters
 * (apps/desktop/src/app/chat/composer/hooks/use-mic-recorder.ts) für die
 * S3.6-Push-to-Talk-Leiste im Jarvis-Chat.
 *
 * Unterschiede zum Desktop-Original (bewusst, Brief S3.6):
 *  - getUserMedia direkt: der Browser fragt die Mic-Permission selbst ab —
 *    den Electron-IPC-Vorgriff `window.hermesDesktop.requestMicrophoneAccess`
 *    gibt es im Browser nicht.
 *  - Pegel-Meter (AudioContext/Analyser) und Silence-Auto-Stop entfallen
 *    (Anti-Scope: kein VAD, kein Silence-Stop — das ist Sprint 4/5).
 *
 * Ablauf: start() → status "recording" → stop() löst mit dem aufgenommenen
 * Blob (oder null bei leerer Aufnahme); cancel() verwirft ohne Ergebnis.
 * Fehler (Permission verweigert, kein Mic, Recorder-Fehler) landen als
 * verständliche deutsche Meldung in `error` — die Komponente zeigt sie als
 * Composer-Fehlerzeile, der Input bleibt unverändert.
 */
import { useEffect, useRef, useState } from "react";

import { de } from "../i18n/de";

const t = de.jarvis;

/** Mime-Fallback (Brief): bevorzugt Opus in WebM, dann nackiges WebM, dann
 *  mp4 (Safari); "" = Browser-Default des MediaRecorder. */
const MIC_MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];

export type MicStatus = "idle" | "recording";

/** blobToDataUrl — 1:1-Port aus apps/desktop (session/hooks/use-prompt-actions/
 *  utils.ts), Fehlertext lokal ersetzt. */
export function blobToDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();

    reader.addEventListener("load", () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
      } else {
        reject(new Error("Audio konnte nicht gelesen werden."));
      }
    });
    reader.addEventListener("error", () =>
      reject(reader.error || new Error("Audio konnte nicht gelesen werden.")),
    );
    reader.readAsDataURL(blob);
  });
}

/** Permission-/Geräte-Fehler auf verständliche deutsche Meldungen mappen
 *  (Vorbild micError() des Desktop-Hooks, reduziert auf die S3.6-Labels). */
function micErrorMessage(error: unknown): string {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return t.micPermissionDenied;
  }
  return t.micError;
}

export function useMicRecorder(): {
  start: () => Promise<void>;
  stop: () => Promise<Blob | null>;
  cancel: () => void;
  status: MicStatus;
  error: string | null;
} {
  const [status, setStatus] = useState<MicStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const stopResolverRef = useRef<((blob: Blob | null) => void) | null>(null);

  const cleanup = () => {
    // Tracks beim Stop sauber schließen, sonst bleibt die Mic-LED an.
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    recorderRef.current = null;
    setStatus("idle");
  };

  // Hängende Aufnahme beim Unmount beenden.
  useEffect(() => () => cleanup(), []);

  const start = async (): Promise<void> => {
    if (recorderRef.current) return;
    setError(null);

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError(t.micError);
      return;
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      setError(micErrorMessage(err));
      return;
    }

    const mimeType =
      typeof MediaRecorder.isTypeSupported === "function"
        ? (MIC_MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported(type)) ?? "")
        : "";

    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    } catch (err) {
      stream.getTracks().forEach((track) => track.stop());
      setError(micErrorMessage(err));
      return;
    }

    chunksRef.current = [];
    streamRef.current = stream;
    recorderRef.current = recorder;

    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) {
        chunksRef.current.push(event.data);
      }
    };

    recorder.onstop = () => {
      const chunks = chunksRef.current;
      const recordingType = recorder.mimeType || mimeType || "audio/webm";
      chunksRef.current = [];
      cleanup();
      const resolver = stopResolverRef.current;
      stopResolverRef.current = null;
      resolver?.(chunks.length ? new Blob(chunks, { type: recordingType }) : null);
    };

    recorder.onerror = () => {
      const resolver = stopResolverRef.current;
      stopResolverRef.current = null;
      cleanup();
      setError(t.micError);
      resolver?.(null);
    };

    recorder.start();
    setStatus("recording");
  };

  const stop = (): Promise<Blob | null> =>
    new Promise<Blob | null>((resolve) => {
      const recorder = recorderRef.current;
      if (!recorder || recorder.state === "inactive") {
        cleanup();
        resolve(null);
        return;
      }
      stopResolverRef.current = resolve;
      recorder.stop();
    });

  const cancel = () => {
    const recorder = recorderRef.current;
    const resolver = stopResolverRef.current;
    stopResolverRef.current = null;
    if (recorder && recorder.state !== "inactive") {
      recorder.ondataavailable = null;
      recorder.onerror = null;
      recorder.onstop = null;
      recorder.stop();
    }
    cleanup();
    resolver?.(null);
  };

  return { start, stop, cancel, status, error };
}
