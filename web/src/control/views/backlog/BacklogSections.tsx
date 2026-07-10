import { Keyboard, LayoutGrid, List } from "lucide-react";

import { cn } from "@/lib/utils";
import { FoBacklogCard } from "../../components/FoBacklogCard";
import { Card, Disclosure, Eyebrow, Panel, Section, SkeletonCard, Stagger, StaggerItem } from "../../components/primitives";
import { KpiTile, SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import { de } from "../../i18n/de";
import {
  buildFoCommissionPrompt,
  reasonCodesForFoItem,
  staleSignalForFoItem,
} from "../../lib/foBacklog";
import type { FoOwnerLoad, FoQuickView, FoRankedCandidate } from "../../lib/foBacklog";
import type { BacklogDetail, BacklogItem } from "../../lib/schemas";
import type { CommissionState, DispatchFoState, FoBoardStatus } from "../../hooks/useControlData";
import { CopyButton } from "./CopyButton";
import { FoBacklogQueueTable, FoBacklogQueueSkeleton } from "./FoBacklogQueueTable";
import { ReasonChips } from "./ReasonChips";
import { ACTIVE_COLUMNS, clockLabel, QUICK_VIEWS, sourceRef, STATUS_TONE, type ViewMode } from "./shared";
import { partitionReadinessZones } from "./readinessZones";

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
          <div className={cn("mr-2 text-ink-3", loading ? "text-sec" : "font-data text-micro tabular-nums")}>{loading ? de.backlog.loading : de.backlog.updatedAt(clockLabel(nowSec))}</div>
          <CopyButton text={auditPrompt} label={de.backlog.audit} copiedLabel={de.backlog.auditCopied} />
          <button type="button" onClick={() => onViewMode("queue")} aria-label="Queue-Ansicht" aria-pressed={viewMode === "queue"} className={cn("grid size-12 place-items-center rounded-card border transition", viewMode === "queue" ? "border-live bg-live/10 text-live" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink")} title="Queue">
            <List className="h-4 w-4" />
          </button>
          <button type="button" onClick={() => onViewMode("board")} aria-label="Board-Ansicht" aria-pressed={viewMode === "board"} className={cn("grid size-12 place-items-center rounded-card border transition", viewMode === "board" ? "border-live bg-live/10 text-live" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink")} title="Board">
            <LayoutGrid className="h-4 w-4" />
          </button>
        </>
      }
    >
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_auto] lg:items-center" aria-label="Backlog Zusammenfassung">
        <p className="text-sec leading-relaxed text-ink-2">
          {activeTotal > 0
            ? de.backlog.summaryBreakdown(breakdown.now, breakdown.next, breakdown.in_progress, breakdown.blocked, breakdown.later)
            : de.backlog.subtitle}
        </p>
        <div className="grid grid-cols-[repeat(2,minmax(0,1fr))] gap-2 sm:flex sm:flex-wrap lg:justify-end">
          <KpiTile className="sm:min-w-28" label="Aktiv" value={activeTotal} />
          <KpiTile className="sm:min-w-28" label="Erledigt" value={doneTotal} />
        </div>
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
    return allItemsLength > 0 ? <div className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">{de.backlog.noNextTask}</div> : null;
  }
  return (
    <Card surface="card" className="border-live/30 p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Eyebrow className="text-live">{de.backlog.nextTask}</Eyebrow>
          <p className="mt-0.5 truncate text-sec font-medium text-ink">{nextTask.title}</p>
          <p className="mt-0.5 font-data text-micro tabular-nums text-ink-3">{sourceRef(nextTask)}</p>
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
    <Section title="Top-Kandidaten vergleichen" className="rounded-panel border border-line bg-surface-1 p-3">
      <Stagger className="grid gap-2 md:grid-cols-3">
        {topCandidates.map((candidate, index) => {
          const item = candidate.item;
          const candidateDetail = detailById[item.id];
          return (
            <StaggerItem key={item.id}>
              <article className="flex min-w-0 flex-col gap-1.5 rounded-card border border-line bg-surface-2 p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-data text-micro tabular-nums text-ink-3">#{index + 1} · {item.id}</span>
                  <SignalLabel tone={signalToneFromLegacy(STATUS_TONE[item.status] ?? "amber")} label={item.status || "?"} />
                </div>
                <button type="button" onClick={() => onOpen(item.id)} className="inline-flex min-h-12 items-center truncate text-left text-sec font-medium text-ink hover:text-live">
                  {item.title}
                </button>
                <p className="text-micro text-ink-2">{item.risk || "?"} · {item.area || "?"} · {staleSignalForFoItem(item, nowSec).label}</p>
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
            <span className="truncate text-sec font-medium text-ink">{owner.owner}</span>
            <span className="font-data text-sec tabular-nums text-ink-2">{owner.total}</span>
          </div>
          <p className="mt-1 text-sec text-ink-2">High {owner.highRisk} · Stale {owner.stale} · Unready {owner.unready}</p>
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
            "inline-flex min-h-12 items-center rounded-card border px-2.5 text-sec font-medium transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-live/70",
            quickView === view.id ? "border-live bg-live/10 text-bronze-hi" : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink",
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
        className="ml-auto inline-flex min-h-12 items-center gap-1.5 rounded-card border border-line px-2.5 text-sec text-ink-2 transition hover:bg-surface-3 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-live/70"
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
      <Card surface="card" className="border-line px-3 py-2 text-sec text-ink-2">
        <Eyebrow className="inline">Tastatur:</Eyebrow>{" "}
        <kbd className="font-data">j</kbd>/<kbd className="font-data">k</kbd> bewegen ·{" "}
        <kbd className="font-data">Enter</kbd> öffnen ·{" "}
        <kbd className="font-data">Esc</kbd> schließen ·{" "}
        <kbd className="font-data">?</kbd> Hilfe
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
          <section key={col.key} className="min-w-0 rounded-panel border border-line bg-surface-1 p-3">
            <div className="mb-2 flex items-center justify-between">
              <SignalLabel tone={signalToneFromLegacy(col.tone)} label={col.label} />
              <span className="font-data text-sec tabular-nums text-ink-3">{items.length}</span>
            </div>
            <div className="space-y-2">
              {items.length ? items.map((item) => <FoBacklogCard key={item.id} item={item} nowSec={nowSec} isNext={item.id === nextTaskId} onOpen={onOpen} />) : <p className="py-3 text-center text-sec text-ink-3">{de.backlog.emptyColumn}</p>}
            </div>
          </section>
        );
      })}
    </div>
  );
}

export function ReadinessZones({
  filteredActive,
  nowSec,
  nextTaskId,
  activeId,
  detailById,
  onOpen,
  onCommission,
  commissionState,
  boardStatusById,
  onDispatch,
  dispatchStateByTaskId,
}: {
  filteredActive: BacklogItem[];
  nowSec: number;
  nextTaskId: string | null;
  activeId: string | null;
  detailById: Record<string, BacklogDetail | undefined>;
  onOpen: (id: string) => void;
  onCommission?: (item: BacklogItem) => void;
  commissionState?: Record<string, CommissionState>;
  boardStatusById?: Record<string, FoBoardStatus>;
  onDispatch?: (taskId: string) => void;
  dispatchStateByTaskId?: Record<string, DispatchFoState>;
}) {
  const { ready, grooming, ideas } = partitionReadinessZones(filteredActive);

  return (
    <div className="space-y-3">
      {/* Zone 1 — Bereit: always open */}
      <section aria-label="Bereit">
        <div className="mb-2 flex items-center gap-2">
          <SignalLabel tone="ok" label="Bereit" />
          <span className="font-data text-sec tabular-nums text-ink-3">{ready.length}</span>
        </div>
        {ready.length ? (
          <FoBacklogQueueTable
            items={ready}
            nowSec={nowSec}
            nextTaskId={nextTaskId}
            activeId={activeId}
            detailById={detailById}
            onOpen={onOpen}
            onCommission={onCommission}
            commissionState={commissionState}
            boardStatusById={boardStatusById}
            onDispatch={onDispatch}
            dispatchStateByTaskId={dispatchStateByTaskId}
          />
        ) : (
          <p className="py-3 text-center text-sec text-ink-3">Keine bereit-Tasks.</p>
        )}
      </section>

      {/* Zone 2 — Schleifen: collapsed by default */}
      <section aria-label="Schleifen">
        <Disclosure
          defaultOpen={false}
          summary={
            <div className="flex min-h-12 items-center gap-2">
              <SignalLabel tone="warn" label="Schleifen" />
              <span className="font-data text-sec tabular-nums text-ink-3">{grooming.length}</span>
            </div>
          }
        >
          {grooming.length ? (
            <FoBacklogQueueTable
              items={grooming}
              nowSec={nowSec}
              nextTaskId={nextTaskId}
              activeId={activeId}
              detailById={detailById}
              onOpen={onOpen}
              onCommission={onCommission}
              commissionState={commissionState}
              boardStatusById={boardStatusById}
              onDispatch={onDispatch}
              dispatchStateByTaskId={dispatchStateByTaskId}
            />
          ) : (
            <p className="py-3 text-center text-sec text-ink-3">{de.backlog.empty}</p>
          )}
        </Disclosure>
      </section>

      {/* Zone 3 — Ideenspeicher: collapsed by default */}
      <section aria-label="Ideenspeicher">
        <Disclosure
          defaultOpen={false}
          summary={
            <div className="flex min-h-12 items-center gap-2">
              <Eyebrow>Ideenspeicher</Eyebrow>
              <span className="font-data text-sec tabular-nums text-ink-3">{ideas.length}</span>
            </div>
          }
        >
          {ideas.length ? (
            <FoBacklogQueueTable
              items={ideas}
              nowSec={nowSec}
              nextTaskId={nextTaskId}
              activeId={activeId}
              detailById={detailById}
              onOpen={onOpen}
              onCommission={onCommission}
              commissionState={commissionState}
              boardStatusById={boardStatusById}
              onDispatch={onDispatch}
              dispatchStateByTaskId={dispatchStateByTaskId}
            />
          ) : (
            <p className="py-3 text-center text-sec text-ink-3">{de.backlog.empty}</p>
          )}
        </Disclosure>
      </section>
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
  boardStatusById,
  onDispatch,
  dispatchStateByTaskId,
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
  boardStatusById?: Record<string, FoBoardStatus>;
  onDispatch?: (taskId: string) => void;
  dispatchStateByTaskId?: Record<string, DispatchFoState>;
}) {
  if (loading) return <FoBacklogQueueSkeleton />;
  return filteredActive.length ? (
    <ReadinessZones
      filteredActive={filteredActive}
      nowSec={nowSec}
      nextTaskId={nextTaskId}
      activeId={activeId}
      detailById={detailById}
      onOpen={onOpen}
      onCommission={onCommission}
      commissionState={commissionState}
      boardStatusById={boardStatusById}
      onDispatch={onDispatch}
      dispatchStateByTaskId={dispatchStateByTaskId}
    />
  ) : (
    <p className="py-4 text-center text-body text-ink-3">{de.backlog.empty}</p>
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
          <SignalLabel tone="ok" label={de.backlog.colDone} />
          <span className="font-data text-sec tabular-nums text-ink-3">{doneItems.length}</span>
        </div>
        {doneItems.length > 5 ? (
          <button type="button" onClick={onToggleShowAll} className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 hover:bg-surface-3 hover:text-ink">
            {showAllDone ? de.backlog.showRecent : de.backlog.showAll}
          </button>
        ) : null}
      </div>
      <Disclosure
        open
        summary={
          <div className="flex min-h-12 items-center">
            <Eyebrow>{de.backlog.colDone} Queue</Eyebrow>
          </div>
        }
      >
        {doneItems.length ? (
          <FoBacklogQueueTable items={showAllDone ? doneItems : doneItems.slice(0, 5)} nowSec={nowSec} nextTaskId={null} detailById={detailById} onOpen={onOpen} />
        ) : (
          <p className="py-2 text-sec text-ink-3">{de.backlog.empty}</p>
        )}
      </Disclosure>
    </Card>
  );
}
