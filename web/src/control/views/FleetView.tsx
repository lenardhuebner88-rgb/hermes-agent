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
import { useHermesWorkers, useAllBoardWorkers, useBoardCatalog, useBoard, usePlanSpecs, useHermesRunsCosts, useHermesRunsDaily, useHermesReliability, useLanesCatalog, useAccountUsage, useSystemHealth, usePressureStatus, usePlanSpecDetail, useKanbanDecisionQueue, useReleaseStatus, useReleaseMode } from "../hooks/useControlData";
import { useFleetBoardSelection } from "../hooks/useFleetBoardSelection";
import { planSpecAwaitsPlanAction, derivePendingItems, buildChainChips, type PendingItem } from "../lib/fleetHub";
import { nowSec } from "../lib/derive";
import { de } from "../i18n/de";
import type { Worker, ChainGraphResponse, PlanSpecRecord } from "../lib/types";
import { HeuteTab } from "./fleet/HeuteTab";
import { WorkerTab } from "./fleet/WorkerTab";
import { KettenTab } from "./fleet/KettenTab";
import { BoardTab } from "./fleet/BoardTab";
import { NodeDetailContent, NodeDetailDrawer } from "./fleet/NodeDetailDrawer";
import { PlanSpecDetailContent, PlanSpecDetailDrawer } from "./fleet/PlanSpecDetailDrawer";
import { PlanTab } from "./fleet/PlanTab";
import { RisikoTab } from "./fleet/RisikoTab";
import { SubtabChips, TwoPane } from "../components/leitstand";
import { Led } from "../components/atoms";
import { BoardSwitcher } from "../components/fleet/BoardIdentity";
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

  const workers = useHermesWorkers();
  const fleetWorkers = useAllBoardWorkers();
  const boardCatalog = useBoardCatalog();
  const { selectedBoard, setSelectedBoard } = useFleetBoardSelection(boardCatalog.data);
  const board = useBoard();
  const selectedBoardData = useBoard(selectedBoard);
  const planspecs = usePlanSpecs({ scope: "open", limit: 10 });
  const selectedPlanspecs = usePlanSpecs({ scope: "open", limit: 10 }, selectedBoard);
  const costs = useHermesRunsCosts();
  const daily = useHermesRunsDaily();
  const reliability = useHermesReliability();
  const lanesCatalog = useLanesCatalog();
  const accountUsage = useAccountUsage();
  const systemHealth = useSystemHealth();
  const pressureStatus = usePressureStatus();
  const decisionQueue = useKanbanDecisionQueue();
  const releaseStatus = useReleaseStatus();
  const releaseMode = useReleaseMode();

  useEffect(() => {
    const reset = window.setTimeout(() => {
      setKettenRootId(null);
      setNodeDetailId(null);
      setNodeDetailChainNodes([]);
    }, 0);
    return () => window.clearTimeout(reset);
  }, [selectedBoard]);

  const now = nowSec();

  // Abgeleitete Daten
  const fleetWorkerData = fleetWorkers.data ?? workers.data;
  const activeWorkers = (fleetWorkerData?.workers ?? []).filter((w) => w.run_status === "running");
  const allWorkers = fleetWorkerData?.workers ?? [];
  const defaultActiveWorkers = (workers.data?.workers ?? []).filter((w) => w.run_status === "running");

  // Blockierte Tasks aus Board
  const blockedTasks = (board.data?.columns.find((c) => c.name === "blocked")?.tasks ?? []);
  const blockedCount = blockedTasks.length;

  // Offene PlanSpecs die auf Operator-Freigabe oder Kettenstart warten
  const allPlanspecs = planspecs.data?.planspecs ?? [];
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecAwaitsPlanAction(ps)).length;
  const activePlanspecs = allPlanspecs.filter((ps) => ps.kanban_state === "running" || ps.kanban_state === "queued");

  // Geparkte Release-Gates (post-merge, wartet auf Operator-Ausführung) — einziges
  // Zuhause ist Fleet → Risiko, aus dem /control-Postfach verschoben.
  const releaseGateDecisions = (decisionQueue.data?.decisions ?? []).filter((d) => d.kind === "release_gate_parked");

  // "Wartet auf dich"-Items: wartende Freigaben + Operator-Halts
  const pendingItems = useMemo(
    () => derivePendingItems(
      allPlanspecs.map((ps) => ({ freigabe: ps.freigabe, kanban_state: ps.kanban_state, topic: ps.topic, filename: ps.filename })),
      blockedTasks.map((t) => ({ id: t.id, title: t.title, block_reason: t.block_reason ?? null })),
    ),
    [allPlanspecs, blockedTasks],
  );

  const subtabDefs: SubtabDef[] = [
    { id: "heute", label: de.fleet.subtabHeute },
    { id: "worker", label: de.fleet.subtabWorker, count: activeWorkers.length > 0 ? activeWorkers.length : undefined },
    { id: "ketten", label: de.fleet.subtabKetten, count: activePlanspecs.length > 0 ? activePlanspecs.length : undefined },
    { id: "board", label: de.fleet.subtabBoard },
    { id: "plan", label: de.fleet.subtabPlan, count: pendingApprovals > 0 ? pendingApprovals : undefined },
    { id: "risiko", label: de.fleet.subtabRisiko, warn: blockedCount > 0 },
  ];

  // Ketten-Chips für die persistente rechte Spalte auf Tablet/Desktop.
  // Root-Auswahl und Fetch-Board müssen immer aus derselben Board-Payload stammen.
  const activeBoardData = selectedBoard ? selectedBoardData.data : board.data;
  const activeBoardTasksForKetten = (activeBoardData?.columns ?? []).flatMap((column) => column.tasks).map((t) => ({
    id: t.id,
    title: t.title,
    root_id: t.root_id,
    status: t.status,
    completed_at: t.completed_at,
  }));
  const kettenChipsForAside = buildChainChips(activeBoardTasksForKetten);

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

  function openPlanSpecDetail(item: PlanSpecRecord) {
    closeNodeDetail();
    setPlanspecDrawerItem(item);
  }

  function selectBoard(boardSlug: string | null) {
    setKettenRootId(null);
    closeNodeDetail();
    setSelectedBoard(boardSlug);
  }

  const desktopDetail = isLg
    ? nodeDetailId
      ? (
          <div id="fleet-detail-pane">
            <NodeDetailContent
              taskId={nodeDetailId}
              chainNodes={nodeDetailChainNodes}
              now={now}
              onClose={closeNodeDetail}
              onChanged={board.reload}
            />
          </div>
        )
      : planspecDrawerItem
        ? (
            <div id="fleet-detail-pane">
              <PlanSpecDetailContent
                item={planspecDrawerItem}
                detail={planspecDetail.data}
                loading={planspecDetail.loading}
                error={planspecDetail.error}
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
    && subtab !== "ketten"
    && subtab !== "heute"
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
            selectedNodeId={selectedBoard ? null : nodeDetailId}
            detailControlsId={!selectedBoard ? "fleet-detail-pane" : undefined}
            onOpenNodeDetail={selectedBoard ? () => undefined : openNodeDetail}
          />
        </div>
      )
    : undefined;

  return (
    <div data-fleet-theme className="fleet-root flex min-h-0 flex-col" style={{ maxWidth: "100%", overflow: "hidden" }}>
      {/* Subtab-Chips — geteilter Leitstand-Baustein, Fleet-Skin via classes.
          Erste Inhaltszeile direkt unter der Shell-Masthead (W3-1a). */}
      <div ref={subtabStripRef}>
        <SubtabChips
          items={subtabDefs}
          active={subtab}
          onSelect={setSubtab}
          ariaLabelPrefix="Subtab"
          className="py-2.5 fleet-subtabs"
          classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
        />
      </div>

      {/* "Wartet auf dich"-Zeile: kompakter warn-Callout am Kopf des Inhaltsbereichs.
          Auf Heute übernimmt der Tab selbst den Handlungsblock (kein Doppel-Callout),
          auf allen anderen Subtabs bleibt diese Zeile die gemeinsame Affordanz. */}
      {pendingItems.length > 0 && subtab !== "heute" ? (
        <PendingBar items={pendingItems} onNavigate={(target) => setSubtab(target)} />
      ) : null}

      {/* Unter lg bleibt das bestehende Drawer-Verhalten erhalten. */}
      {!isLg && nodeDetailId ? (
        <NodeDetailDrawer
          taskId={nodeDetailId}
          chainNodes={nodeDetailChainNodes}
          now={now}
          onClose={closeNodeDetail}
          onChanged={board.reload}
        />
      ) : null}

      {!isLg && planspecDrawerItem ? (
        <PlanSpecDetailDrawer
          item={planspecDrawerItem}
          detail={planspecDetail.data}
          loading={planspecDetail.loading}
          error={planspecDetail.error}
          onClose={closePlanSpecDetail}
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
            )}
            {subtab === "worker" && (
              <WorkerTab
                activeWorkers={activeWorkers}
                board={board.data}
                reliability={reliability.data}
                now={now}
                cap={workers.data?.cap ?? null}
                doneToday={costs.data?.today.runs ?? null}
                currentBoard={boardCatalog.data?.current ?? "default"}
                initialOpen={drawerWorker}
                onOpenChain={(rootId: string) => {
                  setKettenRootId(rootId);
                  setDrawerWorker(null);
                  setSubtab("ketten");
                }}
              />
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
                  selectedNodeId={selectedBoard ? null : nodeDetailId}
                  detailControlsId={!selectedBoard && isLg ? "fleet-detail-pane" : undefined}
                  onOpenNodeDetail={selectedBoard ? () => undefined : openNodeDetail}
                />
              </>
            )}
            {subtab === "plan" && (
              <>
                {boardCatalog.data ? (
                  <BoardSwitcher boards={boardCatalog.data.boards} current={boardCatalog.data.current} selected={selectedBoard} onSelect={selectBoard} />
                ) : null}
                <PlanTab
                  allPlanspecs={selectedBoard ? (selectedPlanspecs.data?.planspecs ?? []) : allPlanspecs}
                  costs={costs.data}
                  lanesCatalog={lanesCatalog.data}
                  accountUsage={accountUsage.data}
                  readOnly={selectedBoard != null}
                  onApproveSuccess={() => {
                    // Refetch planspecs nach Freigabe
                    void planspecs.reload();
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
                  readOnly={selectedBoard != null}
                  selectedNodeId={selectedBoard ? null : nodeDetailId}
                  detailControlsId={!selectedBoard && isLg ? "fleet-detail-pane" : undefined}
                  onOpenNodeDetail={selectedBoard ? () => undefined : openNodeDetail}
                />
              </>
            )}
            {subtab === "risiko" && (
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
