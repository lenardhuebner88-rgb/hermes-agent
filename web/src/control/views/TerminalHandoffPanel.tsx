import { useCallback, useState, type ReactNode } from "react";
import { AlertTriangle, CheckCircle2, ClipboardList, FileText, Play, X } from "lucide-react";

import { api, fetchJSON } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  buildPlanSpecDraft,
  defaultSlug,
  findingsFromError,
  LIVE_TEST_DEPTHS,
  stripAnsi,
  type LiveTestDepth,
} from "../lib/terminalHandoff";

interface TerminalHandoffPanelProps {
  target: { session: string; window: string } | null;
  /** Reads the current xterm selection (plain text) from the parent. */
  getSelection: () => string;
  onClose: () => void;
}

type Mode = "planspec" | "kanban";
type Source = "selection" | "tail";

interface ValidateResult {
  ok: boolean;
  disposition: string;
  findings: string[];
  would_block: boolean;
  freigabe: string;
}
interface IngestResult {
  ok: boolean;
  root_task_id: string;
  child_ids: string[];
  subtask_count: number;
  freigabe: string;
  live_test_depth: string;
}
interface TaskCreateResult {
  task: { id: string; title?: string; status?: string };
}
interface DispatchPreview {
  spawned: Array<[string, string, string]>;
  promoted: string[];
  reclaimed: string[];
}

const KANBAN = "/api/plugins/kanban";

function ResultBox({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "info";
  children: ReactNode;
}) {
  const cls =
    tone === "ok"
      ? "border-status-ok/30 bg-status-ok/10 text-status-ok"
      : tone === "warn"
        ? "border-status-warn/30 bg-status-warn/10 text-status-warn"
        : "border-line bg-surface-2 text-ink-2";
  return <div className={cn("rounded-card border p-3 text-xs", cls)}>{children}</div>;
}

