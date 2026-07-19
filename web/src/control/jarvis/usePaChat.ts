/**
 * usePaChat — Chat-Kern der Jarvis-Zone (Sprint 1, Karte e).
 *
 * Kontrakt (hermes_cli/pa_chat.py, LIVE — kein Mock im Chat-Pfad):
 *  - Verlauf: GET /api/pa/messages (Bubble-Quelle der Wahrheit, über den
 *    pollingStore gepollt — dedupliziert, stale-while-error).
 *  - Senden: POST /api/pa/message {text, attachments?} → {turn_id} →
 *    Poll GET /api/pa/turns/{id} bis done|error → danach Verlauf neu laden.
 *  - Upload: POST /api/pa/upload (multipart Feld "file") → {asset_id} →
 *    attachments:[{asset_id}] in der nächsten Message (max 1 Bild/Turn).
 *
 * Fehler werden NIE still geschluckt: Turn-Fehler landen als Error-Bubble
 * mit Fehlertext (das Backend persistiert die Fehler-Reply ebenfalls als
 * Assistant-Message — erkannte Fehler-Inhalte behalten nach dem Reload ihr
 * Error-Styling), POST-/Upload-Fehler als Composer-Fehlerzeile.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type PaChatMessage } from "@/lib/api";
import { extractDetail, usePolling } from "../hooks/internal";
import { getSnapshot } from "../hooks/pollingStore";
import { de } from "../i18n/de";

const t = de.jarvis;

/** Turn-Poll-Kadenz: am pollingStore-Muster des Tabs orientiert (Fragen 5 s),
 *  während eines aktiven Turns deutlich enger für den Chat-Fluss. */
export const PA_TURN_POLL_INTERVAL_MS = 1_500;
/** Backend-Engine-Timeout ist 180 s — danach aufgeben, nie endlos warten. */
export const PA_TURN_MAX_WAIT_MS = 190_000;
/** Verlauf-Frische im Hintergrund (pollingStore, geteilte Infrastruktur). */
export const PA_MESSAGES_POLL_INTERVAL_MS = 10_000;
const PA_MESSAGES_KEY = "pa/messages";

/** Backend-Kontrakt pa_chat.py: Bilder, max 15 MiB. */
export const PA_UPLOAD_MAX_BYTES = 15 * 1024 * 1024;
export const PA_UPLOAD_ACCEPT = "image/png,image/jpeg,image/gif,image/webp,image/bmp";
const PA_UPLOAD_MIMES = new Set(PA_UPLOAD_ACCEPT.split(","));

export interface PaAttachment {
  asset_id: string;
  name: string;
  previewUrl: string;
}

export interface PaActiveTurn {
  text: string;
  attachment: PaAttachment | null;
  phase: "waiting" | "error";
  error: string | null;
}

export interface UsePaChatOptions {
  /** Test-Hebel: kürzere Turn-Poll-Kadenz. */
  turnPollIntervalMs?: number;
  turnMaxWaitMs?: number;
}

