import { forwardRef, memo, useState } from "react";
import { cn } from "@/lib/utils";
import { fmtDur, fmtTokens, formatEffectiveCost, workerHealth } from "../../lib/derive";
import { StatusPill } from "../../components/atoms";
import { WorkerCard } from "../../components/WorkerCard";
import type { ChainGraphNode, Worker } from "../../lib/types";
import type { WorkerActionKey } from "../../components/WorkerCard";
import { statusDot, statusTone } from "./dagLayout";
import { de } from "../../i18n/de";
import { taskStatusLabel, profileLabel } from "../../lib/tones";

export interface ChainNodeCardProps {
  node: ChainGraphNode;
  isRoot: boolean;
  /** Round C: laufender Worker für diesen Knoten (null = noch nicht gestartet). */
  worker?: Worker | null;
  /** Round C: true wenn der Task blocked + Operator-Hold ist → Fortsetzen-Button. */
  isOperatorHeld?: boolean;
  now?: number;
  inspectLoading?: boolean;
  onInspect?: (runId: string) => void;
  onWorkerAction?: (runId: string, action: WorkerActionKey, extra?: { model_override?: string; assignee?: string }) => void | Promise<void>;
  workerActionBusy?: boolean;
  /** Round C: Callback für Fortsetzen (Unblock via PATCH /tasks/{id} → ready). */
  onResume?: (taskId: string) => void | Promise<void>;
  resumeBusy?: boolean;
}

