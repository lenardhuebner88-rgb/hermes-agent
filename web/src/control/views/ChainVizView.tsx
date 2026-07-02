import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useBoard, useChainGraph, useHermesReviewVerdicts, useHermesWorkers, useRunInspect, useTaskAction } from "../hooks/useControlData";
import { buildChains } from "../lib/fleet";
import { fmtAge, fmtTokens, formatEffectiveCost, nowSec, workerSortRank } from "../lib/derive";
import { Hero } from "../components/Hero";
import { Eyebrow, SkeletonCard } from "../components/primitives";
import { de } from "../i18n/de";
import { ChainSelector } from "./ketten/ChainSelector";
import { KettenGraph } from "./ketten/KettenGraph";
import type { ReviewRunState } from "./ketten/ChainNodeCard";
import type { ChainGraphNode, Worker } from "../lib/types";
import type { WorkerActionKey } from "../components/WorkerCard";
import { fetchJSON } from "@/lib/api";

// ── Chain summary (derived from already-loaded graph nodes, no new endpoint) ──

interface ChainSummaryProps {
  nodes: ChainGraphNode[];
  rootId: string;
}

function ChainSummary({ nodes, rootId: _rootId }: ChainSummaryProps) {
  // Derive totals by folding over the already-loaded nodes.
  const totals = useMemo(() => {
    let costEffective = 0;
    let tokens = 0;
    let runs = 0;
    let done = 0;
    let running = 0;
    let waiting = 0;

    for (const n of nodes) {
      costEffective += n.cost_effective_usd ?? 0;
      tokens += (n.input_tokens ?? 0) + (n.output_tokens ?? 0);
      // Count runs from latest_run presence as a proxy (each node ~ 1 run).
      if (n.latest_run != null || n.cost_usd > 0 || n.input_tokens > 0) runs += 1;
      if (n.status === "done") done++;
      else if (n.status === "running") running++;
      else waiting++;
    }

    const total = nodes.length;
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    return { costEffective, tokens, runs, done, running, waiting, total, pct };
  }, [nodes]);

  if (nodes.length === 0) return null;

  const { text: costText } = formatEffectiveCost({
    cost_usd: totals.costEffective,
    cost_effective_usd: totals.costEffective,
    tokens: totals.tokens,
  });

  return (
    <div
      className="rounded-[14px] border border-[var(--hc-border)] bg-[var(--hc-panel-card)] px-[18px] py-4 shadow-[var(--hc-elev-1)]"
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
            {totals.tokens > 0 ? fmtTokens(totals.tokens) : "—"}
          </p>
        </div>
        <div className="rounded-[10px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2.5 py-2">
          <p className="hc-eyebrow" style={{ fontSize: 9 }}>{de.ketten.summaryStatRuns}</p>
          <p className="hc-mono mt-1 text-[15px] font-semibold tabular-nums text-[var(--hc-text)]">
            {totals.runs}
          </p>
        </div>
      </div>
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
}: ChainPanelProps) {
  const graph = useChainGraph(rootId);
  const now = nowSec();

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
      {/* Kette-Summary card — totals derived from loaded nodes, no new endpoint */}
      <ChainSummary nodes={graph.data.nodes} rootId={graph.data.root_id} />

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
    // User selection via ChainSelector (local state).
    if (selectedRootId && allSelectableChains.some((c) => c.rootId === selectedRootId)) {
      return selectedRootId;
    }
    // Default: first active chain (or first done if no active).
    return activeChains[0]?.rootId ?? doneChains[0]?.rootId ?? null;
  }, [activeChains, doneChains, allSelectableChains, requestedRoot, selectedRootId]);

  // Keep URL in sync when user selects via selector.
  function handleSelect(rootId: string) {
    setSelectedRootId(rootId);
    setParams(rootId ? { root: rootId } : {}, { replace: true });
  }

  return (
    <div className="mx-auto w-full max-w-6xl">
      <header className="mb-4 flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <p className="hc-eyebrow">{de.ketten.eyebrow}</p>
          <h1 className="mt-1 text-2xl font-semibold tracking-normal text-[var(--hc-text)]">
            {de.ketten.title}
          </h1>
          <p className="mt-1 text-sm text-[var(--hc-text-soft)]">{de.ketten.subtitle} {de.ketten.subtitleDagHint}</p>
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
        <div className="grid gap-4">
          <div className="hc-surface-card p-3 lg:max-w-md">
            <Eyebrow>{de.ketten.chooseChain}</Eyebrow>
            <div className="mt-2">
              <ChainSelector
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
            />
          ) : null}
        </div>
      )}
    </div>
  );
}
