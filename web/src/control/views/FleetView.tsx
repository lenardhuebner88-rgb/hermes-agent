/**
 * FleetView — Operator-Lagezentrum: Hermes-Flotte auf einen Blick.
 *
 * Subtabs: Heute · Worker · Ketten · Plan · Risiko
 * Scope 1: Heute-Subtab (Lagezeile + KPI-Panel + Worker-Karten + PlanSpec-Karten)
 * Scope 4: Worker-Subtab + Worker-Drawer (Overlay Bottom-Sheet)
 * Scope t3: Ketten-Subtab (Jetzt-zentriert) + Karten-Detail-Drawer
 * Plan/Risiko: saubere EmptyState-Platzhalter (Folge-Subtasks)
 *
 * Design: dunkles Marineblau-Theme NUR im Fleet-Tab-Scope ([data-fleet-theme]).
 * Glow/Puls ausschließlich bei laufender Aktivität (Licht = Leben).
 */
import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import { useHermesWorkers, useBoard, usePlanSpecs, useHermesRunsCosts, useHermesReliability, useChainGraph, useWorkerActivity, useHermesReviewVerdicts, useTaskBodyOnDemand, useTaskDeliverablesOnDemand, usePlanSpecDetail, useLanesCatalog, useAccountUsage, useSystemHealth, usePressureStatus } from "../hooks/useControlData";
import {
  buildLagezeile,
  etaFraction,
  heartbeatAge,
  fmtSeconds,
  deriveKpi,
  fmtTokens,
  fmtUsd,
  planSpecWaitsForOperator,
  profileInitial,
  profileColorClass,
  buildChainChips,
  buildSegments,
  pickFocusNode,
  chainProgress,
  chainTotalCostUsd,
  budgetTone,
  derivePlanLanes,
  buildApproveRequest,
  fmtResetAt,
  derivePendingItems,
  deriveEffectivePlanPath,
  type PendingItem,
  type ChainChipDef,
  type SegmentKind,
} from "../lib/fleetHub";
import { nowSec } from "../lib/derive";
import { de } from "../i18n/de";
// Worker, BoardResponse, BoardTask, ChainGraphResponse: ALLE aus lib/types.
import type { Worker, BoardResponse, BoardTask, ChainGraphResponse } from "../lib/types";
import type { PlanSpecsResponse, RunsCostsResponse, ReliabilityResponse, LanesCatalogResponse } from "../lib/schemas";
import type { SystemHealthResponse, PressureStatusResponse } from "../lib/types";
import { Overlay } from "../components/Overlay";
import { WorkerLogTail } from "../components/WorkerCard";
import { openAuthedApiFile, fetchJSON } from "@/lib/api";
import "./fleet/fleet.css";

type PlanSpecRecord = PlanSpecsResponse["planspecs"][number];

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

