/**
 * JarvisChat — Bubble-Verlauf + Frag-Leiste der Jarvis-Zone.
 *
 * Verlauf aus GET /api/pa/messages (roles user/assistant, Provenienz-Badge
 * `model` je Assistant-Bubble); Senden über die A4-Frag-Leiste (unten
 * fixiert): pending-Bubble mit Denk-Zustand während des Turn-Polls,
 * Error-Bubble mit Fehlertext bei Fehlern — nie ein stiller Fehler.
 * Bild-Paste/Attach → POST /api/pa/upload → attachments im Message-POST
 * (max 1 Bild/Turn, Backend-Kontrakt), Vorschau-Thumbnail in der Leiste.
 */
import { useEffect, useRef, useState, type ClipboardEvent, type FormEvent } from "react";
import { ImagePlus, Send, X } from "lucide-react";

import type { PaChatMessage } from "@/lib/api";
import { de } from "../i18n/de";
import { JARVIS_ASK_HINT } from "./mockContent";
import { PA_UPLOAD_ACCEPT, usePaChat } from "./usePaChat";

const t = de.jarvis;

function formatBubbleTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function MessageBubble({
  message,
  isError,
}: {
  message: PaChatMessage;
  isError: (content: string) => boolean;
}) {
  if (message.role === "user") {
    return <div className="jv-bubble jv-bubble-user">{message.content}</div>;
  }
  const error = isError(message.content);
  return (
    <div className={error ? "jv-bubble jv-bubble-error" : "jv-bubble jv-bubble-assistant"}>
      {error ? <span className="jv-errlabel">{t.errorLabel}</span> : null}
      {message.content}
      {/* Provenienz-Badge: Modell dezent (Platzhalter für das S2-Roster). */}
      <span className="jv-badge">
        {message.model} · {formatBubbleTime(message.ts)}
      </span>
    </div>
  );
}

export function JarvisChat({ turnPollIntervalMs }: { turnPollIntervalMs?: number } = {}) {
  // turnPollIntervalMs ist eine Di/Test-Naht (kürzere Turn-Poll-Kadenz in
  // Komponententests); Produktiv Default: PA_TURN_POLL_INTERVAL_MS.
  const chat = usePaChat({ turnPollIntervalMs });
  const [text, setText] = useState("");
  const threadRef = useRef<HTMLDivElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const seenCountRef = useRef(0);

  // Auto-Anschluss ans Verlaufsende: nur bei NEUEN Inhalten (neue Bubble oder
  // Turn-Zustandswechsel), nie beim bloßen Hintergrund-Refresh der History.
  const messageCount = chat.messages?.length ?? 0;
  const turnPhase = chat.activeTurn?.phase ?? null;
  const didInitRef = useRef(false);
  useEffect(() => {
    const el = threadRef.current;
    if (!el) return;
    const grew = messageCount > seenCountRef.current;
    seenCountRef.current = messageCount;
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
    // Inhalten NACH der ersten Anzeige (z. B. eigene Frage + Antwort).
    if (!didInitRef.current) {
      didInitRef.current = true;
      return;
    }
    if (!grew && turnPhase === null) return;
    el.lastElementChild?.scrollIntoView?.({ block: "end" });
  }, [messageCount, turnPhase]);

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || chat.sending) return;
    setText("");
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

  const hasThread = messageCount > 0 || chat.activeTurn !== null;

  return (
    <>
      {hasThread ? (
        <div className="jv-chat" ref={threadRef} role="log" aria-label={t.chatRegion}>
          {chat.messages?.map((message, index) => (
            <MessageBubble
              key={`${message.ts}-${message.role}-${index}`}
              message={message}
              isError={chat.isErrorContent}
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
        </div>
      ) : null}

      {chat.composerError ? (
        <div className="jv-composer-error" role="alert">
          {chat.composerError}
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
            disabled={chat.sending || chat.uploading}
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
