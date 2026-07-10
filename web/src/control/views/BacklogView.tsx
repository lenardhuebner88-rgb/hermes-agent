import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { TriangleAlert } from "lucide-react";

import { de } from "../i18n/de";
import { useBacklog, useBacklogDetail, useCommissionToFleet, useDispatchFoTask, useFoBoardStatus, type CommissionPayload } from "../hooks/useControlData";
import { StaleBadge } from "../components/atoms";
import {
  buildFoAuditPrompt,
  buildFoCommissionPrompt,
  computeNextFoTaskId,
  filterFoItems,
  foHealthStripCounts,
  matchesFoQuickView,
  ownerLoadSummary,
  rankFoItems,
  rankedQueueWithReasons,
  sortFoItems,
} from "../lib/foBacklog";
import type { FoQuickView, FoSortKey } from "../lib/foBacklog";
import type { Density } from "../hooks/useDensity";
import type { BacklogItem, BacklogDetail } from "../lib/schemas";
import { TwoPane, useTwoPaneExpanded } from "../components/leitstand";
import {
  BacklogBoard,
  BacklogHeroPanel,
  CandidateCompareStrip,
  DoneSection,
  KeyboardHelp,
  NextTaskSpotlight,
  OwnerLoadStrip,
  QuickViewChips,
  QueueSurface,
} from "./backlog/BacklogSections";
import { ControlsBar } from "./backlog/ControlsBar";
import { FoDetailContent, FoDetailDrawer } from "./backlog/FoDetailDrawer";
import { FoHealthStrip } from "./backlog/FoHealthStrip";
import { VIEW_STORAGE_KEY, type ViewMode } from "./backlog/shared";

export { FoBacklogQueueTable } from "./backlog/FoBacklogQueueTable";
export { FoHealthStrip } from "./backlog/FoHealthStrip";
export { ReasonChips } from "./backlog/ReasonChips";

const EMPTY_ITEMS: BacklogItem[] = [];

// Map an FO risk level onto a kanban priority int (board sorts priority DESC).
function riskToPriority(risk?: string): number {
  return ({ high: 2, medium: 1, low: 0 } as Record<string, number>)[(risk ?? "").toLowerCase()] ?? 0;
}

// Build the Kanban task payload from an FO backlog item (+ its detail when the
// drawer has fetched it). The body carries the spec + acceptance criteria + a
// provenance line back to the backlog source so the run is traceable.
function buildCommissionPayload(item: BacklogItem, detail?: BacklogDetail): CommissionPayload {
  const lines: string[] = [];
  const bodyText = (detail?.body || item.excerpt || "").trim();
  if (bodyText) lines.push(bodyText);
  const criteria = (detail?.acceptance_criteria ?? []).filter((c) => c.trim() !== "");
  if (criteria.length) {
    lines.push("", "Akzeptanzkriterien:");
    for (const c of criteria) lines.push(`- ${c}`);
  }
  const next = (detail?.next_action || "").trim();
  if (next) lines.push("", `Next action: ${next}`);
  const src = detail?.source_path || item.source_path || item.id;
  lines.push("", `— Aus dem Family-Organizer-Backlog in die Fleet kopiert. Quelle: ${src} (${item.id}).`);
  return { title: `[FO] ${item.title}`, body: lines.join("\n"), priority: riskToPriority(item.risk) };
}

type PersistedView = {
  viewMode?: ViewMode;
  filterRisk?: string;
  filterStale?: boolean;
  filterOwner?: string;
  sortKey?: FoSortKey;
  quickView?: FoQuickView;
};

function loadPersistedView(): PersistedView {
  if (typeof window === "undefined") return {};
  try {
    const raw = window.localStorage.getItem(VIEW_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as PersistedView) : {};
  } catch {
    return {};
  }
}