export const ChainNodeCard = memo(
  forwardRef<HTMLDivElement, ChainNodeCardProps>(function ChainNodeCard(
    { node, isRoot, worker, isOperatorHeld, now, inspectLoading, onInspect, onWorkerAction, workerActionBusy, onResume, resumeBusy },
    ref,
  ) {
    // now-Fallback ohne Date.now() im Default-Parameter (impure function lint).
    const effectiveNow = now ?? 0;
    const tone = statusTone(node.status);
    const dot = statusDot(node.status);

    // Round C: laufende Knoten sind erweiterbar (Cockpit inline).
    const [expanded, setExpanded] = useState(false);

    // Prefer the richer latest_run runtime; fall back to the task-level value.
    const runtime = node.latest_run?.runtime_seconds ?? node.runtime_seconds ?? null;

    // Progress rollup drives the bar. null / total<=0 → no real subtask data.
    const progress = node.progress;
    const hasProgress = progress != null && progress.total > 0;
    const pct = hasProgress ? Math.round((progress.done / progress.total) * 100) : 0;
    const isDone = node.status === "done";
    const isRunning = node.status === "running";
    // "Waiting on predecessor": an unstarted node with upstream deps and no own
    // subtask progress. Gated on status (not started_at) so a running/review
    // node is never mislabeled as waiting.
    const isWaiting =
      !hasProgress &&
      !isRoot &&
      node.parents.length > 0 &&
      (node.status === "todo" || node.status === "ready" || node.status === "scheduled");

    const barWidth = hasProgress ? pct : isDone ? 100 : 0;
    const fillTone = isDone
      ? "bg-emerald-500"
      : isRunning
        ? "bg-gradient-to-r from-cyan-400 to-cyan-500"
        : isRoot
          ? "bg-gradient-to-r from-indigo-500 to-violet-500"
          : "bg-[var(--hc-border-strong)]";

    // Right-of-bar percent label (mono); empty for a started-but-unmeasured node.
    const pctLabel = hasProgress ? `${pct}%` : isDone ? "100%" : "";
    // Telemetry chips below the bar: elapsed runtime + subtask rollup OR the
    // "waiting on predecessor" hint. Liveness stays on the pipeline dot (not a
    // card heartbeat — see ChainNodeCard.test.tsx).
    const subtaskLabel = hasProgress ? de.ketten.progressOf(progress.done, progress.total) : null;

    return (
      <div
        ref={ref}
        data-node-id={node.id}
        className={cn(
          "relative z-10 min-w-0 max-w-full overflow-hidden rounded-[14px] border bg-[var(--hc-panel-card)] px-[18px] py-4 shadow-[var(--hc-elev-1)] transition",
          "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
          isRunning && "border-cyan-400/70",
          isRoot && "ring-1 ring-[var(--hc-accent-border)]",
        )}
      >
        {/* Header: task id (left, B9b: links to Flow?task=) · focus marker + status pill (right). */}
        <div className="flex min-w-0 items-center justify-between gap-2">
          {/* B9b: task-id chip links to /control/flow?task=<id>; FlowView reads
              ?task= and scrolls+selects the task. Plain <a> avoids router-context
              requirement in server-render tests. */}
          <a
            href={`/control/flow?task=${encodeURIComponent(node.id)}`}
            className="hc-mono min-w-0 flex-1 truncate text-[11px] text-[var(--hc-text-dim)] hover:text-[var(--hc-text-soft)] hover:underline"
          >
            {de.ketten.nodeTaskId} {node.id}
          </a>
          <div className="flex shrink-0 items-center gap-2">
            {isRoot ? (
              <span
                className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--hc-accent-text)]"
                title={de.ketten.focusRootTooltip}
              >
                {de.ketten.focusRoot}
              </span>
            ) : null}
            <StatusPill tone={tone} label={taskStatusLabel[node.status] ?? node.status} dot={dot} size="sm" />
            {/* Round C: Aufklapp-Toggle für laufende Knoten */}
            {isRunning ? (
              <button
                type="button"
                onClick={() => setExpanded((v) => !v)}
                className="hc-mono rounded-full border border-[var(--hc-border)] px-2 py-0.5 text-[10px] text-[var(--hc-text-soft)] transition hover:border-[var(--hc-border-strong)]"
                aria-expanded={expanded}
              >
                {expanded ? "▲" : "▼"}
              </button>
            ) : null}
          </div>
        </div>

        <p className="mt-2 line-clamp-2 text-base font-semibold leading-snug text-[var(--hc-text)]">
          {node.title}
        </p>

        {/* Progress: bar with the percent read-out inline on the right. */}
        <div className="mt-3 flex items-center gap-2.5">
          <div
            className="relative h-[7px] flex-1 overflow-hidden rounded-full bg-[rgba(26,29,40,.06)]"
            role="progressbar"
            aria-label={de.ketten.progressLabel}
            aria-valuenow={barWidth}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className={cn("h-full rounded-full transition-all", fillTone)} style={{ width: `${barWidth}%` }} />
          </div>
          <span className="hc-mono w-9 shrink-0 text-right text-[11px] font-semibold tabular-nums text-[var(--hc-text-soft)]">
            {pctLabel}
          </span>
        </div>

        {/* Telemetry chips: elapsed + subtask rollup, or waiting hint. */}
        {(runtime != null || subtaskLabel || isWaiting) && (
          <div className="mt-3 flex flex-wrap items-center gap-1.5">
            {runtime != null ? (
              <span className="hc-mono inline-flex items-center rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 text-[10px] text-[var(--hc-text-soft)]">
                {fmtDur(runtime)}
              </span>
            ) : null}
            {subtaskLabel ? (
              <span className="hc-mono inline-flex items-center rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 text-[10px] text-[var(--hc-text-soft)]">
                {subtaskLabel}
              </span>
            ) : null}
            {isWaiting ? (
              <span className="hc-mono inline-flex items-center rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 text-[10px] text-[var(--hc-text-dim)]">
                {de.ketten.waitingOnPredecessor}
              </span>
            ) : null}
          </div>
        )}

        {/* Round C: Inline WorkerCard-Cockpit für laufende Knoten (aufgeklappt). */}
        {isRunning && expanded ? (
          <div className="mt-4 border-t border-[var(--hc-border)] pt-4">
            {worker && onInspect && onWorkerAction ? (
              <WorkerCard
                worker={worker}
                health={workerHealth(worker, effectiveNow)}
                density="airy"
                collapsible={false}
                now={effectiveNow}
                inspectLoading={inspectLoading}
                onInspect={onInspect}
                onAction={onWorkerAction}
                actionBusy={workerActionBusy}
              />
            ) : worker == null ? (
              <p className="hc-mono text-[11px] text-[var(--hc-text-dim)]">
                {de.flow.chainNodeWorkerStarting}
              </p>
            ) : (
              <p className="hc-mono text-[11px] text-[var(--hc-text-dim)]">
                {de.flow.chainNodeWorkerNoData}
              </p>
            )}
          </div>
        ) : null}

        {/* Round C: Fortsetzen-Button für Operator-Hold-Knoten. */}
        {isOperatorHeld && onResume ? (
          <div className="mt-4 border-t border-[var(--hc-border)] pt-3">
            <button
              type="button"
              disabled={resumeBusy}
              onClick={() => void onResume(node.id)}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-[8px] border border-emerald-400/50 bg-emerald-400/10 px-3 py-1.5 text-xs font-medium text-emerald-300 transition",
                "hover:border-emerald-400/80 hover:bg-emerald-400/20 disabled:opacity-50 disabled:cursor-not-allowed",
              )}
            >
              {resumeBusy ? "…" : de.flow.chainNodeResume}
            </button>
            <p className="mt-1.5 text-[10px] text-[var(--hc-text-dim)]">
              {de.flow.chainNodeResumeHint}
            </p>
          </div>
        ) : null}

        {/* B8: "Run ansehen"-Button für blockierte Knoten mit vorhandenem latest_run.
            Gibt Operator Einblick in den fehlgeschlagenen Run zur Fehlersuche. */}
        {!isRunning && node.status === "blocked" && node.latest_run?.id != null && onInspect ? (
          <div className="mt-3 border-t border-[var(--hc-border)] pt-2.5">
            <button
              type="button"
              onClick={() => onInspect(String(node.latest_run!.id))}
              className="hc-mono inline-flex items-center gap-1 rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-2 py-1 text-[10px] text-[var(--hc-text-soft)] transition hover:border-[var(--hc-border-strong)]"
            >
              {de.ketten.viewRunLabel}
            </button>
          </div>
        ) : null}

        {/* Footer: assignee + profile chip (left) · cost read-out (right), on a hairline.
            B6: profile chip shown for ALL nodes (done/blocked/running) — helps Operator
            trace cost and failure attribution without opening the inspect drawer. */}
        <div className="mt-3 flex min-w-0 items-center justify-between gap-2 border-t border-[var(--hc-border)] pt-2.5">
          <div className="flex min-w-0 shrink-0 items-center gap-1.5">
            {node.assignee ? (
              <span className="inline-flex items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-1 text-xs font-medium text-[var(--hc-accent-text)]">
                {node.assignee}
              </span>
            ) : null}
            {node.latest_run?.profile ? (
              <span className="hc-mono inline-flex items-center rounded-[7px] border border-[var(--hc-border)] bg-[var(--hc-panel)] px-1.5 py-0.5 text-[10px] text-[var(--hc-text-dim)]">
                {profileLabel[node.latest_run.profile] ?? node.latest_run.profile}
              </span>
            ) : null}
            {!node.assignee && !node.latest_run?.profile ? <span aria-hidden /> : null}
          </div>
          <NodeCost
            costUsd={node.cost_usd}
            costEffectiveUsd={node.cost_effective_usd}
            inputTokens={node.input_tokens}
            outputTokens={node.output_tokens}
          />
        </div>
      </div>
    );
  }),
);

