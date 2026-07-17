import { useCallback, useEffect, useMemo, useState } from "react";
import { AnimatePresence } from "motion/react";
import { TriangleAlert } from "lucide-react";

import { de } from "../i18n/de";
import { useOrchestrationBacklog, useOrchestrationBacklogDetail } from "../hooks/backlogOrchestration";
import { useCommissionToFleet, type CommissionPayload } from "../hooks/commissionCapture";
import { BacklogDetailDrawer } from "../components/BacklogDetailDrawer";
import {
  buildCommissionPrompt,
  computeNextTaskId,
  deriveQueueSignals,
  filterItems,
  isKnownStatus,
  nextActionForItem,
  projectFromRoot,
  readiness,
  sortItems,
} from "../lib/orchestration";
import type { SortKey } from "../lib/orchestration";
import type { Density } from "../hooks/useDensity";
import type { OrchestrationItem, OrchestrationDetail } from "../lib/schemas";
import { ControlsBar } from "./orchestrator/ControlsBar";
import {
  CommissionBanner,
  ContractDriftCallout,
  DoneSection,
  OrchestratorBoard,
  OrchestratorHeroPanel,
  QueueSurface,
  SignalStrip,
} from "./orchestrator/OrchestratorSections";
import {
  buildOperatorBrief,
  readinessChip,
  sourceLabel,
  sourcePath,
  type DetailChip,
  type ViewMode,
} from "./orchestrator/shared";

export { OrchestratorQueueTable } from "./orchestrator/OrchestratorQueueTable";

const EMPTY_ITEMS: OrchestrationItem[] = [];

function priorityToInt(priority?: string): number {
  return ({ urgent: 3, high: 2, medium: 1, low: 0 } as Record<string, number>)[(priority ?? "").toLowerCase()] ?? 1;
}

// Build the Kanban task payload from an orchestrator backlog item (+ detail).
function buildOrchCommissionPayload(item: OrchestrationItem, detail?: OrchestrationDetail): CommissionPayload {
  const lines: string[] = [];
  const bodyText = (detail?.body || item.excerpt || "").trim();
  if (bodyText) lines.push(bodyText);
  const proofs = (detail?.proofs ?? []).filter((p) => p.trim() !== "");
  if (proofs.length) {
    lines.push("", "Letzte Belege:");
    for (const p of proofs) lines.push(`- ${p}`);
  }
  const meta = [
    item.owner ? `Owner: ${item.owner}` : "",
    item.priority ? `Priorität: ${item.priority}` : "",
    item.planGate ? "Plan-Gate: offen" : "",
  ].filter(Boolean).join(" · ");
  if (meta) lines.push("", meta);
  const src = detail?.source || item.source || detail?.root || item.root || item.id;
  lines.push("", `— Aus dem Orchestrator-Backlog in die Fleet kopiert. Quelle: ${src} (${item.id}).`);
  return { title: `[Orch] ${item.title}`, body: lines.join("\n"), priority: priorityToInt(item.priority) };
}