type FleetSubtab = "heute" | "worker" | "ketten" | "plan" | "risiko";

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

  // Offene PlanSpecs die auf Operator warten
  const allPlanspecs = planspecs.data?.planspecs ?? [];
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecWaitsForOperator(ps.freigabe, ps.kanban_state)).length;
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
    <div data-fleet-theme className="fleet-root flex min-h-0 flex-col" style={{ minHeight: "100%", maxWidth: "100%", overflow: "hidden" }}>
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

      {/* Subtab-Chips */}
      <div className="flex gap-1.5 overflow-x-auto px-0 py-2.5 scrollbar-none" style={{ paddingLeft: 0, paddingRight: 0 }}>
        {subtabDefs.map((def) => (
          <button
            key={def.id}
            type="button"
            className={`fleet-chip${subtab === def.id ? " fleet-chip-on" : ""}`}
            onClick={() => setSubtab(def.id)}
            aria-pressed={subtab === def.id}
            aria-label={`Subtab ${def.label}${def.warn ? " — enthält Warnungen" : ""}`}
          >
            {def.label}
            {def.count != null ? <sup>{def.count}</sup> : null}
            {def.warn ? <span className="fleet-warn-dot" aria-label="Warnung" /> : null}
          </button>
        ))}
      </div>

      {/* Karten-Detail-Drawer (Overlay, rendert außerhalb des Scrollbereichs) */}
      {nodeDetailId ? (
        <NodeDetailDrawer
          taskId={nodeDetailId}
          chainNodes={nodeDetailChainNodes}
          now={now}
          onClose={() => { setNodeDetailId(null); setNodeDetailChainNodes([]); }}
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
                now={now}
                onWorkerClick={(w) => {
                  setDrawerWorker(w);
                  setSubtab("worker");
                }}
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
              />
            )}
            {subtab === "risiko" && (
              <RisikoTab
                allPlanspecs={allPlanspecs}
                blockedTasks={blockedTasks}
                reliability={reliability.data}
                systemHealth={systemHealth.data}
                pressureStatus={pressureStatus.data}
                onNavigateToPlan={() => setSubtab("plan")}
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

// ─── Heute-Subtab ────────────────────────────────────────────────────────────

interface HeuteTabProps {
  allWorkers: Worker[];
  activeWorkers: Worker[];
  blockedCount: number;
  pendingApprovals: number;
  allPlanspecs: PlanSpecRecord[];
  costs: RunsCostsResponse | null;
  now: number;
  onWorkerClick: (w: Worker) => void;
}

function HeuteTab({ allWorkers, activeWorkers, blockedCount, pendingApprovals, allPlanspecs, costs, now, onWorkerClick }: HeuteTabProps) {
  const lagezeile = buildLagezeile({ workers: allWorkers, blockedCount, pendingApprovals });
  const kpi = deriveKpi(
    allWorkers,
    blockedCount,
    costs?.today.actual_cost_usd ?? null,
    costs?.today.runs ?? null,
  );

  return (
    <>
      {/* Lagezeile */}
      <p className="fleet-lage">
        <LagezeileFormatted text={lagezeile} />
      </p>

      {/* KPI-Panel */}
      <div className="fleet-kpanel">
        <div className={`fleet-kp${kpi.aktiv > 0 ? " fleet-kp-aktiv" : ""}`}>
          <div className="fleet-kp-num">{kpi.aktiv}</div>
          <div className="fleet-kp-label">{de.fleet.kpiAktiv}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.blockiert}</div>
          <div className="fleet-kp-label">{de.fleet.kpiBlockiert}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">{kpi.fertig24h ?? "—"}</div>
          <div className="fleet-kp-label">{de.fleet.kpiFertig}</div>
        </div>
        <div className="fleet-kp">
          <div className="fleet-kp-num">
            {kpi.kosten24h != null ? (
              <>
                {kpi.kosten24h.toFixed(1).replace(".", ",")}
                <small>$</small>
              </>
            ) : "—"}
          </div>
          <div className="fleet-kp-label">{de.fleet.kpiKosten}</div>
        </div>
      </div>

      {/* Worker-Karten */}
      {activeWorkers.length === 0 ? null : (
        activeWorkers.map((w) => (
          <WorkerCard key={w.run_id} worker={w} now={now} onClick={() => onWorkerClick(w)} />
        ))
      )}

      {/* PlanSpec-Karten */}
      {allPlanspecs.slice(0, 5).map((ps) => (
        <PlanSpecCard key={ps.path} ps={ps} />
      ))}
    </>
  );
}

// ─── Lagezeile-Formatter ─────────────────────────────────────────────────────

function LagezeileFormatted({ text }: { text: string }) {
  // Einfaches highlighting: "Freigabe" in amber, "wartet" in puls (em)
  // Wir teilen auf " — " und formatieren den letzten Teil hervor wenn Freigabe.
  const parts = text.split(" — ");
  if (parts.length <= 1) return <>{text}</>;
  return (
    <>
      {parts[0]}
      {parts.slice(1).map((part, i) => {
        const isApproval = part.toLowerCase().includes("freigabe") || part.toLowerCase().includes("warten");
        return (
          <span key={i}>
            {" — "}
            {isApproval ? <span className="fleet-amber">{part}</span> : <em>{part}</em>}
          </span>
        );
      })}
    </>
  );
}

// ─── Worker-Karte ────────────────────────────────────────────────────────────

function WorkerCard({ worker: w, now, onClick }: { worker: Worker; now: number; onClick: () => void }) {
  const hbAge = heartbeatAge(w.last_heartbeat_at, now);
  const fraction = etaFraction(w.started_at, w.eta_p50_seconds, now);
  const elapsedSec = Math.max(0, now - w.started_at);
  const initial = profileInitial(w.profile);
  const colorCls = profileColorClass(w.profile);
  const isLive = w.run_status === "running";

  return (
    <button
      type="button"
      className={`fleet-wk text-left${isLive ? " fleet-wk-lebt" : ""}`}
      onClick={onClick}
      aria-label={`Worker ${w.profile} öffnen`}
    >
      {/* Top-Zeile: Avatar + Name + LED */}
      <div className="fleet-wk-top">
        <div className={`fleet-avatar ${colorCls}`}>{initial}</div>
        <div className="fleet-wk-name">
          {w.profile}
          <span>{w.task_id.slice(0, 10)}</span>
        </div>
        {isLive && hbAge != null ? (
          <div className="fleet-led">
            <span className="fleet-led-dot" />
            ♥ {fmtSeconds(hbAge)}
          </div>
        ) : null}
      </div>

      {/* Task-Titel */}
      <div className="fleet-wk-task">{w.task_title}</div>

      {/* Heartbeat-Notiz */}
      {w.last_heartbeat_note ? (
        <div className="fleet-wk-note">{w.last_heartbeat_note}</div>
      ) : null}

      {/* Progress-Rail */}
      {fraction != null ? (
        <div className="fleet-rail">
          <div className="fleet-rail-fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
      ) : null}

      {/* Meta-Zeile */}
      <div className="fleet-wk-meta">
        {w.effective_model ? <b>{w.effective_model.replace(/^claude-/, "").split("-").slice(0, 1).join("")}</b> : null}
        <span>{fmtTokens(w.input_tokens)} → {fmtTokens(w.output_tokens)} tok</span>
        <span>seit {fmtSeconds(elapsedSec)}</span>
        {w.eta_p50_seconds ? (
          <span className="fleet-meta-right">ETA ~{fmtSeconds(w.eta_p50_seconds - elapsedSec > 0 ? w.eta_p50_seconds - elapsedSec : 0)}</span>
        ) : null}
      </div>
    </button>
  );
}

// ─── PlanSpec-Karte ───────────────────────────────────────────────────────────

function PlanSpecCard({ ps }: { ps: PlanSpecRecord }) {
  const fraction = ps.kanban_child_total > 0 ? ps.kanban_child_done / ps.kanban_child_total : null;
  const waitsForOp = planSpecWaitsForOperator(ps.freigabe, ps.kanban_state);
  const isRunning = ps.kanban_state === "running";

  let badgeClass = "fleet-ps-badge-gruen";
  let badgeLabel = ps.status;
  if (waitsForOp) {
    badgeClass = "fleet-ps-badge-amber";
    badgeLabel = de.fleet.psWaitsForOperator;
  } else if (isRunning) {
    badgeClass = "fleet-ps-badge-lauf";
    badgeLabel = `läuft${ps.kanban_child_total > 0 ? ` · ${ps.kanban_child_done}/${ps.kanban_child_total}` : ""}`;
  }

  return (
    <div className="fleet-ps">
      <div className="fleet-ps-top">
        <span className="fleet-ps-name">{ps.topic || ps.filename}</span>
        <span className={`fleet-ps-badge ${badgeClass}`}>{badgeLabel}</span>
      </div>
      {fraction != null ? (
        <div className="fleet-rail">
          <div className="fleet-rail-fill" style={{ width: `${Math.round(fraction * 100)}%` }} />
        </div>
      ) : null}
      <div className="fleet-ps-meta">
        {ps.kanban_child_total > 0 ? (
          <span><b>{ps.kanban_child_done}</b>/{ps.kanban_child_total} Karten</span>
        ) : null}
        <span>{ps.freigabe}</span>
        {ps.live_test_depth ? <span>{ps.live_test_depth}</span> : null}
      </div>
    </div>
  );
}

// ─── Worker-Subtab ────────────────────────────────────────────────────────────

interface WorkerTabProps {
  activeWorkers: Worker[];
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  initialOpen: Worker | null;
  onOpenChain: (rootId: string) => void;
}

function WorkerTab({ activeWorkers, board, reliability, now, initialOpen, onOpenChain }: WorkerTabProps) {
  const [selected, setSelected] = useState<Worker | null>(initialOpen);

  if (activeWorkers.length === 0) {
    return (
      <div className="fleet-empty">
        <p className="fleet-empty-title">{de.fleet.workerEmptyTitle}</p>
        <p className="fleet-empty-sub">{de.fleet.workerEmptyDesc}</p>
      </div>
    );
  }

  return (
    <>
      {activeWorkers.map((w) => (
        <button
          key={w.run_id}
          type="button"
          className="fleet-wk fleet-wk-lebt text-left"
          onClick={() => setSelected(w)}
          aria-label={`Worker ${w.profile} Details`}
        >
          <div className="fleet-wk-top">
            <div className={`fleet-avatar ${profileColorClass(w.profile)}`}>{profileInitial(w.profile)}</div>
            <div className="fleet-wk-name">{w.profile}</div>
            {w.last_heartbeat_at ? (
              <div className="fleet-led">
                <span className="fleet-led-dot" />
                ♥ {fmtSeconds(heartbeatAge(w.last_heartbeat_at, now) ?? 0)}
              </div>
            ) : null}
          </div>
          {etaFraction(w.started_at, w.eta_p50_seconds, now) != null ? (
            <div className="fleet-rail">
              <div
                className="fleet-rail-fill"
                style={{ width: `${Math.round((etaFraction(w.started_at, w.eta_p50_seconds, now) ?? 0) * 100)}%` }}
              />
            </div>
          ) : null}
        </button>
      ))}

      {selected ? (
        <WorkerDrawer
          worker={selected}
          board={board}
          reliability={reliability}
          now={now}
          onClose={() => setSelected(null)}
          onOpenChain={onOpenChain}
        />
      ) : null}
    </>
  );
}

// ─── Worker-Drawer ────────────────────────────────────────────────────────────

interface WorkerDrawerProps {
  worker: Worker;
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  onClose: () => void;
  onOpenChain: (rootId: string) => void;
}

function WorkerDrawer({ worker: w, board, reliability, now, onClose, onOpenChain }: WorkerDrawerProps) {
  const elapsedSec = Math.max(0, now - w.started_at);
  const hbAge = heartbeatAge(w.last_heartbeat_at, now);
  const initial = profileInitial(w.profile);
  const colorCls = profileColorClass(w.profile);

  // Profil-Verlässlichkeit aus Reliability-Daten (ReliabilityResponse aus lib/schemas)
  const relProfile = reliability?.profiles?.find((p) => p.profile === w.profile);

  // Ketten-Position: root_id via Board-Lookup (BoardResponse aus lib/types)
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);
  const boardTask = allBoardTasks.find((t) => t.id === w.task_id);
  // root_id ist entweder der eigene Task (Root) oder der Parent-Root
  const chainRootId = boardTask?.root_id ?? null;
  const branchName = boardTask?.branch_name ?? null;
  const chainMembers = chainRootId
    ? allBoardTasks.filter((t) => t.root_id === chainRootId || t.id === chainRootId)
    : [];

  // Drawer via shared Overlay: garantiert zentriert ab sm, Escape-Handling,
  // Scroll-Lock und Portal via document.body — kein eigener portal nötig.
  // data-fleet-theme wird auf das Overlay-Kind gesetzt damit das dark Theme greift.
  return (
    <Overlay onClose={onClose} ariaLabel={`Worker ${w.profile} Details`} maxWidthClassName="max-w-lg">
      <div data-fleet-theme className="fleet-drawer-inner">
        {/* Grab Handle */}
        <div className="fleet-grab" />

        {/* Header */}
        <div className="fleet-dr-head">
          <div className={`fleet-avatar fleet-avatar-gross ${colorCls}`}>{initial}</div>
          <div className="fleet-dr-title">
            {w.profile}
            <span>läuft seit {fmtSeconds(elapsedSec)} · {w.task_assignee}</span>
          </div>
          {hbAge != null ? (
            <div className="fleet-led" style={{ marginLeft: "auto" }}>
              <span className="fleet-led-dot" />
              ♥ {fmtSeconds(hbAge)}
            </div>
          ) : null}
        </div>

        {/* Task: title + task_id + branch_name (Requirement: title + task_id + branch) */}
        <div className="fleet-dr-task">
          {w.task_title}
          <code>{w.task_id}{branchName ? ` · ${branchName}` : ""}</code>
        </div>

        {/* KV-Grid */}
        <div className="fleet-grid2">
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerModell}</div>
            <div className="fleet-kv-v">{w.effective_model ?? w.model_override ?? "—"}</div>
          </div>
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerHeartbeat}</div>
            <div className="fleet-kv-v">{hbAge != null ? fmtSeconds(hbAge) : "—"}</div>
          </div>
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerTokens}</div>
            <div className="fleet-kv-v">{fmtTokens(w.input_tokens)} → {fmtTokens(w.output_tokens)}</div>
          </div>
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerLaufzeit}</div>
            <div className="fleet-kv-v">{fmtSeconds(elapsedSec)}</div>
          </div>
        </div>

        {/* Verlässlichkeit */}
        {relProfile ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.drawerReliability}</div>
            <div className="fleet-kv-v" style={{ fontFamily: "var(--hc-font-mono)", fontSize: 11.5 }}>
              {relProfile.completed_rate != null
                ? `${Math.round(relProfile.completed_rate * 100)} % abgeschlossen`
                : "—"}
              {relProfile.retries > 0 ? ` · ${relProfile.retries} Retries` : ""}
            </div>
          </div>
        ) : null}

        {/* Ketten-Position */}
        {chainMembers.length > 0 ? (
          <div>
            {chainMembers.slice(0, 4).map((t) => {
              const isActive = t.id === w.task_id;
              const isDone = t.status === "done";
              return (
                <div key={t.id} className="fleet-mini">
                  <span
                    className={`fleet-mini-dot ${isActive ? "fleet-mini-dot-lauf" : isDone ? "fleet-mini-dot-done" : "fleet-mini-dot-offen"}`}
                  />
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {t.title}
                  </span>
                  <code>{isActive ? "läuft" : isDone ? "done" : "wartet"}</code>
                </div>
              );
            })}
          </div>
        ) : null}

        {/* Action-Buttons */}
        <div className="fleet-actions">
          {chainRootId ? (
            <button
              type="button"
              className="fleet-btn fleet-btn-primar"
              onClick={() => onOpenChain(chainRootId)}
            >
              {de.fleet.drawerKetteOeffnen}
            </button>
          ) : null}
          <button
            type="button"
            className="fleet-btn"
            disabled
            title="Log-Drawer kommt im nächsten Subtask"
          >
            {de.fleet.drawerLog}
          </button>
          <button type="button" className="fleet-btn" onClick={onClose}>
            {de.fleet.drawerSchliessen}
          </button>
        </div>
      </div>
    </Overlay>
  );
}