export function TerminalHandoffPanel({ target, getSelection, onClose }: TerminalHandoffPanelProps) {
  const [mode, setMode] = useState<Mode>("planspec");
  const [source, setSource] = useState<Source>("selection");
  const [tailLines, setTailLines] = useState(120);
  const [title, setTitle] = useState("");
  const [liveTestDepth, setLiveTestDepth] = useState<LiveTestDepth>("smoke");
  const [captured, setCaptured] = useState("");
  const [draft, setDraft] = useState("");
  const [taskBody, setTaskBody] = useState("");

  const [validateResult, setValidateResult] = useState<ValidateResult | null>(null);
  const [ingestResult, setIngestResult] = useState<IngestResult | null>(null);
  const [taskResult, setTaskResult] = useState<TaskCreateResult | null>(null);
  const [dispatchPreview, setDispatchPreview] = useState<DispatchPreview | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(async (key: string, fn: () => Promise<void>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }, []);

  // Capture ONLY fills the working text — it never validates, ingests, creates
  // or dispatches (AC-2). It opens no flow on its own.
  const doCapture = useCallback(
    (sourceOverride?: Source, tailOverride?: number) =>
      run("capture", async () => {
        const effectiveSource = sourceOverride ?? source;
        const effectiveTailLines = tailOverride ?? tailLines;
        if (sourceOverride) setSource(sourceOverride);
        if (tailOverride !== undefined) setTailLines(effectiveTailLines);
        let text: string;
        if (effectiveSource === "selection") {
          text = stripAnsi(getSelection());
        } else {
          if (!target) throw new Error("Kein Terminal-Fenster gewählt.");
          const resp = await api.captureAgentTerminalWindow(target.session, target.window, -effectiveTailLines);
          text = stripAnsi(resp.content);
        }
        setCaptured(text);
        if (mode === "planspec") {
          setDraft(buildPlanSpecDraft(text, { title, liveTestDepth }));
        } else {
          setTaskBody(text);
        }
      }),
    [run, source, getSelection, target, tailLines, mode, title, liveTestDepth],
  );

  const rebuildDraft = useCallback(() => {
    setDraft(buildPlanSpecDraft(captured, { title, liveTestDepth }));
  }, [captured, title, liveTestDepth]);

  const doValidate = useCallback(
    () =>
      run("validate", async () => {
        const result = await fetchJSON<ValidateResult>(`${KANBAN}/planspecs/validate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: draft, slug: defaultSlug(title) }),
        });
        setValidateResult(result);
      }),
    [run, draft, title],
  );

  const doIngest = useCallback(
    () =>
      run("ingest", async () => {
        try {
          const result = await fetchJSON<IngestResult>(`${KANBAN}/planspecs/ingest-draft`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: draft, slug: defaultSlug(title), author: "dashboard" }),
          });
          setIngestResult(result);
        } catch (err) {
          const findings = findingsFromError(err);
          if (findings) {
            setValidateResult({
              ok: false,
              disposition: "block",
              findings,
              would_block: true,
              freigabe: "",
            });
            throw new Error("Ingest blockiert — siehe Validate-Ergebnis.");
          }
          throw err;
        }
      }),
    [run, draft, title],
  );

  const doCreateTask = useCallback(
    () =>
      run("task", async () => {
        const result = await fetchJSON<TaskCreateResult>(`${KANBAN}/tasks`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            title: title.trim() || "Terminal-Handoff Triage",
            body: taskBody,
            triage: true,
          }),
        });
        setTaskResult(result);
      }),
    [run, title, taskBody],
  );

  // Optional, clearly SEPARATE from real dispatch: always dry_run=true. There is
  // no control in this panel that calls /dispatch without dry_run — live
  // dispatch stays an operator action on the board (AC-2/AC-6).
  const doDispatchDryRun = useCallback(
    () =>
      run("dispatch", async () => {
        const result = await fetchJSON<DispatchPreview>(`${KANBAN}/dispatch?dry_run=true&max=8`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });
        setDispatchPreview(result);
      }),
    [run],
  );

  const hasText = mode === "planspec" ? draft.trim().length > 0 : taskBody.trim().length > 0;

  return (
    <div className="fixed inset-0 z-[60] flex items-stretch justify-center bg-surface-0/60 p-0 sm:items-center sm:p-4">{/* TOKEN-REVIEW: was bg-black/60 */}
      <div className="flex h-full w-full max-w-3xl flex-col overflow-hidden rounded-none border border-line bg-surface-1 shadow-2xl sm:h-auto sm:max-h-[90vh] sm:rounded-panel">
        <div className="flex items-center justify-between border-b border-line-soft px-4 py-3">
          <div>
            <p className="hc-eyebrow">Terminal → Handoff</p>
            <h2 className="text-sm font-semibold text-ink">Auswahl in PlanSpec oder Kanban überführen</h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Handoff schließen"
            className="rounded-card border border-line p-1.5 text-ink-2 hover:bg-surface-3"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex-1 space-y-3 overflow-auto p-4 text-sm text-ink-2">
          <div className="rounded-card border border-status-warn/20 bg-status-warn/10 p-3 text-xs text-status-warn">
            Opt-in: Auswahl/Capture füllt nur Text. Nichts wird erstellt, validiert, ingestet oder
            dispatcht ohne expliziten Klick. Kein Auto-Dispatch von Terminal-Output.
          </div>

          {/* Source */}
          <div className="grid gap-2 rounded-card border border-line bg-surface-2 p-3">
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <label className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="handoff-source"
                  checked={source === "selection"}
                  onChange={() => setSource("selection")}
                />
                Auswahl übernehmen
              </label>
              <label className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="handoff-source"
                  checked={source === "tail"}
                  onChange={() => setSource("tail")}
                />
                Letzte
                <input
                  type="number"
                  min={1}
                  max={5000}
                  value={tailLines}
                  onChange={(e) => setTailLines(Math.max(1, Math.min(5000, Number(e.target.value) || 1)))}
                  className="w-16 rounded-card border border-line bg-surface-2 px-1.5 py-0.5 text-ink"
                />
                Zeilen
              </label>
              <button
                type="button"
                onClick={() => void doCapture("selection")}
                disabled={busy !== null}
                className="ml-auto rounded-card border border-line bg-surface-2 px-3 py-1.5 text-xs text-ink hover:bg-surface-3 disabled:opacity-50"
              >
                {busy === "capture" ? "Übernehme…" : "Auswahl übernehmen"}
              </button>
              <button
                type="button"
                onClick={() => void doCapture("tail", 120)}
                disabled={busy !== null || !target}
                className="rounded-card border border-line bg-surface-2 px-3 py-1.5 text-xs text-ink hover:bg-surface-3 disabled:opacity-50"
              >
                Letzte 120 Zeilen
              </button>
              <button
                type="button"
                onClick={() => void doCapture()}
                disabled={busy !== null}
                className="rounded-card border border-live/50 bg-live/10 px-3 py-1.5 text-xs text-live hover:bg-live/20 disabled:opacity-50"
              >
                {mode === "planspec" ? "PlanSpec-Draft vorbereiten" : "Kanban-Triage-Task vorbereiten"}
              </button>
            </div>
            {captured && (
              <pre className="max-h-28 overflow-auto rounded-card border border-line bg-surface-2 p-2 text-[11px] text-ink-2">
                {captured.slice(0, 4000)}
              </pre>
            )}
          </div>

          {/* Mode tabs */}
          <div className="flex gap-1.5">
            <button
              type="button"
              onClick={() => setMode("planspec")}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-card border px-3 py-1.5 text-xs",
                mode === "planspec"
                  ? "border-live/60 bg-live/10 text-live"
                  : "border-line text-ink-2 hover:bg-surface-3",
              )}
            >
              <FileText className="h-3.5 w-3.5" />
              PlanSpec-Draft vorbereiten
            </button>
            <button
              type="button"
              onClick={() => setMode("kanban")}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-card border px-3 py-1.5 text-xs",
                mode === "kanban"
                  ? "border-live/60 bg-live/10 text-live"
                  : "border-line text-ink-2 hover:bg-surface-3",
              )}
            >
              <ClipboardList className="h-3.5 w-3.5" />
              Kanban-Triage-Task vorbereiten
            </button>
          </div>

          <div className="grid gap-2">
            <label className="grid gap-1 text-xs">
              <span className="text-ink-3">Titel</span>
              <input
                type="text"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder={mode === "planspec" ? "Kurztitel des Drafts" : "Kurztitel der Triage-Aufgabe"}
                className="rounded-card border border-line bg-surface-2 px-2 py-1.5 text-ink"
              />
            </label>

            {mode === "planspec" && (
              <label className="grid gap-1 text-xs">
                <span className="text-ink-3">live_test_depth</span>
                <select
                  value={liveTestDepth}
                  onChange={(e) => setLiveTestDepth(e.target.value as LiveTestDepth)}
                  className="rounded-card border border-line bg-surface-2 px-2 py-1.5 text-ink"
                >
                  {LIVE_TEST_DEPTHS.map((d) => (
                    <option key={d} value={d} className="bg-surface-1">
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          {mode === "planspec" ? (
            <div className="grid gap-2">
              <div className="flex items-center justify-between">
                <span className="text-xs text-ink-3">PlanSpec-Draft (editierbar)</span>
                <button
                  type="button"
                  onClick={rebuildDraft}
                  disabled={!captured}
                  className="rounded-card border border-line px-2 py-1 text-[11px] text-ink-2 hover:bg-surface-3 disabled:opacity-40"
                >
                  Aus Capture neu bauen
                </button>
              </div>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                rows={10}
                aria-label="PlanSpec-Draft"
                placeholder="Text übernehmen, dann erscheint hier ein PlanSpec-Draft mit freigabe: operator."
                className="w-full rounded-card border border-line bg-surface-2 p-2 font-mono text-[11px] text-ink"
              />
              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void doValidate()}
                  disabled={busy !== null || !hasText}
                  className="rounded-card border border-line bg-surface-2 px-3 py-1.5 text-xs text-ink hover:bg-surface-3 disabled:opacity-50"
                >
                  {busy === "validate" ? "Validiere…" : "Validieren"}
                </button>
                <button
                  type="button"
                  onClick={() => void doIngest()}
                  disabled={busy !== null || !hasText}
                  className="rounded-card border border-live/50 bg-live/10 px-3 py-1.5 text-xs text-live hover:bg-live/20 disabled:opacity-50"
                >
                  {busy === "ingest" ? "Ingest…" : "Ingest (held: operator)"}
                </button>
              </div>

              {validateResult && (
                <ResultBox tone={validateResult.would_block ? "warn" : "ok"}>
                  <div className="mb-1 flex items-center gap-1.5 font-semibold">
                    {validateResult.would_block ? (
                      <AlertTriangle className="h-3.5 w-3.5" />
                    ) : (
                      <CheckCircle2 className="h-3.5 w-3.5" />
                    )}
                    Validate-Ergebnis: {validateResult.disposition}
                    {validateResult.would_block ? " (würde blocken)" : " (ingestbar)"}
                  </div>
                  {validateResult.findings.length > 0 ? (
                    <ul className="ml-4 list-disc space-y-0.5">
                      {validateResult.findings.map((f, i) => (
                        <li key={i}>{f}</li>
                      ))}
                    </ul>
                  ) : (
                    <div>Keine Befunde — Draft ist sauber.</div>
                  )}
                </ResultBox>
              )}

              {ingestResult && (
                <ResultBox tone="ok">
                  <div className="mb-1 flex items-center gap-1.5 font-semibold">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Ingest-Ergebnis (held, freigabe: {ingestResult.freigabe || "operator"})
                  </div>
                  <div>
                    Chain / Root-Task: <span className="font-mono text-ink">{ingestResult.root_task_id}</span>
                  </div>
                  <div>
                    Subtask-IDs:{" "}
                    <span className="font-mono text-ink">
                      {ingestResult.child_ids.length ? ingestResult.child_ids.join(", ") : "—"}
                    </span>
                  </div>
                  <div className="mt-1 text-ink-3">
                    Held für Operator-Freigabe — es wurde nichts dispatcht.
                  </div>
                </ResultBox>
              )}
            </div>
          ) : (
            <div className="grid gap-2">
              <span className="text-xs text-ink-3">Triage-Aufgabentext (editierbar)</span>
              <textarea
                value={taskBody}
                onChange={(e) => setTaskBody(e.target.value)}
                rows={8}
                aria-label="Triage-Aufgabentext"
                placeholder="Text übernehmen, dann hier den Triage-Task-Inhalt anpassen."
                className="w-full rounded-card border border-line bg-surface-2 p-2 text-[12px] text-ink"
              />
              <button
                type="button"
                onClick={() => void doCreateTask()}
                disabled={busy !== null || !hasText}
                className="w-fit rounded-card border border-live/50 bg-live/10 px-3 py-1.5 text-xs text-live hover:bg-live/20 disabled:opacity-50"
              >
                {busy === "task" ? "Erstelle…" : "Triage-Task anlegen"}
              </button>
              {taskResult && (
                <ResultBox tone="ok">
                  <div className="mb-1 flex items-center gap-1.5 font-semibold">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Triage-Task angelegt
                  </div>
                  <div>
                    Task-ID: <span className="font-mono text-ink">{taskResult.task.id}</span>
                  </div>
                  <div className="mt-1 text-ink-3">
                    Status triage — wartet auf Triage/Promotion, kein Auto-Dispatch.
                  </div>
                </ResultBox>
              )}
            </div>
          )}

          {/* Optional dispatch preview — strictly SEPARATE from real dispatch. */}
          <div className="grid gap-2 rounded-card border border-line bg-surface-2 p-3">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-ink-2">Optionale Dispatch-Vorschau</span>
              <button
                type="button"
                onClick={() => void doDispatchDryRun()}
                disabled={busy !== null}
                className="inline-flex items-center gap-1.5 rounded-card border border-line px-2.5 py-1 text-[11px] text-ink-2 hover:bg-surface-3 disabled:opacity-50"
              >
                <Play className="h-3 w-3" />
                {busy === "dispatch" ? "Vorschau…" : "dispatch --dry-run"}
              </button>
            </div>
            <p className="text-[11px] text-ink-3">
              Nur Vorschau (<code>dry_run=true</code>) — dispatcht nichts. Echtes Dispatch erfolgt
              ausschließlich über das Board, getrennt von dieser Ansicht.
            </p>
            {dispatchPreview && (
              <ResultBox tone="info">
                <div className="mb-1 font-semibold">
                  Würde dispatchen: {dispatchPreview.spawned.length} Task(s) — nichts gestartet.
                </div>
                {dispatchPreview.spawned.length > 0 && (
                  <ul className="ml-4 list-disc space-y-0.5">
                    {dispatchPreview.spawned.map(([id, assignee], i) => (
                      <li key={i}>
                        <span className="font-mono text-ink">{id}</span> → {assignee || "—"}
                      </li>
                    ))}
                  </ul>
                )}
              </ResultBox>
            )}
          </div>

          {error && (
            <div className="flex items-start gap-1.5 rounded-card border border-status-alert/30 bg-status-alert/10 p-2 text-xs text-status-alert">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
