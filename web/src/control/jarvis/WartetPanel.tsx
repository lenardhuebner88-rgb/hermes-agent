/**
 * WartetPanel — „Wartet · dezent" der Jarvis-Shell an den ECHTEN offenen
 * Agentenfragen (GET /api/agent-questions über denselben pollingStore-Key
 * wie FragenSection/AnswerSheet — dedupliziert, keine neue Infrastruktur).
 *
 * Bleibt die dezente 3-Zeilen-Variante (A4: Entscheidungen NUR dezent).
 * Seit S2.6 führt ein Expand-Toggle zur vollen Fragen-Ansicht (FragenPanel,
 * dieselben Daten). Tap auf ANTWORTEN öffnet weiterhin die bestehende
 * Beantwortung im klassischen Tab (/control/projekte-klassisch, dort sitzt
 * FragenSection mit dem answer-Endpunkt) — bewusst KEINE neue Antwort-
 * Mechanik hier (Brief). Fehler werden inline gezeigt (ReceiptsFeed-Idiom),
 * nie still.
 */
import { useState } from "react";
import { Link } from "react-router-dom";

import { de } from "../i18n/de";
import { useAgentQuestions } from "../hooks/useControlData";
import { FragenPanel } from "./FragenPanel";

const t = de.jarvis;

/** Wie viele Fragen die dezente Leiste maximal zeigt (A4-LIVE: 3 Zeilen). */
const WARTET_MAX_ROWS = 3;

export function WartetPanel() {
  const questions = useAgentQuestions();
  const [fragenOpen, setFragenOpen] = useState(false);
  const open = questions.data?.questions ?? [];
  const shown = open.slice(0, WARTET_MAX_ROWS);
  const extra = open.length - shown.length;
  // Der Expand braucht geladene Daten und mindestens eine Frage; bei Fehler/
  // Erstladen bleibt nur die dezente Zeile bzw. der Fehler-/Ladehinweis.
  const canExpand = !questions.error && questions.data !== null && open.length > 0;

  return (
    <>
      <div className="jv-ptitle">
        WARTET · DEZENT{" "}
        <span style={{ float: "right", color: "var(--faint)", letterSpacing: ".05em" }}>
          {open.length}
        </span>
      </div>

      {questions.error ? (
        <p className="jv-qerror" role="alert">
          {t.wartetError}
        </p>
      ) : null}

      {!questions.error && questions.data === null ? (
        <p className="jv-qloading">{t.wartetLoading}</p>
      ) : null}

      {!questions.error && questions.data !== null && open.length === 0 ? (
        <div className="jv-quietempty">
          ✓ Nichts wartet — Estate ruhig.
          <span>Neue Entscheidungen erscheinen hier dezent, nie als Popup.</span>
        </div>
      ) : null}

      {shown.map((question) => (
        <div className="jv-qrow" key={question.id} data-testid={`jv-wartet-row-${question.id}`}>
          <span className="jv-rd" aria-hidden="true" />
          <span className="jv-tx" title={question.question_text}>
            {question.question_text}
          </span>
          <Link
            className="jv-ok"
            to="/control/projekte-klassisch"
            aria-label={t.wartetAnswer(question.question_text)}
          >
            ANTWORTEN
          </Link>
        </div>
      ))}

      {canExpand ? (
        <button
          type="button"
          className="jv-expand"
          aria-expanded={fragenOpen}
          aria-controls="jv-fragen-panel"
          onClick={() => setFragenOpen((value) => !value)}
        >
          {fragenOpen ? t.wartetCollapse : extra > 0 ? t.wartetExpand(extra) : t.wartetExpandAll}
        </button>
      ) : null}

      {fragenOpen && canExpand ? (
        <FragenPanel questions={open} onClose={() => setFragenOpen(false)} />
      ) : null}
    </>
  );
}