/** Kompaktes Kosten/Token-Readout für den Knoten-Footer ($ in emerald). */
function NodeCost({
  costUsd,
  costEffectiveUsd,
  inputTokens,
  outputTokens,
}: {
  costUsd: number;
  costEffectiveUsd: number;
  inputTokens: number;
  outputTokens: number;
}) {
  const totalTokens = inputTokens + outputTokens;
  const { text: costText, estimated } = formatEffectiveCost({
    cost_usd: costUsd,
    cost_effective_usd: costEffectiveUsd,
    tokens: totalTokens,
  });

  // Wenn weder Kosten noch Tokens vorhanden — dezentes "—".
  if (costEffectiveUsd === 0 && totalTokens === 0) {
    return (
      <span className="hc-mono shrink-0 text-[10px] text-[var(--hc-text-dim)]" aria-label={de.ketten.costNone}>
        —
      </span>
    );
  }
  const tokLabel = totalTokens > 0 ? `${fmtTokens(totalTokens)} tok` : null;
  return (
    <span className="hc-mono shrink-0 text-[10px] tabular-nums text-[var(--hc-text-dim)]">
      <span
        className="font-semibold text-[var(--hc-emerald)]"
        title={estimated ? de.ketten.costEstimatedTooltip : undefined}
      >
        {costText}
      </span>
      {tokLabel ? ` · ${tokLabel}` : null}
    </span>
  );
}
