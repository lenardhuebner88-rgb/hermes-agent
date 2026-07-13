/**
 * Worker-Subtab — Puls-Leitstand (Variante B).
 *
 * Der Tab ist ein Kontrollraum: eine Puls-Strip-Kopfzeile (Slots/Queue, heute
 * fertig, Live-Token-Summe), pro aktivem Slot eine Zeitachsen-Swimlane (Laufband
 * gegen das p90-Fenster, p50-Marke, Heartbeat-Ticks, step_key) und darunter ein
 * Live-Ereignis-Ticker. Ohne Worker: freie Slot-Lanes + der Ticker als
 * Verlaufsspur — nie ein schwarzes Loch. Tap auf eine Lane öffnet den Fokus-
 * Drawer (erweitert um das vergrößerte Band + Notiz-Historie + „Andere Lanes").
 *
 * Referenz: Design-Board-Karte c_97c25aca (operator-approved Variante B).
 */
import { useState } from "react";
import {
  heartbeatAge,
  fmtSeconds,
  fmtTokens,
  fmtClockTime,
  derivePulse,
  profileInitial,
  profileColorClass,
  premiumLaneMarker,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { Worker, BoardResponse, BoardTask } from "../../lib/types";
import type { ReliabilityResponse } from "../../lib/schemas";
import { Overlay } from "../../components/Overlay";
import { WorkerLogTail } from "../../components/WorkerCard";
import { useWorkerLifecycle, useWorkerActivity, useRunLiveEvents } from "../../hooks/useControlData";
import { WorkerBand } from "./WorkerBand";
import { SlotLane, FreeSlotLane, MiniLane } from "./SlotLane";
import { LiveTicker } from "./LiveTicker";
import { PulseStrip } from "./PulseStrip";
import { BoardBadge } from "../../components/fleet/BoardIdentity";
import { FleetSourceFreshness } from "./FleetSourceFreshness";
import { elapsedSeconds } from "../../lib/derive";

// ─── Worker-Subtab ────────────────────────────────────────────────────────────

interface WorkerTabProps {
  activeWorkers: Worker[];
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  initialOpen: Worker | null;
  onOpenChain: (rootId: string) => void;
  /** F4: Live-Concurrency-Cap (kanban.max_in_progress) für den Pulse-Strip. */
  cap?: number | null;
  /** Heute abgeschlossene Läufe (costs.today.runs) für den Pulse-Strip. */
  doneToday?: number | null;
  currentBoard?: string;
}

interface WorkerSelection {
  key: string;
  snapshot: Worker;
}

function workerIdentity(worker: Worker): string {
  return `${worker.board_slug ?? "current"}:${worker.task_id}`;
}

// Wie viele freie Slot-Lanes der Leerzustand zeigt (Cap, sonst 3; gedeckelt bei
// 5, damit ein hoher Cap den Tab nicht mit Leer-Lanes flutet).
function freeSlotCount(cap: number | null | undefined): number {
  const base = cap && cap >= 1 ? cap : 3;
  return Math.max(1, Math.min(base, 5));
}

export function WorkerTab({
  activeWorkers,
  board,
  reliability,
  now,
  initialOpen,
  onOpenChain,
  cap = null,
  doneToday = null,
  currentBoard = "default",
}: WorkerTabProps) {
  const [selection, setSelection] = useState<WorkerSelection | null>(
    () => (initialOpen ? { key: workerIdentity(initialOpen), snapshot: initialOpen } : null),
  );
  const liveEvents = useRunLiveEvents(true);

  const selectedWorker = selection ? activeWorkers.find((w) => workerIdentity(w) === selection.key) ?? null : null;
  const drawerWorker = selectedWorker ?? selection?.snapshot ?? null;

  // Pulse-Ableitung: queue = ready+scheduled, blocked = blocked-Spalte.
  const columns = board?.columns ?? [];
  const queue = columns
    .filter((c) => c.name === "ready" || c.name === "scheduled")
    .reduce((n, c) => n + c.tasks.length, 0);
  const blocked = (columns.find((c) => c.name === "blocked")?.tasks ?? []).length;
  const pulse = derivePulse({ activeWorkers, cap, queue, doneToday, blocked });

  const otherWorkers = drawerWorker
    ? activeWorkers.filter((w) => workerIdentity(w) !== workerIdentity(drawerWorker))
    : [];

  const select = (w: Worker) => setSelection({ key: workerIdentity(w), snapshot: w });

  const drawer = drawerWorker ? (
    <WorkerDrawer
      worker={drawerWorker}
      active={selectedWorker != null}
      board={board}
      reliability={reliability}
      now={now}
      otherWorkers={otherWorkers}
      onOpenWorker={select}
      onClose={() => setSelection(null)}
      onOpenChain={onOpenChain}
      currentBoard={currentBoard}
    />
  ) : null;

  return (
    <div className="fleet-worker-tab">
      <FleetSourceFreshness sources={[{ label: "Live-Ereignisse", ...liveEvents }]} />
      <PulseStrip pulse={pulse} />

      {activeWorkers.length > 0 ? (
        <>
          <div className="fleet-laneswrap">
            <div className="fleet-axis" aria-hidden="true">
              <span>0</span>
              <span>25%</span>
              <span>50%</span>
              <span>75%</span>
              <span>{de.fleet.pulseP90Window}</span>
            </div>
            {activeWorkers.map((w) => (
              <SlotLane key={workerIdentity(w)} worker={w} now={now} onOpen={() => select(w)} />
            ))}
          </div>
          <LiveTicker
            events={liveEvents.events}
            title={de.fleet.tickerLive}
            loading={liveEvents.loading}
            emptyLabel={de.fleet.tickerEmpty}
          />
        </>
      ) : (
        <>
          <div className="fleet-laneswrap">
            {Array.from({ length: freeSlotCount(cap) }, (_, i) => (
              <FreeSlotLane
                key={`free-${i}`}
                index={i + 1}
                label={i === 0 && queue > 0 ? de.fleet.slotFreeWaiting : de.fleet.slotFree}
              />
            ))}
          </div>
          <LiveTicker
            events={liveEvents.events}
            title={de.fleet.tickerHistory}
            loading={liveEvents.loading}
            emptyLabel={de.fleet.tickerEmpty}
          />
        </>
      )}

      {drawer}
    </div>
  );
}

// ─── Worker-Lifecycle-Steuerung (Gap 1) ──────────────────────────────────────
// Nur Nudge feuert direkt (Kommentar am Task, kein Kill — plugin_api.py). Alle
// anderen Aktionen nehmen den Worker-Prozess weg (unlock/hold/restart/terminate
// laufen über reclaim_task bzw. hold_task) und sind deshalb zwei-Klick-scharf
// wie FleetTaskActions (fleet-ta-btn-Hausmuster, TaskActions.tsx). Fehler
// werden wörtlich gezeigt (AC-2 — nie verschlucken); Erfolg zeigt den deutschen
// detail-Einzeiler des Backends.

type ArmableWorkerAction = "unlock" | "hold" | "restart" | "terminate";

const ARMED_META: Record<ArmableWorkerAction, { label: () => string; confirm: () => string }> = {
  unlock: { label: () => de.fleet.workerUnlock, confirm: () => de.fleet.workerUnlockConfirm },
  hold: { label: () => de.fleet.workerHold, confirm: () => de.fleet.workerHoldConfirm },
  restart: { label: () => de.fleet.workerRestart, confirm: () => de.fleet.workerRestartConfirm },
  terminate: { label: () => de.fleet.workerTerminate, confirm: () => de.fleet.workerTerminateConfirm },
};

export function WorkerLifecycleActions({ runId }: { runId: string }) {
  const { busyId, errorById, run, terminate, clearError } = useWorkerLifecycle();
  const [armed, setArmed] = useState<ArmableWorkerAction | null>(null);
  const [note, setNote] = useState("");

  const busy = busyId === runId;
  const error = errorById[runId] || "";

  const fireNudge = async () => {
    clearError(runId);
    setNote("");
    const res = await run(runId, "nudge");
    if (res.ok) setNote(res.detail || "");
  };

  const fireArmed = async () => {
    const action = armed;
    setArmed(null);
    if (!action) return;
    setNote("");
    const res = action === "terminate" ? await terminate(runId) : await run(runId, action);
    if (res.ok) setNote(res.detail || "");
  };

  const arm = (action: ArmableWorkerAction) => {
    clearError(runId);
    setNote("");
    setArmed(action);
  };

  return (
    <div className="fleet-task-actions">
      {armed ? (
        <div className="fleet-ta-confirm">
          <span className="fleet-ta-confirm-text">{ARMED_META[armed].confirm()}</span>
          <button
            type="button"
            className="fleet-ta-btn"
            style={{ color: "var(--fleet-rot)", borderColor: "rgba(255,93,115,.5)" }}
            disabled={busy}
            onClick={() => void fireArmed()}
          >
            {busy ? de.fleet.workerActionBusy : de.fleet.actionConfirm}
          </button>
          <button type="button" className="fleet-ta-btn" disabled={busy} onClick={() => setArmed(null)}>
            {de.fleet.actionDismiss}
          </button>
        </div>
      ) : (
        <div className="fleet-ta-row">
          <button
            type="button"
            className="fleet-ta-btn"
            style={{ color: "var(--fleet-puls)", borderColor: "rgba(55,224,255,.35)" }}
            disabled={busy}
            onClick={() => void fireNudge()}
          >
            {busy ? de.fleet.workerActionBusy : de.fleet.workerNudge}
          </button>
          {(["unlock", "hold", "restart", "terminate"] as ArmableWorkerAction[]).map((action) => (
            <button
              key={action}
              type="button"
              className="fleet-ta-btn"
              style={
                action === "terminate"
                  ? { color: "var(--fleet-rot)", borderColor: "rgba(255,93,115,.4)" }
                  : { color: "var(--fleet-puls)", borderColor: "rgba(55,224,255,.35)" }
              }
              disabled={busy}
              onClick={() => arm(action)}
            >
              {ARMED_META[action].label()}
            </button>
          ))}
        </div>
      )}
      {error ? <p className="fleet-ta-error" role="alert">{error}</p> : null}
      {!error && note ? <p className="fleet-ta-note">{note}</p> : null}
    </div>
  );
}

// ─── Notiz-Historie (AC-3) ────────────────────────────────────────────────────
// Per-Lane Heartbeat-Notiz-Verlauf aus GET /tasks/{task_id}/activity — nur wenn
// der Drawer offen und der Worker aktiv ist (der Hook pausiert bei null-taskId).

function WorkerNotesHistory({ taskId }: { taskId: string }) {
  const activity = useWorkerActivity(taskId);
  const notes = (activity.data?.events ?? []).filter((e) => e.note && e.note.trim());

  return (
    <div className="fleet-fx-notes-wrap">
      <div className="fleet-fx-notes-head">{de.fleet.drawerNotes}</div>
      {notes.length === 0 ? (
        <div className="fleet-fx-note fleet-fx-note-empty">
          {activity.loading ? "Lädt Notizen …" : de.fleet.drawerNotesEmpty}
        </div>
      ) : (
        <div className="fleet-fx-notes">
          {notes.map((e, i) => (
            <div key={e.id} className="fleet-fx-note">
              <span className="fleet-fx-note-ts">{fmtClockTime(e.at)}</span>{" "}
              <span className={i === 0 ? "fleet-fx-note-cur" : undefined}>
                {i === 0 ? "▸ " : ""}
                {e.note}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Worker-Drawer (Fokus) ────────────────────────────────────────────────────

interface WorkerDrawerProps {
  worker: Worker;
  active: boolean;
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  otherWorkers: Worker[];
  onOpenWorker: (w: Worker) => void;
  onClose: () => void;
  onOpenChain: (rootId: string) => void;
  currentBoard: string;
}

function WorkerDrawer({
  worker: w,
  active,
  board,
  reliability,
  now,
  otherWorkers,
  onOpenWorker,
  onClose,
  onOpenChain,
  currentBoard,
}: WorkerDrawerProps) {
  const elapsedSec = elapsedSeconds(w.started_at, now) ?? Number.NaN;
  const hbAge = heartbeatAge(w.last_heartbeat_at, now);
  const initial = profileInitial(w.profile);
  const colorCls = profileColorClass(w.profile);
  const foreignBoard = Boolean(w.board_slug && w.board_slug !== currentBoard);

  // Profil-Verlässlichkeit aus Reliability-Daten (ReliabilityResponse aus lib/schemas)
  const relProfile = reliability?.profiles?.find((p) => p.profile === w.profile);

  // Ketten-Position: root_id via Board-Lookup (BoardResponse aus lib/types)
  const allBoardTasks: BoardTask[] = (board?.columns ?? []).flatMap((c) => c.tasks);
  const boardTask = foreignBoard ? undefined : allBoardTasks.find((t) => t.id === w.task_id);

  const [logOpen, setLogOpen] = useState(false);
  // root_id ist entweder der eigene Task (Root) oder der Parent-Root
  const chainRootId = boardTask?.root_id ?? null;
  const branchName = boardTask?.branch_name ?? null;
  const chainMembers = chainRootId
    ? allBoardTasks.filter((t) => t.root_id === chainRootId || t.id === chainRootId)
    : [];
  const hasTokenSample = w.token_status === "live" || w.token_status === "partial" || w.input_tokens != null || w.output_tokens != null;
  const tokenDisplay = hasTokenSample
    ? `${fmtTokens(w.input_tokens)} → ${fmtTokens(w.output_tokens)}`
    : de.worker.tokenNoLiveSample;

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
          <div className={`fleet-avatar fleet-avatar-gross ${colorCls}`} {...premiumLaneMarker(w.profile)}>{initial}</div>
          <div className="fleet-dr-title">
            {w.profile}
            <span>läuft seit {fmtSeconds(elapsedSec)} · {w.task_assignee}</span>
          </div>
          <BoardBadge slug={w.board_slug} />
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
          <code>{w.task_id}{branchName ? ` · ${branchName}` : ""}{active ? ` · Run ${w.run_id}` : ""}</code>
        </div>

        {/* Vergrößertes Zeitachsen-Band (AC-3) — nur solange der Worker läuft. */}
        {active ? <WorkerBand worker={w} now={now} size="big" /> : null}

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
            <div className="fleet-kv-v">{tokenDisplay}</div>
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

        {/* Notiz-Historie (AC-3) — nur bei laufendem Worker. */}
        {active && !foreignBoard ? <WorkerNotesHistory taskId={w.task_id} /> : null}

        {active ? null : (
          <div className="fleet-kv" role="status">
            <div className="fleet-kv-k">{de.fleet.workerEndedTitle}</div>
            <div className="fleet-kv-v">{de.fleet.workerEndedDesc}</div>
          </div>
        )}

        {/* Worker-Steuerung: Unlock/Nudge/Restart/Terminate (Gap 1) */}
        {active && !foreignBoard ? <WorkerLifecycleActions runId={w.run_id} /> : null}
        {active && foreignBoard ? (
          <p className="fleet-plan-hint">Fremd-Board · Worker nur beobachten. Keine Board-übergreifende Steuerung.</p>
        ) : null}

        {/* Action-Buttons */}
        <div className="fleet-actions">
          {chainRootId && !foreignBoard ? (
            <button
              type="button"
              className="fleet-btn fleet-btn-primar"
              onClick={() => onOpenChain(chainRootId)}
            >
              {de.fleet.drawerKetteOeffnen}
            </button>
          ) : null}
          {active && !foreignBoard ? (
            <button
              type="button"
              className="fleet-btn"
              onClick={() => setLogOpen((v) => !v)}
            >
              {logOpen ? de.worker.logHide : de.fleet.drawerLog}
            </button>
          ) : null}
          <button type="button" className="fleet-btn" onClick={onClose}>
            {de.fleet.drawerSchliessen}
          </button>
        </div>

        {/* Log-Tail (nur bei offenem Drawer pollend, wie WorkerCard/NodeDetailDrawer) */}
        {active && !foreignBoard && logOpen ? <WorkerLogTail taskId={w.task_id} /> : null}

        {/* Andere Lanes: schneller Sprung zu den übrigen aktiven Workern. */}
        {otherWorkers.length > 0 ? (
          <div className="fleet-fx-other">
            <div className="fleet-sec">{de.fleet.drawerOtherLanes}</div>
            {otherWorkers.map((ow) => (
              <MiniLane key={workerIdentity(ow)} worker={ow} now={now} onOpen={() => onOpenWorker(ow)} />
            ))}
          </div>
        ) : null}
      </div>
    </Overlay>
  );
}
