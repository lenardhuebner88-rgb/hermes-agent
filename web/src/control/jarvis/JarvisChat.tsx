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
 * stiller Fehler (S4-Härtung: auch der Verlaufs-Poll-Fehler und TTS-Fehler
 * landen als Composer-Fehlerzeile statt unsichtbar zu bleiben). Bild-Paste/Attach → POST /api/pa/upload → attachments im
 * Message-POST (max 1 Bild/Turn); bei Engines mit supports_images=false
 * (S2.2-Roster) ist der Attach-Button deaktiviert (Tooltip) statt erst beim
 * Senden in den 400 zu laufen. S3.3-FE: „/plan <idee>" in der Frag-Leiste
 * startet den PlanSpec-Draft-Flow (usePlanspecDraft) statt eines Chat-Turns
 * — die validierte Draft-Card (PlanspecCard) ist eine client-interne Bubble
 * im Thread; „Als Approval einreichen" stellt sie als planspec.ingest-Card
 * in die S2.4-Inbox. S3.6: Push-to-Talk — Mic-Button in der Icon-Leiste
 * (Aufnahme → POST /api/audio/transcribe → Transkript landet im Input, KEIN
 * Auto-Send: der Nutzer prüft/korrigiert und sendet selbst) plus Vorlese-
 * Toggle (in localStorage persistiert): die neueste FERTIGE Assistant-
 * Antwort wird genau einmal über POST /api/audio/speak abgespielt — nie
 * Historie beim Mount, nie doppelt bei Re-Render/Verlauf-Reload.
 * S5-Design („JARVIS OS"): der Assistent ist die Mitte, die Maschine tritt in
 * die Peripherie — Wächter-Nachrichten (engine === "pa-watcher") fluten den
 * Verlauf nicht mehr als Bubbles, sondern werden als deduplizierter Digest in
 * der Periphery-Zeile über dem Gespräch geführt (Tap → Aktivitaet-Drawer der
 * Shell via window-Event, volles Log bleibt erreichbar). Über dem Gespräch
 * lebt der JarvisOrb (idle/listening/thinking/speaking/error) mit dem
 * Engine-Switcher.
 */
import { useEffect, useMemo, useRef, useState, type ClipboardEvent, type FormEvent } from "react";
import { ImagePlus, Loader2, Mic, MicOff, MonitorUp, Send, Volume2, VolumeX, X } from "lucide-react";

import { api, type PaChatMessage, type PaEnginesResponse } from "@/lib/api";
import { de } from "../i18n/de";
import {
  effectiveEngine,
  findEngineSpec,
  isClaudeModel,
  modelLabel,
  useEngineChoice,
  usePaEngines,
} from "./engineSelection";
import { JarvisOrb, type JarvisOrbState } from "./JarvisOrb";
import { JARVIS_ASK_HINT } from "./mockContent";
import { PeripheryStrip } from "./PeripheryStrip";
import { PlanspecCard } from "./PlanspecCard";
import { blobToDataUrl, useMicRecorder } from "./useMicRecorder";
import { PA_UPLOAD_ACCEPT, usePaChat } from "./usePaChat";
import { PLAN_PREFIX_RE, usePlanspecDraft } from "./usePlanspecDraft";
import { useLiveShare } from "./useLiveShare";
import { useSpeechPlayback } from "./useSpeechPlayback";
import { digestWatcherEvents } from "./watcherDigest";

const t = de.jarvis;

/** S5-Design: Tap auf die Periphery-Zeile öffnet den Aktivitaet-Drawer der
 *  Shell. Der Drawer lebt in JarvisShellView — statt Prop-Bohrung durch die
 *  Shell hört sie auf dieses kleine Window-Event. */
export const JARVIS_OPEN_AKTIVITAET_EVENT = "jarvis:open-aktivitaet";

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
        alt={t.attachmentAlt}
        loading="lazy"
        onError={() => setBroken(true)}
      />
    </span>
  );
}