export function OrchestratorBacklogView({ density }: { density: Density }) {
  const backlog = useOrchestrationBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useOrchestrationBacklogDetail();
  const fleet = useCommissionToFleet();
  const commissionItem = useCallback(
    (item: OrchestrationItem, detail?: OrchestrationDetail) =>
      void fleet.commission(item.id, buildOrchCommissionPayload(item, detail), {
        tenant: "orchestrator",
        idempotencyKey: `orch-backlog:${item.id}`,
      }),
    [fleet],
  );
  const [showAllDone, setShowAllDone] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("queue");

  const [q, setQ] = useState("");
  const [filterPriority, setFilterPriority] = useState("");
  const [filterProject, setFilterProject] = useState("");
  const [filterPlanGate, setFilterPlanGate] = useState("");
  const [filterReadiness, setFilterReadiness] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("priority");
  const [fallbackNowSec] = useState(() => Math.floor(Date.now() / 1000));

  const data = backlog.data;
  const nowSec = data?.checked_at ?? fallbackNowSec;
  const gap = density === "compact" ? "gap-3" : "gap-4";
  const allItems = data?.items ?? EMPTY_ITEMS;
  const responseRef = data?.source.ref ?? "";
  const initialLoading = backlog.loading && !data;

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  const nextTaskId = useMemo(() => computeNextTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((item) => item.id === nextTaskId) : null;

  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const nextDetail = nextTaskId ? detailById[nextTaskId] : undefined;
  const commissionPromptForNext = nextDetail ? buildCommissionPrompt(nextDetail) : undefined;

  const projects = useMemo(() => {
    const set = new Set<string>();
    for (const item of allItems) {
      const project = projectFromRoot(item.root);
      if (project !== "Orchestration") set.add(project);
    }
    return [...set].sort();
  }, [allItems]);

  const byStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

  const filteredActive = useMemo(() => {
    const active = allItems.filter((item) => item.status !== "done");
    const filtered = filterItems(
      active,
      q,
      { priority: filterPriority, project: filterProject, planGate: filterPlanGate, readiness: filterReadiness },
      allItems,
    );
    return sortItems(filtered, sortKey, allItems);
  }, [allItems, q, filterPriority, filterProject, filterPlanGate, filterReadiness, sortKey]);

  const filteredByStatus = useMemo(() => {
    const map: Record<string, OrchestrationItem[]> = {};
    for (const item of filteredActive) (map[item.status] ??= []).push(item);
    return map;
  }, [filteredActive]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    arr.sort((a, b) => b.created.localeCompare(a.created) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const signals = useMemo(() => deriveQueueSignals(allItems, data?.contract_health, nowSec), [allItems, data?.contract_health, nowSec]);
  const activeTotal = allItems.filter((item) => item.status !== "done").length;
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;
  const drawerNextAction = selectedItem ? nextActionForItem(selectedItem, allItems) : "";

  const detailReadiness = selectedItem ? readinessChip(readiness(selectedItem, allItems)) : null;
  const detailChips: DetailChip[] = [
    ...(detailReadiness ? [detailReadiness] : []),
    ...(selectedItem && !isKnownStatus(selectedItem.status) ? [{ label: `${de.orchestrator.statusDrift}: ${selectedItem.status}`, tone: "alert" as const }] : []),
    ...((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).length ? [{ label: de.orchestrator.dependsOn((selectedItem?.dependsOn ?? detail?.dependsOn ?? []).length), tone: "neutral" as const }] : []),
  ];
  const detailFields: Array<{ label: string; value: string }> = detail
    ? (
        [
          detail.status ? { label: de.orchestrator.colStatus, value: detail.status } : null,
          detail.priority ? { label: de.orchestrator.priority, value: detail.priority } : null,
          detail.owner ? { label: de.orchestrator.colOwner, value: detail.owner } : null,
          { label: de.orchestrator.planGate, value: detail.planGate ? de.orchestrator.yes : de.orchestrator.no },
          detail.gate ? { label: de.orchestrator.gate, value: detail.gate } : null,
          detail.created ? { label: de.orchestrator.created, value: detail.created } : null,
        ] as Array<{ label: string; value: string } | null>
      ).filter((field): field is { label: string; value: string } => field !== null)
    : [];

  const sourceRef = openId
    ? [
        { label: de.orchestrator.colSource, value: detail?.source || selectedItem?.source || sourceLabel(selectedItem ?? ({ root: detail?.root ?? "" } as OrchestrationItem)) },
        { label: "Ref", value: responseRef },
        { label: de.orchestrator.detailSpec, value: sourcePath(openId) },
        { label: de.orchestrator.root, value: detail?.root || selectedItem?.root || "" },
      ]
    : [];
  const proofTimeline = detail?.proofs?.length ? detail.proofs : [detail?.lastProof || selectedItem?.lastProof || ""].filter(Boolean);
  const commissionPromptForDrawer = detail ? buildCommissionPrompt(detail) : undefined;
  const operatorBriefForDrawer = buildOperatorBrief(selectedItem, detail, drawerNextAction, responseRef);
  const hasContractDrift = Boolean(data && signals.contractDrift > 0);

  return (
    <div className="space-y-4">
      <OrchestratorHeroPanel
        activeTotal={activeTotal}
        loading={initialLoading}
        nowSec={nowSec}
        data={data ?? undefined}
      />

      {backlog.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{de.orchestrator.error}</div> : null}
      {data?.error ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{de.orchestrator.sourceMissing}</div> : null}
      {hasContractDrift && data ? <ContractDriftCallout data={data} /> : null}

      {allItems.length > 0 ? <SignalStrip signals={signals} /> : null}

      {nextTask ? (
        <CommissionBanner
          nextId={nextTask.id}
          nextTitle={nextTask.title}
          prompt={commissionPromptForNext}
        />
      ) : allItems.length > 0 ? (
        <div className="rounded-card border border-line bg-surface-2 px-3 py-2 text-sec text-ink-2">{de.orchestrator.noNextTask}</div>
      ) : null}

      {allItems.length > 0 ? (
        <ControlsBar
          q={q}
          onQ={setQ}
          filterPriority={filterPriority}
          onFilterPriority={setFilterPriority}
          filterProject={filterProject}
          onFilterProject={setFilterProject}
          filterPlanGate={filterPlanGate}
          onFilterPlanGate={setFilterPlanGate}
          filterReadiness={filterReadiness}
          onFilterReadiness={setFilterReadiness}
          sortKey={sortKey}
          onSort={setSortKey}
          projects={projects}
          viewMode={viewMode}
          onViewMode={setViewMode}
        />
      ) : null}

      <QueueSurface
        loading={initialLoading}
        filteredActive={filteredActive}
        allItems={allItems}
        nowSec={nowSec}
        nextTaskId={nextTaskId}
        onOpen={setOpenId}
        onCommission={(item) => commissionItem(item, detailById[item.id])}
        commissionState={fleet.stateById}
      />

      {viewMode === "board" ? (
        <OrchestratorBoard
          filteredActive={filteredActive}
          filteredByStatus={filteredByStatus}
          allItems={allItems}
          gap={gap}
          nowSec={nowSec}
          nextTaskId={nextTaskId}
          loading={initialLoading}
          onOpen={setOpenId}
        />
      ) : null}

      <DoneSection
        doneItems={doneItems}
        showAllDone={showAllDone}
        allItems={allItems}
        gap={gap}
        nowSec={nowSec}
        onToggleShowAll={() => setShowAllDone((value) => !value)}
        onOpen={setOpenId}
      />

      <AnimatePresence initial={false}>
        {openId ? (
          <BacklogDetailDrawer
            key={openId}
            title={selectedItem?.title ?? detail?.title ?? openId}
            id={openId}
            body={detail?.body ?? ""}
            chips={detailChips}
            fields={detailFields}
            proofTimeline={proofTimeline}
            nextAction={drawerNextAction}
            sourceRef={sourceRef}
            links={detail?.links}
            loading={loadingId === openId}
            error={errorById[openId] || detail?.error}
            commissionPrompt={commissionPromptForDrawer}
            operatorBrief={operatorBriefForDrawer}
            onCommission={selectedItem && selectedItem.status !== "done" ? () => commissionItem(selectedItem, detail) : undefined}
            commissionState={openId ? fleet.stateById[openId] : undefined}
            commissionError={openId ? fleet.errorById[openId] : undefined}
            onClose={() => setOpenId(null)}
          />
        ) : null}
      </AnimatePresence>
    </div>
  );
}
