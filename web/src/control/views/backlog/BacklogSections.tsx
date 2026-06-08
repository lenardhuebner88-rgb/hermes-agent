import { Keyboard, LayoutGrid, List } from "lucide-react";

import { cn } from "@/lib/utils";
import { FoBacklogCard } from "../../components/FoBacklogCard";
import { StatusPill, ToneCallout } from "../../components/atoms";
import { Card, Disclosure, Panel, Section, SkeletonCard, Stat, Stagger, StaggerItem } from "../../components/primitives";
import { de } from "../../i18n/de";
import {
  buildFoCommissionPrompt,
  reasonCodesForFoItem,
  staleSignalForFoItem,
} from "../../lib/foBacklog";
import type { FoOwnerLoad, FoQuickView, FoRankedCandidate } from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/useControlData";
import { CopyButton } from "./CopyButton";
import { FoBacklogQueueTable, FoBacklogQueueSkeleton } from "./FoBacklogQueueTable";
import { ReasonChips } from "./ReasonChips";
import { ACTIVE_COLUMNS, clockLabel, QUICK_VIEWS, sourceRef, STATUS_TONE, type ViewMode } from "./shared";

export function BacklogHeroPanel({
  activeTotal,
  doneTotal,
  breakdown,
  loading,
  nowSec,
  auditPrompt,
  viewMode,
  onViewMode,
}: {
  activeTotal: number;
  doneTotal: number;
  breakdown: { now: number; next: number; in_progress: number; blocked: number; later: number };
  loading: boolean;
  nowSec: number;
  auditPrompt: string;
  viewMode: ViewMode;
  onViewMode: (mode: ViewMode) => void;
}) {
  return (
    <Panel
      eyebrow={de.backlog.eyebrow}
      title={`${de.backlog.title} · ${de.backlog.summaryLine(activeTotal, doneTotal)}`}
      surface="card"
      actions={
        <>
          <div className="mr-2 text-xs hc-soft">{loading ? de.backlog.loading : de.backlog.updatedAt(clockLabel(nowSec))}</div>
          <CopyButton text={auditPrompt} label={de.backlog.audit} copiedLabel={de.backlog.auditCopied} />
          <button type="button" onClick={() => onViewMode("queue")} className={cn("grid h-9 w-9 place-items-center rounded-md border", viewMode === "queue" ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:text-zinc-200")} title="Queue">
            <List className="h-4 w-4" />
          </button>
          <button type="button" onClick={() => onViewMode("board")} className={cn("grid h-9 w-9 place-items-center rounded-md border", viewMode === "board" ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:text-zinc-200")} title="Board">
            <LayoutGrid className="h-4 w-4" />
          </button>
        </>
      }
    >
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_120px] md:items-end">
        <div>
          {activeTotal > 0 ? (
            <p className="text-xs hc-soft">{de.backlog.summaryBreakdown(breakdown.now, breakdown.next, breakdown.in_progress, breakdown.blocked, breakdown.later)}</p>
          ) : (
            <p className="text-xs hc-soft">{de.backlog.subtitle}</p>
          )}
        </div>
        <Stat label="Aktiv" value={activeTotal} hint={`${doneTotal} done`} accent />
      </div>
    </Panel>
  );
}

export function NextTaskSpotlight({
  nextTask,
  allItemsLength,
  nowSec,
  commissionPrompt,
}: {
  nextTask: BacklogItem | null;
  allItemsLength: number;
  nowSec: number;
  commissionPrompt?: string;
}) {
  if (!nextTask) {
    return allItemsLength > 0 ? <ToneCallout tone="zinc">{de.backlog.noNextTask}</ToneCallout> : null;
  }
  return (
    <Card tone="sky" surface="card" className="p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase text-cyan-300">{de.backlog.nextTask}</p>
          <p className="mt-0.5 truncate text-sm font-medium text-white">{nextTask.title}</p>
          <p className="mt-0.5 hc-mono text-[11px] hc-dim">{sourceRef(nextTask)}</p>
          <div className="mt-1.5">
            <ReasonChips codes={reasonCodesForFoItem(nextTask, nowSec)} />
          </div>
        </div>
        <CopyButton text={commissionPrompt} label={de.backlog.commission} copiedLabel={de.backlog.commissionCopied} />
      </div>
    </Card>
  );
}

export function CandidateCompareStrip({
  topCandidates,
  detailById,
  nowSec,
  onOpen,
}: {
  topCandidates: FoRankedCandidate[];
  detailById: Record<string, BacklogDetail | undefined>;
  nowSec: number;
  onOpen: (id: string) => void;
}) {
  if (topCandidates.length <= 1) return null;
  return (
    <Section title="Top-Kandidaten vergleichen" className="rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3">
      <Stagger className="grid gap-2 md:grid-cols-3">
        {topCandidates.map((candidate, index) => {
          const item = candidate.item;
          const candidateDetail = detailById[item.id];
          return (
            <StaggerItem key={item.id}>
              <article className="flex min-w-0 flex-col gap-1.5 rounded-md border border-[var(--hc-border)] bg-white/[.02] p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="hc-mono text-[10px] text-zinc-500">#{index + 1} · {item.id}</span>
                  <StatusPill tone={STATUS_TONE[item.status] ?? "amber"} label={item.status || "?"} />
                </div>
                <button type="button" onClick={() => onOpen(item.id)} className="truncate text-left text-sm font-medium text-white hover:text-cyan-200">
                  {item.title}
                </button>
                <p className="text-[11px] hc-dim">{item.risk || "?"} · {item.area || "?"} · {staleSignalForFoItem(item, nowSec).label}</p>
                <ReasonChips codes={candidate.reasonCodes} max={3} />
                <div className="mt-auto pt-1">
                  <CopyButton text={candidateDetail ? buildFoCommissionPrompt(candidateDetail) : undefined} label={de.backlog.commission} copiedLabel={de.backlog.commissionCopied} />
                </div>
              </article>
            </StaggerItem>
          );
        })}
      </Stagger>
    </Section>
  );
}

export function OwnerLoadStrip({ ownerLoad }: { ownerLoad: FoOwnerLoad[] }) {
  if (!ownerLoad.length) return null;
  return (
    <section className="grid gap-2 md:grid-cols-2 xl:grid-cols-4" aria-label="Owner load summary">
      {ownerLoad.map((owner) => (
        <Card key={owner.owner} surface="card" className="px-3 py-2">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate text-sm font-medium text-white">{owner.owner}</span>
            <span className="hc-mono text-xs hc-soft">{owner.total}</span>
          </div>
          <p className="mt-1 text-xs hc-dim">High {owner.highRisk} · Stale {owner.stale} · Unready {owner.unready}</p>
        </Card>
      ))}
    </section>
  );
}

export function QuickViewChips({
  allItemsLength,
  quickView,
  showHelp,
  onQuickView,
  onToggleHelp,
}: {
  allItemsLength: number;
  quickView: FoQuickView;
  showHelp: boolean;
  onQuickView: (view: FoQuickView) => void;
  onToggleHelp: () => void;
}) {
  if (allItemsLength <= 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Gespeicherte Ansichten">
      {QUICK_VIEWS.map((view) => (
        <button
          key={view.id}
          type="button"
          aria-pressed={quickView === view.id}
          onClick={() => onQuickView(view.id)}
          className={cn(
            "rounded-md border px-2.5 py-1 text-xs font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70",
            quickView === view.id ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200" : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
          )}
        >
          {view.label}
        </button>
      ))}
      <button
        type="button"
        aria-pressed={showHelp}
        onClick={onToggleHelp}
        title="Tastenkürzel"
        className="ml-auto inline-flex items-center gap-1.5 rounded-md border border-white/10 px-2.5 py-1 text-xs text-zinc-400 transition hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400/70"
      >
        <Keyboard className="h-3.5 w-3.5" /> Tasten
      </button>
    </div>
  );
}

export function KeyboardHelp({ showHelp }: { showHelp: boolean }) {
  if (!showHelp) return null;
  return (
    <section aria-label="Tastenkürzel">
      <Card tone="sky" surface="card" className="px-3 py-2 text-xs text-zinc-200">
        <span className="font-semibold text-cyan-200">Tastatur:</span>{" "}
        <kbd className="hc-mono">j</kbd>/<kbd className="hc-mono">k</kbd> bewegen ·{" "}
        <kbd className="hc-mono">Enter</kbd> öffnen ·{" "}
        <kbd className="hc-mono">Esc</kbd> schließen ·{" "}
        <kbd className="hc-mono">?</kbd> Hilfe
      </Card>
    </section>
  );
}

export function BacklogBoard({
  filteredByStatus,
  gap,
  nowSec,
  nextTaskId,
  loading,
  onOpen,
}: {
  filteredByStatus: Record<string, BacklogItem[]>;
  gap: string;
  nowSec: number;
  nextTaskId: string | null;
  loading: boolean;
  onOpen: (id: string) => void;
}) {
  if (loading) {
    return (
      <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
        {ACTIVE_COLUMNS.map((col) => <SkeletonCard key={col.key} rows={4} />)}
      </div>
    );
  }
  return (
    <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}>
      {ACTIVE_COLUMNS.map((col) => {
        const items = filteredByStatus[col.key] ?? [];
        return (
          <section key={col.key} className="min-w-0 rounded-md border border-[var(--hc-border)] bg-white/[.02] p-3">
            <div className="mb-2 flex items-center justify-between">
              <StatusPill tone={col.tone} label={col.label} />
              <span className="hc-mono text-xs hc-dim">{items.length}</span>
            </div>
            <div className="space-y-2">
              {items.length ? items.map((item) => <FoBacklogCard key={item.id} item={item} nowSec={nowSec} isNext={item.id === nextTaskId} onOpen={onOpen} />) : <p className="py-3 text-center text-xs hc-dim">{de.backlog.emptyColumn}</p>}
            </div>
          </section>
        );
      })}
    </div>
  );
}

export function QueueSurface({
  loading,
  filteredActive,
  nowSec,
  nextTaskId,
  activeId,
  detailById,
  onOpen,
  onCommission,
  commissionState,
}: {
  loading: boolean;
  filteredActive: BacklogItem[];
  nowSec: number;
  nextTaskId: string | null;
  activeId: string | null;
  detailById: Record<string, BacklogDetail | undefined>;
  onOpen: (id: string) => void;
  onCommission?: (item: BacklogItem) => void;
  commissionState?: Record<string, CommissionState>;
}) {
  if (loading) return <FoBacklogQueueSkeleton />;
  return filteredActive.length ? (
    <FoBacklogQueueTable items={filteredActive} nowSec={nowSec} nextTaskId={nextTaskId} activeId={activeId} detailById={detailById} onOpen={onOpen} onCommission={onCommission} commissionState={commissionState} />
  ) : (
    <p className="py-4 text-center text-sm hc-dim">{de.backlog.empty}</p>
  );
}

export function DoneSection({
  doneItems,
  showAllDone,
  nowSec,
  detailById,
  onToggleShowAll,
  onOpen,
}: {
  doneItems: BacklogItem[];
  showAllDone: boolean;
  nowSec: number;
  detailById: Record<string, BacklogDetail | undefined>;
  onToggleShowAll: () => void;
  onOpen: (id: string) => void;
}) {
  return (
    <Card surface="card" className="p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <StatusPill tone="emerald" label={de.backlog.colDone} />
          <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
        </div>
        {doneItems.length > 5 ? (
          <button type="button" onClick={onToggleShowAll} className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5">
            {showAllDone ? de.backlog.showRecent : de.backlog.showAll}
          </button>
        ) : null}
      </div>
      <Disclosure
        open
        summary={
          <div className="text-[11px] font-semibold uppercase text-zinc-400">
            {de.backlog.colDone} Queue
          </div>
        }
      >
        {doneItems.length ? (
          <FoBacklogQueueTable items={showAllDone ? doneItems : doneItems.slice(0, 5)} nowSec={nowSec} nextTaskId={null} detailById={detailById} onOpen={onOpen} />
        ) : (
          <p className="py-2 text-xs hc-dim">{de.backlog.empty}</p>
        )}
      </Disclosure>
    </Card>
  );
}
