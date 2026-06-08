/**
 * FleetPipeline — the operator pipeline (Ziel 2). A stage rail with REAL
 * per-stage counts from GET /board, and a list of tasks awaiting an operator
 * decision whose buttons drive REAL Kanban transitions via PATCH /tasks/{id}:
 *   Plan (triage→todo) · Dispatch (todo→ready, auto-dispatches) ·
 *   Ship (review→done) · Rework (review→blocked) · Reopen (blocked→ready).
 * Worker/gate-driven stages (ready/running/review-verify) show an honest,
 * explaining guard instead of a fake button. Every write is confirm-gated and
 * surfaces the backend's 409 detail verbatim (e.g. "blocked by parent(s)").
 */
import { useMemo, useState } from "react";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { TONE_HEX, taskStatusLabel } from "../../lib/tones";
import { buildPipeline, statusToStage, stageActions, STAGE_META, type StageAction } from "../../lib/fleet";
import type { BoardTask, TaskStatus } from "../../lib/types";
import { useTaskAction } from "../../hooks/useControlData";
import { Led } from "../atoms";
import { Eyebrow } from "../primitives";
import { FleetPanel, FleetEmptyState } from "./atoms";

const STATUS_DOT: Record<string, "live" | "warn" | "error" | "ready" | "idle"> = {
  triage: "idle", todo: "idle", scheduled: "idle", ready: "ready",
  running: "live", blocked: "error", review: "warn", done: "ready",
};

function StageTag({ status }: { status: TaskStatus }) {
  const stage = statusToStage(status);
  const tone = stage ? STAGE_META[stage].tone : "zinc";
  const color = TONE_HEX[tone];
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[0.7rem] font-medium"
      style={{ borderColor: `${color}55`, color, background: `${color}14` }}
    >
      <Led kind={STATUS_DOT[status] ?? "idle"} size={6} />
      {stage ? STAGE_META[stage].label : taskStatusLabel[status]} · {taskStatusLabel[status]}
    </span>
  );
}

function ActionButton({ action, onClick, disabled }: { action: StageAction; onClick: () => void; disabled?: boolean }) {
  const color = TONE_HEX[action.tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex min-h-8 items-center rounded-full border px-3 text-xs font-medium transition disabled:opacity-40",
        action.intent === "danger" ? "hover:bg-red-500/10" : "hover:bg-white/5",
      )}
      style={{ borderColor: `${color}55`, color }}
    >
      {action.label}
    </button>
  );
}

function ActionRow({
  task,
  busy,
  error,
  onAct,
  onClearError,
}: {
  task: BoardTask;
  busy: boolean;
  error?: string;
  onAct: (action: StageAction) => void;
  onClearError: () => void;
}) {
  const [pending, setPending] = useState<StageAction | null>(null);
  const actions = stageActions(task.status);
  const isReview = task.status === "review";

  return (
    <li className="rounded-lg border border-[var(--hc-border)] bg-[var(--hc-panel)] p-3">
      <div className="flex flex-col gap-2.5 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StageTag status={task.status} />
            <span className="hc-mono text-[0.7rem] hc-dim">
              {task.id}{task.assignee ? ` · ${task.assignee}` : ""}
            </span>
          </div>
          <p className="mt-1.5 line-clamp-1 text-sm font-medium text-white">{task.title}</p>
          {task.latest_summary ? <p className="line-clamp-1 text-xs hc-soft">{task.latest_summary}</p> : null}
        </div>

        <div className="flex shrink-0 flex-col items-start gap-1.5 sm:items-end">
          {pending ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs hc-soft">{pending.confirm}</span>
              <button
                type="button"
                onClick={() => { onClearError(); onAct(pending); setPending(null); }}
                disabled={busy}
                className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-3 text-xs font-medium text-[var(--hc-accent-text)] disabled:opacity-40"
              >
                {busy ? "…" : "Bestätigen"}
              </button>
              <button type="button" onClick={() => setPending(null)} className="inline-flex min-h-8 items-center rounded-full border border-[var(--hc-border-strong)] px-3 text-xs hc-soft">
                Abbrechen
              </button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-1.5">
              {actions.map((action) => (
                <ActionButton key={action.key} action={action} onClick={() => setPending(action)} disabled={busy} />
              ))}
            </div>
          )}
        </div>
      </div>

      {isReview && !pending ? (
        <p className="mt-2 flex items-center gap-1.5 text-[0.7rem] hc-dim">
          <ShieldCheck className="h-3.5 w-3.5 text-cyan-300" />
          Verify: Verifier-Gate läuft automatisch — Ship nimmt manuell ab, Rework schickt zurück.
        </p>
      ) : null}

      {error ? (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-red-300">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0">{error}</span>
        </p>
      ) : null}
    </li>
  );
}

export function FleetPipeline({ tasks, reload }: { tasks: BoardTask[]; reload: () => void | Promise<void> }) {
  const pipeline = useMemo(() => buildPipeline(tasks), [tasks]);
  const { busyId, errorById, run, clearError } = useTaskAction(reload);
  const maxCount = Math.max(1, ...pipeline.buckets.map((b) => b.count));

  const onAct = (taskId: string, action: StageAction) => {
    void run(taskId, action.target, action.key === "rework" ? { block_reason: "Operator-Rework aus dem Fleet-Tab" } : undefined);
  };

  const blockedMeta = pipeline.blockedCount > 0 ? de.fleet.pipelineBlocked(pipeline.blockedCount) : `${pipeline.total} Aufgaben`;

  return (
    <FleetPanel eyebrow={de.fleet.pipelineEyebrow} meta={blockedMeta}>
      <p className="mb-3 hc-type-label hc-dim">{de.fleet.pipelineHint}</p>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {pipeline.buckets.map((bucket) => {
          const color = TONE_HEX[bucket.meta.tone];
          const fill = bucket.count > 0 ? Math.min(1, bucket.count / maxCount) : 0;
          return (
            <div key={bucket.stage} className="hc-fleet-pod">
              <Eyebrow>{bucket.meta.label}</Eyebrow>
              <div className="hc-fleet-pod-value mt-1.5" style={{ fontSize: "1.4rem" }}>{bucket.count}</div>
              <div className="hc-stage-rail mt-2" style={{ "--hc-role": color } as React.CSSProperties}>
                <i style={{ width: `${fill * 100}%` }} />
              </div>
              <p className="mt-1.5 hc-type-label hc-dim leading-tight">{bucket.meta.purpose}</p>
            </div>
          );
        })}
      </div>

      <div className="mt-4">
        {pipeline.actionable.length === 0 ? (
          <FleetEmptyState ok title={de.fleet.pipelineEmptyTitle} desc={de.fleet.pipelineEmptyDesc} />
        ) : (
          <ul className="space-y-2">
            {pipeline.actionable.map((task) => (
              <ActionRow
                key={task.id}
                task={task as BoardTask}
                busy={busyId === task.id}
                error={errorById[task.id] || undefined}
                onAct={(action) => onAct(task.id, action)}
                onClearError={() => clearError(task.id)}
              />
            ))}
          </ul>
        )}
      </div>
    </FleetPanel>
  );
}
