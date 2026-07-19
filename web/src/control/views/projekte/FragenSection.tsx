/**
 * FragenSection — offene Agentenfragen im Projekte-Tab (Feature A, Slice 1+2).
 *
 * Listet ALLE offenen Fragen aus dem Frage-Assistent-Store und beantwortet sie
 * mit einem Tap über den bestehenden answer-Endpunkt — ohne Discord, ohne
 * Terminal, ohne Agent-Terminals-Tab. Die Antwort ist IMMER eine Options-Wahl:
 * Freitext lehnt das Backend ab (`free-text-not-supported`), deshalb gibt es
 * hier bewusst kein Textfeld.
 *
 * Aufbau folgt SessionsSection (Eyebrow-Header + Rows); Options-Buttons,
 * Empfohlen-Marker, optimistisches Entfernen und 409/superseded-Handling
 * folgen dem gelesenen AnswerSheet-Idiom (kein Fork). Slice 2: zeigt eine
 * vorhandene KI-Antwort-Empfehlung (`suggestions[0]` = Top-Rang) bronze mit
 * Rationale unter den Optionen und sendet `via_suggestion` nur beim Tap auf
 * DIESE Option — jede andere Wahl geht ohne das Feld raus (der Server
 * stempelt dann selbst `suggested_edited`/`operator_free`). Kein Keyboard-Map:
 * bei N Fragen gleichzeitig ist eine globale 1–9-Belegung mehrdeutig — der
 * Kontrakt hier ist Tap/Klick.
 */
import { useCallback, useEffect, useState } from "react";

import { api, type AgentQuestionEvent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../../components/primitives";
import { extractDetail } from "../../hooks/internal";
import { de } from "../../i18n/de";
import { formatStandingAge } from "./derive";

const t = de.projekte;

/** AnswerSheet-Idiom: fetchJSON wirft `Error("409: {...}")` — superseded/not-open. */
function isSupersededError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  if (!msg.startsWith("409")) return false;
  return /superseded|not-open/i.test(msg);
}

/** Slice 2: fetchJSON wirft `Error("400: {...}")` — via_suggestion verweist auf
 *  keine gespeicherte Suggestion (z. B. stale Poll-Snapshot). Nicht still
 *  schlucken: Fehler unter der Frage zeigen, Refresh-Knopf bleibt. */
function isInvalidSuggestionError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  return msg.startsWith("400") && msg.includes("invalid-suggestion");
}

type AnswerError = { id: number; line: string; superseded: boolean };

export interface FragenSectionProps {
  /** Offene Fragen aus GET /api/agent-questions?status=open (newest-first). */
  questions: ReadonlyArray<AgentQuestionEvent>;
  /** Fetch-Fehler des agent-questions-Polls — inline in der Sektion
   *  (ReceiptsFeed-Idiom: die Sektion fehlt bei Fehler nie komplett). */
  error?: boolean;
  /** Reload offener Fragen nach Antwort / Aktualisieren. */
  reload: () => void | Promise<unknown>;
  /** Optimistisches Entfernen beantworteter Fragen aus dem Poll-Snapshot. */
  updateData?: (
    updater: (prev: { questions: AgentQuestionEvent[] } | null) => {
      questions: AgentQuestionEvent[];
    } | null,
  ) => void;
}

