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

    const metaText = hasProgress
      ? `${pct}% — ${de.ketten.progressOf(progress.done, progress.total)}`
      : isWaiting
        ? de.ketten.waitingOnPredecessor
        : null;

    return (
      <div
        ref={ref}
        data-node-id={node.id}
        className={cn(
          "relative z-10 min-w-0 max-w-full overflow-hidden rounded-lg border bg-[var(--hc-panel-card)] p-3 shadow-sm transition",
          "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
          isRunning && "border-cyan-400/70",
          isRoot && "ring-1 ring-[var(--hc-accent-border)]",
        )}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="hc-mono min-w-0 flex-1 truncate text-[10px] text-[var(--hc-text-dim)]">
            {de.ketten.nodeTaskId} {node.id}
          </span>
          {isRoot ? (
            <span className="shrink-0 rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--hc-accent-text)]">
              {de.ketten.focusRoot}
            </span>
          ) : null}
        </div>

        <p className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-[var(--hc-text)]">
          {node.title}
        </p>

        {/* Progress section: bar + meta (subtask rollup left, runtime right). */}
        <div className="mt-2.5">
          <div
            className="h-1.5 overflow-hidden rounded-full bg-[var(--hc-border)]"
            role="progressbar"
            aria-label={de.ketten.progressLabel}
            aria-valuenow={barWidth}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div className={cn("h-full rounded-full transition-all", fillTone)} style={{ width: `${barWidth}%` }} />
          </div>
          <div className="mt-1 flex items-center justify-between gap-2 text-[11px] text-[var(--hc-text-dim)]">
            <span className="min-w-0 truncate">{metaText}</span>
            <span className="hc-mono shrink-0 tabular-nums">{runtime != null ? fmtDur(runtime) : "—"}</span>
          </div>
        </div>

        {/* Compact footer: status pill + assignee, separated by a hairline. */}
        <div className="mt-2.5 flex min-w-0 items-center justify-between gap-2 border-t border-[var(--hc-border)] pt-2.5">
          <StatusPill tone={tone} label={taskStatusLabel[node.status] ?? node.status} dot={dot} size="sm" />
          {node.assignee ? (
            <span className="inline-flex shrink-0 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-1 text-xs font-medium text-[var(--hc-accent-text)]">
              {node.assignee}
            </span>
          ) : null}
        </div>

        {/* Cost badge — dezent unterhalb des Footers; "—" wenn keine Daten. */}
        <NodeCostBadge
          costUsd={node.cost_usd}
          costEffectiveUsd={node.cost_effective_usd}
          inputTokens={node.input_tokens}
          outputTokens={node.output_tokens}
        />
      </div>
    );
  }),
);

/** Kompaktes Kosten/Token-Badge für einen DAG-Knoten. */
function NodeCostBadge({
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
      <div className="mt-1.5 text-right text-[10px] text-[var(--hc-text-dim)]" aria-label={de.ketten.costNone}>
        —
      </div>
    );
  }
  const tokLabel = totalTokens > 0 ? `${fmtTokens(totalTokens)} tok` : null;
  return (
    <div className="hc-mono mt-1.5 text-right text-[10px] tabular-nums text-[var(--hc-text-dim)]">
      {estimated ? (
        <span title={de.ketten.costEstimatedTooltip}>{costText}</span>
      ) : (
        costText
      )}
      {tokLabel ? ` · ${tokLabel}` : null}
    </div>
  );
}
