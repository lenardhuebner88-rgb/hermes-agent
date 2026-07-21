/**
 * InboxPanel — volle Inbox-Ansicht der Jarvis-Shell (Sprint 2, Karte S2.4):
 * der Expand-Drawer aus „Wartet · dezent" heraus. Zeigt ALLE Items des
 * /api/pa/inbox-Snapshots (WartetPanel reicht sie per Props durch — kein
 * zweiter Poll, kein Fork der Datenquelle).
 *
 * Item-Typen:
 *  - pa_action → Approval-Card: Kategorie, Ziel lesbar aus action_payload;
 *    S6: Grund und Payload bleiben bis zum Aufklappen kompakt verborgen.
 *    (tmux.* → session:window, kanban.* → card_id, planspec.ingest →
 *    draft_id mit PLANSPEC-Chip, S3.3-FE), Keys-Vorschau, reason.
 *    Ausführen/Ablehnen über den BESTEHENDEN Endpoint
 *    POST /api/agent-questions/{question_id}/answer ("1"/"2", answered_by
 *    operator). Nach Confirm verschwindet die Karte über den Inbox-Refresh
 *    (Server-Wahrheit) und das Panel zeigt den Hinweis „Evidenz im Chat"
 *    (der Executor legt dort eine pa-executor-Assistant-Bubble ab). 409
 *    (stale/double-tap) → Refresh statt Fehlerzustand. KEINE Pane-Liveness-
 *    Annahmen hier — die Ausführung ist Executor-Sache.
 *  - question → Karte mit Optionen (read-only) + Antwort-Link in den
 *    Klassik-Tab (S1-Muster, keine neue Antwort-Mechanik).
 *  - held_task / freigabe_gate → Karte mit Status/Freigabe + Board-Link.
 *
 * S7.6: Decision-Cards — Zeile 1 ist die Entscheidung (Server-`summary`,
 * Fallback: clientseitige Destillation), Badges zeigen 🔑 Operator-Freigabe,
 * Alter und Blockradius auf einen Blick; Roh-Titel und Details bleiben bis
 * zum Expand verborgen. S8 ergänzt für Kanban-Cards ein rein informatives
 * WHY aus der Server-Antwort. Aktionen (PRÜFEN/Ausführen/Links) unverändert.
 *
 * ESC oder × schließt die Ansicht.
 */
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { api, type PaInboxActionItem, type PaInboxItem } from "@/lib/api";
import { extractDetail } from "../hooks/internal";
import { de } from "../i18n/de";
import { decisionAge, decisionHeadline, needsOperatorKey } from "./decisionTitle";

const t = de.jarvis;

/** S7.6: Roh-Titel hinter dem Expand zeigen, wenn er sich von der
 *  Headline unterscheidet (leere Titel haben nichts zu zeigen). */
function rawTitleDiffers(item: PaInboxItem, headline: string): boolean {
  const raw = item.title.trim();
  return raw !== "" && raw !== headline;
}

/** Board-Deep-Link für Kanban-Items (Fleet-Board, task-Parameter wie die
 *  bestehenden Klassik-Links). */
function boardLink(cardId: string): string {
  return `/control/fleet?task=${encodeURIComponent(cardId)}`;
}

/** Ziel einer pa_action lesbar aus dem typisierten Payload (Brief-Beispiel:
 *  „tmux.send_keys → work:kimi"). Kein JSON-Dump, keine Pane-Annahmen.
 *  S3.3-FE: planspec.ingest zeigt die draft_id als Zielzeile. */
function actionTarget(item: PaInboxActionItem): string | null {
  const payload = item.action_payload?.payload;
  if (!payload) return null;
  if (payload.session && payload.window) return `${payload.session}:${payload.window}`;
  if (payload.card_id) return payload.card_id;
  if (payload.draft_id) return payload.draft_id;
  return null;
}

/** fetchJSON-Fehler tragen das HTTP-Präfix ("409: {…}") — 409 heißt hier:
 *  stale/double-tap, die Inbox hat die Antwort schon (oder der Operator war
 *  woanders schneller) → Refresh statt Fehlerbehandlung. */
function isConflict(err: unknown): boolean {
  return err instanceof Error && err.message.startsWith("409:");
}

export interface InboxPanelProps {
  items: ReadonlyArray<PaInboxItem>;
  onClose: () => void;
  /** Server-Wahrheit neu laden (nach einer Antwort verschwindet die Karte). */
  onRefresh: () => Promise<void>;
  /** Hinweis-Zeile im dezenten Panel setzen (Evidenz-/Stale-Hinweis). */
  onHint: (hint: string) => void;
}

