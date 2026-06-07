import { cn } from "@/lib/utils";
import { StatusPill } from "../../components/atoms";
import { Skeleton, SkeletonRow } from "../../components/primitives";
import { de } from "../../i18n/de";
import { ageLabel, nextActionForItem } from "../../lib/orchestration";
import type { OrchestrationItem } from "../../lib/schemas";
import { ownerLabel, priorityTone, proofLabel, sourceLabel, statusTone } from "./shared";

export function OrchestratorQueueTable({
  items,
  allItems,
  nowSec,
  nextTaskId,
  onOpen,
}: {
  items: ReadonlyArray<OrchestrationItem>;
  allItems: ReadonlyArray<OrchestrationItem>;
  nowSec: number;
  nextTaskId: string | null;
  onOpen: (id: string) => void;
}) {
  if (items.length === 0) {
    return <p className="py-4 text-center text-sm hc-dim">{de.orchestrator.empty}</p>;
  }

  return (
    <section className="overflow-hidden rounded-md border border-[var(--hc-border)] bg-white/[.015]">
      <div className="border-b border-[var(--hc-border)] bg-white/[.025] px-3 py-2">
        <h3 className="text-sm font-semibold text-white">{de.orchestrator.queueTitle}</h3>
      </div>
      <div className="hidden grid-cols-[minmax(220px,2fr)_96px_112px_112px_84px_140px_150px_minmax(160px,1.2fr)] gap-3 border-b border-[var(--hc-border)] bg-white/[.035] px-3 py-2 text-[10px] font-semibold uppercase tracking-wide hc-dim md:grid">
        <span>{de.orchestrator.colTitle}</span>
        <span>{de.orchestrator.colStatus}</span>
        <span>{de.orchestrator.colRiskPriority}</span>
        <span>{de.orchestrator.colOwner}</span>
        <span>{de.orchestrator.colAge}</span>
        <span>{de.orchestrator.colSource}</span>
        <span>{de.orchestrator.colLastProof}</span>
        <span>{de.orchestrator.colNextAction}</span>
      </div>
      <div className="divide-y divide-[var(--hc-border)]">
        {items.map((item) => {
          const nextAction = nextActionForItem(item, allItems);
          const isNext = item.id === nextTaskId;
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onOpen(item.id)}
              className={cn(
                "grid w-full grid-cols-1 gap-2 px-3 py-3 text-left transition hover:bg-white/[.035] focus:outline-none focus:ring-2 focus:ring-inset focus:ring-cyan-400/60 md:grid-cols-[minmax(220px,2fr)_96px_112px_112px_84px_140px_150px_minmax(160px,1.2fr)] md:items-center md:gap-3",
                isNext && "bg-cyan-500/5",
              )}
            >
              <div className="min-w-0">
                {isNext ? (
                  <span className="mb-1 inline-block rounded-sm bg-cyan-400/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-cyan-300">
                    {de.orchestrator.nextBadge}
                  </span>
                ) : null}
                <p className="truncate text-sm font-medium text-white">{item.title}</p>
                <p className="mt-0.5 truncate text-[11px] hc-mono hc-dim">{item.id}</p>
              </div>
              <div>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colStatus}</span>
                <StatusPill tone={statusTone(item.status)} label={item.status || de.orchestrator.statusDrift} />
              </div>
              <div>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colRiskPriority}</span>
                <StatusPill tone={priorityTone(item.priority)} label={item.priority || "n/a"} />
              </div>
              <div className={cn("truncate text-sm", item.owner ? "text-zinc-100" : "text-amber-200")}>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colOwner}</span>
                {ownerLabel(item)}
              </div>
              <div className="text-sm hc-soft">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colAge}</span>
                {ageLabel(item.created, nowSec)}
              </div>
              <div className="truncate text-sm hc-soft">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colSource}</span>
                {sourceLabel(item)}
              </div>
              <div className={cn("truncate text-sm", item.lastProof ? "text-zinc-100" : "hc-dim")}>
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colLastProof}</span>
                {proofLabel(item)}
              </div>
              <div className="min-w-0 text-sm text-white">
                <span className="mb-1 block text-[10px] uppercase hc-dim md:hidden">{de.orchestrator.colNextAction}</span>
                <span className="line-clamp-2">{nextAction}</span>
              </div>
            </button>
          );
        })}
      </div>
    </section>
  );
}

export function OrchestratorQueueSkeleton() {
  return (
    <div className="overflow-hidden rounded-md border border-[var(--hc-border)] bg-white/[.015]" aria-busy="true" aria-label="Orchestrator queue loading">
      <div className="border-b border-[var(--hc-border)] bg-white/[.025] px-3 py-2">
        <Skeleton className="h-4 w-36" />
      </div>
      <div className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-b border-[var(--hc-border)] bg-white/[.035] px-3 py-2">
        <Skeleton className="h-3 w-24" />
        <Skeleton className="h-3 w-14" />
        <Skeleton className="h-3 w-20" />
        <Skeleton className="h-3 w-28" />
      </div>
      {Array.from({ length: 5 }).map((_, index) => (
        <div key={index} className="grid grid-cols-[minmax(180px,1fr)_80px_80px_minmax(160px,1fr)] gap-3 border-t border-[var(--hc-border)] px-3 py-3 first:border-t-0">
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