// ─── Ketten-Subtab ────────────────────────────────────────────────────────────

type ChainNode = ChainGraphResponse["nodes"][number];

interface KettenTabProps {
  board: BoardResponse | null;
  initialRootId: string | null;
  now: number;
  /** Callback: öffnet den Karten-Detail-Drawer. chainNodes erlaubt dem Drawer, Kettenkosten zu zeigen. */
  onOpenNodeDetail: (taskId: string, chainNodes?: ChainNode[]) => void;
}

function KettenTab({ board, initialRootId, now, onOpenNodeDetail }: KettenTabProps) {
  // Alle Board-Tasks (flat, alle Spalten)
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);

  // Ketten-Chips aus Board-Tasks ableiten
  const chips = buildChainChips(
    allBoardTasks.map((t) => ({
      id: t.id,
      title: t.title,
      root_id: t.root_id,
      status: t.status,
      completed_at: t.completed_at,
    })),
  );

  // Ausgewählte Kette:
  // - initialRootId (von Worker-Drawer via "Kette öffnen") hat Priorität, aber nur einmalig.
  // - Wenn Board-Daten erst nach Mount laden (cold start: chips zunächst leer), wird
  //   auto-selektiert via useMemo (erste aktive Kette oder erste verfügbare).
  // - userSelectedRootId: User-Auswahl überschreibt Auto-Select. Null = keine Auswahl noch.
  const [userSelectedRootId, setUserSelectedRootId] = useState<string | null>(initialRootId);

  // Derived State: selectedRootId wird auto-berechnet, nicht setState!
  // Falls userSelectedRootId oder initialRootId: use das. Sonst auto: erste aktive oder erste Kette.
  const selectedRootId = useMemo(() => {
    if (userSelectedRootId) return userSelectedRootId;
    return chips.find((c) => c.state === "active")?.rootId ?? chips[0]?.rootId ?? null;
  }, [userSelectedRootId, chips]);

  const handleChipSelect = useCallback((rootId: string) => {
    setUserSelectedRootId(rootId);
  }, []);

  // Chain-Graph für die ausgewählte Kette
  const { data: chainGraph, loading: chainLoading } = useChainGraph(selectedRootId);
  const nodes = chainGraph?.nodes ?? [];

  // Verdicts für Gate-Status
  const verdicts = useHermesReviewVerdicts();

  if (chips.length === 0) {
    return (
      <div className="fleet-empty">
        <p className="fleet-empty-title">{de.fleet.kettenLeer}</p>
        <p className="fleet-empty-sub">{de.fleet.kettenLeerDesc}</p>
      </div>
    );
  }

  return (
    <>
      {/* Ketten-Chips */}
      <div className="fleet-kchips">
        {chips.map((chip) => (
          <button
            key={chip.rootId}
            type="button"
            className={`fleet-kchip${selectedRootId === chip.rootId ? " fleet-kchip-on" : ""}`}
            onClick={() => handleChipSelect(chip.rootId)}
            aria-pressed={selectedRootId === chip.rootId}
          >
            {chip.state === "active" ? (
              <ChainProgressRing progress={chip.progress} total={chip.total} />
            ) : chip.state === "pending" ? (
              <span className="fleet-kchip-pending" aria-hidden="true">⏳</span>
            ) : (
              <span className="fleet-kchip-ok" aria-hidden="true">✓</span>
            )}
            {chip.label.length > 22 ? chip.label.slice(0, 22) + "…" : chip.label}
          </button>
        ))}
      </div>

      {/* Ketten-Inhalt */}
      {selectedRootId && (chainLoading && !chainGraph) ? (
        <div className="fleet-empty">
          <p className="fleet-empty-sub" style={{ fontFamily: "var(--hc-font-mono)", fontSize: 11 }}>Lade Kette …</p>
        </div>
      ) : selectedRootId && nodes.length > 0 ? (
        <KettenGraph
          rootId={selectedRootId}
          nodes={nodes}
          now={now}
          chips={chips}
          verdicts={(verdicts.data?.reviews ?? []).map((v) => ({
            task_id: v.task_id,
            task_status: v.task_status,
            review_run_state: v.review_run_state ?? "pending",
            reviewer_profile: v.reviewer_profile,
          }))}
          onOpenNodeDetail={onOpenNodeDetail}
        />
      ) : selectedRootId ? (
        <div className="fleet-empty">
          <p className="fleet-empty-sub">Keine Ketten-Nodes geladen.</p>
        </div>
      ) : null}
    </>
  );
}

// ─── SVG-Fortschritts-Ring ────────────────────────────────────────────────────

function ChainProgressRing({ progress, total }: { progress: number; total: number }) {
  const r = 6;
  const circ = 2 * Math.PI * r; // ~37.7
  const dash = progress * circ;
  const gap = circ - dash;
  return (
    <svg className="fleet-ring" viewBox="0 0 16 16" aria-label={`${Math.round(progress * 100)}%`}>
      <circle className="fleet-ring-bg" cx="8" cy="8" r={r} />
      {total > 0 ? (
        <circle
          className="fleet-ring-fg"
          cx="8"
          cy="8"
          r={r}
          strokeDasharray={`${dash.toFixed(2)} ${gap.toFixed(2)}`}
        />
      ) : null}
    </svg>
  );
}

// ─── Ketten-Graph (Jetzt-zentriert) ───────────────────────────────────────────

interface KettenGraphProps {
  rootId: string;
  nodes: ChainNode[];
  now: number;
  chips: ChainChipDef[];
  verdicts: Array<{ task_id: string; task_status: string; review_run_state: string; reviewer_profile: string | null }>;
  /** Callback: öffnet den Karten-Detail-Drawer + übergibt die Ketten-Nodes für Kostendarstellung. */
  onOpenNodeDetail: (taskId: string, chainNodes: ChainNode[]) => void;
}