export function InboxPanel({ items, onClose, onRefresh, onHint }: InboxPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeButtonRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const trapFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      panelRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div
      ref={panelRef}
      className="jv-float jv-fragen"
      id="jv-inbox-panel"
      role="dialog"
      aria-modal="true"
      aria-label={t.inboxPanelTitle}
      onKeyDown={trapFocus}
    >
      <div className="jv-ptitle jv-fragen-head">
        {t.inboxPanelTitle}{" "}
        <span style={{ color: "var(--faint)", letterSpacing: ".05em" }}>{items.length}</span>
        <button
          ref={closeButtonRef}
          type="button"
          className="jv-fclose"
          onClick={onClose}
          aria-label={t.inboxClose}
        >
          ×
        </button>
      </div>

      <div className="jv-fbody">
        {items.length === 0 ? <p className="jv-qloading">{t.inboxEmpty}</p> : null}
        {items.map((item) => {
          if (item.type === "pa_action") {
            return (
              <ApprovalCard key={item.id} item={item} onRefresh={onRefresh} onHint={onHint} />
            );
          }
          if (item.type === "question") {
            return <QuestionCard key={item.id} item={item} />;
          }
          return <TaskCard key={item.id} item={item} />;
        })}
      </div>
    </div>
  );
}

function ApprovalCard({
  item,
  onRefresh,
  onHint,
}: {
  item: PaInboxActionItem;
  onRefresh: () => Promise<void>;
  onHint: (hint: string) => void;
}) {
  const [pending, setPending] = useState<"1" | "2" | null>(null);
  const [cardError, setCardError] = useState<string | null>(null);

  const category = item.action_payload?.category ?? item.category ?? null;
  const target = actionTarget(item);
  const keys = item.action_payload?.payload.keys ?? null;
  const reason = item.action_payload?.reason ?? null;
  // S7.6: Decision-Headline statt Roh-Titel (summary > Destillation).
  const headline = decisionHeadline(item);
  const showRawTitle = rawTitleDiffers(item, headline);
  const age = decisionAge(item.ts);
  // S3.3-FE: planspec.ingest-Cards tragen den PLANSPEC-Chip statt des
  // generischen PA-AKTION-Chips; der Ausführen/Ablehnen-Flow ist derselbe.
  const isPlanspec = category === "planspec.ingest";
  const executeLabel = item.options.find((opt) => Number(opt.nr) === 1)?.label ?? t.inboxExecute;
  const rejectLabel = item.options.find((opt) => Number(opt.nr) === 2)?.label ?? t.inboxReject;

  const answer = async (value: "1" | "2") => {
    if (pending !== null) return;
    setPending(value);
    setCardError(null);
    try {
      const result = await api.answerAgentQuestion(item.question_id, value);
      const executed = result.executed === true;
      const verified = result.verified !== false;
      onHint(
        executed
          ? verified
            ? t.inboxHintExecuted
            : t.inboxHintFailed
          : t.inboxHintRejected,
      );
      // Server-Wahrheit: die beantwortete Karte fällt aus der Inbox.
      await onRefresh().catch(() => {});
    } catch (err) {
      if (isConflict(err)) {
        // Stale/Doppel-Tap: jemand war schneller — Inbox refreshen (Brief),
        // die Karte verschwindet mit der Server-Wahrheit von selbst.
        onHint(t.inboxHintStale);
        await onRefresh().catch(() => {});
      } else {
        setCardError(`${t.inboxAnswerFailed} ${extractDetail(err)}`);
      }
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="jv-frage jv-appr" data-testid={`jv-appr-${item.id}`}>
      <p className="jv-frage-meta">
        <span className={isPlanspec ? "jv-appr-chip jv-planspec" : "jv-appr-chip"}>
          {isPlanspec ? t.inboxPlanspecChip : t.inboxActionChip}
        </span>
        {category ? <span className="jv-appr-cat">{category}</span> : null}
        {age ? <span className="jv-badge jv-badge-age">{age}</span> : null}
        {item.block_radius > 0 ? (
          <span className="jv-badge jv-badge-blocked">{t.inboxBlocked(item.block_radius)}</span>
        ) : null}
      </p>

      <p className="jv-frage-text">{headline}</p>

      {category && target ? (
        <p className="jv-appr-target" data-testid={`jv-appr-target-${item.id}`}>
          {category} → {target}
        </p>
      ) : null}

      {showRawTitle || keys || reason ? (
        <details className="jv-appr-details">
          <summary>{t.inboxDetails}</summary>
          {showRawTitle ? (
            <p className="jv-appr-raw" data-testid={`jv-appr-raw-${item.id}`}>
              {item.title}
            </p>
          ) : null}
          {keys ? (
            <p className="jv-appr-keys" title={keys}>
              {t.inboxKeysLabel} {keys.length > 120 ? `${keys.slice(0, 120)}…` : keys}
            </p>
          ) : null}
          {reason ? <p className="jv-appr-reason">{reason}</p> : null}
        </details>
      ) : null}

      <div className="jv-appr-actions">
        <button
          type="button"
          className="jv-appr-btn jv-appr-go"
          disabled={pending !== null}
          onClick={() => void answer("1")}
        >
          {pending === "1" ? "…" : executeLabel}
        </button>
        <button
          type="button"
          className="jv-appr-btn jv-appr-no"
          disabled={pending !== null}
          onClick={() => void answer("2")}
        >
          {pending === "2" ? "…" : rejectLabel}
        </button>
      </div>

      {cardError ? (
        <p className="jv-appr-error" role="alert">
          {cardError}
        </p>
      ) : null}
    </div>
  );
}

function QuestionCard({ item }: { item: Extract<PaInboxItem, { type: "question" }> }) {
  // S7.6: Decision-Headline + Alter/Blockradius als Badges.
  const headline = decisionHeadline(item);
  const showRawTitle = rawTitleDiffers(item, headline);
  const age = decisionAge(item.ts);
  return (
    <div className="jv-frage" data-testid={`jv-inbox-q-${item.id}`}>
      <p className="jv-frage-meta">
        <span className="jv-frage-kind">{item.kind?.trim() || "agent"}</span>
        {age ? <span className="jv-badge jv-badge-age">{age}</span> : null}
        {item.block_radius > 0 ? (
          <span className="jv-badge jv-badge-blocked">{t.inboxBlocked(item.block_radius)}</span>
        ) : null}
      </p>
      <p className="jv-frage-text">{headline}</p>
      {showRawTitle ? (
        <details className="jv-appr-details">
          <summary>{t.inboxRawDetails}</summary>
          <p className="jv-appr-raw">{item.title}</p>
        </details>
      ) : null}
      {item.options.length > 0 ? (
        <div className="jv-fopts" role="group" aria-label={t.inboxOptionsLabel}>
          {item.options.map((opt) => (
            <div className={opt.recommended ? "jv-fopt jv-rec" : "jv-fopt"} key={String(opt.nr)}>
              <span className="jv-fnr">{opt.nr}</span>
              <span className="jv-flabel">{opt.label}</span>
              {opt.recommended ? <span className="jv-ftag">{t.inboxRecommended}</span> : null}
            </div>
          ))}
        </div>
      ) : null}
      <Link
        className="jv-fanswer"
        to="/control/projekte-klassisch"
        aria-label={t.wartetAnswer(item.title)}
      >
        {t.fragenAnswerLink}
      </Link>
    </div>
  );
}

function TaskCard({
  item,
}: {
  item: Extract<PaInboxItem, { type: "held_task" | "freigabe_gate" }>;
}) {
  const isGate = item.type === "freigabe_gate";
  // S7.6: Decision-Card — Headline (summary > Destillation), Badges für
  // 🔑 Operator-Freigabe / Alter / Blockradius; Roh-Titel und Status bleiben
  // bis zum Expand verborgen.
  const headline = decisionHeadline(item);
  const showRawTitle = rawTitleDiffers(item, headline);
  const why = item.why?.trim();
  const consequenceOnDecline = item.consequence_on_decline?.trim();
  const age = decisionAge(item.ts);
  const statusLine = [item.status, item.freigabe ? `freigabe: ${item.freigabe}` : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <div className="jv-frage" data-testid={`jv-inbox-t-${item.id}`}>
      <p className="jv-frage-meta">
        <span className={isGate ? "jv-appr-chip jv-gate" : "jv-appr-chip jv-held"}>
          {isGate ? t.inboxGateChip : t.inboxHeldChip}
        </span>
        {needsOperatorKey(item) ? (
          <span
            className="jv-badge jv-badge-key"
            title={t.inboxKeyTitle}
            data-testid={`jv-key-${item.id}`}
          >
            🔑
          </span>
        ) : null}
        {age ? <span className="jv-badge jv-badge-age">{age}</span> : null}
        {item.block_radius > 0 ? (
          <span className="jv-badge jv-badge-blocked">{t.inboxBlocked(item.block_radius)}</span>
        ) : null}
      </p>
      <p className="jv-frage-text">{headline}</p>
      {why || consequenceOnDecline ? (
        <div className="jv-decision-why" title={item.title}>
          {why ? (
            <p>
              <span>{t.inboxWhyLabel}</span>
              {why}
            </p>
          ) : null}
          {consequenceOnDecline ? (
            <p>
              <span>{t.inboxDeclineLabel}</span>
              {consequenceOnDecline}
            </p>
          ) : null}
        </div>
      ) : null}
      {showRawTitle || statusLine ? (
        <details className="jv-appr-details">
          <summary>{t.inboxTaskDetails}</summary>
          {showRawTitle ? (
            <p className="jv-appr-raw" data-testid={`jv-inbox-raw-${item.id}`}>
              {item.title}
            </p>
          ) : null}
          {statusLine ? <p className="jv-appr-reason">{statusLine}</p> : null}
        </details>
      ) : null}
      <Link
        className="jv-fanswer"
        to={boardLink(item.card_id)}
        aria-label={t.inboxBoardAria(item.title)}
      >
        {t.inboxBoardLink}
      </Link>
    </div>
  );
}
