/**
 * usePaChat — Chat-Kern der Jarvis-Zone (Sprint 1 Karte e, Sprint 2:
 * S2.2 Engine-Wahl, M1/M2-FE Härtung, S2.5 project_scope-Verdrahtung).
 *
 * Kontrakt (hermes_cli/pa_chat.py, LIVE — kein Mock im Chat-Pfad):
 *  - Verlauf: GET /api/pa/messages (Bubble-Quelle der Wahrheit, über den
 *    pollingStore gepollt — dedupliziert, stale-while-error). Seitenweise
 *    rückwärts über den before_id-Cursor: „Ältere laden" holt die nächste
 *    Seite und hängt sie VORNE an; next_before_id=null = Ende. Der Poll
 *    ersetzt nur die jüngste Seite — geladene Alt-Seiten bleiben bestehen.
 *  - Senden: POST /api/pa/message {text, attachments?, engine?, model?,
 *    project_scope?} → {turn_id} → Poll GET /api/pa/turns/{id} bis done|error
 *    → danach Verlauf neu laden. engine+model kommen aus dem S2.2-Switcher-
 *    Store (Wahl gilt für den nächsten Turn); project_scope ist verdrahtet,
 *    die Shell öffnet derzeit kein Projekt → Feld entfällt (S2.5).
 *  - Upload: POST /api/pa/upload (multipart Feld "file") → {asset_id} →
 *    attachments:[{asset_id}] in der nächsten Message (max 1 Bild/Turn).
 *    Engines mit supports_images=false (Roster) lehnen Bilder ab — der
 *    Composer blockt sie clientseitig, statt den 400 erst beim Senden
 *    auszulösen.
 *
 * Fehler werden NIE still geschluckt: Turn-Fehler landen als Error-Bubble
 * mit Fehlertext; der Verlauf markiert fehlgeschlagene Turns serverseitig
 * über status==="error" (M2 — keine Inhalts-Heuristik mehr). POST-/Upload-
 * Fehler erscheinen als Composer-Fehlerzeile.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, type PaChatMessage, type PaMessagesPage } from "@/lib/api";
import { extractDetail, usePolling } from "../hooks/internal";
import { getSnapshot } from "../hooks/pollingStore";
import { de } from "../i18n/de";
import { findEngineSpec, getEngineChoice, getPaEnginesSnapshot } from "./engineSelection";

const t = de.jarvis;

/** Turn-Poll-Kadenz: am pollingStore-Muster des Tabs orientiert (Fragen 5 s),
 *  während eines aktiven Turns deutlich enger für den Chat-Fluss. */
export const PA_TURN_POLL_INTERVAL_MS = 1_500;
/** Backend-Engine-Timeout ist 180 s — danach aufgeben, nie endlos warten. */
export const PA_TURN_MAX_WAIT_MS = 190_000;
/** Verlauf-Frische im Hintergrund (pollingStore, geteilte Infrastruktur). */
export const PA_MESSAGES_POLL_INTERVAL_MS = 10_000;
const PA_MESSAGES_KEY = "pa/messages";
/** Seitengröße des Verlaufs (Backend-Default 30, capped 100). */
export const PA_MESSAGES_PAGE_SIZE = 30;

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
  /** S2.5: View-State (offenes Projekt) reitet im Message-POST mit. Die
   *  Jarvis-Shell hat aktuell kein In-Shell-Projekt-Drilldown — der Hook
   *  verdrahtet das Feld, die Shell lässt es bewusst leer. */
  projectScope?: string;
}

/** Ältere Seiten vorne anhängen, Duplikate (Überlapp durch nachgeladene
 *  jüngste Seite) über die stabile Server-ID entfernen. */
function mergeMessages(older: PaChatMessage[], latest: PaChatMessage[]): PaChatMessage[] {
  const seen = new Set<number>();
  const merged: PaChatMessage[] = [];
  for (const message of [...older, ...latest]) {
    if (seen.has(message.id)) continue;
    seen.add(message.id);
    merged.push(message);
  }
  return merged;
}

