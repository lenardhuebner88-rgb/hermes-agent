/**
 * FleetView — Operator-Lagezentrum: Hermes-Flotte auf einen Blick.
 *
 * Subtabs: Heute · Worker · Ketten · Plan · Risiko
 * Diese Datei ist nur noch die Shell (State + Subtab-Switch + "Wartet auf dich"-
 * Zeile). Die Subtabs leben als eigene Dateien unter ./fleet/ — reine
 * Zerlegung, kein Verhalten geändert:
 *   HeuteTab · WorkerTab (+ WorkerDrawer) · KettenTab · NodeDetailDrawer · PlanTab · RisikoTab.
 * Pure Logik bleibt in lib/fleetHub.ts; geteilte Typen/Formatter in ./fleet/shared.ts.
 *
 * Masthead: seit W3-1a (2026-07-10) rendert Fleet KEIN eigenes Masthead mehr —
 * die Shell-Puls-Leiste (ControlShell) trägt Label "Fleet" + Instrumente +
 * die NotificationBridge-Glocke (schließt den bekannten P2 "Glocke auf Fleet
 * unsichtbar"). [data-fleet-theme] bleibt als dunkler Content-Scope bestehen.
 *
 * Design: dunkles Marineblau-Theme NUR im Fleet-Tab-Scope ([data-fleet-theme]).
 * Glow/Puls ausschließlich bei laufender Aktivität (Licht = Leben).
 */
import { useState, useMemo, useEffect, useRef } from "react";
import { ArrowRight } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { useHermesRunsCosts } from "../hooks/costsUsage";
import { useKanbanDecisionQueue } from "../hooks/decisionInbox";
import { usePlanSpecs, useLanesCatalog, usePlanSpecDetail } from "../hooks/planSpecsLanes";
import { useHermesRunsDaily, useHermesReliability } from "../hooks/runsDigestRollup";
import { useSystemHealth, usePressureStatus, useReleaseStatus, useReleaseMode } from "../hooks/systemReleaseHealth";
import { useHermesWorkers, useAllBoardWorkers, useBoardCatalog, useBoard } from "../hooks/workersBoard";
import { useFleetBoardSelection } from "../hooks/useFleetBoardSelection";
import { planSpecAwaitsPlanAction, derivePendingItems, buildChainChips, type PendingItem } from "../lib/fleetHub";
import { selectableFleetBoards, type BoardsResponse } from "../lib/multiBoard";
import { useClientNowSeconds } from "../lib/clock";
import { de } from "../i18n/de";
import type { Worker, ChainGraphResponse, PlanSpecRecord, TaskStatus } from "../lib/types";
import { HeuteTab } from "./fleet/HeuteTab";
import { WorkerTab } from "./fleet/WorkerTab";
import { KettenTab } from "./fleet/KettenTab";
import { BoardTab } from "./fleet/BoardTab";
import { NodeDetailContent, NodeDetailDrawer } from "./fleet/NodeDetailDrawer";
import { PlanSpecDetailContent, PlanSpecDetailDrawer } from "./fleet/PlanSpecDetailDrawer";
import { PlanSpecTransitionPanel } from "./fleet/PlanSpecTransitionPanel";
import { PlanTab, type PlanSegment } from "./fleet/PlanTab";
import { RisikoTab } from "./fleet/RisikoTab";
import { SubtabChips, TwoPane } from "../components/leitstand";
import { Led, StaleBadge } from "../components/atoms";
import { BoardSwitcher } from "../components/fleet/BoardIdentity";
import { FleetSourceFreshness } from "./fleet/FleetSourceFreshness";
import "./fleet/fleet.css";

// ─── Viewport-Hook ───────────────────────────────────────────────────────────

/** true wenn Viewport ≥ lg (1024 px) — analog zu AgentTerminalsView-Muster. */
function useIsLg(): boolean {
  const [matches, setMatches] = useState(() =>
    typeof window === "undefined" ? false : window.matchMedia("(min-width: 1024px)").matches,
  );
  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia("(min-width: 1024px)");
    const onChange = () => setMatches(media.matches);
    onChange();
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, []);
  return matches;
}

// ─── Subtab-Definition ───────────────────────────────────────────────────────

type FleetSubtab = "heute" | "worker" | "ketten" | "board" | "plan" | "risiko";

interface SubtabDef {
  id: FleetSubtab;
  label: string;
  count?: number;
  warn?: boolean;
}

