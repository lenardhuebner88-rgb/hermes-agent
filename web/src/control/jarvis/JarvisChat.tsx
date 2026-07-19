/**
 * JarvisChat — Bubble-Verlauf + Frag-Leiste der Jarvis-Zone.
 *
 * Verlauf aus GET /api/pa/messages (roles user/assistant, Provenienz-Badge
 * `model` je Assistant-Bubble; bei claude-Modellen dezenter „MAX"-Marker —
 * Fork 19: Hinweis, kein Cap). M1/M2-FE: Bilder der History rendern als
 * Thumbnails über die authentifizierte Asset-URL (404 = gepruntes Asset →
 * Broken-Attachment-Chip, der Thread bleibt); Error-Bubbles kommen aus
 * status==="error" des Turns, nicht mehr aus einer Inhalts-Heuristik;
 * „Ältere laden" blättert über den before_id-Cursor rückwärts. Senden über
 * die A4-Frag-Leiste (unten fixiert): pending-Bubble mit Denk-Zustand
 * während des Turn-Polls, Error-Bubble mit Fehlertext bei Fehlern — nie ein
 * stiller Fehler. Bild-Paste/Attach → POST /api/pa/upload → attachments im
 * Message-POST (max 1 Bild/Turn); bei Engines mit supports_images=false
 * (S2.2-Roster) ist der Attach-Button deaktiviert (Tooltip) statt erst beim
 * Senden in den 400 zu laufen. S3.3-FE: „/plan <idee>" in der Frag-Leiste
 * startet den PlanSpec-Draft-Flow (usePlanspecDraft) statt eines Chat-Turns
 * — die validierte Draft-Card (PlanspecCard) ist eine client-interne Bubble
 * im Thread; „Als Approval einreichen" stellt sie als planspec.ingest-Card
 * in die S2.4-Inbox.
 */
import { useEffect, useRef, useState, type ClipboardEvent, type FormEvent } from "react";
import { ImagePlus, Send, X } from "lucide-react";

import { api, type PaChatMessage } from "@/lib/api";
import { de } from "../i18n/de";
import {
  effectiveEngine,
  findEngineSpec,
  useEngineChoice,
  usePaEngines,
} from "./engineSelection";
import { JARVIS_ASK_HINT } from "./mockContent";
import { PlanspecCard } from "./PlanspecCard";
import { PA_UPLOAD_ACCEPT, usePaChat } from "./usePaChat";
import { PLAN_PREFIX_RE, usePlanspecDraft } from "./usePlanspecDraft";

const t = de.jarvis;

function formatBubbleTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Thumbnail eines History-Attachments über die authentifizierte Asset-URL.
 *  404 (gepruntes Upload) → Broken-Chip statt kaputtem Bild — die Bubble
 *  und der Thread bleiben (Brief: nicht den Thread verwerfen). */
function AttachmentThumb({ assetId }: { assetId: string }) {
  const [broken, setBroken] = useState(false);
  if (broken) {
    return <span className="jv-attbroken">{t.attachmentGone}</span>;
  }
  return (
    <span className="jv-attref">
      <img
        src={api.paAssetUrl(assetId)}
        alt=""
        loading="lazy"
        onError={() => setBroken(true)}
      />
    </span>
  );
}

function MessageBubble({
  message,
  claudeModels,
}: {
  message: PaChatMessage;
  /** Modelle der claude-Engine aus dem Roster (MAX-Marker); null = Roster
   *  noch nicht da → kein Marker (dezent, kein Crash). */
  claudeModels: ReadonlyArray<string> | null;
}) {
  const attachments = message.attachments ?? [];
  if (message.role === "user") {
    return (
      <div className="jv-bubble jv-bubble-user">
        {message.content}
        {attachments.map((att) => (
          <AttachmentThumb key={att.asset_id} assetId={att.asset_id} />
        ))}
      </div>
    );
  }
  const error = message.status === "error";
  const maxMarker = claudeModels !== null && claudeModels.includes(message.model);
  return (
    <div className={error ? "jv-bubble jv-bubble-error" : "jv-bubble jv-bubble-assistant"}>
      {error ? <span className="jv-errlabel">{t.errorLabel}</span> : null}
      {message.content || message.error}
      {attachments.map((att) => (
        <AttachmentThumb key={att.asset_id} assetId={att.asset_id} />
      ))}
      {/* Provenienz-Badge: Modell dezent; „MAX" = Max-Abo-Hinweis (Fork 19). */}
      <span className="jv-badge">
        {message.model}
        {maxMarker ? (
          <span className="jv-max" title={t.maxMarkerTitle}>
            {" "}
            · {t.maxMarker}
          </span>
        ) : null}{" "}
        · {formatBubbleTime(message.ts)}
      </span>
    </div>
  );
}

