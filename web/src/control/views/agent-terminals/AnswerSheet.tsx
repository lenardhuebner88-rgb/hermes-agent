/**
 * Antwort-Sheet for open agent-questions (Frage-Assistent P0c).
 * Shows the newest open question, option buttons + keyboard 1–9 / y/n,
 * POST answer, then advances to the next open question or empty state.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";

import { api, type AgentQuestionEvent } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Overlay } from "../../components/Overlay";
import { Eyebrow } from "../../components/primitives";
import { extractDetail } from "../../hooks/internal";

function formatStandingAge(ts: string, nowMs: number = Date.now()): string {
  const ms = Date.parse(ts);
  if (!Number.isFinite(ms)) return "steht seit kurzem";
  const sec = Math.max(0, Math.round((nowMs - ms) / 1000));
  if (sec < 60) return `steht seit ${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `steht seit ${min} min`;
  const h = Math.round(min / 60);
  return `steht seit ${h} h`;
}

function shortCwd(cwd: string | null | undefined): string {
  if (!cwd) return "—";
  const trimmed = cwd.trim();
  if (trimmed.length <= 42) return trimmed;
  return `…${trimmed.slice(-40)}`;
}

function isSupersededError(err: unknown): boolean {
  const msg = err instanceof Error ? err.message : String(err);
  if (!msg.startsWith("409")) return false;
  return /superseded|not-open/i.test(msg);
}

export type AnswerSheetProps = {
  questions: AgentQuestionEvent[];
  onClose: () => void;
  /** Reload open questions after answer / refresh. */
  reload: () => void | Promise<unknown>;
  /** Optimistic list update after a successful answer. */
  updateData?: (
    updater: (prev: { questions: AgentQuestionEvent[] } | null) => {
      questions: AgentQuestionEvent[];
    } | null,
  ) => void;
  /** Deep-link focus: show this open event first when present. */
  focusId?: number | null;
  /** Deep-link hit a closed/missing event — show hint instead of options. */
  closedHint?: string | null;
};

