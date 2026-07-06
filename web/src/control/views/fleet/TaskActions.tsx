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
import { useChainActions, useFixRedispatch, useTaskAction } from "../../hooks/useControlData";
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
  /** Board nach erfolgreicher Aktion neu laden. */
  onChanged?: () => void | Promise<void>;
  /** Nach erfolgreichem Abbrechen/Ketten-Abbruch aufgerufen (z. B. Drawer schließen). */
  onCancelled?: () => void;
  /** Opt-in: zeigt Ship/Rework auch für `status === "review"`. Default bleibt
   *  versteckt (siehe Kommentar unten) — nur der NodeDetailDrawer setzt dies,
   *  weil er die Verifier-Urteile (Ergebnis-Tab) direkt neben den Buttons zeigt. */
  allowReviewStage?: boolean;
}

export function FleetTaskActions({ taskId, status, chainRootId, onChanged, onCancelled, allowReviewStage }: FleetTaskActionsProps) {
  const [armed, setArmed] = useState<string | null>(null);
  const [chainNote, setChainNote] = useState<string>("");
  const task = useTaskAction(onChanged);
  const redispatch = useFixRedispatch();
  const chain = useChainActions();

  const busy = task.busyId === taskId || redispatch.busyId === taskId || chain.busy != null;
  const st = status as TaskStatus;

  // 409/Guard-Fehler aus den drei Hooks — verbatim, nie verschluckt (AC-2).
  const errorText = task.errorById[taskId] || redispatch.errorById[taskId] || chain.error || "";
  const retryDone = Boolean(redispatch.doneIds[taskId]);

  const actions: UiAction[] = [];
  // (1) Stage-Übergänge — stageActions wiederverwenden (Reopen = Unblock/PATCH
  // ready). Für `review` ist die Abnahme (Ship) bzw. Rückweisung (Rework) eines
  // Verifier-Gates standardmäßig VERSTECKT — kein ungeschütztes Blind-Approve
  // ohne den Verifier-Kontext im Blick. `allowReviewStage` ist der bewusste
  // Opt-in dafür: nur der NodeDetailDrawer setzt ihn, weil er die Verifier-
  // Urteile (Ergebnis-Tab) direkt neben diesen Buttons zeigt — RisikoTabs
  // blockierte Zeilen lassen den Default unangetastet.
  const stage = st === "review" ? (allowReviewStage ? stageActions("review") : []) : stageActions(st);
  for (const a of stage) {
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
      {chainNote ? <p className="fleet-ta-note">{chainNote}</p> : null}
      {retryDone && !chainNote ? <p className="fleet-ta-note">{de.fleet.actionRetryDone}</p> : null}
    </div>
  );
}
