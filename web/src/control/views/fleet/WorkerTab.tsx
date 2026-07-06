/**
 * Worker-Subtab + Worker-Drawer (Overlay Bottom-Sheet).
 *
 * Aus FleetView.tsx extrahiert — reine Zerlegung, kein Verhalten geändert.
 */
import { useState } from "react";
import {
  runProgressFraction,
  heartbeatAge,
  fmtSeconds,
  fmtTokens,
  profileInitial,
  profileColorClass,
} from "../../lib/fleetHub";
import { de } from "../../i18n/de";
import type { Worker, BoardResponse, BoardTask } from "../../lib/types";
import type { ReliabilityResponse } from "../../lib/schemas";
import { Overlay } from "../../components/Overlay";
import { WorkerLogTail } from "../../components/WorkerCard";
import { useWorkerLifecycle } from "../../hooks/useControlData";

// ─── Worker-Subtab ────────────────────────────────────────────────────────────

interface WorkerTabProps {
  activeWorkers: Worker[];
  board: BoardResponse | null;
  reliability: ReliabilityResponse | null;
  now: number;
  initialOpen: Worker | null;
  onOpenChain: (rootId: string) => void;
}

export function WorkerTab({ activeWorkers, board, reliability, now, initialOpen, onOpenChain }: WorkerTabProps) {
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
          {runProgressFraction(w, now) != null ? (
            <div className="fleet-rail" title={w.run_progress == null ? "Fortschritt geschätzt (ETA-Heuristik)" : "Fortschritt (Runtime-Cap)"}>
              <div
                className="fleet-rail-fill"
                style={{ width: `${Math.round((runProgressFraction(w, now) ?? 0) * 100)}%` }}
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

  const [logOpen, setLogOpen] = useState(false);
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

        {/* Worker-Steuerung: Unlock/Nudge/Restart/Terminate (Gap 1) */}
        <WorkerLifecycleActions runId={w.run_id} />

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
            onClick={() => setLogOpen((v) => !v)}
          >
            {logOpen ? de.worker.logHide : de.fleet.drawerLog}
          </button>
          <button type="button" className="fleet-btn" onClick={onClose}>
            {de.fleet.drawerSchliessen}
          </button>
        </div>

        {/* Log-Tail (nur bei offenem Drawer pollend, wie WorkerCard/NodeDetailDrawer) */}
        {logOpen ? <WorkerLogTail taskId={w.task_id} /> : null}
      </div>
    </Overlay>
  );
}
