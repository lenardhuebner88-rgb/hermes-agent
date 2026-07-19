/**
 * FragenPanel — die volle Fragen-Ansicht der Jarvis-Shell (Sprint 2, Karte
 * S2.6): Expand aus „Wartet · dezent" heraus. Gleiche Daten wie die
 * Klassik-FragenSection (GET /api/agent-questions, status=open, newest-first)
 * — WartetPanel reicht seinen pollingStore-Snapshot per Props durch, kein
 * zweiter Poll, kein Fork der Datenquelle.
 *
 * Antworten läuft bewusst weiter über den bewährten Klassik-Pfad (Link auf
 * /control/projekte-klassisch pro Frage, dort sitzt FragenSection mit dem
 * answer-Endpunkt) — KEINE neue Antwort-Mechanik hier (Brief). Optionen,
 * Empfohlen-Marker und der KI-Vorschlag (suggestions[0] = Top-Rang, mit
 * Rationale + Provenienz) werden read-only gezeigt: Filter/Status wie
 * Klassik. ESC oder × schließt die Ansicht.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { AgentQuestionEvent } from "@/lib/api";
import { de } from "../i18n/de";
import { formatStandingAge } from "../views/projekte/derive";

const t = de.jarvis;
const tp = de.projekte;

export interface FragenPanelProps {
  /** Offene Fragen aus dem agent-questions-Poll (bereits open-gefiltert). */
  questions: ReadonlyArray<AgentQuestionEvent>;
  onClose: () => void;
}

export function FragenPanel({ questions, onClose }: FragenPanelProps) {
  // Live-Tick für „steht seit …" — dasselbe 15s-Idiom wie die Klassik.
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="jv-float jv-fragen" id="jv-fragen-panel" role="region" aria-label={t.fragenPanelTitle}>
      <div className="jv-ptitle jv-fragen-head">
        {t.fragenPanelTitle}{" "}
        <span style={{ color: "var(--faint)", letterSpacing: ".05em" }}>{questions.length}</span>
        <button type="button" className="jv-fclose" onClick={onClose} aria-label={t.fragenClose}>
          ×
        </button>
      </div>

      <div className="jv-fbody">
        {questions.length === 0 ? <p className="jv-qloading">{tp.fragenEmpty}</p> : null}
        {questions.map((question) => (
          <FrageCard key={question.id} question={question} nowMs={nowMs} />
        ))}
      </div>
    </div>
  );
}

function FrageCard({ question, nowMs }: { question: AgentQuestionEvent; nowMs: number }) {
  // Slice-2-Idiom der Klassik: Top-Vorschlag = suggestions[0] (Array-Ordnung
  // = Ranking); null/leer = kein Vorschlag, kein Platzhalter.
  const topSuggestion =
    question.suggestions && question.suggestions.length > 0 ? question.suggestions[0] : null;

  return (
    <div className="jv-frage" data-testid={`jv-frage-${question.id}`}>
      <p className="jv-frage-meta">
        <span className="jv-frage-kind">{question.kind?.trim() || "agent"}</span>
        {" · "}
        <span>
          {question.session}:{question.window}
        </span>
        {" · "}
        <span data-testid={`jv-frage-age-${question.id}`}>{formatStandingAge(question.ts, nowMs)}</span>
      </p>

      <p className="jv-frage-text">{question.question_text}</p>

      {/* Optionen read-only (Antwort nur über den Klassik-Pfad): dieselbe
          Marker-Logik wie FragenSection — Empfohlen in cyan (Jarvis-Primär-
          kanal), KI-Top-Vorschlag in amber. */}
      {question.options.length > 0 ? (
        <div className="jv-fopts" role="group" aria-label={tp.fragenOptionsLabel}>
          {question.options.map((opt) => {
            const isTopSuggestion =
              topSuggestion !== null && String(opt.nr) === String(topSuggestion.nr);
            return (
              <div
                className={
                  isTopSuggestion ? "jv-fopt jv-sug" : opt.recommended ? "jv-fopt jv-rec" : "jv-fopt"
                }
                key={String(opt.nr)}
              >
                <span className="jv-fnr">{opt.nr}</span>
                <span className="jv-flabel">{opt.label}</span>
                {isTopSuggestion ? (
                  <span className="jv-ftag jv-ftag-sug">{tp.fragenSuggestedMarker}</span>
                ) : opt.recommended ? (
                  <span className="jv-ftag">{tp.fragenRecommended}</span>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}

      {topSuggestion ? (
        <div className="jv-fsug" data-testid={`jv-frage-suggestion-${question.id}`}>
          <b>{tp.fragenSuggestionTitle(topSuggestion.nr)}</b>
          <p>{topSuggestion.rationale}</p>
          {question.suggested_by || question.suggest_confidence ? (
            <span className="jv-fsug-prov">
              {[
                question.suggested_by ? tp.fragenSuggestedBy(question.suggested_by) : null,
                question.suggest_confidence
                  ? tp.fragenSuggestionConfidence(question.suggest_confidence)
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          ) : null}
        </div>
      ) : null}

      <Link
        className="jv-fanswer"
        to="/control/projekte-klassisch"
        aria-label={t.wartetAnswer(question.question_text)}
      >
        {t.fragenAnswerLink}
      </Link>
    </div>
  );
}
