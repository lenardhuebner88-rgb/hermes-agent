import { cn } from "@/lib/utils";
import { CommissionButton } from "../../components/fleet/CommissionButton";
import { SignalChip } from "../../components/leitstand";
import { Skeleton, SkeletonRow } from "../../components/primitives";
import { de } from "../../i18n/de";
import { ageLabel, nextActionForItem } from "../../lib/orchestration";
import type { OrchestrationItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/commissionCapture";
import { ownerLabel, priorityTone, proofLabel, sourceLabel, statusTone } from "./shared";

export function OrchestratorQueueTable({
  items,
  allItems,
  nowSec,
  nextTaskId,
  onOpen,
  onCommission,
  commissionState = {},
}: {
  items: ReadonlyArray<OrchestrationItem>;
  allItems: ReadonlyArray<OrchestrationItem>;
  nowSec: number;
  nextTaskId: string | null;
  onOpen: (id: string) => void;
  onCommission?: (item: OrchestrationItem) => void;
  commissionState?: Record<string, CommissionState>;
}) {
  if (items.length === 0) {
    return <p className="py-4 text-center text-body text-ink-3">{de.orchestrator.empty}</p>;
  }

  // 8-col track minima (220+96+112+112+84+140+150+160) + 7×gap-3 = 1158px ≈ 72.375rem.
  // md:min-w-[73rem] keeps header/rows aligned and full-width hover surfaces when the
  // tablet viewport is narrower than the grid (overflow scrolls inside, not page-wide).
  const queueGridCols =
    "md:grid-cols-[minmax(220px,2fr)_96px_112px_112px_84px_140px_150px_minmax(160px,1.2fr)]";

  return (
    <section className="overflow-hidden rounded-panel border border-line bg-surface-1">
      <div className="border-b border-line-soft bg-surface-2 px-3 py-2">
        <h3 className="font-display text-sec font-semibold uppercase tracking-[0.08em] text-ink">{de.orchestrator.queueTitle}</h3>
      </div>
      <div className="overflow-x-auto">
        <div className="md:min-w-[73rem]">
          <div className={cn("hidden gap-3 border-b border-line-soft bg-surface-2 px-3 py-2 font-display text-micro font-semibold uppercase tracking-[0.08em] text-ink-3 md:grid", queueGridCols)}>
            <span>{de.orchestrator.colTitle}</span>
            <span>{de.orchestrator.colStatus}</span>
            <span>{de.orchestrator.colRiskPriority}</span>
            <span>{de.orchestrator.colOwner}</span>
            <span>{de.orchestrator.colAge}</span>
            <span>{de.orchestrator.colSource}</span>
            <span>{de.orchestrator.colLastProof}</span>
            <span>{de.orchestrator.colNextAction}</span>
          </div>
          <div className="divide-y divide-line-soft">
            {items.map((item) => {
              const nextAction = nextActionForItem(item, allItems);
              const isNext = item.id === nextTaskId;
              return (
                <div
                  key={item.id}
                  role="button"
                  tabIndex={0}
                  onClick={() => onOpen(item.id)}
                  onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onOpen(item.id); } }}
                  className={cn(
                    "grid min-h-12 w-full cursor-pointer grid-cols-1 gap-2 px-3 py-3 text-left transition hover:bg-surface-3 focus:outline-none focus:ring-2 focus:ring-inset focus:ring-live/60 md:items-center md:gap-3",
                    queueGridCols,
                    isNext && "bg-live/5",
                  )}
                >
                  <div className="min-w-0">
                    {isNext ? (
                      <span className="mb-1 inline-block font-display text-micro font-semibold uppercase tracking-[0.08em] text-live">
                        {de.orchestrator.nextBadge}
                      </span>
                    ) : null}
                    <p className="truncate text-sec font-medium text-ink">{item.title}</p>
                    <p className="mt-0.5 truncate font-data text-micro tabular-nums text-ink-3">{item.id}</p>
                  </div>
                  <div>
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colStatus}</span>
                    <SignalChip tone={statusTone(item.status)} label={item.status || de.orchestrator.statusDrift} />
                  </div>
                  <div>
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colRiskPriority}</span>
                    <SignalChip tone={priorityTone(item.priority)} label={item.priority || "n/a"} />
                  </div>
                  <div className={cn("truncate text-sec", item.owner ? "text-ink" : "text-ink-2")}>
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colOwner}</span>
                    {ownerLabel(item)}
                  </div>
                  <div className="font-data text-sec tabular-nums text-ink-2">
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colAge}</span>
                    {ageLabel(item.created, nowSec)}
                  </div>
                  <div className="truncate font-data text-sec text-ink-2">
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colSource}</span>
                    {sourceLabel(item)}
                  </div>
                  <div className={cn("truncate text-sec", item.lastProof ? "text-ink" : "text-ink-3")}>
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colLastProof}</span>
                    {proofLabel(item)}
                  </div>
                  <div className="min-w-0 text-sec text-ink">
                    <span className="mb-1 block font-display text-micro uppercase tracking-[0.08em] text-ink-3 md:hidden">{de.orchestrator.colNextAction}</span>
                    <span className="line-clamp-2">{nextAction}</span>
                    {onCommission ? (
                      <div className="mt-2">
                        <CommissionButton state={commissionState[item.id]} onClick={(event) => { event.stopPropagation(); onCommission(item); }} />
                      </div>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

export function OrchestratorQueueSkeleton() {
  return (
    <div className="overflow-hidden rounded-panel border border-line bg-surface-1" aria-busy="true" aria-label="Orchestrator queue loading">
      <div className="border-b border-line-soft bg-surface-2 px-3 py-2">
        <Skeleton className="h-4 w-36" />
      </div>
      <div className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-b border-line-soft bg-surface-2 px-3 py-2">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-28" />
      </div>
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-t border-line-soft px-3 py-3 first:border-t-0">
          <div className="space-y-2">
            <SkeletonRow className="w-3/4" />
            <SkeletonRow className="w-1/3" />
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
  );
}
