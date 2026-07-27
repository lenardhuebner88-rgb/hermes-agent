/**
 * FleetTaskActions — Operator-Steuerung für einen einzelnen Task (S3).
 *
 * Wird vom NodeDetailDrawer und von den Blocked-Zeilen des Risiko-Subtabs
 * genutzt. Rendert die statusabhängigen Stage-Übergänge (stageActions,
 * unverändert wiederverwendet — u. a. Reopen = Unblock via PATCH ready) plus die
 * Management-Aktionen, die das Stage-Modell nicht ausdrücken kann:
 *   • Retry           — Unblock + Dispatcher-Tick (useFixRedispatch-Muster)
 *   • Abbrechen       — Einzeltask archivieren (PATCH archived)
 *   • Kette abbrechen — POST /tasks/{root}/cancel-chain
 * Jede Aktion wird zwei-klick-scharfgestellt (erster Klick = arm, zweiter =
 * feuern). Eine 409-Antwort (blockierende Parents) wird inline gezeigt, nie
 * verschluckt (AC-2).
 */
import { useCallback, useState } from "react";
import { manageActions, stageActions } from "../../lib/fleet";
import type { TaskStatus } from "../../lib/types";
import { useChainActions } from "../../hooks/chainFlow";
import { useFixRedispatch, useTaskAction } from "../../hooks/taskActions";
import { de } from "../../i18n/de";

interface UiAction {
  key: string;
  label: string;
  confirm: string;
  danger: boolean;
  /** Feuert die Aktion; `detail` trägt eine 409-/Guard-Meldung bei ok=false. */
  run: () => Promise<{ ok: boolean; detail?: string }>;
  /** Nach Erfolg den Task als "weg" behandeln (z. B. Drawer schließen). */
  closesTask?: boolean;
}

export interface FleetTaskActionsProps {
  taskId: string;
  status: TaskStatus | string;
  /** Root der Kette (min-level Node bzw. Board-root_id). null ⇒ kein Ketten-Abbruch. */
  chainRootId?: string | null;
  /** Gehaltene Operator-Kette (Root-ID + Gliederzahl). Wenn gesetzt, wird der
   * Starten-Stage-Übergang durch die Kettenfreigabe ersetzt (KF-2): Einzelstarts
   * aus gehaltenen Ketten lehnt das Backend mit 409 ab — der ehrliche Weg ist
   * die Freigabe der ganzen Kette über POST /tasks/{root}/flow-release. */
  heldChain?: { rootId: string; memberCount: number } | null;
  /** Führt die Kettenfreigabe aus (useFlowRelease.release). Pflicht bei heldChain. */
  onReleaseChain?: ((rootId: string) => Promise<{ ok: boolean; released?: number; detail?: string }>) | null;
  /** Fehlergrund der letzten Freigabe (useFlowRelease.errorById) — verbatim, nie verschluckt. */
  releaseChainError?: string | null;
  /** Freigabe läuft gerade (useFlowRelease.busyId). */
  releaseBusy?: boolean;
  /** Board nach erfolgreicher Aktion neu laden. */
  onChanged?: () => void | Promise<void>;
  /** Nach erfolgreichem Abbrechen/Ketten-Abbruch aufgerufen (z. B. Drawer schließen). */
  onCancelled?: () => void;
  /** Current-state reason why the stage advance is impossible. Management
   * actions remain available; the invalid stage request is never submitted. */
  stageBlockReason?: string | null;
}