export function AnswerSheet({
  questions,
  onClose,
  reload,
  updateData,
  focusId = null,
  closedHint = null,
}: AnswerSheetProps) {
  const ordered = useMemo(() => {
    if (focusId == null) return questions;
    const idx = questions.findIndex((q) => q.id === focusId);
    if (idx < 0) return questions;
    return [questions[idx], ...questions.filter((_, i) => i !== idx)];
  }, [questions, focusId]);
  const current = ordered[0] ?? null;
  const [sending, setSending] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  // Error state carries the question id it belongs to: the 5s poll can swap
  // the head question WHILE a POST is in flight (backend claims at POST start,
  // verify-sleep makes the request take 2-4s), and a stale failure must never
  // render under the next question (Codex-Lens I1 #1).
  const [errorState, setErrorState] = useState<{
    id: number;
    line: string;
    superseded: boolean;
  } | null>(null);
  const [verifyHint, setVerifyHint] = useState<string | null>(null);
  const inFlightRef = useRef<number | null>(null);

  // Clear stale error when the head question changes — but never touch
  // `sending` here: a mid-flight head swap must NOT re-enable the buttons
  // while our POST is still running. Keep verifyHint so a verified:false
  // note remains visible while the next (or empty) state shows.
  useEffect(() => {
    if (inFlightRef.current !== null) return;
    setErrorState(null);
  }, [current?.id]);

  // Live-ticking age ("steht seit X min") — recompute every 15s.
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 15_000);
    return () => window.clearInterval(id);
  }, []);

  const standingAge = useMemo(
    () => (current ? formatStandingAge(current.ts, nowMs) : ""),
    [current, nowMs],
  );

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
    async (answer: string) => {
      if (!current || sending) return;
      const target = current;
      setSending(true);
      inFlightRef.current = target.id;
      setErrorState(null);
      setVerifyHint(null);
      try {
        const result = await api.answerAgentQuestion(target.id, answer);
        removeQuestion(target.id);
        if (result.verified === false) {
          setVerifyHint("Antwort gesendet — Bestätigung im Terminal ausstehend");
        }
        void reload();
      } catch (err) {
        if (isSupersededError(err)) {
          setErrorState({ id: target.id, line: "Frage hat sich geändert", superseded: true });
        } else {
          setErrorState({ id: target.id, line: extractDetail(err), superseded: false });
        }
      } finally {
        inFlightRef.current = null;
        setSending(false);
      }
    },
    [current, sending, removeQuestion, reload],
  );

  // Keyboard: map 1–9 / y / n onto options[].nr while sheet is open.
  useEffect(() => {
    if (!current || sending) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const tag = target?.tagName?.toLowerCase();
      if (tag === "input" || tag === "textarea" || target?.isContentEditable) return;
      const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
      if (!/^[1-9yn]$/.test(key)) return;
      const match = current.options.find((opt) => String(opt.nr) === key);
      if (!match) return;
      e.preventDefault();
      void submitAnswer(String(match.nr));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [current, sending, submitAnswer]);

  const onRefresh = () => {
    setErrorState(null);
    void reload();
  };

  // A stale error (belonging to a question that is no longer head) is hidden;
  // when the list is empty we keep it visible — "Frage hat sich geändert" +
  // Aktualisieren is exactly what the operator needs then.
  const visibleError = errorState && (!current || errorState.id === current.id) ? errorState : null;

  return (
    <Overlay
      onClose={onClose}
      ariaLabel="Frage beantworten"
      maxWidthClassName="max-w-md"
      closeDisabled={sending}
    >
      <div className="flex flex-col gap-3" data-testid="answer-sheet">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Eyebrow>Agent-Frage</Eyebrow>
            {current ? (
              <p className="mt-0.5 text-[11px] text-ink-3">
                <span className="font-medium text-ink-2">{current.kind?.trim() || "agent"}</span>
                {" · "}
                <span className="font-mono">
                  {current.session}:{current.window}
                </span>
                {" · "}
                <span className="font-mono" title={current.cwd ?? undefined}>
                  {shortCwd(current.cwd)}
                </span>
                {" · "}
                <span data-testid="answer-sheet-age">{standingAge}</span>
              </p>
            ) : (
              <h2 className="text-sm font-semibold text-ink">
                {closedHint ? "Frage nicht mehr offen" : "Keine offenen Fragen"}
              </h2>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={sending}
            aria-label="Schließen"
            className="shrink-0 rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {!current && (
          <p className="text-sm text-ink-3" data-testid="answer-sheet-closed-hint">
            {closedHint
              || "Keine offenen Fragen. Das Sheet kann geschlossen werden."}
          </p>
        )}

        {current && closedHint && focusId != null && current.id !== focusId && (
          // Deep-link target is gone but OTHER questions are open: without this
          // banner the sheet would silently show a different question and the
          // operator could answer the wrong one (Codex review, I3).
          <p
            className="rounded-card border border-status-warn/40 bg-status-warn/10 px-3 py-2 text-[12px] text-status-warn"
            data-testid="answer-sheet-deeplink-hint"
          >
            {`Die verlinkte Frage ist ${closedHint} — unten steht die nächste offene Frage.`}
          </p>
        )}

        {current && (
          <>
            <div className="max-h-[min(40dvh,18rem)] overflow-y-auto overscroll-contain rounded-card border border-line bg-surface-2 p-3">
              <p className="whitespace-pre-wrap text-sm leading-5 text-ink">
                {current.question_text}
              </p>
            </div>

            <div className="flex flex-col gap-2" role="group" aria-label="Antwortoptionen">
              {current.options.map((opt) => (
                <button
                  key={String(opt.nr)}
                  type="button"
                  disabled={sending}
                  onClick={() => void submitAnswer(String(opt.nr))}
                  className={cn(
                    "flex min-h-11 w-full items-center gap-3 rounded-card border border-line bg-surface-2 px-3 py-2 text-left text-sm text-ink transition",
                    "hover:border-live/40 hover:bg-surface-3 disabled:cursor-not-allowed disabled:opacity-60",
                  )}
                >
                  <span className="inline-flex h-7 min-w-7 shrink-0 items-center justify-center rounded-card border border-line bg-surface-1 font-mono text-xs font-semibold text-ink">
                    {opt.nr}
                  </span>
                  <span className="min-w-0 flex-1">{opt.label}</span>
                  {opt.recommended && (
                    <span className="shrink-0 rounded-full border border-status-warn/40 bg-status-warn/10 px-2 py-0.5 text-micro font-medium text-status-warn">
                      Empfohlen
                    </span>
                  )}
                </button>
              ))}
            </div>

            {sending && (
              <p role="status" className="text-[11px] text-ink-3">
                Sende…
              </p>
            )}
          </>
        )}

        {verifyHint && (
          <p role="status" className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-[12px] text-status-warn">
            {verifyHint}
          </p>
        )}

        {visibleError && (
          <div
            role="alert"
            className={cn(
              "rounded-card border px-3 py-2 text-[12px]",
              visibleError.superseded
                ? "border-status-warn/30 bg-status-warn/10 text-status-warn"
                : "border-status-alert/30 bg-status-alert/10 text-status-alert",
            )}
          >
            <p>{visibleError.line}</p>
            <button
              type="button"
              onClick={onRefresh}
              className="mt-2 inline-flex min-h-11 items-center rounded-card border border-line bg-surface-2 px-3 text-sm text-ink-2 hover:bg-surface-3"
            >
              Aktualisieren
            </button>
          </div>
        )}
      </div>
    </Overlay>
  );
}