/** BoardTab filter vocabulary — only accept these as ?status= deep-link values. */
const FLEET_DEEP_LINK_STATUSES: ReadonlySet<string> = new Set([
  "triage", "todo", "scheduled", "ready", "running",
  "blocked", "review", "done", "archived",
]);

function parseDeepLinkStatus(raw: string | null): TaskStatus | null {
  if (raw == null || raw === "") return null;
  return FLEET_DEEP_LINK_STATUSES.has(raw) ? (raw as TaskStatus) : null;
}

const PLAN_SEGMENTS: ReadonlySet<string> = new Set([
  "all", "draft", "ready", "attention", "held", "board", "done",
]);

function parsePlanSegment(raw: string | null): PlanSegment | null {
  if (raw == null || raw === "") return null;
  return PLAN_SEGMENTS.has(raw) ? (raw as PlanSegment) : null;
}

/**
 * Resolve ?board= against the live catalog.
 * - undefined → invalid / ignore (do not change selection)
 * - null → select the global current board (clear foreign selection)
 * - string → select that foreign board slug
 */
function resolveDeepLinkBoard(
  boardParam: string,
  catalog: BoardsResponse | null,
): string | null | undefined {
  if (!catalog) return undefined;
  const available = selectableFleetBoards(catalog.boards);
  if (!available.some((entry) => entry.slug === boardParam)) return undefined;
  return boardParam === catalog.current ? null : boardParam;
}

// ─── Haupt-View ──────────────────────────────────────────────────────────────

