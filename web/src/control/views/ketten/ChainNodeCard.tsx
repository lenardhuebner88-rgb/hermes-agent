import { forwardRef, memo } from "react";
import { cn } from "@/lib/utils";
import { fmtDur, fmtTokens, formatEffectiveCost } from "../../lib/derive";
import { StatusPill } from "../../components/atoms";
import type { ChainGraphNode } from "../../lib/types";
import { statusDot, statusTone } from "./dagLayout";
import { de } from "../../i18n/de";
import { taskStatusLabel } from "../../lib/tones";

export interface ChainNodeCardProps {
  node: ChainGraphNode;
  isRoot: boolean;
}

export const ChainNodeCard = memo(
  forwardRef<HTMLDivElement, ChainNodeCardProps>(function ChainNodeCard({ node, isRoot }, ref) {
    const tone = statusTone(node.status);
    const dot = statusDot(node.status);

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
        {/* Header: task id (left) · focus marker + status pill (right). */}
        <div className="flex min-w-0 items-center justify-between gap-2">
          <span className="hc-mono min-w-0 flex-1 truncate text-[11px] text-[var(--hc-text-dim)]">
            {de.ketten.nodeTaskId} {node.id}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            {isRoot ? (
              <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--hc-accent-text)]">
                {de.ketten.focusRoot}
              </span>
            ) : null}
            <StatusPill tone={tone} label={taskStatusLabel[node.status] ?? node.status} dot={dot} size="sm" />
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

        {/* Footer: assignee (left) · cost read-out (right), on a hairline. */}
        <div className="mt-3 flex min-w-0 items-center justify-between gap-2 border-t border-[var(--hc-border)] pt-2.5">
          {node.assignee ? (
            <span className="inline-flex shrink-0 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-1 text-xs font-medium text-[var(--hc-accent-text)]">
              {node.assignee}
            </span>
          ) : (
            <span aria-hidden />
          )}
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