export function usePaChat(options: UsePaChatOptions = {}) {
  const turnPollIntervalMs = options.turnPollIntervalMs ?? PA_TURN_POLL_INTERVAL_MS;
  const turnMaxWaitMs = options.turnMaxWaitMs ?? PA_TURN_MAX_WAIT_MS;
  const projectScope = options.projectScope;

  const messagesPoll = usePolling<PaMessagesPage>(
    PA_MESSAGES_KEY,
    () => api.listPaMessages(PA_MESSAGES_PAGE_SIZE),
    PA_MESSAGES_POLL_INTERVAL_MS,
  );

  const [activeTurn, setActiveTurn] = useState<PaActiveTurn | null>(null);
  const [attachment, setAttachment] = useState<PaAttachment | null>(null);
  const [uploading, setUploading] = useState(false);
  const [composerError, setComposerError] = useState<string | null>(null);
  /** Manuell nachgeladene ältere Seiten (vorne angehängt); der Cursor zeigt
   *  auf die jeweils älteste geladene Seite (null = keine mehr, undefined =
   *  noch keine Alt-Seite geholt → Cursor der jüngsten Seite verwenden). */
  const [olderMessages, setOlderMessages] = useState<PaChatMessage[]>([]);
  const [olderCursor, setOlderCursor] = useState<number | null | undefined>(undefined);
  const [loadingOlder, setLoadingOlder] = useState(false);
  /** Wird gesetzt, BEVOR Alt-Seiten ankommen: der Auto-Anschluss ans
   *  Verlaufsende darf bei einem Prepend nicht ans Ende springen. */
  const prependingRef = useRef(false);
  /** Monoton: nur der jüngste Sende-Vorgang darf noch pollen/finalisieren. */
  const generationRef = useRef(0);

  // Hängende Turn-Polls beim Unmount beenden.
  useEffect(() => {
    return () => {
      generationRef.current += 1;
    };
  }, []);

  const latestMessages = useMemo(() => messagesPoll.data?.messages ?? null, [messagesPoll.data]);
  const messages = useMemo(
    () =>
      latestMessages === null
        ? olderMessages.length > 0
          ? olderMessages
          : null
        : mergeMessages(olderMessages, latestMessages),
    [olderMessages, latestMessages],
  );
  const nextBeforeId = olderCursor !== undefined ? olderCursor : (messagesPoll.data?.next_before_id ?? null);

  const loadOlder = useCallback(async () => {
    if (loadingOlder || nextBeforeId === null) return;
    prependingRef.current = true;
    setLoadingOlder(true);
    try {
      const page = await api.listPaMessages(PA_MESSAGES_PAGE_SIZE, nextBeforeId);
      setOlderMessages((current) => mergeMessages(page.messages, current));
      setOlderCursor(page.next_before_id);
    } catch (err) {
      // Fehler beim Nachladen: sichtbar als Composer-Zeile, nie still — und
      // der Prepend-Schutz wird zurückgenommen (es kam ja nichts an).
      prependingRef.current = false;
      setComposerError(`${t.loadOlderFailed} ${extractDetail(err)}`);
    } finally {
      setLoadingOlder(false);
    }
  }, [loadingOlder, nextBeforeId]);

  /** Der Chat konsumiert den Auto-Anschluss-Schutz beim Rendern. */
  const consumePrepending = useCallback(() => {
    const was = prependingRef.current;
    prependingRef.current = false;
    return was;
  }, []);

  const removeAttachment = useCallback(() => {
    setAttachment((current) => {
      if (current) URL.revokeObjectURL(current.previewUrl);
      return null;
    });
  }, []);

  /** Engine-Fähigkeiten für den nächsten Turn (Roster-Snapshot, kein eigener
   *  Fetch): die Wahl des Switchers + Roster-Default entscheiden, ob Bilder
   *  erlaubt sind. Roster unbekannt → erlauben (Backend-Default sol kann es). */
  const imagesAllowed = useCallback((): boolean => {
    const choice = getEngineChoice();
    const roster = getPaEnginesSnapshot();
    if (!roster) return true;
    const engine = choice?.engine ?? roster.default_engine;
    return findEngineSpec(roster, engine)?.supports_images ?? true;
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
      if (!imagesAllowed()) {
        setComposerError(t.engineNoImages);
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
    [imagesAllowed, removeAttachment],
  );

  const send = useCallback(
    async (text: string, sendOptions: { attachmentAssetId?: string } = {}) => {
      const trimmed = text.trim();
      if (!trimmed || activeTurn?.phase === "waiting") return;
      // S2.2: Switcher-Wahl gilt für genau diesen (nächsten) Turn.
      const choice = getEngineChoice();
      // Live-Screen-Share (S-live): der Composer reicht die materialisierte
      // asset_id des aktuellen Bildschirm-Frames herein. previewUrl ist die
      // authentifizierte Asset-URL (kein blob:), revoke bleibt ein No-op.
      const liveAttachment: PaAttachment | null = sendOptions.attachmentAssetId
        ? {
            asset_id: sendOptions.attachmentAssetId,
            name: t.liveShareActive,
            previewUrl: api.paAssetUrl(sendOptions.attachmentAssetId),
          }
        : null;
      const sentAttachment = attachment ?? liveAttachment;
      if (sentAttachment && !imagesAllowed()) {
        // Anhang entstand vor einem Engine-Wechsel auf eine Nicht-Vision-
        // Engine: clientseitig erklären statt den 400 des Backends.
        setComposerError(t.engineNoImages);
        return;
      }
      const generation = generationRef.current + 1;
      generationRef.current = generation;
      setComposerError(null);

      setAttachment(null); // wandert in die Pending-User-Bubble
      setActiveTurn({ text: trimmed, attachment: sentAttachment, phase: "waiting", error: null });

      // S4-Härtung: die Blob-URL des Anhangs wird in JEDEM Finalize-/Abbruch-
      // Pfad revoked — auch mitten im Turn (ersetzt/unmounted), sonst leakt
      // sie bis zum Tab-Close.
      const releaseAttachment = () => {
        if (sentAttachment) URL.revokeObjectURL(sentAttachment.previewUrl);
      };

      const finalize = async (assistantText: string | null, isError: boolean) => {
        // Quelle der Wahrheit: Verlauf neu laden (done UND error — das Backend
        // persistiert beide als Assistant-Message, die Fehler-Reply trägt
        // status==="error"). reload() dedupliziert gegen einen gerade
        // laufenden Poll (pollingStore-Kontrakt) — ohne Landed-Check könnte
        // die lokale Pending-Bubble verschwinden, bevor die Server-Bubble da
        // ist (liest sich wie Datenverlust). Deshalb: warten, bis der Verlauf
        // die Assistant-Message DIESES Turns trägt (gebunden: 4 Versuche,
        // danach trägt die nächste Poll-Runde nach).
        let landed = false;
        for (let attempt = 0; attempt < 4 && !landed; attempt++) {
          await messagesPoll.reload().catch(() => {});
          if (generationRef.current !== generation) {
            releaseAttachment();
            return;
          }
          const fresh = getSnapshot<PaMessagesPage>(PA_MESSAGES_KEY)?.data;
          landed =
            assistantText == null ||
            (fresh?.messages.some(
              (m) => m.role === "assistant" && m.content === assistantText,
            ) ?? false);
          if (!landed && attempt < 3) {
            await new Promise((resolve) => setTimeout(resolve, turnPollIntervalMs));
          }
        }
        if (generationRef.current !== generation) {
          releaseAttachment();
          return;
        }
        releaseAttachment();
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
        const turnOptions = {
          ...(choice ? { engine: choice.engine, model: choice.model } : {}),
          ...(projectScope ? { projectScope } : {}),
        };
        const created = await api.sendPaMessage(
          trimmed,
          sentAttachment ? [{ asset_id: sentAttachment.asset_id }] : undefined,
          Object.keys(turnOptions).length > 0 ? turnOptions : undefined,
        );
        turnId = created.turn_id;
      } catch (err) {
        setActiveTurn(null);
        releaseAttachment();
        setComposerError(`${t.sendFailed} ${extractDetail(err)}`);
        return;
      }

      const deadline = Date.now() + turnMaxWaitMs;
      for (;;) {
        if (generationRef.current !== generation) {
          releaseAttachment();
          return; // ersetzt/unmounted
        }
        await new Promise((resolve) => setTimeout(resolve, turnPollIntervalMs));
        if (generationRef.current !== generation) {
          releaseAttachment();
          return;
        }
        try {
          const turn = await api.getPaTurn(turnId);
          if (generationRef.current !== generation) {
            releaseAttachment();
            return;
          }
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
    [activeTurn?.phase, attachment, imagesAllowed, messagesPoll, projectScope, turnPollIntervalMs, turnMaxWaitMs],
  );

  /** Vollständiger lokaler Reset (G2b-Controller-Naht): Alt-Seiten + Cursor,
   *  Pending-/Fehlerzustände, Scroll-Prepend-Schutz; in-flight Turn-Polls
   *  über den Generation-Counter invalidieren; danach frisch vom Server. */
  const resetState = useCallback(() => {
    generationRef.current += 1;
    prependingRef.current = false;
    setOlderMessages([]);
    setOlderCursor(undefined);
    setLoadingOlder(false);
    setActiveTurn(null);
    setComposerError(null);
    setUploading(false);
    setAttachment((current) => {
      if (current) URL.revokeObjectURL(current.previewUrl);
      return null;
    });
    void messagesPoll.reload().catch(() => {});
  }, [messagesPoll]);

  return {
    messages,
    messagesLoading: messagesPoll.loading && messagesPoll.data === null,
    messagesError: messagesPoll.error,
    nextBeforeId,
    loadingOlder,
    loadOlder,
    consumePrepending,
    activeTurn,
    sending: activeTurn?.phase === "waiting",
    attachment,
    uploading,
    composerError,
    attachFile,
    removeAttachment,
    send,
    resetState,
  };
}
