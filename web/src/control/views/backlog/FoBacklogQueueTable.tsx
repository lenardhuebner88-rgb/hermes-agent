import { cn } from "@/lib/utils";
import { StatusPill } from "../../components/atoms";
import { CommissionButton } from "../../components/fleet/CommissionButton";
import { Skeleton, SkeletonRow } from "../../components/primitives";
import {
  nextActionForFoItem,
  qualityFlagsForFoItem,
  queueStateForFoItem,
  staleSignalForFoItem,
} from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/useControlData";
import { OWNER_TONE, relLabel, RISK_TONE, sourceRef, STATUS_TONE } from "./shared";

export function FoBacklogQueueTable({
  items,
  nowSec,
  nextTaskId,
  activeId = null,
  detailById = {},
  onOpen,
  onCommission,
  commissionState = {},
}: {
  items: BacklogItem[];
  nowSec: number;
  nextTaskId: string | null;
  activeId?: string | null;
  detailById?: Record<string, BacklogDetail | undefined>;
  onOpen: (id: string) => void;
  onCommission?: (item: BacklogItem) => void;
  commissionState?: Record<string, CommissionState>;
}) {
  return (
    <div className="overflow-x-auto rounded-md border border-[var(--hc-border)] bg-white/[.015]">
      <table className="w-full table-fixed border-collapse text-left text-sm">
        <thead className="bg-white/[.035] text-[11px] uppercase text-zinc-400">
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
            return (
              <tr
                key={item.id}
                data-fo-row={item.id}
                tabIndex={0}
                aria-current={item.id === activeId ? "true" : undefined}
                onClick={() => onOpen(item.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpen(item.id);
                  }
                }}
                className={cn(
                  "cursor-pointer border-t border-[var(--hc-border)] align-top outline-none transition hover:bg-white/[.035] focus-visible:bg-white/[.045]",
                  item.id === nextTaskId && "bg-cyan-500/[.06]",
                  item.id === activeId && "bg-white/[.05] ring-2 ring-inset ring-cyan-400/70",
                )}
              >
                <td className="px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <span className="truncate font-medium text-white">{item.title}</span>
                      {item.id === nextTaskId ? <span className="shrink-0 rounded-sm bg-cyan-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-cyan-200">NEXT</span> : null}
                    </div>
                    {item.excerpt ? <p className="mt-1 line-clamp-2 text-xs hc-dim">{item.excerpt}</p> : null}
                    {flags.length ? (
                      <div className="mt-1 flex flex-wrap gap-1">
                        {flags.slice(0, 3).map((flag) => <span key={`${item.id}-${flag.kind}`} className={cn("rounded-sm px-1.5 py-0.5 text-[10px]", flag.severity === "risk" ? "bg-amber-500/10 text-amber-200" : "bg-zinc-500/10 text-zinc-300")}>{flag.label}</span>)}
                      </div>
                    ) : null}
                  </div>
                </td>
                <td className="px-3 py-2"><StatusPill tone={queueState.state === "drift" ? "amber" : STATUS_TONE[queueState.state]} label={item.status || "missing"} /></td>
                <td className="hidden px-3 py-2 md:table-cell"><StatusPill tone={RISK_TONE[item.risk] ?? "amber"} label={item.risk || "missing"} /></td>
                <td className="hidden px-3 py-2 lg:table-cell"><StatusPill tone={OWNER_TONE[item.owner] ?? "amber"} label={item.owner || "missing"} /></td>
                <td className="hidden px-3 py-2 text-zinc-200 xl:table-cell">{item.area || "-"}</td>
                <td className="hidden px-3 py-2 md:table-cell"><span className="hc-mono text-xs hc-soft">{relLabel(item.updated, nowSec)}</span></td>
                <td className="hidden px-3 py-2 lg:table-cell"><span className={cn("text-xs", stale.state === "stale" ? "text-red-200" : stale.state === "missing_update" ? "text-amber-200" : "hc-soft")}>{stale.label}</span></td>
                <td className="hidden px-3 py-2 xl:table-cell"><span className="block truncate hc-mono text-[11px] text-zinc-400">{sourceRef(item)}</span><span className="hc-mono text-[11px] text-zinc-500">{item.id}</span></td>
                <td className="px-3 py-2">
                  <p className="line-clamp-3 text-sm text-zinc-100">{nextActionForFoItem(item, detail)}</p>
                  {onCommission ? (
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
    <div className="overflow-hidden rounded-md border border-[var(--hc-border)] bg-white/[.015]" aria-busy="true" aria-label="Backlog queue loading">
      <div className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-b border-[var(--hc-border)] bg-white/[.035] px-3 py-2">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-28" />
      </div>
      <div className="space-y-0">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-t border-[var(--hc-border)] px-3 py-3 first:border-t-0">
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