export function FleetView() {
  const [subtab, setSubtab] = useState<FleetSubtab>("heute");
  const isLg = useIsLg();
  // Aktiven Subtab-Chip automatisch in den sichtbaren Bereich scrollen, damit
  // er auf schmalen Viewports nie hinter dem rechten Rand verschwindet (AC-R2).
  const subtabStripRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const active = subtabStripRef.current?.querySelector<HTMLElement>('[aria-pressed="true"]');
    // scrollIntoView fehlt in jsdom (Testumgebung) — defensiv aufrufen.
    if (typeof active?.scrollIntoView === "function") {
      active.scrollIntoView({ inline: "nearest", block: "nearest" });
    }
  }, [subtab]);
  const [drawerWorker, setDrawerWorker] = useState<Worker | null>(null);
  // PlanSpec-Detail-Drawer: vom Fleet-Besitz aus öffenbar (Heute/Plan-Karten).
  const [planspecDrawerItem, setPlanspecDrawerItem] = useState<PlanSpecRecord | null>(null);
  const planspecDetail = usePlanSpecDetail(planspecDrawerItem?.path ?? null);
  // rootId für den Ketten-Subtab: wird beim "Kette öffnen"-Klick im Worker-Drawer gesetzt.
  const [kettenRootId, setKettenRootId] = useState<string | null>(null);
  // Karten-Detail-Drawer: task_id des geöffneten Nodes
  const [nodeDetailId, setNodeDetailId] = useState<string | null>(null);
  // Ketten-Nodes beim Öffnen des Drawers mitgeben → ErgebnisTab kann chainCost berechnen
  const [nodeDetailChainNodes, setNodeDetailChainNodes] = useState<ChainGraphResponse["nodes"]>([]);
  // One-shot deep-link status for BoardTab (consumed from ?status= once per mount).
  const [deepLinkStatusFilter, setDeepLinkStatusFilter] = useState<TaskStatus | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const deepLinkConsumedRef = useRef(false);
  const planDeepLinkConsumedRef = useRef(false);

  const workers = useHermesWorkers();
  const fleetWorkers = useAllBoardWorkers();
  const boardCatalog = useBoardCatalog();
  const { selectedBoard, setSelectedBoard } = useFleetBoardSelection(boardCatalog.data);
  const board = useBoard();
  const selectedBoardData = useBoard(selectedBoard);
  const planspecs = usePlanSpecs({ scope: "all", limit: 500 });
  const selectedPlanspecs = usePlanSpecs({ scope: "all", limit: 500 }, selectedBoard);
  const planSegment = parsePlanSegment(searchParams.get("planSegment")) ?? "all";
  const planQuery = searchParams.get("planSearch") ?? "";

  // Deep-link ?board=<slug>&status=<TaskStatus> und/oder ?task=<id> — one-shot
  // idiom (AgentTerminalsView). Board/Status: Katalog abwarten, Board-Subtab
  // aktivieren. task: den Node-Detail-Drawer für den referenzierten Task öffnen
  // (Kanban-Attention aus dem Postfach → /control/fleet?task=<id> landet hier
  // und fokussiert den Task, statt einen inerten Query-Param stehen zu lassen).
  useEffect(() => {
    if (deepLinkConsumedRef.current) return;
    const boardParam = searchParams.get("board");
    const statusParam = searchParams.get("status");
    const taskParam = searchParams.get("task");
    const hasBoard = boardParam != null && boardParam !== "";
    const hasStatus = statusParam != null && statusParam !== "";
    const hasTask = taskParam != null && taskParam !== "";
    if (!hasBoard && !hasStatus && !hasTask) return;
    // Race-guard: mit ?board= erst konsumieren, wenn der Katalog wirklich DA ist.
    // Ein transient fehlgeschlagener erster Poll hat loading=false + data=null —
    // dann NICHT konsumieren (Params bleiben in der URL, der nächste erfolgreiche
    // Poll wendet den Deep-Link an), sonst ginge der Link still verloren.
    // ?task= hängt NICHT am Katalog (NodeDetail lädt selbst per Task-ID).
    if (hasBoard && boardCatalog.data == null) return;
    deepLinkConsumedRef.current = true;
    const next = new URLSearchParams(searchParams);
    next.delete("board");
    next.delete("status");
    next.delete("task");
    setSearchParams(next, { replace: true });

    if (hasBoard) {
      const resolved = resolveDeepLinkBoard(boardParam, boardCatalog.data);
      if (resolved !== undefined) {
        setSelectedBoard(resolved);
      }
    }
    const status = parseDeepLinkStatus(statusParam);
    if (status != null) {
      setDeepLinkStatusFilter(status);
    }
    if (hasTask) {
      // Öffnet den Task-Detail-Drawer (mobil) bzw. die Detail-Pane (Desktop);
      // NodeDetailContent holt Body/Deliverables/Aktivität selbst per Task-ID,
      // funktioniert also auch für einen bereits abgeschlossenen Task.
      openNodeDetail(taskParam as string);
    }
    // Board/Status landen auf dem Board-Subtab; ein reiner ?task=-Link belässt
    // den aktiven Subtab (der Drawer/die Detail-Pane überlagert ohnehin).
    if (hasBoard || hasStatus) {
      setSubtab("board");
    }
  }, [
    searchParams,
    setSearchParams,
    boardCatalog.data,
    setSelectedBoard,
  ]);

  // Plan-Adressierbarkeit ist vom Board/Task-One-Shot getrennt: ?plan= wartet
  // auf die passende PlanSpec-Liste, öffnet genau einmal und wird anschließend
  // ersetzt. Persistente Filterparameter bleiben dagegen in der URL und setzen
  // den Plan-Subtab bei Reload oder History-Navigation wieder aktiv.
  useEffect(() => {
    const planParam = searchParams.get("plan");
    const hasPlan = planParam != null && planParam !== "";
    const hasPlanSegment = parsePlanSegment(searchParams.get("planSegment")) != null;
    const hasPlanSearch = (searchParams.get("planSearch") ?? "") !== "";
    if (hasPlan || hasPlanSegment || hasPlanSearch) {
      setSubtab("plan");
    }
    if (!hasPlan || planDeepLinkConsumedRef.current) return;
    // Bei kombinierten ?board=&plan=-Links zuerst die Board-Auswahl anwenden;
    // erst der nachfolgende Poll kennt die Plan-Liste des Ziel-Boards.
    if ((searchParams.get("board") ?? "") !== "") return;
    const source = selectedBoard ? selectedPlanspecs.data : planspecs.data;
    if (source == null) return;

    planDeepLinkConsumedRef.current = true;
    const next = new URLSearchParams(searchParams);
    next.delete("plan");
    setSearchParams(next, { replace: true });
    const item = source.planspecs.find((candidate) => candidate.path === planParam);
    if (item) openPlanSpecDetail(item);
  }, [
    searchParams,
    setSearchParams,
    selectedBoard,
    planspecs.data,
    selectedPlanspecs.data,
  ]);

  // Der Deep-Link-Status gilt nur für die MOUNT-Instanz von BoardTab, die ihn
  // konsumiert. Verlässt der Operator den Board-Subtab, ist er verbraucht —
  // sonst würde ein Remount (Heute→Board) den Filter still zurücksetzen.
  useEffect(() => {
    if (subtab !== "board" && deepLinkStatusFilter != null) {
      setDeepLinkStatusFilter(null);
    }
  }, [subtab, deepLinkStatusFilter]);
  const costs = useHermesRunsCosts();
  const daily = useHermesRunsDaily();
  const reliability = useHermesReliability();
  const lanesCatalog = useLanesCatalog();
  const systemHealth = useSystemHealth();
  const pressureStatus = usePressureStatus();
  const decisionQueue = useKanbanDecisionQueue();
  const releaseStatus = useReleaseStatus();
  const releaseMode = useReleaseMode();
  const activeBoardState = selectedBoard ? selectedBoardData : board;

  // Board-Wechsel schließt eine offene Detail-Ansicht (sie gehört zum anderen
  // Board). Beim ERSTEN Lauf (Mount) NICHT zurücksetzen: sonst würde der
  // setTimeout(0) einen per ?task= gerade geöffneten Deep-Link-Drawer wieder
  // schließen. Auf Mount ist ohnehin nichts offen — das Skippen ist ein No-op,
  // außer für den Deep-Link.
  const boardResetMountedRef = useRef(false);
  useEffect(() => {
    if (!boardResetMountedRef.current) {
      boardResetMountedRef.current = true;
      return;
    }
    const reset = window.setTimeout(() => {
      setKettenRootId(null);
      setNodeDetailId(null);
      setNodeDetailChainNodes([]);
      setPlanspecDrawerItem(null);
    }, 0);
    return () => window.clearTimeout(reset);
  }, [selectedBoard]);

  const now = useClientNowSeconds();

  // Abgeleitete Daten
  const fleetWorkerData = fleetWorkers.data ?? workers.data;
  const activeWorkers = (fleetWorkerData?.workers ?? []).filter((w) => w.run_status === "running");
  const allWorkers = fleetWorkerData?.workers ?? [];
  const defaultActiveWorkers = (workers.data?.workers ?? []).filter((w) => w.run_status === "running");

  // Blockierte Tasks aus Board
  const blockedTasks = (board.data?.columns.find((c) => c.name === "blocked")?.tasks ?? []);
  const blockedCount = blockedTasks.length;

  // Offene PlanSpecs die auf Operator-Freigabe oder Kettenstart warten
  const planRecords = planspecs.data?.planspecs ?? [];
  const allPlanspecs = planRecords.filter((item) => item.open);
  // Das geöffnete Detail wird beim Klick als Momentaufnahme abgelegt. Nach
  // einer Mutation (Übergabe/Freigabe) lädt `onChanged` die Liste neu — die
  // Momentaufnahme blieb dabei stehen, sodass eine erfolgreich übergebene
  // Kette im offenen Detail weiter „bereit, Übergabe vorprüfen" zeigte: keine
  // sichtbare Änderung trotz erfolgter Mutation. Der Datensatz wird deshalb
  // aus der Liste nachgezogen; die Momentaufnahme bleibt nur Rückfallebene
  // (Deep-Link vor dem ersten Laden, Quelle nicht mehr in der Liste).
  const activePlanspecItem = useMemo(() => {
    if (!planspecDrawerItem) return null;
    return planRecords.find((record) => record.path === planspecDrawerItem.path) ?? planspecDrawerItem;
  }, [planRecords, planspecDrawerItem]);
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecAwaitsPlanAction(ps)).length;
  // Geparkte Release-Gates (post-merge, wartet auf Operator-Ausführung) — einziges
  // Zuhause ist Fleet → Risiko, aus dem /control-Postfach verschoben.
  const releaseGateDecisions = (decisionQueue.data?.decisions ?? []).filter((d) => d.kind === "release_gate_parked");

  // "Wartet auf dich"-Items: wartende Freigaben + Operator-Halts
  const pendingItems = useMemo(
    () => derivePendingItems(
      allPlanspecs.map((ps) => ({ freigabe: ps.freigabe, kanban_state: ps.kanban_state, topic: ps.topic, filename: ps.filename })),
      blockedTasks.map((t) => ({ id: t.id, title: t.title, operator_question: t.operator_question })),
    ),
    [allPlanspecs, blockedTasks],
  );

  // Ketten-Chips für Badge und persistente rechte Spalte aus derselben Board-Payload.
  const activeBoardData = selectedBoard ? selectedBoardData.data : board.data;
  const activeBoardTasksForKetten = useMemo(
    () => (activeBoardData?.columns ?? []).flatMap((column) => column.tasks).map((t) => ({
      id: t.id,
      title: t.title,
      root_id: t.root_id,
      status: t.status,
      completed_at: t.completed_at,
    })),
    [activeBoardData],
  );
  const kettenChipsForAside = useMemo(
    () => buildChainChips(activeBoardTasksForKetten, activeBoardData?.chain_summaries),
    [activeBoardTasksForKetten, activeBoardData?.chain_summaries],
  );
  const runningChainCount = kettenChipsForAside.filter((chip) => chip.state === "active").length;

  const subtabDefs: SubtabDef[] = [
    { id: "heute", label: de.fleet.subtabHeute },
    { id: "worker", label: de.fleet.subtabWorker, count: activeWorkers.length > 0 ? activeWorkers.length : undefined },
    { id: "ketten", label: de.fleet.subtabKetten, count: runningChainCount > 0 ? runningChainCount : undefined },
    { id: "board", label: de.fleet.subtabBoard },
    { id: "plan", label: de.fleet.subtabPlan, count: pendingApprovals > 0 ? pendingApprovals : undefined },
    { id: "risiko", label: de.fleet.subtabRisiko, warn: blockedCount > 0 },
  ];

  function closeNodeDetail() {
    setNodeDetailId(null);
    setNodeDetailChainNodes([]);
  }

  function openNodeDetail(id: string, chainNodes: ChainGraphResponse["nodes"] = []) {
    setPlanspecDrawerItem(null);
    setNodeDetailId(id);
    setNodeDetailChainNodes(chainNodes);
  }

  function closePlanSpecDetail() {
    setPlanspecDrawerItem(null);
  }

  /**
   * Subtab-Wechsel schließt ein offenes Detail.
   *
   * Ohne das blieb ein offenes Detail über dem neu gewählten Tab stehen: der
   * Wechsel war vollzogen (aria-pressed korrekt), sichtbar war aber weiter das
   * Detail des alten Tabs. Auf Desktop (TwoPane) belegte die rechte Spalte
   * dauerhaft ein fremdes PlanSpec-Detail, unter lg lag der modale Drawer
   * komplett darüber.
   */
  function selectSubtab(next: FleetSubtab) {
    closeNodeDetail();
    closePlanSpecDetail();
    setSubtab(next);
  }

  function openPlanSpecDetail(item: PlanSpecRecord) {
    closeNodeDetail();
    setPlanspecDrawerItem(item);
  }

  function openPlanSpecChain(rootId: string) {
    setKettenRootId(rootId);
    closePlanSpecDetail();
    setSubtab("ketten");
  }

  function replacePlanFilters(nextSegment: PlanSegment, nextQuery: string) {
    const next = new URLSearchParams(searchParams);
    if (nextSegment === "all") next.delete("planSegment");
    else next.set("planSegment", nextSegment);
    if (nextQuery === "") next.delete("planSearch");
    else next.set("planSearch", nextQuery);
    setSearchParams(next, { replace: true });
  }

  const planAction = activePlanspecItem ? (
    <PlanSpecTransitionPanel
      key={`${selectedBoard ?? "current"}:${activePlanspecItem.path}`}
      item={activePlanspecItem}
      boardSlug={selectedBoard}
      readOnly={selectedBoard != null}
      onChanged={() => selectedBoard ? selectedPlanspecs.reload() : planspecs.reload()}
      onOpenTask={selectedBoard ? undefined : openNodeDetail}
    />
  ) : null;

  function selectBoard(boardSlug: string | null) {
    setKettenRootId(null);
    closeNodeDetail();
    closePlanSpecDetail();
    setSelectedBoard(boardSlug);
  }

  const desktopDetail = isLg
    ? nodeDetailId
      ? (
          <div id="fleet-detail-pane" key={`${selectedBoard ?? "current"}:${nodeDetailId}`}>
            <NodeDetailContent
              taskId={nodeDetailId}
              board={selectedBoard}
              readOnly={selectedBoard != null}
              chainNodes={nodeDetailChainNodes}
              now={now}
              onClose={closeNodeDetail}
              onChanged={activeBoardState.reload}
            />
          </div>
        )
      : activePlanspecItem
        ? (
            <div id="fleet-detail-pane" key={`${selectedBoard ?? "current"}:${activePlanspecItem.path}`}>
              <PlanSpecDetailContent
                item={activePlanspecItem}
                detail={planspecDetail.data}
                loading={planspecDetail.loading}
                error={planspecDetail.error}
                stale={planspecDetail.isStale}
                actions={planAction}
                onOpenChain={openPlanSpecChain}
              />
            </div>
          )
        : undefined
    : undefined;

  const desktopDetailLabel = nodeDetailId
    ? "Task-Details"
    : planspecDrawerItem
      ? "PlanSpec-Details"
      : "Aktive Kette";

  // Heute ist ein Tages-Cockpit, kein zweiter Ketten-Tab: die volle Kette
  // wird auf Heute NICHT mehr automatisch in die rechte Pane gespiegelt (AC-4).
  const desktopIdleDetail = isLg
    && desktopDetail === undefined
    && kettenChipsForAside.length > 0
    && !["ketten", "heute", "board", "plan"].includes(subtab)
    ? (
        <div id="fleet-detail-pane">
          <KettenTab
            key={selectedBoard ?? "current"}
            board={activeBoardData}
            boardSlug={selectedBoard}
            workers={selectedBoard ? activeWorkers.filter((worker) => worker.board_slug === selectedBoard) : undefined}
            readOnly={selectedBoard != null}
            initialRootId={selectedBoard ? null : kettenRootId ?? (kettenChipsForAside.find((c) => c.state === "active")?.rootId ?? null)}
            now={now}
            selectedNodeId={nodeDetailId}
            detailControlsId="fleet-detail-pane"
            onOpenNodeDetail={openNodeDetail}
          />
        </div>
      )
    : undefined;

  return (
    <div data-fleet-theme data-fleet-subtab={subtab} className="fleet-root flex min-h-0 flex-col" style={{ maxWidth: "100%" }}>
      {/* Subtab-Chips — geteilter Leitstand-Baustein, Fleet-Skin via classes.
          Erste Inhaltszeile direkt unter der Shell-Masthead (W3-1a). */}
      <div ref={subtabStripRef}>
        <SubtabChips
          items={subtabDefs}
          active={subtab}
          onSelect={selectSubtab}
          ariaLabelPrefix="Subtab"
          className="py-2.5 fleet-subtabs"
          classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
        />
      </div>

      {(activeBoardState.isStale || activeBoardState.error) ? (
        <div className="mb-2 flex items-center justify-end gap-2" role="status" aria-live="polite">
          <StaleBadge
            isStale={activeBoardState.isStale}
            lastUpdated={activeBoardState.lastUpdated}
            errorObj={activeBoardState.errorObj}
            error={activeBoardState.error}
          />
          <button
            type="button"
            className="inline-flex min-h-8 items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 py-1 text-micro font-medium text-ink-2 transition hover:border-live hover:text-ink"
            onClick={() => void activeBoardState.reload()}
          >
            {de.fleet.freshnessReload}
          </button>
        </div>
      ) : null}

      {/* Board und Plan tragen ihre eigene Arbeitsmenge im Register. Dort wäre
          die globale Wartet-Zeile eine doppelte, raumgreifende Affordanz. */}
      {pendingItems.length > 0
        && subtab !== "heute"
        && subtab !== "board"
        && subtab !== "plan" ? (
        <PendingBar items={pendingItems} onNavigate={(target) => setSubtab(target)} />
      ) : null}

      {/* Unter lg bleibt das bestehende Drawer-Verhalten erhalten. */}
      {!isLg && nodeDetailId ? (
        <NodeDetailDrawer
          key={`${selectedBoard ?? "current"}:${nodeDetailId}`}
          taskId={nodeDetailId}
          board={selectedBoard}
          readOnly={selectedBoard != null}
          chainNodes={nodeDetailChainNodes}
          now={now}
          onClose={closeNodeDetail}
          onChanged={activeBoardState.reload}
        />
      ) : null}

      {!isLg && activePlanspecItem ? (
        <PlanSpecDetailDrawer
          key={`${selectedBoard ?? "current"}:${activePlanspecItem.path}`}
          item={activePlanspecItem}
          detail={planspecDetail.data}
          loading={planspecDetail.loading}
          error={planspecDetail.error}
          stale={planspecDetail.isStale}
          onClose={closePlanSpecDetail}
          actions={planAction}
          onOpenChain={openPlanSpecChain}
        />
      ) : null}

      <TwoPane
        detail={desktopDetail}
        detailLabel={desktopDetailLabel}
        onCloseDetail={nodeDetailId ? closeNodeDetail : planspecDrawerItem ? closePlanSpecDetail : undefined}
        idleDetail={desktopIdleDetail}
        list={(
        <div className="fleet-tablet-main">
          {/* Scrollbarer Inhalt */}
          <div className="fleet-tablet-main-scroll">
            {subtab === "heute" && (
              <>
                <FleetSourceFreshness sources={[
                  { label: "Worker (alle Boards)", ...fleetWorkers },
                  { label: "PlanSpecs", ...planspecs },
                  { label: "Kosten", ...costs },
                  { label: "Tagesmetriken", ...daily },
                ]} />
                <HeuteTab
                  allWorkers={allWorkers}
                  activeWorkers={activeWorkers}
                  blockedCount={blockedCount}
                  pendingApprovals={pendingApprovals}
                  allPlanspecs={allPlanspecs}
                  costs={costs.data}
                  daily={daily.data}
                  now={now}
                  pendingItems={pendingItems}
                  onWorkerClick={(w) => {
                    setDrawerWorker(w);
                    setSubtab("worker");
                  }}
                  onPlanSpecClick={openPlanSpecDetail}
                  onNavigate={setSubtab}
                />
              </>
            )}
            {subtab === "worker" && (
              <>
                <FleetSourceFreshness sources={[
                  { label: "Worker (alle Boards)", ...fleetWorkers },
                  { label: "Worker (aktuelles Board)", ...workers },
                  { label: "Verlässlichkeit", ...reliability },
                  { label: "Kosten", ...costs },
                ]} />
                <WorkerTab
                  activeWorkers={activeWorkers}
                  board={board.data}
                  reliability={reliability.data}
                  now={now}
                  cap={workers.data?.cap ?? null}
                  doneToday={costs.data?.today.runs ?? null}
                  tokensToday={
                    costs.data?.today.input_tokens != null || costs.data?.today.output_tokens != null
                      ? (costs.data.today.input_tokens ?? 0) + (costs.data.today.output_tokens ?? 0)
                      : null
                  }
                  onWorkersChanged={() => {
                    void fleetWorkers.reload();
                    void workers.reload();
                  }}
                  workersScopeAllBoards={fleetWorkers.data != null}
                  currentBoard={boardCatalog.data?.current ?? "default"}
                  eventBoards={(boardCatalog.data?.boards ?? [])
                    .filter((catalogBoard) => !catalogBoard.archived)
                    .map((catalogBoard) => catalogBoard.slug)}
                  initialOpen={drawerWorker}
                  onOpenChain={(rootId: string) => {
                    setKettenRootId(rootId);
                    setDrawerWorker(null);
                    setSubtab("ketten");
                  }}
                />
              </>
            )}
            {subtab === "ketten" && (
              <>
                {boardCatalog.data ? (
                  <BoardSwitcher boards={boardCatalog.data.boards} current={boardCatalog.data.current} selected={selectedBoard} onSelect={selectBoard} />
                ) : null}
                <KettenTab
                  key={selectedBoard ?? "current"}
                  board={activeBoardData}
                  boardSlug={selectedBoard}
                  workers={selectedBoard ? activeWorkers.filter((worker) => worker.board_slug === selectedBoard) : undefined}
                  readOnly={selectedBoard != null}
                  initialRootId={selectedBoard ? null : kettenRootId}
                  now={now}
                  selectedNodeId={nodeDetailId}
                  detailControlsId={isLg ? "fleet-detail-pane" : undefined}
                  onOpenNodeDetail={openNodeDetail}
                />
              </>
            )}
            {subtab === "plan" && (
              <>
                {boardCatalog.data ? (
                  <BoardSwitcher boards={boardCatalog.data.boards} current={boardCatalog.data.current} selected={selectedBoard} onSelect={selectBoard} />
                ) : null}
                <FleetSourceFreshness sources={[
                  { label: "PlanSpecs", ...(selectedBoard ? selectedPlanspecs : planspecs) },
                ]} />
                <PlanTab
                  allPlanspecs={selectedBoard ? (selectedPlanspecs.data?.planspecs ?? []) : planRecords}
                  summary={selectedBoard ? selectedPlanspecs.data?.summary : planspecs.data?.summary}
                  loading={selectedBoard ? selectedPlanspecs.loading : planspecs.loading}
                  error={selectedBoard ? selectedPlanspecs.error : planspecs.error}
                  onRetry={selectedBoard ? selectedPlanspecs.reload : planspecs.reload}
                  readOnly={selectedBoard != null}
                  selectedPath={planspecDrawerItem?.path}
                  segment={planSegment}
                  query={planQuery}
                  onSegmentChange={(nextSegment) => replacePlanFilters(nextSegment, planQuery)}
                  onQueryChange={(nextQuery) => replacePlanFilters(planSegment, nextQuery)}
                  onApproveSuccess={() => {
                    void (selectedBoard ? selectedPlanspecs.reload() : planspecs.reload());
                  }}
                  onShowDetail={openPlanSpecDetail}
                />
              </>
            )}
            {subtab === "board" && (
              <>
                {boardCatalog.data ? (
                  <BoardSwitcher boards={boardCatalog.data.boards} current={boardCatalog.data.current} selected={selectedBoard} onSelect={selectBoard} />
                ) : null}
                <BoardTab
                  board={activeBoardData}
                  boardSlug={selectedBoard}
                  readOnly={selectedBoard != null}
                  initialStatusFilter={deepLinkStatusFilter}
                  selectedNodeId={nodeDetailId}
                  detailControlsId={isLg ? "fleet-detail-pane" : undefined}
                  onOpenNodeDetail={openNodeDetail}
                  onOpenChain={openPlanSpecChain}
                />
              </>
            )}
            {subtab === "risiko" && (
              <>
                <FleetSourceFreshness sources={[
                  { label: "Worker (aktuelles Board)", ...workers },
                  { label: "Verlässlichkeit", ...reliability },
                  { label: "Entscheidungsqueue", ...decisionQueue },
                  { label: "Release-Status", ...releaseStatus },
                  { label: "Release-Modus", ...releaseMode },
                  { label: "Lanes", ...lanesCatalog },
                  { label: "Systemzustand", ...systemHealth },
                  { label: "Systemdruck", ...pressureStatus },
                ]} />
                <RisikoTab
                  blockedTasks={blockedTasks}
                  reliability={reliability.data}
                  systemHealth={systemHealth.data}
                  pressureStatus={pressureStatus.data}
                  activeWorkers={defaultActiveWorkers}
                  lanesCatalog={lanesCatalog.data}
                  releaseGateDecisions={releaseGateDecisions}
                  releaseMode={releaseMode.data}
                  onReleaseModeChanged={releaseMode.reload}
                  releaseStatus={releaseStatus.data}
                  onTaskChanged={board.reload}
                />
              </>
            )}
          </div>
        </div>
        )}
      />
    </div>
  );
}