function KettenGraph({ rootId, nodes, now, verdicts, onOpenNodeDetail }: KettenGraphProps) {
  const { pct, done, total } = chainProgress(nodes);
  const focusNode = pickFocusNode(nodes);
  const segments: SegmentKind[] = buildSegments(nodes);
  const totalCost = chainTotalCostUsd(nodes);

  // ETA aus dem Fokus-Node
  const focusLaufzeit = focusNode?.latest_run?.runtime_seconds ?? null;
  const focusHbAge = focusNode?.latest_run?.heartbeat_age_seconds ?? null;

  // Root-Task: ältester erstellter Node (level 0 oder kleinster level)
  const rootNode = [...nodes].sort((a, b) => a.level - b.level)[0] ?? null;
  const rootCreatedAt = rootNode?.created_at ?? 0;
  const rootStartLabel = rootCreatedAt > 0 ? fmtSeconds(Math.max(0, now - rootCreatedAt)) + " her" : "—";

  // Offene Nodes (pending: scheduled/ready/todo/blocked) — NICHT der Fokus-Node
  const openNodes = [...nodes]
    .sort((a, b) => a.level - b.level)
    .filter((n) => n.id !== focusNode?.id && (n.status === "scheduled" || n.status === "ready" || n.status === "todo" || n.status === "blocked"));

  // Fertige Nodes
  const doneNodes = [...nodes]
    .filter((n) => n.status === "done" || n.status === "archived")
    .sort((a, b) => (a.level - b.level));

  // Gate-Node (Review-Status)
  const gateVerdicts = verdicts.filter((v) => v.task_id === rootId || nodes.some((n) => n.id === v.task_id));
  const gateLabel = gateVerdicts.length > 0
    ? `${gateVerdicts[0].reviewer_profile ?? "reviewer"}`
    : "reviewer";
  const gateMeta = gateVerdicts.length > 0
    ? gateVerdicts[0].review_run_state
    : `wartet auf Karte ${total}`;

  const [fertigOpen, setFertigOpen] = useState(false);

  return (
    <>
      {/* Fortschritts-Kopf */}
      <div className="fleet-prog">
        <div className="fleet-prog-top">
          <span className="fleet-prog-pz">{pct} %</span>
          <span className="fleet-prog-pl">
            {focusNode?.status === "running"
              ? de.fleet.kettenKarteLaeuft(done + 1, total)
              : de.fleet.kettenKarteWartet(done + 1, total)}
          </span>
          <span className="fleet-prog-eta">
            {focusLaufzeit != null ? `ETA ~${fmtSeconds(focusLaufzeit)}` : "—"}
            {totalCost != null ? ` · ${fmtUsd(totalCost)}` : ""}
          </span>
        </div>

        {/* Segment-Leiste */}
        <div className="fleet-segs">
          {segments.map((kind, i) => (
            <div
              key={i}
              className={`fleet-seg${kind === "done" ? " fleet-seg-done" : kind === "active" ? " fleet-seg-active" : ""}`}
            />
          ))}
        </div>
        <div className="fleet-seg-l">
          <span>Root {rootStartLabel}</span>
          <span>{gateLabel} · Gate</span>
        </div>
      </div>

      {/* Fokus-Karte */}
      {focusNode ? (
        <button
          type="button"
          className="fleet-fokus text-left"
          onClick={() => onOpenNodeDetail(focusNode.id, nodes)}
          aria-label={`Node ${focusNode.title} öffnen`}
        >
          <div className="fleet-wk-top">
            <div className={`fleet-avatar fleet-avatar-gross ${profileColorClass(focusNode.assignee ?? "")}`}>
              {profileInitial(focusNode.assignee ?? "?")}
            </div>
            <div className="fleet-wk-name">
              {focusNode.assignee ?? "—"}
              <span>{focusNode.id.slice(0, 10)}</span>
            </div>
            {focusNode.status === "running" && focusHbAge != null ? (
              <div className="fleet-led">
                <span className="fleet-led-dot" />
                ♥ {fmtSeconds(focusHbAge)}
              </div>
            ) : (
              <div className="fleet-led" style={{ color: "var(--fleet-t3)", boxShadow: "none" }}>
                {focusNode.status}
              </div>
            )}
          </div>

          <div className="fleet-wk-task" style={{ WebkitLineClamp: 3, fontSize: 13 }}>
            {focusNode.title}
          </div>

          {focusNode.latest_run?.profile ? (
            <div className="fleet-wk-note">
              {focusNode.latest_run.profile}
              {focusNode.latest_run.heartbeat_age_seconds != null
                ? ` · ♥ ${fmtSeconds(focusNode.latest_run.heartbeat_age_seconds)}`
                : ""}
            </div>
          ) : null}

          <div className="fleet-rail">
            <div
              className="fleet-rail-fill"
              style={{
                width: focusNode.progress && focusNode.progress.total > 0
                  ? `${Math.round((focusNode.progress.done / focusNode.progress.total) * 100)}%`
                  : focusNode.status === "running" ? "58%" : "0%",
              }}
            />
          </div>

          <div className="fleet-wk-meta">
            {focusNode.latest_run?.profile ? <b>{focusNode.latest_run.profile.replace(/^claude-/, "").split("-")[0] ?? focusNode.latest_run.profile}</b> : null}
            {(focusNode.input_tokens > 0 || focusNode.output_tokens > 0) ? (
              <span>{fmtTokens(focusNode.input_tokens)} → {fmtTokens(focusNode.output_tokens)} tok</span>
            ) : null}
            <span className="fleet-meta-right">
              {focusNode.status === "running" ? "Karte läuft" : `Karte: ${focusNode.status}`}
              {focusNode.latest_run?.runtime_seconds != null
                ? ` seit ${fmtSeconds(focusNode.latest_run.runtime_seconds)}`
                : ""}
            </span>
          </div>
        </button>
      ) : null}

      {/* Danach-Queue */}
      {openNodes.length > 0 ? (
        <div className="fleet-danach">
          {openNodes.slice(0, 3).map((n, i) => (
            <button
              key={n.id}
              type="button"
              className="fleet-q text-left"
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              aria-label={`Node ${n.title} öffnen`}
            >
              <span className="fleet-q-idx">{done + 1 + i + 1}</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {n.title}
              </span>
              <span className="fleet-q-assignee">{n.assignee ?? "—"}</span>
            </button>
          ))}
        </div>
      ) : null}

      {/* Fertig-Gruppe (kollabiert) */}
      {doneNodes.length > 0 ? (
        <div className="fleet-fertig-grp">
          <button
            type="button"
            className="fleet-f-row"
            style={{ fontWeight: 600, color: "var(--fleet-t3)", opacity: 1, fontSize: 10.5 }}
            onClick={() => setFertigOpen((v) => !v)}
            aria-expanded={fertigOpen}
          >
            <span className="fleet-f-ok" aria-hidden="true">✓</span>
            <span style={{ flex: 1 }}>{de.fleet.kettenFertigGruppe} ({doneNodes.length})</span>
            <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10 }}>
              {fertigOpen ? "▲" : "▼"}
            </span>
          </button>
          {fertigOpen ? doneNodes.map((n) => (
            <button
              key={n.id}
              type="button"
              className="fleet-f-row"
              onClick={() => onOpenNodeDetail(n.id, nodes)}
              aria-label={`Node ${n.title} öffnen`}
            >
              <span className="fleet-f-ok" aria-hidden="true">✓</span>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {n.title}
              </span>
              <span className="fleet-f-meta">
                {n.cost_usd > 0 ? fmtUsd(n.cost_usd) : null}
                {n.latest_run?.runtime_seconds != null
                  ? ` · ${fmtSeconds(n.latest_run.runtime_seconds)}`
                  : ""}
              </span>
            </button>
          )) : null}
        </div>
      ) : null}

      {/* Gate-Ziellinie */}
      <div className="fleet-ziel">
        <span className="fleet-ziel-raute" aria-hidden="true" />
        Release-Gate · {gateLabel}
        <span className="fleet-ziel-meta">{gateMeta}</span>
      </div>
    </>
  );
}

// ─── Karten-Detail-Drawer ─────────────────────────────────────────────────────

type DetailTab = "uebersicht" | "aktivitaet" | "log" | "ergebnis";

interface NodeDetailDrawerProps {
  taskId: string;
  /** Nodes der aktuellen Kette — zur Berechnung der Ketten-Gesamtkosten im Ergebnis-Tab. */
  chainNodes: ChainNode[];
  now: number;
  onClose: () => void;
}

function NodeDetailDrawer({ taskId, chainNodes, now, onClose }: NodeDetailDrawerProps) {
  const [tab, setTab] = useState<DetailTab>("uebersicht");
  const [copied, setCopied] = useState(false);

  // On-Demand-Daten (nur bei offenem Drawer)
  const taskBody = useTaskBodyOnDemand(taskId);
  const deliverablesResult = useTaskDeliverablesOnDemand(taskId);
  const activity = useWorkerActivity(taskId);
  const verdicts = useHermesReviewVerdicts();

  const task = taskBody.data?.task ?? null;
  const runs = taskBody.data?.runs ?? [];
  // Deliverables: eigener Endpoint /tasks/{id}/deliverables — degradiert sauber zu []
  const deliverables = deliverablesResult.data?.deliverables ?? [];
  const events = activity.data?.events ?? [];

  // Review-Verdict für diesen Task
  const taskVerdicts = (verdicts.data?.reviews ?? []).filter((r) => r.task_id === taskId).map((v) => ({
    task_id: v.task_id,
    reviewer_profile: v.reviewer_profile,
    review_run_state: v.review_run_state ?? "pending",
    verifier_verdict: v.verifier_verdict ?? null,
  }));

  const latestRun = runs[0] ?? null;
  const elapsedSec = latestRun?.runtime_seconds ?? (latestRun?.started_at ? Math.max(0, now - latestRun.started_at) : null);

  function handleCopy() {
    void navigator.clipboard.writeText(taskId).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }

  const TABS: Array<{ id: DetailTab; label: string }> = [
    { id: "uebersicht", label: de.fleet.detailTabUebersicht },
    { id: "aktivitaet", label: de.fleet.detailTabAktivitaet },
    { id: "log", label: de.fleet.detailTabLog },
    { id: "ergebnis", label: de.fleet.detailTabErgebnis },
  ];

  return (
    <Overlay onClose={onClose} ariaLabel={`Task ${taskId} Details`} maxWidthClassName="max-w-lg">
      <div data-fleet-theme className="fleet-drawer-inner">
        {/* Grab */}
        <div className="fleet-grab" />

        {/* Kopf */}
        <div className="fleet-dr-head">
          <div className={`fleet-avatar fleet-avatar-gross ${profileColorClass(task?.assignee ?? "")}`}>
            {profileInitial(task?.assignee ?? "?")}
          </div>
          <div className="fleet-dr-title">
            <span style={{ fontSize: 14 }}>{task?.title || taskId}</span>
            <span>
              <button
                type="button"
                className="fleet-copy-id"
                onClick={handleCopy}
                title={de.fleet.detailKopieren}
                aria-label={de.fleet.detailKopieren}
              >
                {taskId}
                <span style={{ fontSize: 9, opacity: 0.7 }}>{copied ? " ✓" : " ⊕"}</span>
              </button>
            </span>
          </div>
          <button
            type="button"
            className="fleet-btn"
            style={{ flex: "0 0 auto", padding: "6px 10px", minHeight: "auto" }}
            onClick={onClose}
            aria-label={de.fleet.detailSchliessen}
          >
            ✕
          </button>
        </div>

        {/* Tab-Leiste */}
        <div className="fleet-detail-tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              className={`fleet-detail-tab${tab === t.id ? " fleet-detail-tab-on" : ""}`}
              onClick={() => setTab(t.id)}
              aria-pressed={tab === t.id}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Tab-Inhalte */}
        <div className="fleet-detail-body">
          {tab === "uebersicht" && (
            <UebersichtTab
              task={task}
              latestRun={latestRun}
              elapsedSec={elapsedSec}
              deliverables={deliverables}
            />
          )}
          {tab === "aktivitaet" && (
            <AktivitaetTab events={events} now={now} loading={activity.loading && !activity.data} />
          )}
          {tab === "log" && (
            <LogTab taskId={taskId} />
          )}
          {tab === "ergebnis" && (
            <ErgebnisTab
              verdicts={taskVerdicts}
              deliverables={deliverables}
              chainCost={chainTotalCostUsd(chainNodes)}
            />
          )}
        </div>
      </div>
    </Overlay>
  );
}

