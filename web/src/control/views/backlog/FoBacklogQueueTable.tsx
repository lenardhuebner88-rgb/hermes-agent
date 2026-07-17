import { ArrowRight, Check, Loader2, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { CommissionButton } from "../../components/fleet/CommissionButton";
import { SignalChip, signalToneFromLegacy } from "../../components/leitstand";
import { Skeleton, SkeletonRow } from "../../components/primitives";
import {
  nextActionForFoItem,
  qualityFlagsForFoItem,
  queueStateForFoItem,
  staleSignalForFoItem,
} from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { DispatchFoState } from "../../hooks/chainFlow";
import type { CommissionState } from "../../hooks/commissionCapture";
import type { FoBoardStatus } from "../../hooks/foBoard";
import { de } from "../../i18n/de";
import { relLabel, RISK_TONE, sourceRef, STATUS_TONE } from "./shared";

// Statuses where the operator needs to click "Freigeben" to move the task onto
// the board. Running/review/done = already active or past, blocked = stuck.
const DISPATCH_ELIGIBLE = new Set(["scheduled", "triage"]);

function DispatchButton({
  state,
  onClick,
}: {
  state?: DispatchFoState;
  onClick: (event: React.MouseEvent) => void;
}) {
  const busy = state === "busy";
  const done = state === "done";
  const err = state === "error";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || done}
      title={de.backlog.dispatchTitle}
      aria-label={de.backlog.dispatchLabel}
      className={cn(
        "inline-flex min-h-12 items-center gap-1 rounded-card border px-3 text-sec font-medium transition disabled:cursor-default",
        done
          ? "border-line bg-surface-2 text-ink-2"
          : "border-live/40 bg-live/10 text-bronze-hi hover:bg-live/15",
      )}
    >
      {busy ? <Loader2 className="h-3 w-3 animate-spin" /> : done ? <Check className="h-3 w-3" /> : err ? <RotateCcw className="h-3 w-3" /> : <ArrowRight className="h-3 w-3" />}
      {busy ? de.backlog.dispatchBusy : done ? de.backlog.dispatchDone : err ? de.backlog.dispatchRetry : de.backlog.dispatchLabel}
    </button>
  );
}

const BOARD_STATUS_TONE: Record<string, "emerald" | "cyan" | "amber" | "red" | "zinc"> = {
  running: "emerald",
  ready: "cyan",
  triage: "zinc",
  scheduled: "zinc",
  blocked: "amber",
  review: "cyan",
};