export function usePaChat(options: UsePaChatOptions = {}) {
  const turnPollIntervalMs = options.turnPollIntervalMs ?? PA_TURN_POLL_INTERVAL_MS;
  const turnMaxWaitMs = options.turnMaxWaitMs ?? PA_TURN_MAX_WAIT_MS;

  const messagesPoll = usePolling<{ messages: PaChatMessage[] }>(
    PA_MESSAGES_KEY,
    () => api.listPaMessages(),
    PA_MESSAGES_POLL_INTERVAL_MS,
  );

  const [activeTurn, setActiveTurn] = useState<PaActiveTurn | null>(null);
  const [attachment, setAttachment] = useState<PaAttachment | null>(null);
  const [uploading, setUploading] = useState(false);
  const [composerError, setComposerError] = useState<string | null>(null);
  /** Fehler-Reply-Texte, die nach dem Verlauf-Reload ihr Error-Styling
   *  behalten (Backend persistiert Fehler als Assistant-Message). */
  const errorContentsRef = useRef<Set<string>>(new Set());
  /** Monoton: nur der jüngste Sende-Vorgang darf noch pollen/finalisieren. */
  const generationRef = useRef(0);

  // Hängende Turn-Polls beim Unmount beenden.
  useEffect(() => {
    return () => {
      generationRef.current += 1;
    };
  }, []);

  const removeAttachment = useCallback(() => {
    setAttachment((current) => {
      if (current) URL.revokeObjectURL(current.previewUrl);
      return null;
    });
  }, []);

  const attachFile = useCallback(
    async (file: File) => {
      if (!PA_UPLOAD_MIMES.has(file.type)) {
        setComposerError(t.uploadNotImage);
        return;
      }
      if (file.size > PA_UPLOAD_MAX_BYTES) {
        setComposerError(t.uploadTooLarge);
        return;
      }
      setComposerError(null);
      setUploading(true);
      try {
        const result = await api.uploadPaImage(file);
        // Max 1 Bild/Turn (Backend-Kontrakt): ein neuer Anhang ersetzt den alten.
        removeAttachment();
        setAttachment({
          asset_id: result.asset_id,
          name: file.name || "Bild",
          previewUrl: URL.createObjectURL(file),
        });
      } catch (err) {
        setComposerError(`${t.uploadFailed} ${extractDetail(err)}`);
      } finally {
        setUploading(false);
      }
    },
    [removeAttachment],
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || activeTurn?.phase === "waiting") return;
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setComposerError(null);

      const sentAttachment = attachment;
      setAttachment(null); // wandert in die Pending-User-Bubble
      setActiveTurn({ text: trimmed, attachment: sentAttachment, phase: "waiting", error: null });

      const finalize = async (assistantText: string | null, isError: boolean) => {
        if (isError && assistantText) errorContentsRef.current.add(assistantText);
        // Quelle der Wahrheit: Verlauf neu laden (done UND error — das Backend
        // persistiert beide als Assistant-Message). reload() dedupliziert
        // gegen einen gerade laufenden Poll (pollingStore-Kontrakt) — ohne
        // Landed-Check könnte die lokale Pending-Bubble verschwinden, bevor
        // die Server-Bubble da ist (liest sich wie Datenverlust). Deshalb:
        // warten, bis der Verlauf die Assistant-Message DIESES Turns trägt
        // (gebunden: 4 Versuche, danach trägt die nächste Poll-Runde nach).
        let landed = false;
        for (let attempt = 0; attempt < 4 && !landed; attempt++) {
          await messagesPoll.reload().catch(() => {});
          if (generationRef.current !== generation) return;
          const fresh = getSnapshot<{ messages: PaChatMessage[] }>(PA_MESSAGES_KEY)?.data;
          landed =
            assistantText == null ||
            (fresh?.messages.some(
              (m) => m.role === "assistant" && m.content === assistantText,
            ) ?? false);
          if (!landed && attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, turnPollIntervalMs));
          }
        }
        if (generationRef.current !== generation) return;
        if (sentAttachment) URL.revokeObjectURL(sentAttachment.previewUrl);
        if (isError && !landed && assistantText) {
          // Server-Verlauf trägt den Fehler (noch) nicht — lokale Error-
          // Bubble stehen lassen statt still zu verlieren.
          setActiveTurn({ text: trimmed, attachment: null, phase: "error", error: assistantText });
          return;
        }
        setActiveTurn(null);
      };

      let turnId: string;
      try {
        const created = await api.sendPaMessage(
          trimmed,
          sentAttachment ? [{ asset_id: sentAttachment.asset_id }] : undefined,
        );
        turnId = created.turn_id;
      } catch (err) {
        setActiveTurn(null);
        if (sentAttachment) URL.revokeObjectURL(sentAttachment.previewUrl);
        setComposerError(`${t.sendFailed} ${extractDetail(err)}`);
        return;
      }

      const deadline = Date.now() + turnMaxWaitMs;
      for (;;) {
        if (generationRef.current !== generation) return; // ersetzt/unmounted
        await new Promise((resolve) => setTimeout(resolve, turnPollIntervalMs));
        if (generationRef.current !== generation) return;
        try {
          const turn = await api.getPaTurn(turnId);
          if (generationRef.current !== generation) return;
          if (turn.status === "done") {
            await finalize(turn.reply, false);
            return;
          }
          if (turn.status === "error") {
            await finalize(turn.error ?? turn.reply ?? "Unbekannter Fehler", true);
            return;
          }
        } catch {
          // Turn-Poll-Fehler (Netzflacke): weiterpollen bis zum Deadline —
          // der Turn läuft serverseitig weiter; erst das Zeitlimit wird
          // zur sichtbaren Fehler-Bubble.
        }
        if (Date.now() >= deadline) {
          await finalize(t.turnTimeout, true);
          return;
        }
      }
    },
    [activeTurn?.phase, attachment, messagesPoll, turnPollIntervalMs, turnMaxWaitMs],
  );

  const isErrorContent = useCallback(
    (content: string) => errorContentsRef.current.has(content),
    [],
  );

  return {
    messages: messagesPoll.data?.messages ?? null,
    messagesLoading: messagesPoll.loading && messagesPoll.data === null,
    messagesError: messagesPoll.error,
    activeTurn,
    sending: activeTurn?.phase === "waiting",
    attachment,
    uploading,
    composerError,
    clearComposerError: () => setComposerError(null),
    attachFile,
    removeAttachment,
    send,
    isErrorContent,
  };
}