// ─── Detail-Drawer Tabs ───────────────────────────────────────────────────────

interface UebersichtTabProps {
  task: {
    id?: string;
    title?: string;
    body?: string | null;
    status?: string;
    assignee?: string | null;
    block_reason?: string | null;
    review_tier?: string | null;
    branch_name?: string | null;
    model_override?: string | null;
    workspace_kind?: string | null;
    workspace_path?: string | null;
    acceptance_criteria?: unknown;
  } | null;
  latestRun: {
    profile?: string | null;
    status?: string;
    started_at?: number | null;
    ended_at?: number | null;
    runtime_seconds?: number | null;
    input_tokens?: number | null;
    output_tokens?: number | null;
    cost_usd?: number | null;
  } | null;
  elapsedSec: number | null;
  deliverables: Array<{ filename: string; url: string; size: number }>;
}

function UebersichtTab({ task, latestRun, elapsedSec, deliverables }: UebersichtTabProps) {
  if (!task) {
    return (
      <div className="fleet-empty" style={{ padding: "16px 4px" }}>
        <p className="fleet-empty-sub">Lade Details …</p>
      </div>
    );
  }

  // Acceptance-Criteria normalisieren
  const ac = task.acceptance_criteria;
  let acList: string[] = [];
  if (Array.isArray(ac)) {
    acList = ac.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object" && "statement" in item) return String((item as { statement: unknown }).statement);
      return String(item);
    });
  } else if (typeof ac === "string" && ac.trim()) {
    acList = [ac];
  }

  return (
    <>
      {/* Status-Badge */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{
          fontFamily: "var(--hc-font-mono)", fontSize: 10,
          padding: "3px 8px", borderRadius: 999,
          border: `1px solid ${task.status === "running" ? "rgba(55,224,255,.4)" : task.status === "done" ? "rgba(67,214,154,.35)" : "var(--fleet-linie)"}`,
          color: task.status === "running" ? "var(--fleet-puls)" : task.status === "done" ? "var(--fleet-gruen)" : "var(--fleet-t2)",
        }}>
          {task.status ?? "—"}
        </span>
        {task.review_tier ? (
          <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, padding: "3px 8px", borderRadius: 999, border: "1px solid var(--fleet-linie)", color: "var(--fleet-t3)" }}>
            {task.review_tier}
          </span>
        ) : null}
      </div>

      {/* Block-Reason */}
      {task.block_reason ? (
        <div style={{ background: "rgba(245,168,60,.08)", border: "1px solid rgba(245,168,60,.3)", borderRadius: 10, padding: "8px 10px", fontSize: 11.5, color: "var(--fleet-signal)", lineHeight: 1.5 }}>
          {de.fleet.detailLabelBlockReason}: {task.block_reason}
        </div>
      ) : null}

      {/* KV-Grid */}
      <div className="fleet-grid2">
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelAssignee}</div>
          <div className="fleet-kv-v">{task.assignee ?? "—"}</div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelModell}</div>
          <div className="fleet-kv-v" style={{ fontSize: 11 }}>{latestRun?.profile ?? task.model_override ?? "—"}</div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelBranch}</div>
          <div className="fleet-kv-v" style={{ fontSize: 11, overflow: "hidden", textOverflow: "ellipsis" }}>
            {task.branch_name ?? task.workspace_path ?? (task.workspace_kind ? `(${task.workspace_kind})` : "—")}
          </div>
        </div>
        <div className="fleet-kv">
          <div className="fleet-kv-k">{de.fleet.detailLabelLaufzeit}</div>
          <div className="fleet-kv-v">{elapsedSec != null ? fmtSeconds(elapsedSec) : "—"}</div>
        </div>
        {(latestRun?.input_tokens != null || latestRun?.output_tokens != null) ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.detailLabelTokens}</div>
            <div className="fleet-kv-v" style={{ fontSize: 11 }}>
              {fmtTokens(latestRun?.input_tokens)} → {fmtTokens(latestRun?.output_tokens)}
            </div>
          </div>
        ) : null}
        {latestRun?.cost_usd != null ? (
          <div className="fleet-kv">
            <div className="fleet-kv-k">{de.fleet.detailLabelKosten}</div>
            <div className="fleet-kv-v">{fmtUsd(latestRun.cost_usd)}</div>
          </div>
        ) : null}
      </div>

      {/* Task-Body */}
      {task.body ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailBodyLabel}
          </div>
          <div style={{ font: "400 12px/1.6 var(--hc-font-sans)", color: "var(--fleet-t2)", borderLeft: "2px solid var(--fleet-puls)", paddingLeft: 10, maxHeight: 120, overflow: "hidden", maskImage: "linear-gradient(to bottom, black 70%, transparent)" }}>
            {task.body.slice(0, 400)}{task.body.length > 400 ? "…" : ""}
          </div>
        </div>
      ) : null}

      {/* Acceptance-Criteria */}
      {acList.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailAcceptanceLabel}
          </div>
          <ul style={{ paddingLeft: 16, margin: 0, display: "flex", flexDirection: "column", gap: 4 }}>
            {acList.slice(0, 8).map((item, i) => (
              <li key={i} style={{ font: "400 11.5px/1.45 var(--hc-font-sans)", color: "var(--fleet-t2)" }}>
                {item}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {/* Deliverables-Liste — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailDeliverables}
          </div>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-puls)", background: "none", border: "none", cursor: "pointer", width: "100%", textAlign: "left", borderBottom: "1px solid var(--fleet-linie)" }}
            >
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {d.filename}
              </span>
              <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
                {d.size > 0 ? `${Math.round(d.size / 1024)} kB` : "↗"}
              </span>
            </button>
          ))}
        </div>
      ) : null}
    </>
  );
}

function AktivitaetTab({
  events,
  now,
  loading,
}: {
  events: Array<{ id: number; kind: string; note: string | null; at: number }>;
  now: number;
  loading: boolean;
}) {
  if (loading) {
    return (
      <div className="fleet-empty" style={{ padding: "12px 4px" }}>
        <p className="fleet-empty-sub">Lade Aktivität …</p>
      </div>
    );
  }
  if (events.length === 0) {
    return (
      <p style={{ font: "400 11.5px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)", padding: "8px 2px" }}>
        {de.fleet.detailActivityEmpty}
      </p>
    );
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {events.slice(0, 20).map((ev) => {
        const age = ev.at > 0 ? Math.max(0, now - ev.at) : null;
        return (
          <div key={ev.id} className="fleet-activity-row">
            <span className="fleet-activity-time">{age != null ? fmtSeconds(age) : "—"}</span>
            <span className="fleet-activity-kind">{ev.kind}</span>
            {ev.note ? (
              <span className="fleet-activity-note">{ev.note}</span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function LogTab({ taskId }: { taskId: string }) {
  // WorkerLogTail gibt es schon im WorkerCard — wir renutzen es.
  return <WorkerLogTail taskId={taskId} />;
}

function ErgebnisTab({
  verdicts,
  deliverables,
  chainCost,
}: {
  verdicts: Array<{ task_id: string; reviewer_profile: string | null; review_run_state: string; verifier_verdict: string | null }>;
  deliverables: Array<{ filename: string; url: string; size: number }>;
  chainCost: number | null;
}) {
  if (verdicts.length === 0 && deliverables.length === 0 && chainCost == null) {
    return (
      <p style={{ font: "400 11.5px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)", padding: "8px 2px" }}>
        {de.fleet.detailErgebnisEmpty}
      </p>
    );
  }

  return (
    <>
      {/* Review-Verdicts */}
      {verdicts.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            Review-Verdicts
          </div>
          {verdicts.map((v) => (
            <div key={`${v.task_id}-${v.reviewer_profile ?? "reviewer"}`} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: "1px solid var(--fleet-linie)", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-t2)" }}>
              <span style={{ flex: 1 }}>{v.reviewer_profile ?? "reviewer"}</span>
              <span style={{
                fontFamily: "var(--hc-font-mono)", fontSize: 10,
                padding: "2px 7px", borderRadius: 999,
                border: `1px solid ${v.verifier_verdict === "APPROVED" ? "rgba(67,214,154,.35)" : "rgba(245,168,60,.4)"}`,
                color: v.verifier_verdict === "APPROVED" ? "var(--fleet-gruen)" : "var(--fleet-signal)",
              }}>
                {v.verifier_verdict ?? v.review_run_state ?? "—"}
              </span>
            </div>
          ))}
        </div>
      ) : null}

      {/* Deliverables — Auth-geschützte Endpoints: openAuthedApiFile statt raw href */}
      {deliverables.length > 0 ? (
        <div>
          <div style={{ font: "500 9.5px/1 var(--hc-font-sans)", color: "var(--fleet-t3)", letterSpacing: "0.08em", textTransform: "uppercase", marginBottom: 6 }}>
            {de.fleet.detailDeliverables}
          </div>
          {deliverables.map((d) => (
            <button
              key={d.url || d.filename}
              type="button"
              onClick={() => void openAuthedApiFile(d.url, d.filename)}
              style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", font: "400 11.5px/1 var(--hc-font-sans)", color: "var(--fleet-puls)", background: "none", border: "none", cursor: "pointer", width: "100%", textAlign: "left", borderBottom: "1px solid var(--fleet-linie)" }}
            >
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {d.filename}
              </span>
              <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
                {d.size > 0 ? `${Math.round(d.size / 1024)} kB` : "↗"}
              </span>
            </button>
          ))}
        </div>
      ) : null}

      {/* Kosten-Beitrag */}
      {chainCost != null ? (
        <div className="fleet-kv" style={{ marginTop: 4 }}>
          <div className="fleet-kv-k">Kosten-Beitrag zur Kette</div>
          <div className="fleet-kv-v">{fmtUsd(chainCost)}</div>
        </div>
      ) : null}
    </>
  );
}