export function FoBacklogQueueTable({
  items,
  nowSec,
  nextTaskId,
  activeId = null,
  selectedId = null,
  detailById = {},
  onOpen,
  onCommission,
  commissionState = {},
  boardStatusById = {},
  onDispatch,
  dispatchStateByTaskId = {},
}: {
  items: BacklogItem[];
  nowSec: number;
  nextTaskId: string | null;
  activeId?: string | null;
  selectedId?: string | null;
  detailById?: Record<string, BacklogDetail | undefined>;
  onOpen: (id: string) => void;
  onCommission?: (item: BacklogItem) => void;
  commissionState?: Record<string, CommissionState>;
  boardStatusById?: Record<string, FoBoardStatus>;
  onDispatch?: (taskId: string) => void;
  dispatchStateByTaskId?: Record<string, DispatchFoState>;
}) {
  return (
    <div className="overflow-x-auto rounded-panel border border-line bg-surface-1">
      <table className="w-full table-fixed border-collapse text-left text-sm">
        <thead className="border-t-2 border-line bg-surface-2 font-display text-micro font-semibold uppercase tracking-[0.12em] text-ink-3">
          <tr>
            <th className="w-[30%] px-3 py-2">Title</th>
            <th className="w-[9%] px-3 py-2">Status</th>
            <th className="hidden w-[8%] px-3 py-2 md:table-cell">Risk</th>
            <th className="hidden w-[10%] px-3 py-2 lg:table-cell">Owner</th>
            <th className="hidden w-[10%] px-3 py-2 xl:table-cell">Area</th>
            <th className="hidden w-[10%] px-3 py-2 md:table-cell">Age/Updated</th>
            <th className="hidden w-[10%] px-3 py-2 lg:table-cell">Stale/Proof</th>
            <th className="hidden w-[13%] px-3 py-2 xl:table-cell">Source/Id</th>
            <th className="w-[28%] px-3 py-2">Next Action</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const detail = detailById[item.id];
            const queueState = queueStateForFoItem(item);
            const flags = qualityFlagsForFoItem(item, detail);
            const stale = staleSignalForFoItem(item, nowSec);
            const boardStatus = boardStatusById[item.id];
            return (
              <tr
                key={item.id}
                data-fo-row={item.id}
                data-active={item.id === activeId ? "true" : undefined}
                tabIndex={0}
                aria-current={item.id === selectedId ? "true" : undefined}
                onClick={(event) => {
                  event.currentTarget.focus();
                  onOpen(item.id);
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpen(item.id);
                  }
                }}
                className={cn(
                  "cursor-pointer border-t border-line-soft align-top outline-none transition hover:bg-surface-3 focus-visible:bg-surface-3 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze",
                  item.id === nextTaskId && "bg-live/5",
                  item.id === selectedId && "shadow-[inset_3px_0_0_var(--color-bronze)] bg-surface-3",
                )}
              >
                <td className="h-12 px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-ink">{item.title}</span>
                      {item.id === nextTaskId ? <span className="shrink-0 font-display text-micro font-semibold uppercase tracking-[0.08em] text-live">NEXT</span> : null}
                    </div>
                    {item.excerpt ? <p className="mt-1 line-clamp-2 text-sec text-ink-2">{item.excerpt}</p> : null}
                    {flags.length ? (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {flags.slice(0, 3).map((flag) => <span key={`${item.id}-${flag.kind}`} className={cn("rounded-card border px-1.5 py-0.5 text-micro", flag.severity === "risk" ? "border-status-warn/30 bg-status-warn/10 text-status-warn" : "border-line bg-surface-2 text-ink-2")}>{flag.label}</span>)}
                      </div>
                    ) : null}
                  </div>
                </td>
                <td className="px-3 py-2"><SignalChip tone={signalToneFromLegacy(queueState.state === "drift" ? "amber" : STATUS_TONE[queueState.state])} label={item.status || "missing"} /></td>
                <td className="hidden px-3 py-2 md:table-cell"><SignalChip tone={signalToneFromLegacy(RISK_TONE[item.risk] ?? "amber")} label={item.risk || "missing"} /></td>
                <td className="hidden px-3 py-2 text-sec text-ink-2 lg:table-cell">{item.owner || "missing"}</td>
                <td className="hidden px-3 py-2 text-ink-2 xl:table-cell">{item.area || "-"}</td>
                <td className="hidden px-3 py-2 md:table-cell"><span className="font-data text-sec tabular-nums text-ink-2">{relLabel(item.updated, nowSec)}</span></td>
                <td className="hidden px-3 py-2 lg:table-cell"><span className={cn("text-sec", stale.state === "stale" ? "text-status-alert" : stale.state === "missing_update" ? "text-status-warn" : "text-ink-2")}>{stale.label}</span></td>
                <td className="hidden px-3 py-2 xl:table-cell"><span className="block truncate font-data text-micro tabular-nums text-ink-2">{sourceRef(item)}</span><span className="font-data text-micro tabular-nums text-ink-3">{item.id}</span></td>
                <td className="px-3 py-2">
                  <p className="line-clamp-3 text-sec text-ink">{nextActionForFoItem(item, detail)}</p>
                  {boardStatus ? (
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <SignalChip tone={signalToneFromLegacy(BOARD_STATUS_TONE[boardStatus.status] ?? "zinc")} label={`Im Board · ${boardStatus.label}`} />
                      {DISPATCH_ELIGIBLE.has(boardStatus.status) && onDispatch ? (
                        <DispatchButton
                          state={dispatchStateByTaskId[boardStatus.taskId]}
                          onClick={(event) => { event.stopPropagation(); onDispatch(boardStatus.taskId); }}
                        />
                      ) : null}
                    </div>
                  ) : onCommission ? (
                    <div className="mt-2">
                      <CommissionButton state={commissionState[item.id]} onClick={(event) => { event.stopPropagation(); onCommission(item); }} />
                    </div>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function FoBacklogQueueSkeleton() {
  return (
    <div className="overflow-hidden rounded-panel border border-line bg-surface-1" aria-busy="true" aria-label="Backlog queue loading">
      <div className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-b border-line-soft border-t-2 border-t-line bg-surface-2 px-3 py-2">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-28" />
      </div>
      <div className="space-y-0">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-t border-line-soft px-3 py-3 first:border-t-0">
            <div className="space-y-2">
              <SkeletonRow className="w-3/4" />
              <SkeletonRow className="w-11/12" />
            </div>
            <Skeleton className="h-6 w-16 rounded-full" />
            <Skeleton className="h-6 w-16 rounded-full" />
            <div className="space-y-2">
              <SkeletonRow />
              <SkeletonRow className="w-2/3" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