export function BacklogView({ density }: { density: Density }) {
  const isExpanded = useTwoPaneExpanded();
  const backlog = useBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useBacklogDetail();
  const fleet = useCommissionToFleet();
  const boardStatusById = useFoBoardStatus();
  const dispatch = useDispatchFoTask();
  const commissionItem = useCallback(
    (item: BacklogItem, detail?: BacklogDetail) =>
      void fleet.commission(item.id, buildCommissionPayload(item, detail), {
        tenant: "family-organizer",
        idempotencyKey: `fo-backlog:${item.id}`,
      }),
    [fleet],
  );
  const [persisted] = useState(loadPersistedView);
  const [showAllDone, setShowAllDone] = useState(false);
  // Deep-link from the Decision Inbox: /control/backlog?focus=<id> seeds the open
  // drawer at mount. The drawer only renders once the item is found in the loaded
  // backlog (selectedItem guard below), so seeding before data arrives is safe and
  // needs no effect/setState — keeps render pure.
  const [focusParams] = useSearchParams();
  const initialFocusId = focusParams.get("focus");
  const [openId, setOpenId] = useState<string | null>(() => initialFocusId);
  // A decision-inbox deep link always lands in the queue instrument so expanded
  // viewports open the inline pane even if the operator last persisted Board.
  const [viewMode, setViewMode] = useState<ViewMode>(() => initialFocusId ? "queue" : (persisted.viewMode ?? "queue"));
  const [q, setQ] = useState("");
  const [filterOwner, setFilterOwner] = useState(persisted.filterOwner ?? "");
  const [filterRisk, setFilterRisk] = useState(persisted.filterRisk ?? "");
  const [filterStale, setFilterStale] = useState(persisted.filterStale ?? false);
  const [sortKey, setSortKey] = useState<FoSortKey>(persisted.sortKey ?? "status");
  const [quickView, setQuickView] = useState<FoQuickView>(persisted.quickView ?? "all");
  const [activeIndex, setActiveIndex] = useState(-1);
  const [showHelp, setShowHelp] = useState(false);
  const [fallbackNowSec] = useState(() => Math.floor(Date.now() / 1000));
  const queueRef = useRef<HTMLDivElement>(null);

  // Persist the operator's working view (not the transient search text).
  useEffect(() => {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        VIEW_STORAGE_KEY,
        JSON.stringify({ viewMode, filterRisk, filterStale, filterOwner, sortKey, quickView }),
      );
    } catch {
      /* storage blocked / quota — view persistence is best-effort */
    }
  }, [viewMode, filterRisk, filterStale, filterOwner, sortKey, quickView]);

  const data = backlog.data;
  const allItems = data?.items ?? EMPTY_ITEMS;
  const nowSec = data?.checked_at ?? fallbackNowSec;
  const gap = density === "compact" ? "gap-3" : "gap-4";
  const initialLoading = backlog.loading && !data;

  useEffect(() => {
    if (openId) void fetchDetail(openId);
  }, [fetchDetail, openId]);

  const nextTaskId = useMemo(() => computeNextFoTaskId(allItems), [allItems]);
  const nextTask = nextTaskId ? allItems.find((item) => item.id === nextTaskId) : null;

  useEffect(() => {
    if (nextTaskId && !detailById[nextTaskId]) void fetchDetail(nextTaskId);
  }, [nextTaskId, detailById, fetchDetail]);

  const owners = useMemo(() => {
    const set = new Set<string>();
    for (const item of allItems) if (item.owner && item.owner !== "unassigned") set.add(item.owner);
    return [...set].sort();
  }, [allItems]);

  const byStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of allItems) (map[item.status] ??= []).push(item);
    return map;
  }, [allItems]);

  const filteredActive = useMemo(() => {
    const active = allItems.filter((item) => item.status !== "done" && matchesFoQuickView(item, quickView));
    const filtered = filterFoItems(active, q, {
      owner: filterOwner || undefined,
      risk: filterRisk || undefined,
      stale: filterStale || undefined,
    });
    const sorted = sortFoItems(filtered, sortKey);
    return sortKey === "risk" ? rankFoItems(sorted, nowSec) : sorted;
  }, [allItems, quickView, q, filterOwner, filterRisk, filterStale, sortKey, nowSec]);

  // Ranked active candidates + reason codes, computed once and reused by the next-task
  // spotlight and the compare-top-candidates strip so they cannot disagree.
  const ranked = useMemo(() => rankedQueueWithReasons(allItems, nowSec), [allItems, nowSec]);
  const topCandidates = useMemo(() => ranked.slice(0, 3), [ranked]);

  // Prefetch detail for the top candidates so their commission prompts are ready.
  useEffect(() => {
    for (const candidate of topCandidates) {
      if (!detailById[candidate.item.id]) void fetchDetail(candidate.item.id);
    }
  }, [topCandidates, detailById, fetchDetail]);

  const filteredByStatus = useMemo(() => {
    const map: Record<string, BacklogItem[]> = {};
    for (const item of filteredActive) (map[item.status] ??= []).push(item);
    return map;
  }, [filteredActive]);

  const doneItems = useMemo(() => {
    const arr = [...(byStatus.done ?? [])];
    arr.sort((a, b) => b.updated.localeCompare(a.updated) || b.id.localeCompare(a.id));
    return arr;
  }, [byStatus]);

  const ownerLoad = useMemo(() => ownerLoadSummary(allItems).slice(0, 4), [allItems]);
  const counts = data?.counts;
  const activeTotal = counts ? counts.now + counts.next + counts.in_progress + counts.blocked + counts.later : allItems.filter((item) => item.status !== "done").length;
  const doneTotal = useMemo(() => allItems.filter((item) => item.status === "done").length, [allItems]);
  // Plain-language status breakdown + a one-click audit prompt seeded with the live board
  // state. Both read from the data the view already has — no new backend.
  const breakdown = useMemo(() => {
    const by = (status: string) => allItems.filter((item) => item.status === status).length;
    return { now: by("now"), next: by("next"), in_progress: by("in_progress"), blocked: by("blocked"), later: by("later") };
  }, [allItems]);
  const auditPrompt = useMemo(() => {
    const health = foHealthStripCounts(allItems, data?.contract_health);
    return buildFoAuditPrompt({
      active: activeTotal,
      done: doneTotal,
      stale: health.stale,
      unowned: health.unowned,
      highRisk: health.highRisk,
      missingAcceptance: health.missingAcceptance,
      contractDrift: health.contractDrift,
    });
  }, [allItems, data?.contract_health, activeTotal, doneTotal]);
  const selectedItem = openId ? allItems.find((item) => item.id === openId) : undefined;
  const detail = openId ? detailById[openId] : undefined;
  const commissionPrompt = detail ? buildFoCommissionPrompt(detail) : undefined;

  // Clamp the roving selection to the current filtered set at render time (no effect →
  // no cascading-render lint). Movements below also clamp, so it self-corrects.
  const clampedIndex = activeIndex >= filteredActive.length ? filteredActive.length - 1 : activeIndex;
  const activeId = clampedIndex >= 0 ? (filteredActive[clampedIndex]?.id ?? null) : null;

  // Bring the roving row into view when it changes.
  useEffect(() => {
    if (!activeId || !queueRef.current) return;
    queueRef.current.querySelector<HTMLElement>(`[data-fo-row="${activeId}"]`)?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  // Queue keyboard nav: j/k move, Enter opens, ? toggles help. Ignores typing in inputs.
  // DrawerShell owns Escape below 1024; the inline desktop pane closes it here.
  const onQueueKey = useCallback(
    (event: KeyboardEvent) => {
      if (viewMode !== "queue") return;
      if (event.key === "Escape" && openId && isExpanded) {
        setOpenId(null);
        return;
      }
      if (openId) return;
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.key === "?") {
        event.preventDefault();
        setShowHelp((value) => !value);
        return;
      }
      if (event.key === "Escape" && showHelp) {
        setShowHelp(false);
        return;
      }
      if (!filteredActive.length) return;
      const last = filteredActive.length - 1;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setActiveIndex((index) => Math.min(last, Math.min(index, last) + 1));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setActiveIndex((index) => Math.max(0, Math.min(index, last) - 1));
      } else if (event.key === "Enter" && clampedIndex >= 0) {
        event.preventDefault();
        setOpenId(filteredActive[clampedIndex].id);
      }
    },
    [viewMode, openId, isExpanded, showHelp, filteredActive, clampedIndex],
  );

  useEffect(() => {
    window.addEventListener("keydown", onQueueKey);
    return () => window.removeEventListener("keydown", onQueueKey);
  }, [onQueueKey]);

  const listContent = (
    <div className="space-y-4">
      <BacklogHeroPanel
        activeTotal={activeTotal}
        doneTotal={doneTotal}
        breakdown={breakdown}
        loading={initialLoading}
        nowSec={nowSec}
        auditPrompt={auditPrompt}
        viewMode={viewMode}
        onViewMode={setViewMode}
      />

      {backlog.error ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{de.backlog.error}</div> : null}
      <StaleBadge isStale={backlog.isStale} lastUpdated={backlog.lastUpdated} errorObj={backlog.errorObj} error={backlog.error} now={nowSec} />
      {data?.error ? <div role="alert" className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{de.backlog.sourceMissing}</div> : null}

      <FoHealthStrip items={allItems} contractHealth={data?.contract_health} />

      <NextTaskSpotlight
        nextTask={nextTask ?? null}
        allItemsLength={allItems.length}
        nowSec={nowSec}
        commissionPrompt={nextTask && detailById[nextTask.id] ? buildFoCommissionPrompt(detailById[nextTask.id]) : undefined}
      />

      <CandidateCompareStrip topCandidates={topCandidates} detailById={detailById} nowSec={nowSec} onOpen={setOpenId} />
      <OwnerLoadStrip ownerLoad={ownerLoad} />

      <QuickViewChips
        allItemsLength={allItems.length}
        quickView={quickView}
        showHelp={showHelp}
        onQuickView={(view) => { setQuickView(view); setActiveIndex(-1); }}
        onToggleHelp={() => setShowHelp((value) => !value)}
      />

      <KeyboardHelp showHelp={showHelp} />

      {allItems.length > 0 ? (
        <ControlsBar
          q={q}
          onQ={setQ}
          filterOwner={filterOwner}
          onFilterOwner={setFilterOwner}
          filterRisk={filterRisk}
          onFilterRisk={setFilterRisk}
          filterStale={filterStale}
          onFilterStale={setFilterStale}
          sortKey={sortKey}
          onSort={setSortKey}
          owners={owners}
        />
      ) : null}

      {viewMode === "queue" ? (
        <div ref={queueRef}>
          <QueueSurface
            loading={initialLoading}
            filteredActive={filteredActive}
            nowSec={nowSec}
            nextTaskId={nextTaskId}
            activeId={activeId}
            selectedId={openId}
            detailById={detailById}
            onOpen={setOpenId}
            onCommission={(item) => commissionItem(item, detailById[item.id])}
            commissionState={fleet.stateById}
            boardStatusById={boardStatusById}
            onDispatch={(taskId) => void dispatch.dispatch(taskId)}
            dispatchStateByTaskId={dispatch.stateById}
          />
        </div>
      ) : (
        <BacklogBoard filteredByStatus={filteredByStatus} gap={gap} nowSec={nowSec} nextTaskId={nextTaskId} loading={initialLoading} onOpen={setOpenId} />
      )}

      <DoneSection
        doneItems={doneItems}
        showAllDone={showAllDone}
        nowSec={nowSec}
        detailById={detailById}
        onToggleShowAll={() => setShowAllDone((value) => !value)}
        onOpen={setOpenId}
      />

    </div>
  );

  const detailContent = selectedItem ? (
    <FoDetailContent
      item={selectedItem}
      detail={detail}
      loading={loadingId === selectedItem.id}
      error={errorById[selectedItem.id] || detail?.error}
      commissionPrompt={commissionPrompt}
      onCommission={() => commissionItem(selectedItem, detail)}
      commissionState={fleet.stateById[selectedItem.id]}
      commissionError={fleet.errorById[selectedItem.id]}
    />
  ) : undefined;

  return (
    <>
      <TwoPane
        list={listContent}
        detail={viewMode === "queue" ? detailContent : undefined}
        detailLabel={selectedItem ? `Backlog-Detail · ${selectedItem.title}` : "Backlog-Detail"}
        onCloseDetail={() => setOpenId(null)}
      />

      {selectedItem && (!isExpanded || viewMode !== "queue") ? (
        <FoDetailDrawer
          key={selectedItem.id}
          item={selectedItem}
          detail={detail}
          loading={loadingId === selectedItem.id}
          error={errorById[selectedItem.id] || detail?.error}
          commissionPrompt={commissionPrompt}
          onCommission={() => commissionItem(selectedItem, detail)}
          commissionState={fleet.stateById[selectedItem.id]}
          commissionError={fleet.errorById[selectedItem.id]}
          onClose={() => setOpenId(null)}
        />
      ) : null}
    </>
  );
}