// ─── Plan-Cockpit (Freigabe) ──────────────────────────────────────────────────

interface PlanTabProps {
  allPlanspecs: PlanSpecRecord[];
  costs: RunsCostsResponse | null;
  lanesCatalog: LanesCatalogResponse | null;
  accountUsage: import("../lib/types").AccountUsageResponse | null;
  onApproveSuccess: () => void;
}

function PlanTab({ allPlanspecs, costs, lanesCatalog, accountUsage, onApproveSuccess }: PlanTabProps) {
  // Nur PlanSpecs die auf Operator-Freigabe warten
  const pendingSpecs = allPlanspecs.filter((ps) => planSpecWaitsForOperator(ps.freigabe, ps.kanban_state));
  const pendingPaths = pendingSpecs.map((ps) => ps.path);

  // selectedPath hält nur die aktive User-Wahl; effectivePath wird ABGELEITET:
  // fällt der gespeicherte Pfad nach Approve/Reload aus pendingPaths heraus,
  // springt die Auswahl automatisch auf den nächsten wartenden Eintrag.
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const effectivePath = deriveEffectivePlanPath(selectedPath, pendingPaths);
  const selectedSpec = pendingSpecs.find((ps) => ps.path === effectivePath) ?? null;

  if (pendingSpecs.length === 0) {
    return (
      <div className="fleet-empty">
        <p className="fleet-empty-title">{de.fleet.planLeer}</p>
        <p className="fleet-empty-sub">{de.fleet.planLeerDesc}</p>
      </div>
    );
  }

  return (
    <>
      {/* Liste wartender PlanSpecs — wenn mehr als eine, als auswählbare Chips */}
      {pendingSpecs.length > 1 ? (
        <div className="fleet-kchips" style={{ marginBottom: 4 }}>
          {pendingSpecs.map((ps) => (
            <button
              key={ps.path}
              type="button"
              className={`fleet-kchip${effectivePath === ps.path ? " fleet-kchip-on" : ""}`}
              onClick={() => setSelectedPath(ps.path)}
              aria-pressed={effectivePath === ps.path}
            >
              {(ps.topic || ps.filename).length > 22
                ? (ps.topic || ps.filename).slice(0, 22) + "…"
                : (ps.topic || ps.filename)}
            </button>
          ))}
        </div>
      ) : null}

      {selectedSpec ? (
        <PlanSpecCockpit
          // key remountet das Cockpit pro Spec — sonst überlebt lokaler State
          // (approveState='success', injectScout, Lane-Wahl) den Sprung auf den
          // nächsten wartenden Spec und sperrt dessen Freigabe-Button.
          key={selectedSpec.path}
          ps={selectedSpec}
          costs={costs}
          lanesCatalog={lanesCatalog}
          accountUsage={accountUsage}
          onApproveSuccess={() => {
            // Nach Approve: gespeicherten Pfad zurücksetzen → Ableitung
            // springt automatisch auf den nächsten wartenden Eintrag.
            setSelectedPath(null);
            onApproveSuccess();
          }}
          onHold={() => setSelectedPath(null)}
        />
      ) : null}
    </>
  );
}

// ─── PlanSpec-Cockpit (eine PlanSpec freigeben) ────────────────────────────────

interface PlanSpecCockpitProps {
  ps: PlanSpecRecord;
  costs: RunsCostsResponse | null;
  lanesCatalog: LanesCatalogResponse | null;
  accountUsage: import("../lib/types").AccountUsageResponse | null;
  onApproveSuccess: () => void;
  onHold: () => void;
}