export function FragenSection({ questions, error = false, reload, updateData }: FragenSectionProps) {
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Eine Antwort gleichzeitig (AnswerSheet-Idiom): die id sperrt alle Options-
  // Buttons; der Fehler trägt die Frage-id und rendert unter IHRER Zeile.
  const [sendingId, setSendingId] = useState<number | null>(null);
  const [answerError, setAnswerError] = useState<AnswerError | null>(null);
  const [verifyHint, setVerifyHint] = useState<string | null>(null);

  // Live-Tick für "steht seit …" — recompute every 15s (AnswerSheet-Idiom).
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  const removeQuestion = useCallback(
    (id: number) => {
      updateData?.((prev) => {
        if (!prev) return prev;
        return { questions: prev.questions.filter((q) => q.id !== id) };
      });
    },
    [updateData],
  );

  const submitAnswer = useCallback(
    async (question: AgentQuestionEvent, answer: string, viaSuggestion?: number) => {
      if (sendingId !== null) return;
      setSendingId(question.id);
      setAnswerError(null);
      setVerifyHint(null);
      try {
        const result = await api.answerAgentQuestion(question.id, answer, viaSuggestion);
        removeQuestion(question.id);
        if (result.verified === false) {
          setVerifyHint(t.fragenVerifyHint);
        }
        void reload();
      } catch (err) {
        if (isSupersededError(err)) {
          setAnswerError({ id: question.id, line: t.fragenSuperseded, superseded: true });
        } else if (isInvalidSuggestionError(err)) {
          setAnswerError({ id: question.id, line: t.fragenInvalidSuggestion, superseded: false });
        } else {
          setAnswerError({ id: question.id, line: extractDetail(err), superseded: false });
        }
      } finally {
        setSendingId(null);
      }
    },
    [sendingId, removeQuestion, reload],
  );

  const onRefresh = () => {
    setAnswerError(null);
    void reload();
  };

  return (
    <section aria-label={t.fragenTitle} className="space-y-3">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <Eyebrow>{t.fragenEyebrow}</Eyebrow>
          <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.fragenTitle}</h3>
        </div>
        {questions.length > 0 ? (
          <span className="font-data text-micro text-ink-3">{t.fragenCount(questions.length)}</span>
        ) : null}
      </header>

      {error ? (
        <div
          role="alert"
          className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"
        >
          {t.fragenError}
        </div>
      ) : null}

      {/* Leer ≠ Fehler: ruhiger Einzeiler (Empty-State-Doktrin), aber nicht
          neben einer Fetch-Warnung — dann wäre "keine Fragen" eine Lüge. */}
      {!error && questions.length === 0 ? (
        <p className="text-sec text-ink-3">{t.fragenEmpty}</p>
      ) : null}

      {questions.length > 0 ? (
        <ul className="space-y-2">
          {questions.map((question) => (
            <FrageRow
              key={question.id}
              question={question}
              nowMs={nowMs}
              sending={sendingId !== null}
              sendingThis={sendingId === question.id}
              answerError={answerError?.id === question.id ? answerError : null}
              onAnswer={(answer, viaSuggestion) => void submitAnswer(question, answer, viaSuggestion)}
              onRefresh={onRefresh}
            />
          ))}
        </ul>
      ) : null}

      {verifyHint ? (
        <p
          role="status"
          className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-micro text-status-warn"
        >
          {verifyHint}
        </p>
      ) : null}
    </section>
  );
}

