import { useState } from "react";
import { Check, ClipboardCopy, TriangleAlert } from "lucide-react";

import { cn } from "@/lib/utils";
import { BacklogCard } from "../../components/BacklogCard";
import { Card, Disclosure, Eyebrow, Panel, SkeletonCard, Stagger, StaggerItem } from "../../components/primitives";
import { KpiTile, SignalChip } from "../../components/leitstand";
import { de } from "../../i18n/de";
import { deriveQueueSignals, isKnownStatus } from "../../lib/orchestration";
import type { OrchestrationBacklogResponse, OrchestrationItem } from "../../lib/schemas";
import type { CommissionState } from "../../hooks/commissionCapture";
import { OrchestratorQueueSkeleton, OrchestratorQueueTable } from "./OrchestratorQueueTable";
import { ACTIVE_COLUMNS, clockLabel } from "./shared";

export function OrchestratorHeroPanel({
  activeTotal,
  loading,
  nowSec,
  data,
}: {
  activeTotal: number;
  loading: boolean;
  nowSec: number;
  data?: OrchestrationBacklogResponse;
}) {
  const updatedAtIso = new Date(nowSec * 1000).toISOString();
  return (
    <Panel
      eyebrow={de.orchestrator.eyebrow}
      title={`${de.orchestrator.title} · ${activeTotal} aktiv`}
      surface="card"
      actions={
        loading ? (
          <div className="text-left text-sec text-ink-2 sm:text-right">{de.orchestrator.loading}</div>
        ) : (
          <time dateTime={updatedAtIso} className="text-left font-data text-micro tabular-nums text-ink-3 sm:text-right">
            {de.orchestrator.updatedAt(clockLabel(nowSec))}
          </time>
        )
      }
    >
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_120px] md:items-end">
        <div>
          <p className="text-sec text-ink-2">{de.orchestrator.subtitle}</p>
          {data ? (
            <p className="mt-1 font-data text-micro tabular-nums text-ink-3">
              {data.contract_health.source_count} Quellen · {data.contract_health.counted_sum} gezählt · {data.source.ref}
            </p>
          ) : null}
        </div>
        <KpiTile label="Aktiv" value={activeTotal} delta={data ? `${data.contract_health.counted_sum} gezählt` : undefined} />
      </div>
    </Panel>
  );
}

export function ContractDriftCallout({ data }: { data: OrchestrationBacklogResponse }) {
  const health = data.contract_health;
  const unknown = health.unknown_statuses.map((entry) => `${entry.status || "(leer)"}:${entry.count}`).join(", ");
  const parts = [
    `${de.orchestrator.sourceCount}: ${health.source_count}`,
    `${de.orchestrator.countGap}: ${Math.max(0, health.source_count - health.counted_sum)}`,
    unknown ? `${de.orchestrator.unknownStatuses}: ${unknown}` : "",
    health.invalid_priority_count ? `${de.orchestrator.invalidPriority}: ${health.invalid_priority_count}` : "",
    health.missing_dep_count ? `${de.orchestrator.missingDeps}: ${health.missing_dep_count}` : "",
  ].filter(Boolean);
  return <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{parts.join(" · ")}</div>;
}

export function CommissionBanner({
  nextId,
  nextTitle,
  prompt,
}: {
  nextId: string;
  nextTitle: string;
  prompt: string | undefined;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    if (!prompt) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      /* clipboard blocked */
    }
  };

  return (
    <Card surface="card" className="border-live/30 p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <Eyebrow className="text-live">{de.orchestrator.nextTask}</Eyebrow>
          <p title={nextTitle} className="mt-0.5 line-clamp-2 text-sec font-medium text-ink sm:truncate">
            {nextTitle}
          </p>
          <p className="mt-0.5 font-data text-micro tabular-nums text-ink-3">{nextId}</p>
        </div>
        <button
          type="button"
          onClick={copy}
          disabled={!prompt}
          className={cn(
            "flex min-h-12 shrink-0 items-center gap-2 rounded-card border px-3 text-sec font-medium transition focus:outline-none focus:ring-2 focus:ring-live/60",
            !prompt
              ? "cursor-wait border-line text-ink-3"
              : "border-live bg-live/10 text-bronze-hi hover:bg-live/20",
          )}
          title={de.orchestrator.commissionHint}
        >
          {copied ? <Check className="h-4 w-4" /> : <ClipboardCopy className="h-4 w-4" />}
          {copied ? de.orchestrator.commissionCopied : de.orchestrator.commission}
        </button>
      </div>
    </Card>
  );
}

export function SignalStrip({ signals }: { signals: ReturnType<typeof deriveQueueSignals> }) {
  const tiles: Array<{ label: string; value: number; dot?: "ready" | "warn" | "error" }> = [
    { label: de.orchestrator.readyStrip, value: signals.ready, dot: "ready" },
    { label: de.orchestrator.blockedStrip, value: signals.blocked, dot: signals.blocked > 0 ? "error" : undefined },
    { label: de.orchestrator.unownedStrip, value: signals.unowned, dot: signals.unowned > 0 ? "warn" : undefined },
    { label: de.orchestrator.staleProofStrip, value: signals.staleProof, dot: signals.staleProof > 0 ? "warn" : undefined },
    { label: de.orchestrator.highRiskStrip, value: signals.highRisk, dot: signals.highRisk > 0 ? "error" : undefined },
    { label: de.orchestrator.contractDrift, value: signals.contractDrift, dot: signals.contractDrift > 0 ? "error" : undefined },
  ];
  return (
    <section className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((tile) => (
        <KpiTile key={tile.label} label={tile.label} value={tile.value} dot={tile.dot} />
      ))}
    </section>
  );
}