function PlanSpecCockpit({ ps, costs, lanesCatalog, accountUsage, onApproveSuccess, onHold }: PlanSpecCockpitProps) {
  // PlanSpec-Detail (subtasks mit lane) laden
  const detail = usePlanSpecDetail(ps.path);

  // Lane-Konfiguration ableiten
  const lanes = useMemo(() => {
    if (detail.data?.subtasks) {
      return derivePlanLanes(detail.data.subtasks);
    }
    return [];
  }, [detail.data]);

  // Preset-Defaults je Lane aus lanesCatalog
  const presetDefaults = useMemo<Record<string, string>>(() => {
    const profiles = lanesCatalog?.profiles ?? [];
    const result: Record<string, string> = {};
    for (const p of profiles) {
      if (p.name && p.default_model) {
        result[p.name] = p.default_model;
      }
    }
    return result;
  }, [lanesCatalog]);

  // Modell-Optionen je Lane
  const modelOptions = lanesCatalog?.models ?? [];

  // Lokaler Zustand: Modell-Auswahl je Lane (initial = Preset-Default)
  // Reset wenn sich lanes ändern (neue PlanSpec ausgewählt)
  const [laneModels, setLaneModels] = useState<Record<string, string>>(() => {
    return {};
  });

  // Scout-Toggle (Default: aus)
  const [injectScout, setInjectScout] = useState(false);

  // Freigabe-State
  const [approveState, setApproveState] = useState<"idle" | "busy" | "success" | "error">("idle");
  const [approveError, setApproveError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  async function handleApprove() {
    if (!ps.kanban_root_task_id) return;
    setApproveState("busy");
    setApproveError(null);
    const body = buildApproveRequest(
      ps.kanban_root_task_id,
      // Merge lokale Auswahl über Presets
      { ...presetDefaults, ...laneModels },
      presetDefaults,
      injectScout,
    );
    try {
      await fetchJSON<unknown>("/api/plugins/kanban/planspecs/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!aliveRef.current) return;
      setApproveState("success");
      // Kurze Pause, dann Callback
      window.setTimeout(() => {
        if (aliveRef.current) onApproveSuccess();
      }, 600);
    } catch (e: unknown) {
      if (!aliveRef.current) return;
      setApproveState("error");
      const msg = e instanceof Error ? e.message : String(e);
      if (msg.includes("409")) {
        setApproveError(de.fleet.planFreigebenFehler409);
      } else if (msg.includes("404")) {
        setApproveError(de.fleet.planFreigebenFehler404);
      } else {
        setApproveError(de.fleet.planFreigebenFehlerUnbekannt);
      }
    }
  }

  // Worktree-Isolation: nur anzeigen wenn Feld existiert
  const hasWorktreeField = detail.data != null && "worktree_isolation" in (detail.data as object);

  return (
    <>
      {/* Kopfkarte (amber) */}
      <div className="fleet-plan-kopf">
        <div className="fleet-plan-kopf-n">
          {ps.topic || ps.filename}
          <span className={`fleet-ps-badge fleet-ps-badge-amber`} style={{ marginLeft: "auto" }}>
            freigabe: operator
          </span>
        </div>
        {detail.data?.goal ? (
          <div className="fleet-plan-kopf-sub">{detail.data.goal}</div>
        ) : null}
        <div className="fleet-plan-kopf-meta">
          {ps.kanban_child_total > 0 ? (
            <span>{ps.kanban_child_total} Karten geplant</span>
          ) : ps.subtask_count > 0 ? (
            <span>{ps.subtask_count} Karten geplant</span>
          ) : null}
          {ps.binding ? <span>binding</span> : null}
          {ps.freigabe ? <span>freigabe: {ps.freigabe}</span> : null}
        </div>
      </div>

      {/* Lane-Konfiguration */}
      {lanes.length > 0 ? (
        <div className="fleet-lane-cfg">
          {lanes.map(({ lane, description }) => {
            const currentModel = laneModels[lane] ?? presetDefaults[lane] ?? "";
            const isChanged = laneModels[lane] != null && laneModels[lane] !== presetDefaults[lane];
            return (
              <div key={lane} className="fleet-lane-row">
                <span className="fleet-lane-ln">{lane}</span>
                <span className="fleet-lane-ld">{description.length > 30 ? description.slice(0, 30) + "…" : description}</span>
                <ModelSelect
                  lane={lane}
                  value={currentModel}
                  options={modelOptions}
                  changed={isChanged}
                  onChange={(model) => setLaneModels((prev) => ({ ...prev, [lane]: model }))}
                />
              </div>
            );
          })}
        </div>
      ) : null}

      {/* Toggles */}
      <div className="fleet-lane-cfg">
        {/* Scout vorab */}
        <div className="fleet-tgl-row">
          <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planScoutVorab}</span>
          <span className="fleet-tgl-td">{de.fleet.planScoutDesc}</span>
          <button
            type="button"
            role="switch"
            aria-checked={injectScout}
            className={`fleet-switch${injectScout ? "" : " fleet-switch-aus"}`}
            style={{ minWidth: 40, minHeight: 40, display: "flex", alignItems: "center", justifyContent: "center" }}
            onClick={() => setInjectScout((v) => !v)}
            aria-label={de.fleet.planScoutVorab}
          />
        </div>

        {/* Live-Test: read-only Pill */}
        <div className="fleet-tgl-row">
          <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planLiveTest}</span>
          <span className="fleet-tgl-td">{de.fleet.planLiveTestDesc}</span>
          {ps.live_test_depth ? (
            <span className="fleet-sel" style={{ pointerEvents: "none", opacity: 0.85 }}>
              {ps.live_test_depth}
            </span>
          ) : (
            <span style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>—</span>
          )}
        </div>

        {/* Worktree-Isolation: nur wenn Feld existiert */}
        {hasWorktreeField ? (
          <div className="fleet-tgl-row" style={{ borderBottom: "none" }}>
            <span style={{ fontWeight: 600, fontSize: 12 }}>{de.fleet.planWorktreeIsoliert}</span>
            <span className="fleet-tgl-td">{de.fleet.planWorktreeDesc}</span>
            <span className="fleet-sel" style={{ pointerEvents: "none", opacity: 0.7 }}>
              {String((detail.data as Record<string, unknown>)["worktree_isolation"] ?? "—")}
            </span>
          </div>
        ) : null}
      </div>

      {/* Token-Budget-Block */}
      <TokenBudgetBlock accountUsage={accountUsage} costs={costs} />

      {/* Fehler-Anzeige */}
      {approveError ? (
        <div style={{
          background: "rgba(255,93,115,.1)",
          border: "1px solid rgba(255,93,115,.35)",
          borderRadius: 11,
          padding: "9px 12px",
          font: "400 11.5px/1.5 var(--hc-font-sans)",
          color: "#ff7d90",
        }}>
          {approveError}
        </div>
      ) : null}

      {/* Erfolgs-Anzeige */}
      {approveState === "success" ? (
        <div style={{
          background: "rgba(67,214,154,.1)",
          border: "1px solid rgba(67,214,154,.35)",
          borderRadius: 11,
          padding: "9px 12px",
          font: "400 11.5px/1.5 var(--hc-font-sans)",
          color: "var(--fleet-gruen)",
        }}>
          {de.fleet.planFreigebenErfolg}
        </div>
      ) : null}

      {/* Aktions-Buttons */}
      <div className="fleet-actions">
        <button
          type="button"
          className="fleet-btn fleet-btn-frei"
          style={{ flex: 2 }}
          onClick={() => void handleApprove()}
          disabled={approveState === "busy" || approveState === "success" || !ps.kanban_root_task_id}
          aria-busy={approveState === "busy"}
        >
          {approveState === "busy" ? "Freigabe läuft …" : de.fleet.planFreigeben}
        </button>
        <button
          type="button"
          className="fleet-btn"
          onClick={onHold}
          disabled={approveState === "busy"}
        >
          {de.fleet.planHalten}
        </button>
      </div>
    </>
  );
}

// ─── Modell-Select (je Lane) ──────────────────────────────────────────────────

interface ModelSelectProps {
  lane: string;
  value: string;
  options: LanesCatalogResponse["models"];
  changed: boolean;
  onChange: (model: string) => void;
}

function ModelSelect({ value, options, changed, onChange }: ModelSelectProps) {
  // Fallback: wenn keine Optionen, zeige freies Textfeld-ähnliches Display
  if (!options || options.length === 0) {
    return (
      <span className="fleet-sel" style={{ opacity: 0.6 }}>{value || "—"}</span>
    );
  }

  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={`fleet-sel${changed ? " fleet-sel-puls" : ""}`}
      style={{
        background: "var(--fleet-karte)",
        border: `1px solid ${changed ? "rgba(55,224,255,.4)" : "var(--fleet-linie-stark)"}`,
        color: changed ? "var(--fleet-puls)" : "var(--fleet-t1)",
        borderRadius: 9,
        padding: "6px 9px",
        font: "500 11px var(--hc-font-mono)",
        cursor: "pointer",
        minHeight: 40,
        minWidth: 90,
      }}
      aria-label={`Modell für Lane ${value}`}
    >
      {value && !options.find((o) => o.id === value) ? (
        <option value={value}>{value}</option>
      ) : null}
      {options.map((o) => (
        <option key={o.id} value={o.id}>{o.label || o.id}</option>
      ))}
    </select>
  );
}

// ─── Token-Budget-Block ───────────────────────────────────────────────────────