// ─── "Wartet auf dich"-Zeile ────────────────────────────────────────────────
// Reshaped W3-1a: vormals ein full-bleed amber Glow-Band (zwei Varianten —
// Desktop-Banner + mobile sticky-bottom-Leiste). Jetzt ein einziger kompakter
// warn-Callout (Design-Vokabular: Led + Text + Pfeil, explizite Status-Tokens)
// am Kopf des Inhaltsbereichs, für jeden Subtab gleich sichtbar — die
// FUNKTION (auf wartende Freigaben/Operator-Halts hinweisen, Klick navigiert
// zum passenden Subtab) bleibt unverändert.

interface PendingBarProps {
  items: PendingItem[];
  onNavigate: (target: "plan" | "risiko") => void;
}

function PendingBar({ items, onNavigate }: PendingBarProps) {
  const first = items[0];
  if (!first) return null;

  const text =
    items.length === 1
      ? de.fleet.pendingBarSingle(first.topic)
      : de.fleet.pendingBarMultiple(items.length);

  function handleClick() {
    // Bei mehreren: navigiere zum Plan wenn Freigaben vorhanden, sonst Risiko
    const target = items.find((i) => i.kind === "approval")?.targetSubtab ?? first.targetSubtab;
    onNavigate(target);
  }

  return (
    <button
      type="button"
      className="mb-2 flex min-h-12 w-full items-center gap-2.5 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-left text-status-warn"
      onClick={handleClick}
      aria-label={text}
      aria-live="polite"
    >
      <Led kind="warn" />
      <span className="flex-1 truncate text-sec font-medium">{text}</span>
      <ArrowRight className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
    </button>
  );
}
