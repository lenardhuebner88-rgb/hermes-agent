/**
 * FleetView — Operator-Lagezentrum: Hermes-Flotte auf einen Blick.
 *
 * Subtabs: Heute · Worker · Ketten · Plan · Risiko
 * Diese Datei ist nur noch die Shell (State + Subtab-Switch + "Wartet auf dich"-Leiste).
 * Die Subtabs leben als eigene Dateien unter ./fleet/ — reine Zerlegung, kein
 * Verhalten geändert:
 *   HeuteTab · WorkerTab (+ WorkerDrawer) · KettenTab · NodeDetailDrawer · PlanTab · RisikoTab.
 * Pure Logik bleibt in lib/fleetHub.ts; geteilte Typen/Formatter in ./fleet/shared.ts.
 *
 * Design: dunkles Marineblau-Theme NUR im Fleet-Tab-Scope ([data-fleet-theme]).
 * Glow/Puls ausschließlich bei laufender Aktivität (Licht = Leben).
 */
import { useState, useMemo, useEffect } from "react";
import { useHermesWorkers, useBoard, usePlanSpecs, useHermesRunsCosts, useHermesRunsDaily, useHermesReliability, useLanesCatalog, useAccountUsage, useSystemHealth, usePressureStatus, usePlanSpecDetail } from "../hooks/useControlData";
import { planSpecAwaitsPlanAction, derivePendingItems, buildChainChips, type PendingItem } from "../lib/fleetHub";
import { nowSec } from "../lib/derive";
import { de } from "../i18n/de";
import type { Worker, ChainGraphResponse, PlanSpecRecord } from "../lib/types";
import { HeuteTab } from "./fleet/HeuteTab";
import { WorkerTab } from "./fleet/WorkerTab";
import { KettenTab } from "./fleet/KettenTab";
import { BoardTab } from "./fleet/BoardTab";
import { NodeDetailDrawer } from "./fleet/NodeDetailDrawer";
import { PlanSpecDetailDrawer } from "./fleet/PlanSpecDetailDrawer";
import { PlanTab } from "./fleet/PlanTab";
import { RisikoTab } from "./fleet/RisikoTab";
import { SubtabChips } from "../components/leitstand";
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
  const board = useBoard();
  const planspecs = usePlanSpecs({ scope: "open", limit: 10 });
  const costs = useHermesRunsCosts();
  const daily = useHermesRunsDaily();
  const reliability = useHermesReliability();
  const lanesCatalog = useLanesCatalog();
  const accountUsage = useAccountUsage();
  const systemHealth = useSystemHealth();
  const pressureStatus = usePressureStatus();

  const now = nowSec();

  // Abgeleitete Daten
  const activeWorkers = (workers.data?.workers ?? []).filter((w) => w.run_status === "running");
  const allWorkers = workers.data?.workers ?? [];

  // Blockierte Tasks aus Board
  const allBoardTasksFlat = (board.data?.columns ?? []).flatMap((c) => c.tasks);
  const blockedTasks = (board.data?.columns.find((c) => c.name === "blocked")?.tasks ?? []);
  const blockedCount = blockedTasks.length;

  // Offene PlanSpecs die auf Operator-Freigabe oder Kettenstart warten
  const allPlanspecs = planspecs.data?.planspecs ?? [];
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecAwaitsPlanAction(ps)).length;
  const activePlanspecs = allPlanspecs.filter((ps) => ps.kanban_state === "running" || ps.kanban_state === "queued");

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

  // Ketten-Chips für die persistente rechte Spalte auf Tablet/Desktop
  const allBoardTasksForKetten = allBoardTasksFlat.map((t) => ({
    id: t.id,
    title: t.title,
    root_id: t.root_id,
    status: t.status,
    completed_at: t.completed_at,
  }));
  const kettenChipsForAside = buildChainChips(allBoardTasksForKetten);

  return (
    <div data-fleet-theme className="fleet-root flex min-h-0 flex-col" style={{ maxWidth: "100%", overflow: "hidden" }}>
      {/* Fleet-Header */}
      <div className="fleet-header">
        <div className="fleet-brand">
          <span className="fleet-brand-h">Hermes</span>
          <span className="fleet-brand-f">Flotte</span>
        </div>
        <div className="fleet-live">
          <span className="fleet-live-dot" />
          LIVE
        </div>
      </div>

      {/* "Wartet auf dich"-Banner (Desktop: oberhalb der Chips, als schmale Zeile) */}
      {pendingItems.length > 0 ? (
        <PendingBar
          items={pendingItems}
          onNavigate={(target) => setSubtab(target)}
          variant="desktop"
        />
      ) : null}

      {/* Subtab-Chips — geteilter Leitstand-Baustein, Fleet-Skin via classes. */}
      <SubtabChips
        items={subtabDefs}
        active={subtab}
        onSelect={setSubtab}
        ariaLabelPrefix="Subtab"
        className="py-2.5"
        classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
      />

      {/* Karten-Detail-Drawer (Overlay, rendert außerhalb des Scrollbereichs) */}
      {nodeDetailId ? (
        <NodeDetailDrawer
          taskId={nodeDetailId}
          chainNodes={nodeDetailChainNodes}
          now={now}
          onClose={() => { setNodeDetailId(null); setNodeDetailChainNodes([]); }}
          onChanged={board.reload}
        />
      ) : null}

      {/* PlanSpec-Detail-Drawer (Volltext, Fleet-Besitz) */}
      {planspecDrawerItem ? (
        <PlanSpecDetailDrawer
          item={planspecDrawerItem}
          detail={planspecDetail.data}
          loading={planspecDetail.loading}
          error={planspecDetail.error}
          onClose={() => { setPlanspecDrawerItem(null); }}
        />
      ) : null}

      {/* Tablet-Layout: ab lg zweispaltig */}
      <div className="fleet-tablet-layout">
        {/* Linke/Haupt-Spalte */}
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
                onWorkerClick={(w) => {
                  setDrawerWorker(w);
                  setSubtab("worker");
                }}
                onPlanSpecClick={(ps) => setPlanspecDrawerItem(ps)}
              />
            )}
            {subtab === "worker" && (
              <WorkerTab
                activeWorkers={activeWorkers}
                board={board.data}
                reliability={reliability.data}
                now={now}
                initialOpen={drawerWorker}
                onOpenChain={(rootId: string) => {
                  setKettenRootId(rootId);
                  setDrawerWorker(null);
                  setSubtab("ketten");
                }}
              />
            )}
            {subtab === "ketten" && (
              <KettenTab
                board={board.data}
                initialRootId={kettenRootId}
                now={now}
                onOpenNodeDetail={(id, chainNodes) => {
                  setNodeDetailId(id);
                  setNodeDetailChainNodes(chainNodes ?? []);
                }}
              />
            )}
            {subtab === "plan" && (
              <PlanTab
                allPlanspecs={allPlanspecs}
                costs={costs.data}
                lanesCatalog={lanesCatalog.data}
                accountUsage={accountUsage.data}
                onApproveSuccess={() => {
                  // Refetch planspecs nach Freigabe
                  void planspecs.reload();
                }}
                onShowDetail={(ps) => setPlanspecDrawerItem(ps)}
              />
            )}
            {subtab === "board" && (
              <BoardTab
                board={board.data}
                onOpenNodeDetail={(id, chainNodes) => {
                  setNodeDetailId(id);
                  setNodeDetailChainNodes(chainNodes ?? []);
                }}
              />
            )}
            {subtab === "risiko" && (
              <RisikoTab
                allPlanspecs={allPlanspecs}
                blockedTasks={blockedTasks}
                reliability={reliability.data}
                systemHealth={systemHealth.data}
                pressureStatus={pressureStatus.data}
                activeWorkers={activeWorkers}
                lanesCatalog={lanesCatalog.data}
                onNavigateToPlan={() => setSubtab("plan")}
                onTaskChanged={board.reload}
              />
            )}
          </div>

          {/* "Wartet auf dich"-Leiste (Mobile: sticky bottom) */}
          {pendingItems.length > 0 ? (
            <PendingBar
              items={pendingItems}
              onNavigate={(target) => setSubtab(target)}
              variant="mobile"
            />
          ) : null}
        </div>

        {/* Rechte Spalte: persistente Kette — nur rendern wenn (a) Viewport ≥ lg
            UND (b) Ketten-Subtab nicht aktiv ist. Verhindert unsichtbares Polling
            auf Mobil und Doppel-Poll wenn Ketten-Subtab bereits KettenTab hält. */}
        {isLg && subtab !== "ketten" ? (
          <aside className="fleet-tablet-aside" aria-label="Aktive Kette">
            {kettenChipsForAside.length > 0 ? (
              <>
                <div className="fleet-aside-head">Aktive Kette</div>
                <KettenTab
                  board={board.data}
                  initialRootId={kettenRootId ?? (kettenChipsForAside.find((c) => c.state === "active")?.rootId ?? null)}
                  now={now}
                  onOpenNodeDetail={(id, chainNodes) => {
                    setNodeDetailId(id);
                    setNodeDetailChainNodes(chainNodes ?? []);
                  }}
                />
              </>
            ) : (
              <div className="fleet-aside-head" style={{ color: "var(--fleet-t3)", fontStyle: "italic" }}>
                Keine aktiven Ketten
              </div>
            )}
          </aside>
        ) : null}
      </div>
    </div>
  );
}

// ─── "Wartet auf dich"-Leiste ─────────────────────────────────────────────────

interface PendingBarProps {
  items: PendingItem[];
  onNavigate: (target: "plan" | "risiko") => void;
  /**
   * "desktop" → nur auf lg+ sichtbar, oberhalb der Chips als Banner.
   * "mobile"  → sticky bottom, nur auf < lg sichtbar (über Safe-Area).
   */
  variant: "desktop" | "mobile";
}

function PendingBar({ items, onNavigate, variant }: PendingBarProps) {
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

  // CSS-Variante: desktop-Banner oder mobile sticky bottom
  const extraClass = variant === "desktop" ? " fleet-pending-bar-desktop" : " fleet-pending-bar-mobile";

  return (
    <button
      type="button"
      className={`fleet-pending-bar${extraClass}`}
      onClick={handleClick}
      aria-label={text}
      aria-live="polite"
    >
      <span className="fleet-pending-bar-dot" aria-hidden="true" />
      <span className="fleet-pending-bar-text">{text}</span>
      <span className="fleet-pending-bar-arrow" aria-hidden="true">→</span>
    </button>
  );
}
