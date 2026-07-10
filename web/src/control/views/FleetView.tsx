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
import { useState, useMemo, useEffect } from "react";
import { ArrowRight } from "lucide-react";
import { useHermesWorkers, useBoard, usePlanSpecs, useHermesRunsCosts, useHermesRunsDaily, useHermesReliability, useLanesCatalog, useAccountUsage, useSystemHealth, usePressureStatus, usePlanSpecDetail, useKanbanDecisionQueue, useReleaseStatus, useReleaseMode } from "../hooks/useControlData";
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
import { Led } from "../components/atoms";
import { toneClasses } from "../lib/tones";
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
  const decisionQueue = useKanbanDecisionQueue();
  const releaseStatus = useReleaseStatus();
  const releaseMode = useReleaseMode();

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
      {/* Subtab-Chips — geteilter Leitstand-Baustein, Fleet-Skin via classes.
          Erste Inhaltszeile direkt unter der Shell-Masthead (W3-1a). */}
      <SubtabChips
        items={subtabDefs}
        active={subtab}
        onSelect={setSubtab}
        ariaLabelPrefix="Subtab"
        className="py-2.5"
        classes={{ chip: "fleet-chip", chipActive: "fleet-chip-on", warnDot: "fleet-warn-dot" }}
      />

      {/* "Wartet auf dich"-Zeile: kompakter warn-Callout am Kopf des Inhaltsbereichs,
          über allen Subtabs gleich sichtbar (kein full-bleed Glow-Band mehr). */}
      {pendingItems.length > 0 ? (
        <PendingBar items={pendingItems} onNavigate={(target) => setSubtab(target)} />
      ) : null}

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
                cap={workers.data?.cap ?? null}
                doneToday={costs.data?.today.runs ?? null}
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
                blockedTasks={blockedTasks}
                reliability={reliability.data}
                systemHealth={systemHealth.data}
                pressureStatus={pressureStatus.data}
                activeWorkers={activeWorkers}
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

// ─── "Wartet auf dich"-Zeile ────────────────────────────────────────────────
// Reshaped W3-1a: vormals ein full-bleed amber Glow-Band (zwei Varianten —
// Desktop-Banner + mobile sticky-bottom-Leiste). Jetzt ein einziger kompakter
// warn-Callout (Design-Vokabular: Led + Text + Pfeil, toneClasses("amber"))
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
      className={`mb-2 flex min-h-11 w-full items-center gap-2.5 rounded-card border px-3 py-2 text-left ${toneClasses("amber")}`}
      onClick={handleClick}
      aria-label={text}
    >
      <Led kind="warn" />
      <span className="flex-1 truncate text-sec font-medium">{text}</span>
      <ArrowRight className="h-4 w-4 shrink-0 opacity-70" aria-hidden="true" />
    </button>
  );
}