export function JarvisChat({ turnPollIntervalMs }: { turnPollIntervalMs?: number } = {}) {
  // turnPollIntervalMs ist eine Di/Test-Naht (kürzere Turn-Poll-Kadenz in
  // Komponententests); Produktiv Default: PA_TURN_POLL_INTERVAL_MS.
  const chat = usePaChat({ turnPollIntervalMs });
  const planspec = usePlanspecDraft();
  const roster = usePaEngines();
  const choice = useEngineChoice();
  const [text, setText] = useState("");
  const threadRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const seenCountRef = useRef(0);

  // Bild-Fähigkeit der Engine für den NÄCHSTEN Turn (Switcher-Wahl +
  // Roster-Default): Nicht-Vision-Engines deaktivieren den Attach-Button
  // mit Tooltip statt erst beim Senden in den Backend-400 zu laufen.
  const engine = effectiveEngine(choice, roster.data);
  const imagesOk = findEngineSpec(roster.data, engine)?.supports_images ?? true;
  const claudeModels =
    roster.data?.engines.find((spec) => spec.engine === "claude")?.models ?? null;

  // Auto-Anschluss ans Verlaufsende: nur bei NEUEN Inhalten (neue Bubble,
  // Draft-Card oder Turn-Zustandswechsel), nie beim bloßen Hintergrund-
  // Refresh der History und nie bei einem Prepend älterer Seiten („Ältere
  // laden"). Draft-Cards zählen beim Phasenwechsel drafting→ready/error als
  // neue Aktivität (sonst bliebe die aufgelöste Card mobil bei leerem
  // Verlauf unter dem Fold, ohne dass ein Effekt erneut läuft).
  const messageCount = chat.messages?.length ?? 0;
  const planCount = planspec.cards.length;
  const settledPlanCount = planspec.cards.filter((c) => c.phase !== "drafting").length;
  const turnPhase = chat.activeTurn?.phase ?? null;
  const consumePrepending = chat.consumePrepending;
  const didInitRef = useRef(false);
  const seenSettledRef = useRef(0);
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    const prepended = consumePrepending();
    const contentCount = messageCount + planCount;
    const grew = contentCount > seenCountRef.current;
    seenCountRef.current = contentCount;
    const settled = settledPlanCount !== seenSettledRef.current;
    seenSettledRef.current = settledPlanCount;
    if (prepended) return; // „Ältere laden": Scrollposition behalten
    // matchMedia fehlt in jsdom — Default ist die Desktop-Variante (Thread-Scroll).
    const mobile =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(max-width: 759px)").matches;
    if (!mobile) {
      // Desktop: der Thread ist eine eigene Scroll-Region — neueste Bubble
      // immer sichtbar, ohne die Seite zu bewegen.
      el.scrollTop = el.scrollHeight;
      didInitRef.current = true;
      return;
    }
    // Mobil liegt der Verlauf im Seitenfluss: beim INITIALEN Verlauf nie die
    // Seite zum Chat ziehen (die Shell soll zuerst wirken) — nur bei neuen
    // Inhalten NACH der ersten Anzeige (z. B. eigene Frage + Antwort, oder
    // eine Draft-Card, die gerade ihren Validate-Status bekommen hat).
    if (!didInitRef.current) {
      didInitRef.current = true;
      return;
    }
    if (!grew && !settled && turnPhase === null) return;
    el.lastElementChild?.scrollIntoView?.({ block: "end" });
  }, [messageCount, planCount, settledPlanCount, turnPhase, consumePrepending]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || chat.sending) return;
    setText("");
    // Jeder neue Submit nimmt einen etwaigen /plan-Usage-Hinweis wieder weg
    // (gleiche Klebrigkeits-Regel wie die Chat-Composer-Fehler).
    planspec.clearUsageError();
    // S3.3-FE: „/plan <idee>" geht in den Draft-Flow (Draft-Card im Thread),
    // nicht in einen Chat-Turn. „/plan" ohne Idee → Usage-Hinweis im Hook.
    const planMatch = PLAN_PREFIX_RE.exec(value);
    if (planMatch) {
      void planspec.submitIdea(planMatch[1] ?? "");
      return;
    }
    void chat.send(value);
  };

  const onPaste = (event: ClipboardEvent<HTMLInputElement>) => {
    const file = Array.from(event.clipboardData?.files ?? []).find((f) =>
      f.type.startsWith("image/"),
    );
    if (file) {
      event.preventDefault();
      void chat.attachFile(file);
    }
  };

  const hasThread = messageCount > 0 || chat.activeTurn !== null || planCount > 0;

  return (
    <>
      {hasThread ? (
        <div className="jv-chat" ref={threadRef} role="log" aria-label={t.chatRegion}>
          {chat.nextBeforeId !== null ? (
            <button
              type="button"
              className="jv-older"
              disabled={chat.loadingOlder}
              onClick={() => void chat.loadOlder()}
            >
              {chat.loadingOlder ? t.loadOlderBusy : t.loadOlder}
            </button>
          ) : null}
          {chat.messages?.map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              claudeModels={claudeModels}
            />
          ))}
          {chat.activeTurn ? (
            <>
              <div className="jv-bubble jv-bubble-user">
                {chat.activeTurn.text}
                {chat.activeTurn.attachment ? (
                  <span className="jv-attref">
                    <img src={chat.activeTurn.attachment.previewUrl} alt="" />
                    <span className="jv-an">{chat.activeTurn.attachment.name}</span>
                  </span>
                ) : null}
              </div>
              {chat.activeTurn.phase === "waiting" ? (
                <div className="jv-bubble jv-bubble-assistant" role="status" aria-label={t.thinking}>
                  <span className="jv-thinking" aria-hidden="true">
                    JARVIS DENKT
                    <span className="jv-dots">
                      <i />
                      <i />
                      <i />
                    </span>
                  </span>
                </div>
              ) : (
                <div className="jv-bubble jv-bubble-error">
                  <span className="jv-errlabel">{t.errorLabel}</span>
                  {chat.activeTurn.error}
                </div>
              )}
            </>
          ) : null}
          {/* S3.3-FE: Draft-Cards des /plan-Flows — client-intern, sie stehen
              NACH den Server-Bubbles (Einreich-Reihenfolge). */}
          {planspec.cards.map((card) => (
            <PlanspecCard key={card.key} card={card} onPropose={planspec.propose} />
          ))}
        </div>
      ) : null}

      {chat.composerError ? (
        <div className="jv-composer-error" role="alert">
          {chat.composerError}
        </div>
      ) : null}
      {planspec.usageError ? (
        <div className="jv-composer-error" role="alert">
          {planspec.usageError}
        </div>
      ) : null}

      <form className="jv-ask" onSubmit={onSubmit} aria-label={t.composerLabel}>
        {chat.attachment ? (
          <span className="jv-attachchip">
            <img src={chat.attachment.previewUrl} alt="" />
            <span className="jv-an">{chat.attachment.name}</span>
            <button
              type="button"
              className="jv-ax"
              aria-label={t.removeAttachment}
              onClick={() => chat.removeAttachment()}
            >
              <X aria-hidden className="h-3.5 w-3.5" />
            </button>
          </span>
        ) : null}
        <input
          type="text"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onPaste={onPaste}
          placeholder={JARVIS_ASK_HINT}
          aria-label={t.inputLabel}
          maxLength={32000}
          disabled={chat.sending}
        />
        <span className="jv-icons">
          <button
            type="button"
            className="jv-ic"
            aria-label={t.attachLabel}
            title={imagesOk ? undefined : t.engineNoImagesTitle}
            disabled={chat.sending || chat.uploading || !imagesOk}
            onClick={() => fileRef.current?.click()}
          >
            <ImagePlus aria-hidden className="h-4 w-4" />
          </button>
          <button
            type="submit"
            className="jv-ic jv-send"
            aria-label={t.sendLabel}
            disabled={chat.sending || !text.trim()}
          >
            <Send aria-hidden className="h-4 w-4" />
          </button>
        </span>
        <input
          ref={fileRef}
          type="file"
          accept={PA_UPLOAD_ACCEPT}
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void chat.attachFile(file);
            event.target.value = "";
          }}
        />
      </form>
    </>
  );
}