function FrageRow({
  question,
  nowMs,
  sending,
  sendingThis,
  answerError,
  onAnswer,
  onRefresh,
}: {
  question: AgentQuestionEvent;
  nowMs: number;
  /** Eine Antwort läuft (irgendwo) — alle Options-Buttons gesperrt. */
  sending: boolean;
  /** DIESE Frage wird gerade beantwortet — "Sende …" unter dieser Zeile. */
  sendingThis: boolean;
  answerError: AnswerError | null;
  onAnswer: (answer: string, viaSuggestion?: number) => void;
  onRefresh: () => void;
}) {
  // Slice 2: Top-Vorschlag = suggestions[0] (Array-Ordnung = Ranking). null
  // oder leer = heutiges Verhalten, kein Platzhalter, keine leere Badge.
  const topSuggestion =
    question.suggestions && question.suggestions.length > 0 ? question.suggestions[0] : null;
  return (
    <li
      className="rounded-card border border-line-soft bg-surface-2 p-3"
      data-testid={`frage-row-${question.id}`}
    >
      <p className="text-micro text-ink-3">
        <span className="font-medium text-ink-2">{question.kind?.trim() || "agent"}</span>
        {" · "}
        <span className="font-data">
          {question.session}:{question.window}
        </span>
        {" · "}
        <span data-testid={`frage-age-${question.id}`}>{formatStandingAge(question.ts, nowMs)}</span>
      </p>

      <p className="mt-1.5 whitespace-pre-wrap text-sm leading-5 text-ink">
        {question.question_text}
      </p>

      {/* Antwort ist IMMER eine Options-Wahl (Freitext backend-verboten).
          Buttons: AnswerSheet-Idiom — 44px mobil (min-h-11), Desktop-Dichte
          ab tab; Empfohlen und KI-Top-Vorschlag in bronze (interaktiver
          Primär-Kanal), kein Freitext-Feld. via_suggestion geht nur beim
          Tap auf die Top-vorgeschlagene Option raus. */}
      {question.options.length > 0 ? (
        <div className="mt-2.5 flex flex-col gap-2" role="group" aria-label={t.fragenOptionsLabel}>
          {question.options.map((opt) => {
            const isTopSuggestion =
              topSuggestion !== null && String(opt.nr) === String(topSuggestion.nr);
            return (
              <button
                key={String(opt.nr)}
                type="button"
                disabled={sending}
                onClick={() =>
                  onAnswer(String(opt.nr), isTopSuggestion ? topSuggestion.nr : undefined)
                }
                className={cn(
                  "flex min-h-11 w-full items-center gap-3 rounded-card border px-3 py-2 text-left text-sm transition tab:min-h-9",
                  "focus-visible:outline-2 focus-visible:outline-bronze disabled:cursor-not-allowed disabled:opacity-60",
                  opt.recommended || isTopSuggestion
                    ? "border-bronze/50 bg-bronze/10 text-ink hover:border-bronze hover:bg-bronze/15"
                    : "border-line bg-surface-2 text-ink hover:border-bronze/40 hover:bg-surface-3",
                )}
              >
                <span
                  className={cn(
                    "inline-flex h-7 min-w-7 shrink-0 items-center justify-center rounded-card border font-data text-xs font-semibold",
                    opt.recommended || isTopSuggestion
                      ? "border-bronze/50 bg-bronze/10 text-bronze-hi"
                      : "border-line bg-surface-1 text-ink",
                  )}
                >
                  {opt.nr}
                </span>
                <span className="min-w-0 flex-1">{opt.label}</span>
                {isTopSuggestion ? (
                  <span className="shrink-0 text-micro font-medium text-bronze-hi">
                    {t.fragenSuggestedMarker}
                  </span>
                ) : opt.recommended ? (
                  <span className="shrink-0 text-micro font-medium text-bronze-hi">
                    {t.fragenRecommended}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : null}

      {/* Slice 2: Top-Vorschlag unter den Optionen — bronze (Token, kein
          Chip: DESIGN.md Kanal-Trennung), Rationale, Provenienz klein. */}
      {topSuggestion ? (
        <div
          className="mt-2 rounded-card border border-bronze/30 bg-bronze/5 px-3 py-2"
          data-testid={`frage-suggestion-${question.id}`}
        >
          <p className="text-micro font-medium text-bronze-hi">
            {t.fragenSuggestionTitle(topSuggestion.nr)}
          </p>
          <p className="mt-1 whitespace-pre-wrap text-sec text-ink-2">
            {topSuggestion.rationale}
          </p>
          {question.suggested_by || question.suggest_confidence ? (
            <p className="mt-1 text-micro text-ink-3">
              {[
                question.suggested_by ? t.fragenSuggestedBy(question.suggested_by) : null,
                question.suggest_confidence
                  ? t.fragenSuggestionConfidence(question.suggest_confidence)
                  : null,
              ]
                .filter(Boolean)
                .join(" · ")}
            </p>
          ) : null}
        </div>
      ) : null}

      {sendingThis ? (
        <p role="status" className="mt-2 text-micro text-ink-3">
          {t.fragenSending}
        </p>
      ) : null}

      {answerError ? (
        <div
          role="alert"
          className={cn(
            "mt-2 rounded-card border px-3 py-2 text-micro",
            answerError.superseded
              ? "border-status-warn/30 bg-status-warn/10 text-status-warn"
              : "border-status-alert/30 bg-status-alert/10 text-status-alert",
          )}
        >
          <p>{answerError.line}</p>
          <button
            type="button"
            onClick={onRefresh}
            className="mt-2 inline-flex min-h-11 items-center rounded-card border border-line bg-surface-2 px-3 text-sm text-ink-2 hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-bronze tab:min-h-9"
          >
            {t.fragenRefresh}
          </button>
        </div>
      ) : null}
    </li>
  );
}
