import { forwardRef, memo } from "react";
import { cn } from "@/lib/utils";
import { fmtAge, fmtDur } from "../../lib/derive";
import { Led, StatusPill } from "../../components/atoms";
import type { ChainGraphNode } from "../../lib/types";
import { statusDot, statusTone } from "./dagLayout";
import { de } from "../../i18n/de";
import { taskStatusLabel } from "../../lib/tones";

export interface ChainNodeCardProps {
  node: ChainGraphNode;
  isRoot: boolean;
  now: number;
}

export const ChainNodeCard = memo(
  forwardRef<HTMLDivElement, ChainNodeCardProps>(function ChainNodeCard({ node, isRoot, now }, ref) {
    const tone = statusTone(node.status);
    const dot = statusDot(node.status);

    // Prefer richer latest_run fields; fall back to task-level fields.
    const heartbeatTs = node.latest_run?.last_heartbeat_at ?? node.last_heartbeat_at;
    const heartbeatAge = heartbeatTs ? now - heartbeatTs : null;
    const runtime = node.latest_run?.runtime_seconds ?? node.runtime_seconds ?? null;

    return (
      <div
        ref={ref}
        data-node-id={node.id}
        className={cn(
          "relative z-10 min-w-0 max-w-full overflow-hidden rounded-lg border bg-[var(--hc-panel-card)] p-3 shadow-sm transition",
          "border-[var(--hc-border)] hover:border-[var(--hc-border-strong)]",
          isRoot && "ring-1 ring-[var(--hc-accent-border)]",
        )}
      >
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="hc-mono min-w-0 max-w-full truncate text-[10px] text-[var(--hc-text-dim)]">
            {de.ketten.nodeTaskId} {node.id}
          </span>
          {isRoot ? (
            <span className="ml-auto rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--hc-accent-text)]">
              {de.ketten.focusRoot}
            </span>
          ) : null}
        </div>

        <p className="mt-1.5 line-clamp-2 text-sm font-semibold leading-snug text-[var(--hc-text)]">
          {node.title}
        </p>

        <div className="mt-2.5 flex min-w-0 flex-wrap items-center gap-1.5">
          <StatusPill tone={tone} label={taskStatusLabel[node.status] ?? node.status} dot={dot} size="sm" />
          {node.assignee ? (
            <span className="inline-flex items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-1 text-xs font-medium text-[var(--hc-accent-text)]">
              {node.assignee}
            </span>
          ) : null}
        </div>

        <div className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          {runtime != null ? (
            <div className="flex min-w-0 items-center gap-1.5 text-[var(--hc-text-soft)]">
              <span className="hc-type-label text-[var(--hc-text-dim)]">{de.ketten.runtimeLabel}</span>
              <span className="hc-mono">{fmtDur(runtime)}</span>
            </div>
          ) : null}
          {heartbeatAge != null ? (
            <div className="flex min-w-0 items-center gap-1.5 text-[var(--hc-text-soft)]">
              <Led kind="live" size={6} />
              <span className="hc-type-label text-[var(--hc-text-dim)]">{de.ketten.heartbeatLabel}</span>
              <span className="hc-mono">{heartbeatAge <= 2 ? de.ketten.heartbeatNow : fmtAge(heartbeatTs!, now)}</span>
            </div>
          ) : null}
        </div>
      </div>
    );
  }),
);
