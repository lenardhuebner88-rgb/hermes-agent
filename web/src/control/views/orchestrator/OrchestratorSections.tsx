import { useState } from "react";
import { Check, ClipboardCopy } from "lucide-react";

import { cn } from "@/lib/utils";
import { BacklogCard } from "../../components/BacklogCard";
import { StatusPill, ToneCallout } from "../../components/atoms";
import { Card, Disclosure, Panel, SkeletonCard, Stat, Stagger, StaggerItem, Text } from "../../components/primitives";
import { de } from "../../i18n/de";
import { deriveQueueSignals, isKnownStatus } from "../../lib/orchestration";
import type { OrchestrationBacklogResponse, OrchestrationItem } from "../../lib/schemas";
import type { ToneName } from "../../lib/types";
import type { CommissionState } from "../../hooks/useControlData";
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
  return (
    <Panel
      eyebrow={de.orchestrator.eyebrow}
      title={`${de.orchestrator.title} · ${activeTotal} aktiv`}
      surface="card"
      actions={<div className="text-left text-xs hc-soft sm:text-right">{loading ? de.orchestrator.loading : de.orchestrator.updatedAt(clockLabel(nowSec))}</div>}
    >
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_120px] md:items-end">
        <div>
          <Text variant="label" className="hc-soft">{de.orchestrator.subtitle}</Text>
          {data ? (
            <p className="mt-1 text-xs hc-dim">
              {data.contract_health.source_count} Quellen · {data.contract_health.counted_sum} gezählt · {data.source.ref}
            </p>
          ) : null}
        </div>
        <Stat label="Aktiv" value={activeTotal} hint={data ? `${data.contract_health.counted_sum} gezählt` : undefined} accent />
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
  return <ToneCallout tone="amber">{parts.join(" · ")}</ToneCallout>;
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
    <Card tone="sky" surface="card" className="p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase text-cyan-300">{de.orchestrator.nextTask}</p>
          <p className="mt-0.5 truncate text-sm font-medium text-white">{nextTitle}</p>
          <p className="mt-0.5 text-[11px] hc-mono hc-dim">{nextId}</p>
        </div>
        <button
          type="button"
          onClick={copy}
          disabled={!prompt}
          className={cn(
            "flex shrink-0 items-center gap-2 rounded-md border px-3 py-2 text-sm font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/60",
            copied
              ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-200"
              : !prompt
                ? "cursor-wait border-white/10 text-zinc-500"
                : "border-cyan-500/30 bg-cyan-500/10 text-cyan-200 hover:bg-cyan-500/20",
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
  const tiles: Array<{ label: string; value: number; tone: ToneName }> = [
    { label: de.orchestrator.readyStrip, value: signals.ready, tone: "emerald" },
    { label: de.orchestrator.blockedStrip, value: signals.blocked, tone: "red" },
    { label: de.orchestrator.unownedStrip, value: signals.unowned, tone: "amber" },
    { label: de.orchestrator.staleProofStrip, value: signals.staleProof, tone: "rose" },
    { label: de.orchestrator.highRiskStrip, value: signals.highRisk, tone: "red" },
    { label: de.orchestrator.contractDrift, value: signals.contractDrift, tone: signals.contractDrift ? "red" : "zinc" },
  ];
  return (
    <section className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
      {tiles.map((tile) => (
        <Stat key={tile.label} label={tile.label} value={tile.value} tone={tile.tone} />
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
                <StatusPill tone={column.tone} label={column.label} />
                <span className="hc-mono text-xs hc-dim">{items.length}</span>
              </div>
              {items.length === 0 ? (
                <p className="py-3 text-center text-xs hc-dim">{de.orchestrator.emptyColumn}</p>
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
          <StatusPill tone="emerald" label={de.orchestrator.colDone} />
          <span className="hc-mono text-xs hc-dim">{doneItems.length}</span>
          <span className="hidden text-xs hc-dim sm:inline">· {de.orchestrator.doneRecentHint}</span>
          <span className="hidden text-xs hc-dim sm:inline">· {de.orchestrator.doneReceiptHint}</span>
        </div>
        {doneItems.length > 5 ? (
          <button
            type="button"
            onClick={onToggleShowAll}
            className="rounded-md border border-white/10 px-2 py-1 text-xs hc-soft hover:bg-white/5 focus:outline-none focus:ring-2 focus:ring-cyan-400/60"
          >
            {showAllDone ? de.orchestrator.showRecent : de.orchestrator.showAll}
          </button>
        ) : null}
      </div>
      <Disclosure
        open
        summary={
          <div className="text-[11px] font-semibold uppercase text-zinc-400">
            {de.orchestrator.colDone} Queue
          </div>
        }
      >
        {doneItems.length === 0 ? (
          <p className="py-2 text-xs hc-dim">{de.orchestrator.empty}</p>
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