export function QueueSurface({
  loading,
  filteredActive,
  allItems,
  nowSec,
  nextTaskId,
  onOpen,
  onCommission,
  commissionState,
}: {
  loading: boolean;
  filteredActive: ReadonlyArray<OrchestrationItem>;
  allItems: ReadonlyArray<OrchestrationItem>;
  nowSec: number;
  nextTaskId: string | null;
  onOpen: (id: string) => void;
  onCommission?: (item: OrchestrationItem) => void;
  commissionState?: Record<string, CommissionState>;
}) {
  if (loading) return <OrchestratorQueueSkeleton />;
  return (
    <OrchestratorQueueTable
      items={filteredActive}
      allItems={allItems}
      nowSec={nowSec}
      nextTaskId={nextTaskId}
      onOpen={onOpen}
      onCommission={onCommission}
      commissionState={commissionState}
    />
  );
}

export function OrchestratorBoard({
  filteredActive,
  filteredByStatus,
  allItems,
  gap,
  nowSec,
  nextTaskId,
  loading,
  onOpen,
}: {
  filteredActive: ReadonlyArray<OrchestrationItem>;
  filteredByStatus: Record<string, OrchestrationItem[]>;
  allItems: ReadonlyArray<OrchestrationItem>;
  gap: string;
  nowSec: number;
  nextTaskId: string | null;
  loading: boolean;
  onOpen: (id: string) => void;
}) {
  if (loading) {
    return (
      <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
        {ACTIVE_COLUMNS.map((column) => <SkeletonCard key={column.key} rows={4} />)}
      </div>
    );
  }
  return (
    <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}>
      <Stagger className="contents">
      {ACTIVE_COLUMNS.map((column) => {
        const items = column.key === "__drift"
          ? filteredActive.filter((item) => item.status !== "done" && !isKnownStatus(item.status))
          : filteredByStatus[column.key] ?? [];
        return (
          <StaggerItem key={column.key}>
            <Card surface="card" className="flex min-w-0 flex-col gap-2 p-3">
              <div className="flex items-center justify-between">
                <SignalChip tone={column.tone} label={column.label} />
                <span className="font-data text-sec tabular-nums text-ink-3">{items.length}</span>
              </div>
              {items.length === 0 ? (
                <p className="py-3 text-center text-sec text-ink-3">{de.orchestrator.emptyColumn}</p>
              ) : (
                <div className="space-y-2">
                  {items.map((item) => (
                    <BacklogCard
                      key={item.id}
                      item={item}
                      allItems={allItems}
                      nowSec={nowSec}
                      onOpen={onOpen}
                      isNext={item.id === nextTaskId}
                    />
                  ))}
                </div>
              )}
            </Card>
          </StaggerItem>
        );
      })}
      </Stagger>
    </div>
  );
}

export function DoneSection({
  doneItems,
  showAllDone,
  allItems,
  gap,
  nowSec,
  onToggleShowAll,
  onOpen,
}: {
  doneItems: OrchestrationItem[];
  showAllDone: boolean;
  allItems: ReadonlyArray<OrchestrationItem>;
  gap: string;
  nowSec: number;
  onToggleShowAll: () => void;
  onOpen: (id: string) => void;
}) {
  return (
    <Card surface="card" className="p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <SignalChip tone="ok" label={de.orchestrator.colDone} />
          <span className="font-data text-sec tabular-nums text-ink-3">{doneItems.length}</span>
          <span className="hidden text-sec text-ink-2 sm:inline">· {de.orchestrator.doneRecentHint}</span>
          <span className="hidden text-sec text-ink-2 sm:inline">· {de.orchestrator.doneReceiptHint}</span>
        </div>
        {doneItems.length > 5 ? (
          <button
            type="button"
            onClick={onToggleShowAll}
            className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 hover:bg-surface-3 hover:text-ink focus:outline-none focus:ring-2 focus:ring-live/60"
          >
            {showAllDone ? de.orchestrator.showRecent : de.orchestrator.showAll}
          </button>
        ) : null}
      </div>
      <Disclosure
        open
        summary={
          <div className="flex min-h-12 items-center">
            <Eyebrow>{de.orchestrator.colDone} Queue</Eyebrow>
          </div>
        }
      >
        {doneItems.length === 0 ? (
          <p className="py-2 text-sec text-ink-3">{de.orchestrator.empty}</p>
        ) : (
          <div className={cn("grid", gap)} style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}>
            {(showAllDone ? doneItems : doneItems.slice(0, 5)).map((item) => (
              <BacklogCard key={item.id} item={item} allItems={allItems} nowSec={nowSec} onOpen={onOpen} />
            ))}
          </div>
        )}
      </Disclosure>
    </Card>
  );
}
