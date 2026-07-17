/**
 * AnswerQuestion (S6) — Inline-Form zum Beantworten einer Operator-Frage.
 *
 * Ein Task mit operator_question-Blockgrund (siehe isOperatorQuestion in
 * fleet.ts) kann hier direkt beantwortet werden: Antwort + Unblock atomar
 * schreiben und danach einen best-effort Dispatcher-Tick auslösen.
 *
 * Die atomare /answer-Transition verhindert Teilwrites bei Zweit-Tab-Races.
 */
import { useState, type FormEvent } from "react";
import { de } from "../../i18n/de";
import { useAnswerQuestion } from "../../hooks/taskActions";

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
