/**
 * PlanspecCard — S3.3-FE Draft-Card im Jarvis-Thread (client-interne Bubble,
 * kein Server-Verlauf). Aufbau wie ein Chat-Turn: User-Bubble mit der
 * „/plan"-Eingabe, darunter der Zustand — pending („JARVIS PLANT"), Fehler
 * (Error-Bubble, nie still) oder der validierte Entwurf.
 *
 * Entwurf: Validate-Status prominent als farbiges Chip (CLEAN grün / WARN
 * amber / BLOCK rot), Findings bei WARN+BLOCK standardmäßig AUFGEKLAPPT
 * (Backend-Handoff: Findings bleiben vor dem Tap sichtbar — einklappbar
 * bleibt es), Slice-Liste (id · title · lane · deps), Planspec-Text als
 * <details>. CLEAN/WARN: „Als Approval einreichen" → propose; BLOCK:
 * Button disabled + Erklärung. Nach dem Einreichen zeigt die Card den
 * Inbox-Hinweis mit der question_id.
 */
import { de } from "../i18n/de";
import type { PlanspecDraftCard } from "./usePlanspecDraft";

const t = de.jarvis;

const STATUS_CLASS: Record<string, string> = {
  CLEAN: "jv-st-clean",
  WARN: "jv-st-warn",
  BLOCK: "jv-st-block",
};
const STATUS_TEXT: Record<string, string> = {
  CLEAN: t.planStatusClean,
  WARN: t.planStatusWarn,
  BLOCK: t.planStatusBlock,
};

export function PlanspecCard({
  card,
  onPropose,
}: {
  card: PlanspecDraftCard;
  onPropose: (card: PlanspecDraftCard) => void;
}) {
  return (
    <>
      <div className="jv-bubble jv-bubble-user">/plan {card.idea}</div>
      {card.phase === "drafting" ? (
        <div className="jv-bubble jv-bubble-assistant" role="status" aria-label={t.planThinking}>
          <span className="jv-thinking" aria-hidden="true">
            JARVIS PLANT
            <span className="jv-dots">
              <i />
              <i />
              <i />
            </span>
          </span>
        </div>
      ) : null}
      {card.phase === "error" ? (
        <div className="jv-bubble jv-bubble-error">
          <span className="jv-errlabel">{t.errorLabel}</span>
          {card.error}
        </div>
      ) : null}
      {card.phase === "ready" && card.draft !== null ? (
        <ReadyCard card={card} onPropose={onPropose} />
      ) : null}
    </>
  );
}

function ReadyCard({
  card,
  onPropose,
}: {
  card: PlanspecDraftCard;
  onPropose: (card: PlanspecDraftCard) => void;
}) {
  const draft = card.draft;
  if (draft === null) return null;
  const status = draft.validation.status;
  const findings = draft.validation.findings;
  const blocked = status === "BLOCK";
  const proposed = card.proposePhase === "done";
  return (
    <div
      className="jv-bubble jv-plan"
      data-testid={`jv-plan-${draft.draft_id}`}
      aria-label={t.planCardAria(draft.draft_id)}
    >
      <span className="jv-plan-head">
        <span className="jv-plan-chip">{t.planChip}</span>
        <span className={`jv-plan-status ${STATUS_CLASS[status] ?? "jv-st-block"}`}>
          {STATUS_TEXT[status] ?? status}
        </span>
      </span>
      <span className="jv-plan-id">{draft.draft_id}</span>

      {findings.length > 0 ? (
        <details className="jv-plan-details" open={status !== "CLEAN" ? true : undefined}>
          <summary>{t.planFindings(findings.length)}</summary>
          <ul className={blocked ? "jv-plan-findings jv-f-block" : "jv-plan-findings jv-f-warn"}>
            {findings.map((finding, index) => (
              <li key={index}>{finding}</li>
            ))}
          </ul>
        </details>
      ) : null}

      {draft.slices.length > 0 ? (
        <ul className="jv-plan-slices" aria-label={t.planSlicesLabel(draft.slices.length)}>
          {draft.slices.map((slice) => (
            <li key={slice.id}>
              <span className="jv-plan-sid">{slice.id}</span> {slice.title}{" "}
              <span className="jv-plan-lane">[{slice.lane}]</span>{" "}
              <span className="jv-plan-deps">
                deps: {slice.deps.length > 0 ? slice.deps.join(", ") : "—"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      <details className="jv-plan-details">
        <summary>{t.planTextLabel}</summary>
        <pre className="jv-plan-pre">{draft.planspec_text}</pre>
      </details>

      {blocked ? <p className="jv-plan-blocked">{t.planBlockedHint}</p> : null}

      {proposed ? (
        <p className="jv-plan-done" data-testid={`jv-plan-done-${draft.draft_id}`}>
          {t.planProposed(card.questionId ?? 0)}
        </p>
      ) : (
        <div className="jv-appr-actions">
          <button
            type="button"
            className="jv-appr-btn jv-appr-go"
            disabled={blocked || card.proposePhase === "pending"}
            onClick={() => onPropose(card)}
          >
            {card.proposePhase === "pending" ? "…" : t.planPropose}
          </button>
        </div>
      )}

      {card.proposeError ? (
        <p className="jv-appr-error" role="alert">
          {card.proposeError}
        </p>
      ) : null}

      <span className="jv-badge">
        {card.engine}
        {card.model ? ` · ${card.model}` : ""}
      </span>
    </div>
  );
}
