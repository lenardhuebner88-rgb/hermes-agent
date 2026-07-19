/**
 * InboxPanel — volle Inbox-Ansicht der Jarvis-Shell (Sprint 2, Karte S2.4):
 * der Expand-Drawer aus „Wartet · dezent" heraus. Zeigt ALLE Items des
 * /api/pa/inbox-Snapshots (WartetPanel reicht sie per Props durch — kein
 * zweiter Poll, kein Fork der Datenquelle).
 *
 * Item-Typen:
 *  - pa_action → Approval-Card: Kategorie, Ziel lesbar aus action_payload
 *    (tmux.* → session:window, kanban.* → card_id), Keys-Vorschau, reason.
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
 * ESC oder × schließt die Ansicht.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type PaInboxActionItem, type PaInboxItem } from "@/lib/api";
import { extractDetail } from "../hooks/internal";
import { de } from "../i18n/de";

const t = de.jarvis;

/** Board-Deep-Link für Kanban-Items (Fleet-Board, task-Parameter wie die
 *  bestehenden Klassik-Links). */
function boardLink(cardId: string): string {
  return `/control/fleet?task=${encodeURIComponent(cardId)}`;
}

/** Ziel einer pa_action lesbar aus dem typisierten Payload (Brief-Beispiel:
 *  „tmux.send_keys → work:kimi"). Kein JSON-Dump, keine Pane-Annahmen. */
function actionTarget(item: PaInboxActionItem): string | null {
  const payload = item.action_payload?.payload;
  if (!payload) return null;
  if (payload.session && payload.window) return `${payload.session}:${payload.window}`;
  if (payload.card_id) return payload.card_id;
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
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="jv-float jv-fragen"
      id="jv-inbox-panel"
      role="region"
      aria-label={t.inboxPanelTitle}
    >
      <div className="jv-ptitle jv-fragen-head">
        {t.inboxPanelTitle}{" "}
        <span style={{ color: "var(--faint)", letterSpacing: ".05em" }}>{items.length}</span>
        <button type="button" className="jv-fclose" onClick={onClose} aria-label={t.inboxClose}>
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
        <span className="jv-appr-chip">{t.inboxActionChip}</span>
        {category ? <span className="jv-appr-cat">{category}</span> : null}
      </p>

      <p className="jv-frage-text">{item.title}</p>

      {category && target ? (
        <p className="jv-appr-target" data-testid={`jv-appr-target-${item.id}`}>
          {category} → {target}
        </p>
      ) : null}

      {keys ? (
        <p className="jv-appr-keys" title={keys}>
          {t.inboxKeysLabel} {keys.length > 120 ? `${keys.slice(0, 120)}…` : keys}
        </p>
      ) : null}

      {reason && !item.title.includes(reason) ? (
        <p className="jv-appr-reason">{reason}</p>
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
  return (
    <div className="jv-frage" data-testid={`jv-inbox-q-${item.id}`}>
      <p className="jv-frage-meta">
        <span className="jv-frage-kind">{item.kind?.trim() || "agent"}</span>
      </p>
      <p className="jv-frage-text">{item.title}</p>
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
  return (
    <div className="jv-frage" data-testid={`jv-inbox-t-${item.id}`}>
      <p className="jv-frage-meta">
        <span className={isGate ? "jv-appr-chip jv-gate" : "jv-appr-chip jv-held"}>
          {isGate ? t.inboxGateChip : t.inboxHeldChip}
        </span>
        {item.status ? <span>{item.status}</span> : null}
        {item.freigabe ? <span> · freigabe: {item.freigabe}</span> : null}
      </p>
      <p className="jv-frage-text">{item.title}</p>
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
