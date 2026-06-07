import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { AnimatePresence } from "motion/react";

import { de } from "../i18n/de";
import { useBacklog, useBacklogDetail } from "../hooks/useControlData";
import { ToneCallout } from "../components/atoms";
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
import type { BacklogItem } from "../lib/schemas";
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
import { FoDetailDrawer } from "./backlog/FoDetailDrawer";
import { FoHealthStrip } from "./backlog/FoHealthStrip";
import { VIEW_STORAGE_KEY, type ViewMode } from "./backlog/shared";

export { FoBacklogQueueTable } from "./backlog/FoBacklogQueueTable";
export { FoHealthStrip } from "./backlog/FoHealthStrip";
export { ReasonChips } from "./backlog/ReasonChips";

const EMPTY_ITEMS: BacklogItem[] = [];

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
  const backlog = useBacklog();
  const { detailById, errorById, loadingId, fetch: fetchDetail } = useBacklogDetail();
  const [persisted] = useState(loadPersistedView);
  const [showAllDone, setShowAllDone] = useState(false);
  // Deep-link from the Decision Inbox: /control/backlog?focus=<id> seeds the open
  // drawer at mount. The drawer only renders once the item is found in the loaded
  // backlog (selectedItem guard below), so seeding before data arrives is safe and
  // needs no effect/setState — keeps render pure.
  const [focusParams] = useSearchParams();
  const [openId, setOpenId] = useState<string | null>(() => focusParams.get("focus"));
  const [viewMode, setViewMode] = useState<ViewMode>(persisted.viewMode ?? "queue");
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

  // Queue keyboard nav: j/k move, Enter opens, ? toggles help. Ignores typing in inputs
  // and yields to the drawer (which owns Escape while open).
  const onQueueKey = useCallback(
    (event: KeyboardEvent) => {
      if (viewMode !== "queue" || openId) return;
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
    [viewMode, openId, showHelp, filteredActive, clampedIndex],
  );

  useEffect(() => {
    window.addEventListener("keydown", onQueueKey);
    return () => window.removeEventListener("keydown", onQueueKey);
  }, [onQueueKey]);

  return (
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

      {backlog.error ? <ToneCallout tone="red">{de.backlog.error}</ToneCallout> : null}
      {data?.error ? <ToneCallout tone="amber">{de.backlog.sourceMissing}</ToneCallout> : null}

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
            detailById={detailById}
            onOpen={setOpenId}
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

      <AnimatePresence initial={false}>
        {selectedItem ? (
          <FoDetailDrawer
            key={selectedItem.id}
            item={selectedItem}
            detail={detail}
            loading={loadingId === selectedItem.id}
            error={errorById[selectedItem.id] || detail?.error}
            commissionPrompt={commissionPrompt}
            onClose={() => setOpenId(null)}
          />
        ) : null}
      </AnimatePresence>
    </div>
  );
}