export function FleetTaskActions({ taskId, status, chainRootId, heldChain, onReleaseChain, releaseChainError, releaseBusy, onChanged, onCancelled, stageBlockReason }: FleetTaskActionsProps) {
  const [armed, setArmed] = useState<string | null>(null);
  const [chainNote, setChainNote] = useState<string>("");
  const task = useTaskAction(onChanged);
  const redispatch = useFixRedispatch();
  const chain = useChainActions();

  const busy = task.busyId === taskId || redispatch.busyId === taskId || chain.busy != null || Boolean(releaseBusy);
  const st = status as TaskStatus;
  // KF-2: Bei einer gehaltenen Kette ersetzt die Freigabe den Einzelstart.
  const releaseMode = Boolean(heldChain && onReleaseChain);

  // 409/Guard-Fehler aus den Hooks — verbatim, nie verschluckt (AC-2/AC-4).
  const errorText = task.errorById[taskId] || redispatch.errorById[taskId] || chain.error || releaseChainError || "";
  const retryDone = Boolean(redispatch.doneIds[taskId]);

  const actions: UiAction[] = [];
  // (0) Kettenfreigabe — steht an Stelle des Starten-Übergangs (AC-1/AC-2):
  // benennt Kette + Gliederzahl und gibt die GANZE Kette frei, nicht die Karte.
  if (releaseMode && heldChain && onReleaseChain) {
    actions.push({
      key: "releaseChain",
      label: de.fleet.actionReleaseChain(heldChain.rootId, heldChain.memberCount),
      confirm: de.fleet.actionReleaseChainConfirm(heldChain.rootId, heldChain.memberCount),
      danger: false,
      run: async () => {
        const res = await onReleaseChain(heldChain.rootId);
        if (res.ok) setChainNote(de.fleet.actionChainReleased(res.released ?? 0));
        return { ok: res.ok, detail: res.ok ? undefined : res.detail };
      },
    });
  }
  // (1) Stage-Übergänge — stageActions wiederverwenden (Reopen = Unblock/PATCH
  // ready). Review bleibt absichtlich ohne manuellen Status-Button: das Backend
  // akzeptiert weder review→done noch review→blocked über diesen PATCH-Pfad.
  const stage = stageBlockReason && (st === "todo" || st === "scheduled") ? [] : stageActions(st);
  for (const a of stage) {
    // Einzelstart (PATCH ready) entfällt bei gehaltener Kette — das Backend
    // lehnt ihn mit 409 ab; die Freigabe oben ist der einzige ehrliche Weg.
    if (releaseMode && a.key === "dispatch") continue;
    actions.push({
      key: `stage:${a.key}`,
      label: a.label,
      confirm: a.confirm,
      danger: a.intent === "danger",
      run: async () => {
        const res = await task.run(taskId, a.target);
        return { ok: res.ok, detail: res.ok ? undefined : res.detail };
      },
    });
  }
  // (2) Management-Aktionen (Retry / Cancel / Cancel-Kette).
  for (const key of manageActions(st, { hasChain: Boolean(chainRootId) })) {
    if (key === "retry") {
      actions.push({
        key: "retry",
        label: de.fleet.actionRetry,
        confirm: de.fleet.actionRetryConfirm,
        danger: false,
        run: async () => {
          const res = await redispatch.run(taskId);
          if (res.ok) await onChanged?.();
          return res;
        },
      });
    } else if (key === "cancel") {
      actions.push({
        key: "cancel",
        label: de.fleet.actionCancelTask,
        confirm: de.fleet.actionCancelTaskConfirm,
        danger: true,
        closesTask: true,
        run: async () => {
          const res = await task.run(taskId, "archived");
          return { ok: res.ok, detail: res.ok ? undefined : res.detail };
        },
      });
    } else if (key === "cancelChain" && chainRootId) {
      actions.push({
        key: "cancelChain",
        label: de.fleet.actionCancelChain,
        confirm: de.fleet.actionCancelChainConfirm,
        danger: true,
        closesTask: true,
        run: async () => {
          const res = await chain.cancelChain(chainRootId);
          if (res.ok) {
            setChainNote(de.fleet.actionChainCancelled(res.terminated.length, res.held.length, res.skipped.length));
            await onChanged?.();
          }
          return { ok: res.ok, detail: res.detail };
        },
      });
    }
  }

  const fire = useCallback(async (a: UiAction) => {
    setArmed(null);
    const res = await a.run();
    if (res.ok && a.closesTask) onCancelled?.();
  }, [onCancelled]);

  if (actions.length === 0) return null;

  const armedAction = actions.find((a) => a.key === armed) ?? null;

  return (
    <div className="fleet-task-actions">
      {armedAction ? (
        <div className="fleet-ta-confirm">
          <span className="fleet-ta-confirm-text">{armedAction.confirm}</span>
          <button
            type="button"
            className="fleet-ta-btn"
            style={{
              color: armedAction.danger ? "var(--fleet-rot)" : "var(--fleet-puls)",
              borderColor: armedAction.danger ? "rgba(255,93,115,.5)" : "rgba(55,224,255,.45)",
            }}
            disabled={busy}
            onClick={() => void fire(armedAction)}
          >
            {busy ? de.fleet.actionBusy : de.fleet.actionConfirm}
          </button>
          <button type="button" className="fleet-ta-btn" disabled={busy} onClick={() => setArmed(null)}>
            {de.fleet.actionDismiss}
          </button>
        </div>
      ) : (
        <div className="fleet-ta-row">
          {actions.map((a) => (
            <button
              key={a.key}
              type="button"
              className="fleet-ta-btn"
              style={{
                color: a.danger ? "var(--fleet-rot)" : "var(--fleet-puls)",
                borderColor: a.danger ? "rgba(255,93,115,.4)" : "rgba(55,224,255,.35)",
              }}
              disabled={busy}
              onClick={() => { task.clearError(taskId); setChainNote(""); setArmed(a.key); }}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
      {errorText ? <p className="fleet-ta-error">{errorText}</p> : null}
      {stageBlockReason ? <p className="fleet-ta-error" role="status">{stageBlockReason}</p> : null}
      {chainNote ? <p className="fleet-ta-note">{chainNote}</p> : null}
      {retryDone && !chainNote ? <p className="fleet-ta-note">{de.fleet.actionRetryDone}</p> : null}
    </div>
  );
}
