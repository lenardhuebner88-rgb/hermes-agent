/**
 * AnswerQuestion (S6) — Inline-Form zum Beantworten einer Operator-Frage.
 *
 * Ein Task mit operator_question-Blockgrund (siehe isOperatorQuestion in
 * fleet.ts) kann hier direkt beantwortet werden: Antwort als Kommentar
 * ablegen (author: operator), Task auf ready setzen, Dispatcher-Tick.
 *
 * Bewusst als Komposition aus drei Fetches im useAnswerQuestion-Hook
 * umgesetzt (kein atomarer /answer-Endpoint — Phase 4 vorbehalten).
 * Das Muster folgt useFixRedispatch in useControlData.ts.
 */
import { useState, type FormEvent } from "react";
import { de } from "../../i18n/de";
import { useAnswerQuestion } from "../../hooks/useControlData";

interface AnswerQuestionProps {
  taskId: string;
}

export function AnswerQuestion({ taskId }: AnswerQuestionProps) {
  const { busyId, doneIds, errorById, run } = useAnswerQuestion();
  const [text, setText] = useState("");

  const busy = busyId === taskId;
  const done = !!doneIds[taskId];
  const err = errorById[taskId] || "";

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const res = await run(taskId, text);
    if (res.ok) {
      setText("");
      // Erfolgsmeldung nach 2.5s ausblenden — der Board-Poll hat bis dahin
      // hoffentlich die neue blocking-Status gefetcht.
      window.setTimeout(() => {
        /* state-owned: doneIds bleibt true, bis die Eltern-Komponente
           aufgrund von Status-Übergang die Zeile fallen lässt */
      }, 2500);
    }
  };

  return (
    <div className="fleet-aq">
      <div className="fleet-aq-title">{de.fleet.answerTitle}</div>
      <form className="fleet-aq-row" onSubmit={submit}>
        <input
          className="fleet-aq-input"
          type="text"
          value={text}
          placeholder={de.fleet.answerPlaceholder}
          onChange={(e) => setText(e.target.value)}
          disabled={busy}
          aria-label={de.fleet.answerPlaceholder}
        />
        <button
          className="fleet-aq-btn"
          type="submit"
          disabled={busy || !text.trim()}
        >
          {busy ? de.fleet.answerBusy : de.fleet.answerSubmit}
        </button>
      </form>
      {done ? (
        <div className="fleet-aq-note">{de.fleet.answerDone}</div>
      ) : null}
      {err ? (
        <div className="fleet-aq-error" role="alert">
          {err}
        </div>
      ) : null}
    </div>
  );
}
