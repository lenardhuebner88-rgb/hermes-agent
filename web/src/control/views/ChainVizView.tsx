import { useCallback, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { AlertTriangle, Check, Loader2, OctagonX, Plus, X } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import { useBoard, useChainActions, useChainGraph, useHermesChainCosts, useHermesReviewVerdicts, useHermesWorkers, useRunInspect, useTaskAction } from "../hooks/useControlData";
import { buildChains } from "../lib/fleet";
import { fmtAge, fmtTokens, formatEffectiveCost, nowSec, workerSortRank } from "../lib/derive";
import { Hero } from "../components/Hero";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { de } from "../i18n/de";
import { ChainListPanel } from "./ketten/ChainListPanel";
import { KettenGraph } from "./ketten/KettenGraph";
import type { ReviewRunState } from "./ketten/ChainNodeCard";
import type { ChainGraphNode, Worker } from "../lib/types";
import type { ChainCostsResponse } from "../lib/schemas";
import type { WorkerActionKey } from "../components/WorkerCard";
import type { ChainCancelResult, ChainTaskCreatePayload, ChainTaskCreateResult } from "../hooks/useControlData";
import { fetchJSON } from "@/lib/api";

const CHAIN_ADD_DEFAULT_ASSIGNEE = "coder";
const CHAIN_ADD_LANE_OPTIONS = ["coder", "coder-claude", "premium", "verifier", "research", "admin"] as const;
const CHAIN_CANCEL_OPEN_STATUSES = new Set<ChainGraphNode["status"]>(["triage", "todo", "scheduled", "ready", "review"]);

export interface ChainCancelImpact {
  total: number;
  running: number;
  heldOpen: number;
  skipped: number;
}

export interface ChainTaskDraft {
  title: string;
  body: string;
  assignee: string;
  parentId: string;
  park: boolean;
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function summarizeChainCancelImpact(nodes: ChainGraphNode[]): ChainCancelImpact {
  let running = 0;
  let heldOpen = 0;
  let skipped = 0;
  for (const node of nodes) {
    if (node.status === "running") running += 1;
    else if (CHAIN_CANCEL_OPEN_STATUSES.has(node.status)) heldOpen += 1;
    else skipped += 1;
  }
  return { total: nodes.length, running, heldOpen, skipped };
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function chainAssigneeOptions(nodes: ChainGraphNode[]): string[] {
  const options: string[] = [];
  const seen = new Set<string>();
  const push = (value: string | null | undefined) => {
    const trimmed = (value ?? "").trim();
    if (!trimmed || seen.has(trimmed)) return;
    seen.add(trimmed);
    options.push(trimmed);
  };
  for (const node of nodes) push(node.assignee);
  for (const option of CHAIN_ADD_LANE_OPTIONS) push(option);
  return options;
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function initialChainTaskDraft(rootId: string, nodes: ChainGraphNode[]): ChainTaskDraft {
  const root = nodes.find((node) => node.id === rootId);
  const assignee = root?.assignee?.trim() || nodes.find((node) => node.assignee?.trim())?.assignee?.trim() || CHAIN_ADD_DEFAULT_ASSIGNEE;
  return { title: "", body: "", assignee, parentId: rootId, park: true };
}

// eslint-disable-next-line react-refresh/only-export-components -- pure helper co-located for unit tests (HMR-only rule)
export function buildChainTaskCreatePayload(draft: ChainTaskDraft): ChainTaskCreatePayload | null {
  const title = draft.title.trim();
  const parentId = draft.parentId.trim();
  if (!title || !parentId) return null;
  const body = draft.body.trim();
  const assignee = draft.assignee.trim();
  return {
    title,
    ...(body ? { body } : {}),
    ...(assignee ? { assignee } : {}),
    parents: [parentId],
    park: draft.park,
  };
}

// ── Chain summary (progress from graph nodes, cost truth from chain-costs) ──

interface ChainSummaryProps {
  nodes: ChainGraphNode[];
  rootId: string;
  costs?: ChainCostsResponse | null;
  costsLoading?: boolean;
  costsError?: string;
}

function ChainSummary({ nodes, rootId: _rootId, costs, costsLoading = false, costsError }: ChainSummaryProps) {
  // Derive progress by folding over the already-loaded graph nodes. B5: money,
  // tokens and run count come only from GET /tasks/:id/chain-costs, the
  // server-side canonical rollup also used by the Flow receipt rail.
  const totals = useMemo(() => {
    let done = 0;
    let running = 0;
    let waiting = 0;

    for (const n of nodes) {
      if (n.status === "done") done++;
      else if (n.status === "running") running++;
      else waiting++;
    }

    const total = nodes.length;
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    return { done, running, waiting, total, pct };
  }, [nodes]);

  if (nodes.length === 0) return null;

  const costTotals = costs?.totals;
  const costTokens = costTotals ? costTotals.input_tokens + costTotals.output_tokens : 0;
  const { text: costText } = costTotals
    ? formatEffectiveCost({
        cost_usd: costTotals.cost_usd,
        cost_effective_usd: costTotals.cost_effective_usd,
        tokens: costTokens,
      })
    : { text: costsLoading ? "…" : "—" };

  return (
    <div
      className="min-w-0 rounded-[14px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-[18px] py-4 shadow-[var(--hc-elev-1)]"
    >
      {/* Eyebrow */}
      <p className="hc-eyebrow">{de.ketten.summaryEyebrow}</p>

      {/* Progress bar */}
      <div className="mt-2.5">
        <div className="flex items-center justify-between gap-2">
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-[rgba(26,29,40,.06)]">
            <div
              className="h-full rounded-full bg-[var(--hc-aurora)]"
              style={{ width: `${totals.pct}%` }}
            />
          </div>
          <span className="hc-mono shrink-0 text-[13px] font-semibold tabular-nums text-[var(--hc-text-soft)]">
            {totals.pct} %
          </span>
        </div>
        <p className="hc-mono mt-1 text-[11px] text-[var(--hc-text-dim)]">
          {de.ketten.summaryProgress(totals.done, totals.total, totals.running, totals.waiting)}
        </p>
      </div>

      {/* 3-column mini-stats: Kosten / Tokens / Runs */}
      <div className="mt-3 grid grid-cols-3 gap-2">
        <div className="rounded-[10px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow" style={{ fontSize: 9 }}>{de.ketten.summaryStatCost}</p>
          <p className="hc-mono mt-1 text-[15px] font-semibold tabular-nums text-[var(--hc-emerald)]">
            {costText}
          </p>
        </div>
        <div className="rounded-[10px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow" style={{ fontSize: 9 }}>{de.ketten.summaryStatTokens}</p>
          <p className="hc-mono mt-1 text-[15px] font-semibold tabular-nums text-[var(--hc-text)]">
            {costTotals ? (costTokens > 0 ? fmtTokens(costTokens) : "—") : costsLoading ? "…" : "—"}
          </p>
        </div>
        <div className="rounded-[10px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow" style={{ fontSize: 9 }}>{de.ketten.summaryStatRuns}</p>
          <p className="hc-mono mt-1 text-[15px] font-semibold tabular-nums text-[var(--hc-text)]">
            {costTotals ? costTotals.run_count : costsLoading ? "…" : "—"}
          </p>
        </div>
      </div>
      <p className="hc-type-label mt-2 text-right text-[var(--hc-text-dim)]">{de.ketten.summaryCostSource}</p>
      {costsError ? <p className="mt-1 text-right hc-type-label text-[var(--hc-red)]">{de.ketten.chainCostsLoadError} {costsError}</p> : null}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────

interface ChainPanelProps {
  rootId: string;
  workerByTaskId: Map<string, Worker>;
  operatorHeldIds: Set<string>;
  blockReasonByTaskId: Map<string, string>;
  reviewStateByTaskId: Map<string, ReviewRunState>;
  inspectLoading: string | null;
  onInspect: (runId: string) => void;
  onWorkerAction: (runId: string, action: WorkerActionKey, extra?: { model_override?: string; assignee?: string }) => void | Promise<void>;
  workerActionBusyRunId: string | null;
  onResume: (taskId: string) => void | Promise<void>;
  resumeBusyId: string | null;
  onChainChanged: (newRootId?: string | null) => void | Promise<void>;
}

interface ChainActionsPanelProps {
  rootId: string;
  nodes: ChainGraphNode[];
  onChanged: (newRootId?: string | null) => void | Promise<void>;
}

function ChainActionsPanel({ rootId, nodes, onChanged }: ChainActionsPanelProps) {
  const impact = useMemo(() => summarizeChainCancelImpact(nodes), [nodes]);
  const assigneeOptions = useMemo(() => chainAssigneeOptions(nodes), [nodes]);
  const parentOptions = useMemo(() => nodes.filter((node) => node.id.trim() !== ""), [nodes]);
  const [confirmingCancel, setConfirmingCancel] = useState(false);
  const [cancelResult, setCancelResult] = useState<ChainCancelResult | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [draft, setDraft] = useState<ChainTaskDraft>(() => initialChainTaskDraft(rootId, nodes));
  const [addResult, setAddResult] = useState<ChainTaskCreateResult | null>(null);
  const [addError, setAddError] = useState("");
  const { busy, error, cancelChain, addTask } = useChainActions();
  const cancelBusy = busy === "cancel";
  const addBusy = busy === "add";

  const handleCancel = useCallback(async () => {
    const result = await cancelChain(rootId);
    setCancelResult(result);
    if (result.ok) {
      setConfirmingCancel(false);
      await onChanged(null);
    }
  }, [cancelChain, onChanged, rootId]);

  const handleAddSubmit = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAddError("");
    setAddResult(null);
    const payload = buildChainTaskCreatePayload(draft);
    if (!payload) {
      setAddError(de.ketten.addTaskTitleRequired);
      return;
    }
    const result = await addTask(payload);
    setAddResult(result);
    if (!result.ok) {
      setAddError(result.detail || de.ketten.addTaskFailed);
      return;
    }
    setDraft((prev) => ({ ...prev, title: "", body: "" }));
    await onChanged(result.taskId ?? null);
  }, [addTask, draft, onChanged]);

  const cancelSummary = cancelResult
    ? de.ketten.cancelSummary(cancelResult.terminated.length, cancelResult.held.length, cancelResult.skipped.length)
    : null;

  return (
    <section className="min-w-0 hc-surface-card space-y-3 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">{de.ketten.actionsEyebrow}</p>
          <h2 className="text-base font-semibold text-[var(--hc-text)]">{de.ketten.actionsTitle}</h2>
          <p className="mt-1 text-xs text-[var(--hc-text-dim)]">{de.ketten.actionsHint(impact.total)}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={cancelBusy}
            onClick={() => setConfirmingCancel(true)}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-[7px] border border-red-500/40 bg-red-500/10 px-3 text-xs font-medium text-red-200 transition hover:border-red-400/70 disabled:opacity-50"
          >
            {cancelBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <OctagonX className="h-3.5 w-3.5" />}
            {de.ketten.cancelChain}
          </button>
          <button
            type="button"
            onClick={() => setAddOpen((open) => !open)}
            className="inline-flex min-h-9 items-center gap-1.5 rounded-[7px] border border-[var(--hc-border)] px-3 text-xs font-medium text-[var(--hc-text-soft)] transition hover:border-[var(--hc-border-strong)]"
          >
            {addOpen ? <X className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            {de.ketten.addTask}
          </button>
        </div>
      </div>

      {confirmingCancel ? (
        <div className="rounded-[10px] border border-red-500/35 bg-red-500/10 p-3">
          <p className="flex items-start gap-2 text-sm text-red-100">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{de.ketten.cancelConfirmHint(impact.running, impact.heldOpen, impact.skipped)}</span>
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="button"
              disabled={cancelBusy}
              onClick={() => void handleCancel()}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-[7px] border border-red-400/50 bg-red-500/20 px-3 text-xs font-semibold text-red-100 disabled:opacity-50"
            >
              {cancelBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <OctagonX className="h-3.5 w-3.5" />}
              {cancelBusy ? de.ketten.cancelBusy : de.ketten.cancelConfirm}
            </button>
            <button
              type="button"
              disabled={cancelBusy}
              onClick={() => setConfirmingCancel(false)}
              className="inline-flex min-h-9 items-center rounded-[7px] border border-[var(--hc-border)] px-3 text-xs text-[var(--hc-text-soft)] disabled:opacity-50"
            >
              {de.ketten.cancelAbort}
            </button>
          </div>
        </div>
      ) : null}

      {cancelSummary ? (
        <p className={`hc-type-label ${cancelResult?.ok ? "text-[var(--hc-emerald)]" : "text-[var(--hc-red)]"}`}>
          {cancelResult?.ok ? cancelSummary : cancelResult?.detail || de.ketten.cancelFailed}
        </p>
      ) : null}
      {error ? <p className="hc-type-label text-[var(--hc-red)]">{error}</p> : null}

      {addOpen ? (
        <form onSubmit={handleAddSubmit} className="rounded-[10px] border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3">
          <div className="grid gap-3 md:grid-cols-2">
            <label className="block text-xs text-[var(--hc-text-soft)]">
              {de.ketten.addTaskTitle}
              <input
                value={draft.title}
                onChange={(event) => setDraft((prev) => ({ ...prev, title: event.target.value }))}
                className="mt-1 min-h-10 w-full rounded-[7px] border border-[var(--hc-border)] bg-black/20 px-3 text-sm text-[var(--hc-text)] outline-none focus:border-[var(--hc-accent-border)]"
                placeholder={de.ketten.addTaskTitlePlaceholder}
                required
              />
            </label>
            <label className="block text-xs text-[var(--hc-text-soft)]">
              {de.ketten.addTaskAssignee}
              <input
                value={draft.assignee}
                list="chain-add-assignee-options"
                onChange={(event) => setDraft((prev) => ({ ...prev, assignee: event.target.value }))}
                className="mt-1 min-h-10 w-full rounded-[7px] border border-[var(--hc-border)] bg-black/20 px-3 text-sm text-[var(--hc-text)] outline-none focus:border-[var(--hc-accent-border)]"
              />
              <datalist id="chain-add-assignee-options">
                {assigneeOptions.map((option) => <option key={option} value={option} />)}
              </datalist>
            </label>
            <label className="block text-xs text-[var(--hc-text-soft)]">
              {de.ketten.addTaskParent}
              <select
                value={draft.parentId}
                onChange={(event) => setDraft((prev) => ({ ...prev, parentId: event.target.value }))}
                className="mt-1 min-h-10 w-full rounded-[7px] border border-[var(--hc-border)] bg-black/20 px-3 text-sm text-[var(--hc-text)] outline-none focus:border-[var(--hc-accent-border)]"
              >
                {parentOptions.map((node) => (
                  <option key={node.id} value={node.id}>
                    {node.id === rootId ? `${node.title} · ${de.ketten.addTaskRootParent}` : `${node.title} · ${node.status}`}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex min-h-10 items-center gap-2 text-xs text-[var(--hc-text-soft)] md:mt-5">
              <input
                type="checkbox"
                checked={draft.park}
                onChange={(event) => setDraft((prev) => ({ ...prev, park: event.target.checked }))}
                className="h-4 w-4 accent-[var(--hc-accent)]"
              />
              {de.ketten.addTaskPark}
            </label>
          </div>
          <label className="mt-3 block text-xs text-[var(--hc-text-soft)]">
            {de.ketten.addTaskBody}
            <textarea
              value={draft.body}
              onChange={(event) => setDraft((prev) => ({ ...prev, body: event.target.value }))}
              className="mt-1 min-h-[92px] w-full resize-y rounded-[7px] border border-[var(--hc-border)] bg-black/20 px-3 py-2 text-sm text-[var(--hc-text)] outline-none focus:border-[var(--hc-accent-border)]"
              placeholder={de.ketten.addTaskBodyPlaceholder}
            />
          </label>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={addBusy || !draft.title.trim()}
              className="inline-flex min-h-9 items-center gap-1.5 rounded-[7px] border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 text-xs font-semibold text-[var(--hc-accent-text)] disabled:opacity-50"
            >
              {addBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Plus className="h-3.5 w-3.5" />}
              {addBusy ? de.ketten.addTaskBusy : de.ketten.addTaskSubmit}
            </button>
            {addResult?.ok ? (
              <span className="inline-flex items-center gap-1.5 hc-type-label text-[var(--hc-emerald)]">
                <Check className="h-3.5 w-3.5" />
                {de.ketten.addTaskCreated(addResult.taskId ?? "—")}
              </span>
            ) : null}
            {addError ? <span className="hc-type-label text-[var(--hc-red)]">{addError}</span> : null}
          </div>
        </form>
      ) : null}
    </section>
  );
}

function ChainPanel({
  rootId,
  workerByTaskId,
  operatorHeldIds,
  blockReasonByTaskId,
  reviewStateByTaskId,
  inspectLoading,
  onInspect,
  onWorkerAction,
  workerActionBusyRunId,
  onResume,
  resumeBusyId,
  onChainChanged,
}: ChainPanelProps) {
  const graph = useChainGraph(rootId);
  const reloadGraph = graph.reload;
  const chainCosts = useHermesChainCosts(rootId);
  const now = nowSec();

  const handleChainChanged = useCallback(async (newRootId?: string | null) => {
    if (newRootId) {
      await onChainChanged(newRootId);
      return;
    }
    await Promise.all([reloadGraph(), onChainChanged(null)]);
  }, [reloadGraph, onChainChanged]);

  if (graph.error) {
    return (
      <div className="hc-surface-card p-4">
        <p className="text-sm text-red-700">{de.ketten.loadError}</p>
        <p className="mt-1 text-xs text-[var(--hc-text-dim)]">{graph.error}</p>
      </div>
    );
  }
  if (graph.loading && !graph.data) {
    return <SkeletonCard rows={3} />;
  }
  if (!graph.data) return null;

  return (
    <>
      <ChainActionsPanel
        rootId={graph.data.root_id}
        nodes={graph.data.nodes}
        onChanged={handleChainChanged}
      />

      {/* Kette-Summary card: progress from graph nodes, cost truth from chain-costs. */}
      <ChainSummary nodes={graph.data.nodes} rootId={graph.data.root_id} costs={chainCosts.data} costsLoading={chainCosts.loading} costsError={chainCosts.error} />

      <KettenGraph
        nodes={graph.data.nodes}
        edges={graph.data.edges}
        rootId={graph.data.root_id}
        workerByTaskId={workerByTaskId}
        operatorHeldIds={operatorHeldIds}
        now={now}
        inspectLoading={inspectLoading}
        onInspect={onInspect}
        onWorkerAction={onWorkerAction}
        workerActionBusyRunId={workerActionBusyRunId}
        onResume={onResume}
        resumeBusyId={resumeBusyId}
        blockReasonByTaskId={blockReasonByTaskId}
        reviewStateByTaskId={reviewStateByTaskId}
      />
      <p className="text-right text-xs text-[var(--hc-text-dim)]">
        {graph.data.checked_at ? (
          <time dateTime={new Date(graph.data.checked_at * 1000).toISOString()}>
            {de.ketten.checkedAt(fmtAge(graph.data.checked_at, now))}
          </time>
        ) : (
          de.ketten.checkedAt(fmtAge(graph.data.checked_at, now))
        )}
      </p>
    </>
  );
}

export function ChainVizView(_props: { density?: unknown }) {
  const [params, setParams] = useSearchParams();
  const board = useBoard();
  const boardReload = board.reload;
  const [selectedRootId, setSelectedRootId] = useState<string | null>(null);

  // Round C: Worker + Inspect + Resume wiring für die KettenGraph-Pipeline.
  const workers = useHermesWorkers();
  const { inspectByRun, loadingRun, inspect } = useRunInspect();
  const [busyRun, setBusyRun] = useState<string | null>(null);
  const workersReload = workers.reload;

  const workerByTaskId = useMemo(() => {
    const now = nowSec();
    const map = new Map<string, Worker>();
    for (const w of workers.data?.workers ?? []) {
      // merge live inspect data if present
      map.set(w.task_id, { ...w, inspect: inspectByRun[w.run_id] ?? w.inspect });
    }
    // sort stability: not needed for Map lookup but preserve for determinism
    void workerSortRank; void now;
    return map;
  }, [workers.data, inspectByRun]);

  const onWorkerAction = useCallback(async (
    runId: string,
    action: WorkerActionKey,
    extra?: { model_override?: string; assignee?: string },
  ) => {
    setBusyRun(runId);
    try {
      if (action === "terminate") {
        await fetchJSON<{ ok?: boolean }>(
          `/api/plugins/kanban/runs/${encodeURIComponent(runId)}/terminate`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason: "Operator-Terminate aus dem Ketten-Tab" }) },
        );
      } else {
        await fetchJSON<{ ok?: boolean }>(
          `/api/plugins/kanban/workers/${encodeURIComponent(runId)}/action`,
          { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, confirm: true, ...extra }) },
        );
      }
    } catch {
      // errors are non-fatal in the Ketten tab
    } finally {
      setBusyRun(null);
      await workersReload();
    }
  }, [workersReload]);

  // Resume: unblock-path via PATCH /tasks/{id} (status → ready).
  const { run: runTaskAction, busyId: resumeBusyId } = useTaskAction(board.reload);
  const onResume = useCallback(async (taskId: string) => {
    await runTaskAction(taskId, "ready");
  }, [runTaskAction]);

  // Operator-held tasks: blocked tasks where block_reason contains "operator hold"
  // (case-insensitive). Tasks blocked by other causes (circuit-breaker, review-
  // required, dependency stall) do NOT show the Resume button. Older board
  // payloads without block_reason fall through to null → not treated as held.
  const operatorHeldIds = useMemo(() => {
    const allTasks = board.data?.columns.flatMap((c) => c.tasks) ?? [];
    const held = new Set<string>();
    for (const t of allTasks) {
      if (
        t.status === "blocked" &&
        t.block_reason != null &&
        t.block_reason.toLowerCase().includes("operator hold")
      ) {
        held.add(t.id);
      }
    }
    return held;
  }, [board.data]);

  // Blocker-Grund je task_id: aus dem Board-Endpoint (BoardTask.block_reason),
  // nur für blockierte Tasks mit gesetztem Grund.
  const blockReasonByTaskId = useMemo(() => {
    const allTasks = board.data?.columns.flatMap((c) => c.tasks) ?? [];
    const map = new Map<string, string>();
    for (const t of allTasks) {
      if (t.status === "blocked" && t.block_reason != null && t.block_reason.trim() !== "") {
        map.set(t.id, t.block_reason);
      }
    }
    return map;
  }, [board.data]);

  // Review-Run-State je task_id: aus dem review-verdicts Endpoint.
  const verdicts = useHermesReviewVerdicts();
  const reviewStateByTaskId = useMemo(() => {
    const map = new Map<string, ReviewRunState>();
    for (const r of verdicts.data?.reviews ?? []) {
      if (r.review_run_state != null) {
        map.set(r.task_id, r.review_run_state as ReviewRunState);
      }
    }
    return map;
  }, [verdicts.data]);

  const { activeChains, doneChains } = useMemo(() => {
    if (!board.data) return { activeChains: [], doneChains: [] };
    const allTasks = board.data.columns.flatMap((c) => c.tasks);
    const built = buildChains(allTasks);
    return { activeChains: built.active, doneChains: built.done };
  }, [board.data]);

  // URL-param ?root= wins; fall back to user state, then first active chain.
  const requestedRoot = params.get("root")?.trim() || null;
  const allSelectableChains = useMemo(
    () => [...activeChains, ...doneChains],
    [activeChains, doneChains],
  );
  const focusedRootId = useMemo(() => {
    if (activeChains.length === 0 && doneChains.length === 0) return null;
    // URL param takes precedence if it exists in any chain (active or done).
    if (requestedRoot && allSelectableChains.some((c) => c.rootId === requestedRoot)) {
      return requestedRoot;
    }
    // User selection via ChainListPanel (local state).
    if (selectedRootId && allSelectableChains.some((c) => c.rootId === selectedRootId)) {
      return selectedRootId;
    }
    // Default: first active chain (or first done if no active).
    return activeChains[0]?.rootId ?? doneChains[0]?.rootId ?? null;
  }, [activeChains, doneChains, allSelectableChains, requestedRoot, selectedRootId]);

  // Keep URL in sync when user selects via selector.
  const handleSelect = useCallback((rootId: string) => {
    setSelectedRootId(rootId);
    setParams(rootId ? { root: rootId } : {}, { replace: true });
  }, [setParams]);

  const handleChainChanged = useCallback(async (newRootId?: string | null) => {
    await boardReload();
    if (newRootId) {
      handleSelect(newRootId);
    }
  }, [boardReload, handleSelect]);

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="mb-1.5 flex flex-col gap-1 sm:mb-4 sm:gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <p className="hc-eyebrow">{de.ketten.eyebrow}</p>
          <h1 className="mt-1 text-base font-semibold tracking-normal text-[var(--hc-text)] sm:text-2xl">
            {de.ketten.title}
          </h1>
          {/* Beschreibungstext nur ab sm — auf dem Handy trägt der Titel schon
              die Aussage; Text+Chips+Suche brauchten sonst zu viel Scrollweg
              bis zur ersten Kette (Operator-Feedback 2026-07-02). */}
          <p className="mt-1 hidden text-sm text-[var(--hc-text-soft)] sm:block">{de.ketten.subtitle} {de.ketten.subtitleDagHint}</p>
        </div>
        {focusedRootId ? (
          <Link
            to={`/control/flow?task=${encodeURIComponent(focusedRootId)}`}
            className="hc-mono inline-flex shrink-0 items-center rounded-[7px] border border-[var(--hc-border)] px-2.5 py-1 text-[11px] text-[var(--hc-text-soft)] transition hover:border-[var(--hc-border-strong)]"
          >
            {de.ketten.openInFlow}
          </Link>
        ) : null}
      </header>

      {board.loading && !board.data ? (
        <SkeletonCard rows={4} />
      ) : board.error && !board.data ? (
        <Hero eyebrow={de.ketten.eyebrow} tone="red" title={de.ketten.loadError} subtitle={board.error} />
      ) : activeChains.length === 0 && doneChains.length === 0 ? (
        <Hero
          eyebrow={de.ketten.eyebrow}
          tone="zinc"
          title={de.ketten.emptyTitle}
          subtitle={de.ketten.emptyDesc}
        />
      ) : (
        <div className="grid gap-3 sm:gap-4">
          {/* min-w-0: ohne das erzwingt ein GRID-Item ohne explizite min-width
              seine automatische Mindestgröße in BEIDEN Achsen — ein langer,
              nicht umbrechender Ketten-Titel weiter unten (ChainRow-Titel,
              truncate = white-space:nowrap) drückt diese Karte sonst real auf
              ~1800px auf, die dann von einem Ahnen hart geclippt wird statt
              sauber zu ellipsieren (Alt-Bug, ui-verifier-Fund 2026-07-02,
              per getBoundingClientRect-Repro bestätigt). */}
          <div className="min-w-0 hc-surface-card p-2 sm:p-3">
            {/* "Kette wählen" nur ab sm sichtbar — die sr-only-Suchlabel +
                Filterchips machen den Zweck auf dem Handy schon klar, das
                Label war nur zusätzlicher Scrollweg (Operator-Feedback
                2026-07-02). */}
            <Eyebrow className="hidden sm:block">{de.ketten.chooseChain}</Eyebrow>
            <div className="sm:mt-2">
              <ChainListPanel
                chains={activeChains}
                doneChains={doneChains}
                selectedRootId={focusedRootId}
                onSelect={handleSelect}
                disabled={board.loading}
              />
            </div>
          </div>

          {focusedRootId ? (
            <ChainPanel
              key={focusedRootId}
              rootId={focusedRootId}
              workerByTaskId={workerByTaskId}
              operatorHeldIds={operatorHeldIds}
              blockReasonByTaskId={blockReasonByTaskId}
              reviewStateByTaskId={reviewStateByTaskId}
              inspectLoading={loadingRun}
              onInspect={inspect}
              onWorkerAction={onWorkerAction}
              workerActionBusyRunId={busyRun}
              onResume={onResume}
              resumeBusyId={resumeBusyId}
              onChainChanged={handleChainChanged}
            />
          ) : null}
        </div>
      )}
    </div>
  );
}