function TokenBudgetBlock({
  accountUsage,
  costs,
}: {
  accountUsage: import("../lib/types").AccountUsageResponse | null;
  costs: RunsCostsResponse | null;
}) {
  const providers = accountUsage?.providers ?? [];

  // Alle Fenster aus allen Providern mit used_percent
  const allWindows = providers.flatMap((prov) =>
    prov.windows
      .filter((w) => w.used_percent != null)
      .map((w) => ({ ...w, providerTitle: prov.title || prov.provider }))
  );

  return (
    <div className="fleet-budget-g">
      <div className="fleet-bg-head">
        <span className="fleet-bg-t">{de.fleet.planTokenBudget}</span>
        {allWindows.length > 0 ? (
          <code style={{ fontFamily: "var(--hc-font-mono)", fontSize: 10, color: "var(--fleet-t3)" }}>
            {allWindows.slice(0, 2).map((w, i) => (
              <span key={i}>
                {i > 0 ? " · " : ""}
                {de.fleet.planTokenReset(fmtResetAt(w.reset_at))}
              </span>
            ))}
          </code>
        ) : null}
      </div>

      {allWindows.length === 0 ? (
        <p style={{ font: "400 11px/1.4 var(--hc-font-sans)", color: "var(--fleet-t3)" }}>
          {de.fleet.planBudgetNichtVerfuegbar}
        </p>
      ) : (
        allWindows.map((w, i) => {
          const pct = w.used_percent ?? 0;
          const tone = budgetTone(w.used_percent);
          const barColor = tone === "danger"
            ? "linear-gradient(90deg,rgba(255,93,115,.5),#ff5d73)"
            : tone === "warn"
            ? "linear-gradient(90deg,rgba(245,168,60,.4),var(--fleet-signal))"
            : "linear-gradient(90deg,rgba(67,214,154,.5),var(--fleet-gruen))";

          return (
            <div key={i} className="fleet-bg-row">
              <span className="fleet-bg-bl">{w.label || w.providerTitle}</span>
              <div className="fleet-bg-bar">
                <i style={{ width: `${Math.min(100, pct)}%`, background: barColor }} />
              </div>
              <span className="fleet-bg-bv" style={{
                color: tone === "danger" ? "#ff7d90" : tone === "warn" ? "var(--fleet-signal)" : "var(--fleet-t1)",
              }}>
                {Math.round(pct)} %
              </span>
            </div>
          );
        })
      )}

      {/* Kosten heute + Woche */}
      <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
        {costs?.today?.actual_cost_usd != null ? (
          <div className="fleet-kv" style={{ flex: 1 }}>
            <div className="fleet-kv-k">{de.fleet.planKostenHeute}</div>
            <div className="fleet-kv-v">{fmtUsd(costs.today.actual_cost_usd)}</div>
          </div>
        ) : null}
        {costs?.window?.actual_cost_usd != null ? (
          <div className="fleet-kv" style={{ flex: 1 }}>
            <div className="fleet-kv-k">{de.fleet.planKostenWoche}</div>
            <div className="fleet-kv-v">{fmtUsd(costs.window.actual_cost_usd)}</div>
          </div>
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

// ─── Risiko-Subtab ────────────────────────────────────────────────────────────

interface RisikoTabProps {
  allPlanspecs: PlanSpecRecord[];
  blockedTasks: Array<{ id: string; title: string; status: string; block_reason?: string | null }>;
  reliability: ReliabilityResponse | null;
  systemHealth: SystemHealthResponse | null;
  pressureStatus: PressureStatusResponse | null;
  onNavigateToPlan: () => void;
}

function RisikoTab({
  allPlanspecs,
  blockedTasks,
  reliability,
  systemHealth,
  pressureStatus,
  onNavigateToPlan,
}: RisikoTabProps) {
  // (a) Wartende Freigaben
  const pendingApprovals = allPlanspecs.filter((ps) => planSpecWaitsForOperator(ps.freigabe, ps.kanban_state));

  // (a) Blockierte Tasks — Operator-Halts vs. sonstige blockierte
  const operatorHalts = blockedTasks.filter((t) => {
    const r = (t.block_reason ?? "").toLowerCase();
    return r.includes("operator");
  });
  const otherBlocked = blockedTasks.filter((t) => {
    const r = (t.block_reason ?? "").toLowerCase();
    return !r.includes("operator");
  });

  // Gesamte blockierte Tasks für den Leer-Zustand
  const totalBlockedCount = blockedTasks.length;
  const totalBoardTasks = 0; // Wir haben keinen Gesamtcount leicht verfügbar, daher weglassen

  // (b) Zuverlässigkeit je Lane
  const profiles = reliability?.profiles ?? [];

  // (c) System-Puls
  const gateway = systemHealth?.subsystems?.gateway;
  const dispatcher = systemHealth?.subsystems?.kanban_dispatcher;
  const host = pressureStatus?.host;
  const tokenPressure = pressureStatus?.token_pressure;

  const hasAnything = pendingApprovals.length > 0 || operatorHalts.length > 0 || otherBlocked.length > 0;

  return (
    <>
      {/* Lagezeile */}
      <p className="fleet-lage">
        {!hasAnything
          ? <>{de.fleet.risikoLageNichtsBlockiert}</>
          : <span className="fleet-amber">{de.fleet.risikoLageBlockiert(pendingApprovals.length + operatorHalts.length)}</span>
        }
      </p>

      {/* (a) Operator-Entscheidungen: wartende Freigaben */}
      {pendingApprovals.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoFreigabenTitle}</div>
          {pendingApprovals.map((ps) => (
            <div key={ps.path} className="fleet-risiko-approval">
              <div className="fleet-risiko-approval-n">
                {ps.topic || ps.filename}
                <span className="fleet-ps-badge fleet-ps-badge-amber" style={{ marginLeft: "auto" }}>
                  freigabe: operator
                </span>
              </div>
              {ps.freigabe ? (
                <div className="fleet-plan-kopf-meta" style={{ marginTop: 2 }}>
                  {ps.kanban_child_total > 0 ? <span>{ps.kanban_child_total} Karten geplant</span> : null}
                  {ps.binding ? <span>binding</span> : null}
                </div>
              ) : null}
              {/* Konfiguration gehört ins Plan-Subtab-Cockpit — kein Blind-Approve hier */}
              <button
                type="button"
                className="fleet-btn fleet-btn-primar"
                style={{ marginTop: 2, alignSelf: "flex-start", padding: "8px 14px", minHeight: 36 }}
                onClick={onNavigateToPlan}
                aria-label={`${ps.topic || ps.filename} im Plan-Subtab konfigurieren`}
              >
                {de.fleet.risikoFreigabeZumPlan}
              </button>
            </div>
          ))}
        </>
      ) : null}

      {/* (a) Blockierte Tasks: Operator-Halts */}
      {operatorHalts.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoBlockiertTitle}</div>
          {operatorHalts.map((t) => (
            <div key={t.id} className="fleet-risiko-blocked" aria-label={`Blockierter Task: ${t.title}`}>
              <div className="fleet-risiko-blocked-n">
                {t.title}
                <span className="fleet-ps-badge fleet-ps-badge-amber" style={{ marginLeft: "auto" }}>
                  {de.fleet.risikoOperatorHalt}
                </span>
              </div>
              {t.block_reason ? (
                <div style={{ font: "400 11px/1.4 var(--hc-font-mono)", color: "var(--fleet-t3)", paddingLeft: 0 }}>
                  {t.block_reason}
                </div>
              ) : null}
            </div>
          ))}
        </>
      ) : null}

      {/* Sonstige blockierte Tasks (keine Operator-Halts) — kompakt */}
      {otherBlocked.length > 0 ? (
        <div className="fleet-risiko-rel">
          {otherBlocked.slice(0, 5).map((t) => (
            <div key={t.id} className="fleet-risiko-rel-row">
              <span className="fleet-risiko-rel-lane" style={{ width: "auto", flex: 1 }}>{t.title}</span>
              {t.block_reason ? (
                <span className="fleet-risiko-rel-val" style={{ flex: "none", fontSize: 10, color: "var(--fleet-t3)" }}>
                  {t.block_reason.slice(0, 40)}
                </span>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}

      {/* (b) Zuverlässigkeit je Lane */}
      {profiles.length > 0 ? (
        <>
          <div className="fleet-risiko-sec">{de.fleet.risikoZuverlässigkeitTitle}</div>
          <div className="fleet-risiko-rel" aria-label="Zuverlässigkeit je Profil">
            {profiles.map((p) => {
              const isLowSample = p.low_sample || p.runs < 5;
              const completedPct = p.completed_rate != null ? Math.round(p.completed_rate * 100) : null;
              const failedPct = p.failed_rate != null ? Math.round(p.failed_rate * 100) : null;

              return (
                <div key={p.profile} className="fleet-risiko-rel-row">
                  <span className="fleet-risiko-rel-lane">{p.profile}</span>
                  {isLowSample ? (
                    <span className="fleet-risiko-low-sample" aria-label="Wenig Daten — kein sicheres Urteil möglich">
                      {de.fleet.risikoWenigDaten}
                    </span>
                  ) : (
                    <span className="fleet-risiko-rel-val">
                      {completedPct != null ? `${de.fleet.risikoAbschlussRate} ${completedPct} %` : "—"}
                      {failedPct != null && failedPct > 0 ? ` · ${de.fleet.risikoFailed} ${failedPct} %` : ""}
                      {p.retries > 0 ? ` · ${de.fleet.risikoRetries} ${p.retries}` : ""}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </>
      ) : null}

      {/* (c) System-Puls */}
      <div className="fleet-risiko-sec">{de.fleet.risikoSystemPulsTitle}</div>
      <div className="fleet-puls-table" aria-label="System-Puls">
        {/* Gateway */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoGateway}</span>
          <span className={`fleet-puls-val ${gateway?.heartbeat_age_s != null && gateway.heartbeat_age_s < 30 ? "fleet-puls-val-gruen" : gateway ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {gateway?.heartbeat_age_s != null
              ? de.fleet.risikoHeartbeatFrisch(Math.round(gateway.heartbeat_age_s))
              : gateway?.status === "healthy"
              ? de.fleet.risikoGrün
              : de.fleet.risikoHeartbeatNichtVerfügbar}
          </span>
        </div>

        {/* Dispatcher */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoDispatcher}</span>
          <span className={`fleet-puls-val ${dispatcher?.heartbeat_age_s != null && dispatcher.heartbeat_age_s < 30 ? "fleet-puls-val-gruen" : dispatcher ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {dispatcher?.heartbeat_age_s != null
              ? de.fleet.risikoHeartbeatFrisch(Math.round(dispatcher.heartbeat_age_s))
              : dispatcher?.status === "healthy"
              ? de.fleet.risikoGrün
              : de.fleet.risikoHeartbeatNichtVerfügbar}
          </span>
        </div>

        {/* CPU / RAM */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoCpuRam}</span>
          <span className="fleet-puls-val fleet-puls-val-normal">
            {host?.cpu_percent != null || host?.memory_percent != null
              ? `${host.cpu_percent != null ? Math.round(host.cpu_percent) : "—"} % · ${host.memory_percent != null ? Math.round(host.memory_percent) : "—"} %`
              : "—"}
          </span>
        </div>

        {/* Token-Pressure */}
        <div className="fleet-puls-row">
          <span className="fleet-puls-label">{de.fleet.risikoTokenPressure}</span>
          <span className={`fleet-puls-val ${tokenPressure?.class === "normal" ? "fleet-puls-val-gruen" : tokenPressure ? "fleet-puls-val-warn" : "fleet-puls-val-normal"}`}>
            {tokenPressure?.class ?? "—"}
          </span>
        </div>
      </div>

      {/* (d) Gepflegter Leerzustand */}
      {!hasAnything ? (
        <div className="fleet-risiko-leer">
          <div className="fleet-risiko-leer-title">{de.fleet.risikoLeerState}</div>
          <div className="fleet-risiko-leer-sub">
            {totalBlockedCount === 0
              ? "Alle Karten sauber durch."
              : de.fleet.risikoLeerStateSub(totalBoardTasks)}
          </div>
        </div>
      ) : null}
    </>
  );
}