function MessageBubble({
  message,
  roster,
}: {
  message: PaChatMessage;
  /** Roster für den MAX-Marker (isClaudeModel); null = Roster noch nicht da
   *  → kein Marker (dezent, kein Crash). */
  roster: PaEnginesResponse | null;
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
  const maxMarker = isClaudeModel(roster, message.model);
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

  // S3.6 — Push-to-Talk + Vorlesen. mic.error hält Recorder-/Permission-
  // Fehler, micTranscribeError die des Transkribierens; beide erscheinen als
  // Composer-Fehlerzeile, der Input bleibt bei Fehlern unverändert.
  const mic = useMicRecorder();
  const {
    play: speakPlay,
    enabled: speakEnabled,
    setEnabled: setSpeakEnabled,
    speakError,
    playing: speakPlaying,
  } = useSpeechPlayback();
  const [transcribing, setTranscribing] = useState(false);
  const [micTranscribeError, setMicTranscribeError] = useState<string | null>(null);
  // Live-Screen-Share (S-live): eine echte, fortlaufende Bildschirmteilung mit
  // sichtbarem Aktivzustand — nie der Bild-Picker. liveShareNotice trägt den
  // ehrlichen „hier nicht verfügbar"-Hinweis für nicht unterstützte Browser.
  const liveShare = useLiveShare({ errorText: t.liveShareError });
  const [liveShareNotice, setLiveShareNotice] = useState<string | null>(null);

  // Bild-Fähigkeit der Engine für den NÄCHSTEN Turn (Switcher-Wahl +
  // Roster-Default): Nicht-Vision-Engines deaktivieren den Attach-Button
  // mit Tooltip statt erst beim Senden in den Backend-400 zu laufen.
  const engine = effectiveEngine(choice, roster.data);
  const imagesOk = findEngineSpec(roster.data, engine)?.supports_images ?? true;

  // S5-Design: Wächter-Nachrichten (engine === "pa-watcher") verlassen das
  // Gespräch — die Konversation rendert nur Mensch ↔ Assistent, der Wächter
  // landet als deduplizierter Digest in der Periphery-Zeile.
  const conversation = useMemo(
    () => (chat.messages ?? []).filter((m) => m.engine !== "pa-watcher"),
    [chat.messages],
  );
  const watcherDigest = useMemo(
    () => digestWatcherEvents(chat.messages ?? []),
    [chat.messages],
  );

  // S5-Design: Orb-Zustand aus den Chat-Hooks — Priorität
  // error > listening > thinking > speaking > idle.
  const orbState: JarvisOrbState =
    chat.composerError || chat.messagesError
      ? "error"
      : mic.status === "recording"
        ? "listening"
        : chat.activeTurn !== null
          ? "thinking"
          : speakPlaying
            ? "speaking"
            : "idle";
  // Anzeigename des effektiven Modells für die aria-Zeile des Orbs.
  const engineLabel = modelLabel(
    choice?.model ?? findEngineSpec(roster.data, engine)?.default_model ?? engine,
  );

  // Auto-Anschluss ans Verlaufsende: nur bei NEUEN Inhalten (neue Bubble,
  // Draft-Card oder Turn-Zustandswechsel), nie beim bloßen Hintergrund-
  // Refresh der History und nie bei einem Prepend älterer Seiten („Ältere
  // laden"). Draft-Cards zählen beim Phasenwechsel drafting→ready/error als
  // neue Aktivität (sonst bliebe die aufgelöste Card mobil bei leerem
  // Verlauf unter dem Fold, ohne dass ein Effekt erneut läuft).
  const messageCount = conversation.length;
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

  // S3.6 — Vorlesen: die NEUESTE fertige Assistant-Antwort genau EINMAL
  // abspielen. Robust gegen Doppel-Wiedergabe: der erste geladene Verlauf
  // (Mount) ist die Baseline und wird NIE vorgelesen; danach löst nur eine
  // neue Assistant-Message-ID aus. Der Toggle-Stand selbst holt nichts nach.
  // S5-Design: nur die Konversation (Mensch ↔ Assistent) — Wächter-Bundles
  // werden nie vorgelesen.
  const lastAssistantMessage = useMemo(() => {
    for (let index = conversation.length - 1; index >= 0; index -= 1) {
      const message = conversation[index];
      if (
        message.role === "assistant" &&
        message.status === "done" &&
        message.content.trim()
      ) {
        return message;
      }
    }
    return null;
  }, [conversation]);

  const speakBaselineRef = useRef(false);
  const lastSpokenIdRef = useRef<number | null>(null);
  useEffect(() => {
    if (chat.messages === null) return; // Verlauf noch nicht geladen
    if (!speakBaselineRef.current) {
      speakBaselineRef.current = true;
      lastSpokenIdRef.current = lastAssistantMessage?.id ?? null;
      return;
    }
    if (!lastAssistantMessage || lastSpokenIdRef.current === lastAssistantMessage.id) return;
    lastSpokenIdRef.current = lastAssistantMessage.id;
    if (speakEnabled) {
      void speakPlay(lastAssistantMessage.content);
    }
  }, [chat.messages, lastAssistantMessage, speakEnabled, speakPlay]);

  // S3.6 — Push-to-Talk: Klick startet die Aufnahme, erneuter Klick stoppt
  // und transkribiert in den Input (KEIN Auto-Send — der Nutzer prüft und
  // sendet selbst). Fehler: deutsche Meldung, Input unverändert.
  const onMicClick = async () => {
    if (transcribing) return;
    if (mic.status === "recording") {
      const blob = await mic.stop();
      if (!blob) return; // leere/verworfene Aufnahme — Input unverändert
      setTranscribing(true);
      try {
        const dataUrl = await blobToDataUrl(blob);
        const result = await api.transcribeAudio(dataUrl, blob.type || undefined);
        const transcript = (result.transcript ?? "").trim();
        if (!result.ok || !transcript) {
          throw new Error("empty transcript");
        }
        setText((prev) => (prev ? `${prev} ${transcript}` : transcript));
        setMicTranscribeError(null);
      } catch {
        setMicTranscribeError(t.micError);
      } finally {
        setTranscribing(false);
      }
      return;
    }
    setMicTranscribeError(null);
    void mic.start();
  };

  // Screenshare-Button: echter Live-Share-Toggle. Nie den Bild-Picker öffnen.
  // Unsupported-Browser (mobiles Chrome/Samsung/iOS): ehrlicher Hinweis statt
  // Erfolgssimulation. Bild anhängen bleibt eine davon getrennte Aktion.
  const onScreenshareClick = () => {
    setLiveShareNotice(null);
    liveShare.clearError();
    if (!liveShare.supported) {
      setLiveShareNotice(t.liveShareUnsupported);
      return;
    }
    if (liveShare.active) {
      liveShare.stop();
      return;
    }
    void liveShare.start();
  };

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const value = text.trim();
    if (!value || chat.sending) return;
    // Jeder neue Submit nimmt einen etwaigen /plan-Usage-Hinweis wieder weg
    // (gleiche Klebrigkeits-Regel wie die Chat-Composer-Fehler).
    planspec.clearUsageError();
    // S3.3-FE: „/plan <idee>" geht in den Draft-Flow (Draft-Card im Thread),
    // nicht in einen Chat-Turn. „/plan" ohne Idee → Usage-Hinweis im Hook.
    const planMatch = PLAN_PREFIX_RE.exec(value);
    if (planMatch) {
      setText("");
      void planspec.submitIdea(planMatch[1] ?? "");
      return;
    }
    if (liveShare.active) {
      // A selected text-only engine must never silently bypass the live frame.
      if (!imagesOk) {
        setLiveShareNotice(t.engineNoImagesTitle);
        return;
      }
      // Without an explicit static attachment, materialise the freshest uploaded
      // live frame. Failure is fail-closed in useLiveShare: preserve the question
      // so the user can retry instead of sending a misleading text-only turn.
      if (!chat.attachment) {
        const assetId = await liveShare.attachCurrentFrame();
        if (!assetId) return;
        setText("");
        void chat.send(value, { attachmentAssetId: assetId });
        return;
      }
    }
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

  const hasThread = messageCount > 0 || chat.activeTurn !== null || planCount > 0;

  // S5-Design: Tap auf die Periphery-Zeile → Aktivitaet-Drawer der Shell
  // (Drawer-State lebt in JarvisShellView, Öffnung via Window-Event).
  const onOpenLog = () => {
    window.dispatchEvent(new CustomEvent(JARVIS_OPEN_AKTIVITAET_EVENT));
  };

  return (
    <div className="jv-chatcol">
      {/* S5-Design: Identität (Orb + Engine-Wahl) und Maschinenraum
          (Periphery-Zeile) ÜBER dem Gespräch. */}
      <div className="jv-orbhead">
        <JarvisOrb state={orbState} engineLabel={engineLabel} />
        <PeripheryStrip digest={watcherDigest} onOpenLog={onOpenLog} />
      </div>

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
          {conversation.map((message) => (
            <MessageBubble
              key={message.id}
              message={message}
              roster={roster.data ?? null}
            />
          ))}
          {chat.activeTurn ? (
            <>
              <div className="jv-bubble jv-bubble-user">
                {chat.activeTurn.text}
                {chat.activeTurn.attachment ? (
                  <span className="jv-attref">
                    <img
                      src={chat.activeTurn.attachment.previewUrl}
                      alt={chat.activeTurn.attachment.name}
                    />
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
      {/* Verlaufs-Poll-Fehler (S4-Härtung): der Hintergrund-Refresh darf nie
          still scheitern — dieselbe Fehlerzeile wie der Composer. */}
      {chat.messagesError ? (
        <div className="jv-composer-error" role="alert">
          {t.historyFailed} {chat.messagesError}
        </div>
      ) : null}
      {planspec.usageError ? (
        <div className="jv-composer-error" role="alert">
          {planspec.usageError}
        </div>
      ) : null}
      {mic.error || micTranscribeError ? (
        <div className="jv-composer-error" role="alert">
          {mic.error ?? micTranscribeError}
        </div>
      ) : null}
      {/* TTS-Fehler (S4-Härtung): ein fehlgeschlagenes Vorlesen ist sichtbar,
          blockiert aber nie den Chat. */}
      {speakError ? (
        <div className="jv-composer-error" role="alert">
          {speakError}
        </div>
      ) : null}
      {liveShare.error || liveShareNotice ? (
        <div className="jv-composer-error" role="alert">
          {liveShare.error ?? liveShareNotice}
        </div>
      ) : null}

      {liveShare.active ? (
        <div className="jv-liveshare-status" role="status">
          <span className="jv-live-dot" aria-hidden="true" />
          <span className="jv-live-label">{t.liveShareActive}</span>
          <button
            type="button"
            className="jv-live-stop"
            onClick={() => liveShare.stop()}
          >
            {t.liveShareStopAction}
          </button>
        </div>
      ) : null}

      <form className="jv-ask" onSubmit={onSubmit} aria-label={t.composerLabel}>
        {chat.attachment ? (
          <span className="jv-attachchip">
            <img src={chat.attachment.previewUrl} alt={chat.attachment.name} />
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
          {/* S-live — Live-Screen-Share-Toggle (echtes fortlaufendes Teilen,
              nie der Bild-Picker). Aktiv-Zustand sichtbar über .jv-live-on und
              die Status-Zeile; nicht unterstützte Browser bleiben klickbar, um
              den ehrlichen Hinweis zu zeigen. */}
          <button
            type="button"
            className={liveShare.active ? "jv-ic jv-screenshare jv-live-on" : "jv-ic jv-screenshare"}
            aria-label={liveShare.active ? t.liveShareStop : t.liveShareStart}
            aria-pressed={liveShare.active}
            title={liveShare.active ? t.liveShareStop : t.liveShareStart}
            disabled={chat.sending}
            onClick={onScreenshareClick}
          >
            <MonitorUp aria-hidden className="h-4 w-4" />
          </button>
          {/* S3.6 — Push-to-Talk: idle → recording (Puls) → transcribing
              (Spinner); das Transkript landet im Input, kein Auto-Send. */}
          <button
            type="button"
            className={mic.status === "recording" ? "jv-ic jv-mic jv-mic-rec" : "jv-ic jv-mic"}
            aria-label={
              transcribing
                ? t.micTranscribing
                : mic.status === "recording"
                  ? t.micRecording
                  : t.micLabel
            }
            aria-pressed={mic.status === "recording"}
            disabled={transcribing}
            onClick={() => void onMicClick()}
          >
            {transcribing ? (
              <Loader2 aria-hidden className="h-4 w-4 jv-spin" />
            ) : mic.status === "recording" ? (
              <MicOff aria-hidden className="h-4 w-4" />
            ) : (
              <Mic aria-hidden className="h-4 w-4" />
            )}
          </button>
          {/* S3.6 — Vorlese-Toggle (persistiert): fertige Assistant-Antworten
              einmal abspielen; Aktiv-Zustand sichtbar über .jv-on. */}
          <button
            type="button"
            className={speakEnabled ? "jv-ic jv-speak jv-on" : "jv-ic jv-speak"}
            aria-label={t.speakLabel}
            aria-pressed={speakEnabled}
            title={t.speakLabel}
            onClick={() => setSpeakEnabled(!speakEnabled)}
          >
            {speakEnabled ? (
              <Volume2 aria-hidden className="h-4 w-4" />
            ) : (
              <VolumeX aria-hidden className="h-4 w-4" />
            )}
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
    </div>
  );
}
