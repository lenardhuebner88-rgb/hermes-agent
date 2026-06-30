/**
 * FleetPipeline — the operator pipeline (Ziel 2). A stage rail with REAL
 * per-stage counts from GET /board, and a list of tasks awaiting an operator
 * decision whose buttons drive REAL Kanban transitions via PATCH /tasks/{id}:
 *   Plan (triage→todo) · Dispatch (todo→ready, auto-dispatches) ·
 *   Ship (review→done) · Nacharbeit (review→blocked) · Reopen (blocked→ready).
 * Worker/gate-driven stages (ready/running/review-verify) show an honest,
 * explaining guard instead of a fake button. Every write is confirm-gated and
 * surfaces the backend's 409 detail verbatim (e.g. "blocked by parent(s)").
 */
import { useMemo, useState } from "react";
import { AlertTriangle, FileText, HeartPulse, Link2, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/utils";
import { de } from "../../i18n/de";
import { TONE_HEX, taskStatusLabel } from "../../lib/tones";
import { buildPipeline, statusToStage, stageActions, stageGuard, STAGE_META, type StageAction } from "../../lib/fleet";
import type { BoardTask, TaskStatus, Worker } from "../../lib/types";
import { useTaskAction } from "../../hooks/useControlData";
import { Led } from "../atoms";
import { Eyebrow } from "../primitives";
import { FleetPanel, FleetEmptyState } from "./atoms";

const STATUS_DOT: Record<string, "live" | "warn" | "error" | "ready" | "idle"> = {
  triage: "idle", todo: "idle", scheduled: "idle", ready: "ready",
  running: "live", blocked: "error", review: "warn", done: "ready",
};

function ageLabel(seconds: number | null | undefined): string {
  if (seconds == null) return "unbekannt";
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

function taskProgress(task: BoardTask): { label: string; pct: number | null } {
  if (task.progress && task.progress.total > 0) {
    return {
      label: `${task.progress.done}/${task.progress.total} · ${Math.round((task.progress.done / task.progress.total) * 100)}%`,
      pct: Math.round((task.progress.done / task.progress.total) * 100),
    };
  }
  if (task.status === "running") return { label: "Worker aktiv", pct: 50 };
  if (task.status === "review") return { label: "Verifier-Gate", pct: 80 };
  if (task.status === "ready") return { label: "Dispatch-Warteschlange", pct: 35 };
  if (task.status === "scheduled") return { label: "Freigabe geplant", pct: 25 };
  if (task.status === "todo") return { label: "Plan bereit", pct: 15 };
  if (task.status === "triage") return { label: "Capture", pct: 5 };
  if (task.status === "blocked") return { label: "Blockiert", pct: null };
  return { label: "Terminal", pct: 100 };
}

function taskActivity(task: BoardTask, worker?: Worker): string {
  if (worker?.last_heartbeat_note) return worker.last_heartbeat_note;
  if (task.latest_summary) return task.latest_summary;
  if (task.status === "blocked" && task.block_reason) return task.block_reason;
  return stageGuard(task.status) ?? "Wartet auf Operator-Entscheidung.";
}

function taskRisk(task: BoardTask, worker: Worker | undefined, now: number): { label: string; tone: "red" | "amber" | "cyan" | "emerald" | "zinc" } {
  if (task.status === "blocked") return { label: task.block_reason || "Blocker pruefen", tone: "red" };
  if ((task.auto_retry_count ?? 0) > 0) return { label: `Retry ${task.auto_retry_count}`, tone: "amber" };
  if (task.status === "running" && !worker) return { label: "Worker-Signal fehlt", tone: "amber" };
  const heartbeatAge = worker?.last_heartbeat_at ? now - worker.last_heartbeat_at : null;
  if (heartbeatAge != null && heartbeatAge > 300) return { label: `Heartbeat ${ageLabel(heartbeatAge)} alt`, tone: "amber" };
  if (task.status === "review") return { label: "Review beobachten", tone: "cyan" };
  if (task.status === "ready") return { label: "Dispatcher naechster Schritt", tone: "emerald" };
  return { label: "Im Fluss", tone: "zinc" };
}

function LifecycleLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      className="inline-flex min-h-8 items-center gap-1 rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft transition hover:border-[var(--hc-accent-border)] hover:text-white"
    >
      {children}
    </a>
  );
}

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
  worker,
  now,
  busy,
  error,
  onAct,
  onClearError,
  onSelectTask,
}: {
  task: BoardTask;
  worker?: Worker;
  now: number;
  busy: boolean;
  error?: string;
  onAct: (action: StageAction) => void;
  onClearError: () => void;
  onSelectTask?: (taskId: string) => void;
}) {
  const [pending, setPending] = useState<StageAction | null>(null);
  const actions = stageActions(task.status);
  const isReview = task.status === "review";
  const progress = taskProgress(task);
  const risk = taskRisk(task, worker, now);
  const riskColor = TONE_HEX[risk.tone];
  const heartbeatAge = worker?.last_heartbeat_at ? now - worker.last_heartbeat_at : null;
  const startedAge = task.age?.started_age_seconds ?? null;
  const createdAge = task.age?.created_age_seconds ?? null;
  const activityAge = heartbeatAge ?? startedAge ?? createdAge;

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
          <p className="line-clamp-2 text-xs hc-soft">{taskActivity(task, worker)}</p>
        </div>

        <div className="flex shrink-0 flex-col items-start gap-1.5 sm:max-w-[24rem] sm:items-end">
          <div className="flex flex-wrap items-center gap-1.5 sm:justify-end">
            <span
              className="inline-flex min-h-7 items-center rounded-full border px-2 text-[0.68rem] font-medium"
              style={{ borderColor: `${riskColor}55`, color: riskColor, background: `${riskColor}12` }}
            >
              {risk.label}
            </span>
            <span className="hc-mono text-[0.68rem] hc-dim">
              {worker ? `${worker.profile} · ${worker.run_id}` : "kein Worker"}
            </span>
            {heartbeatAge != null ? (
              <span className="inline-flex items-center gap-1 hc-mono text-[0.68rem] text-[var(--hc-emerald)]">
                <HeartPulse className="h-3 w-3" />{ageLabel(heartbeatAge)}
              </span>
            ) : null}
          </div>
          <div className="flex w-full min-w-[12rem] items-center gap-2">
            <div className="hc-stage-rail min-w-0 flex-1">
              <i style={{ width: `${progress.pct ?? 0}%` }} />
            </div>
            <span className="hc-mono shrink-0 text-[0.68rem] hc-dim">{progress.label}</span>
          </div>
          <p className="hc-mono text-[0.68rem] hc-dim">
            letzte Aktivitaet {ageLabel(activityAge)} · {task.root_id ? `Root ${task.root_id}` : task.tenant || "ohne Projekt"}
          </p>
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
              {actions.length === 0 && stageGuard(task.status) ? (
                <span className="text-xs hc-dim">{stageGuard(task.status)}</span>
              ) : null}
            </div>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <LifecycleLink href={`/control/flow?task=${encodeURIComponent(task.id)}`}>
          <Link2 className="h-3.5 w-3.5" />Details
        </LifecycleLink>
        <button
          type="button"
          onClick={() => onSelectTask?.(task.id)}
          className="inline-flex min-h-8 items-center gap-1 rounded-full border border-[var(--hc-border-strong)] px-2.5 text-xs hc-soft transition hover:border-[var(--hc-accent-border)] hover:text-white"
        >
          <FileText className="h-3.5 w-3.5" />Receipt
        </button>
        <LifecycleLink href={`/control/flow?task=${encodeURIComponent(task.id)}#flow-section-recovery`}>Alerts</LifecycleLink>
        {task.status === "blocked" ? (
          <LifecycleLink href="#flow-section-blocked">Remediation</LifecycleLink>
        ) : null}
      </div>

      {isReview && !pending ? (
        <p className="mt-2 flex items-center gap-1.5 text-[0.7rem] hc-dim">
          <ShieldCheck className="h-3.5 w-3.5 text-cyan-300" />
          Prüfung: Verifier-Gate läuft automatisch — Ausliefern nimmt manuell ab, Nacharbeit schickt zurück.
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

export function FleetPipeline({
  tasks,
  workers = [],
  now,
  reload,
  onSelectTask,
}: {
  tasks: BoardTask[];
  workers?: Worker[];
  now: number;
  reload: () => void | Promise<void>;
  onSelectTask?: (taskId: string) => void;
}) {
  const pipeline = useMemo(() => buildPipeline(tasks), [tasks]);
  const workersByTask = useMemo(() => new Map(workers.map((worker) => [worker.task_id, worker])), [workers]);
  const { busyId, errorById, run, clearError } = useTaskAction(reload);
  const maxCount = Math.max(1, ...pipeline.buckets.map((b) => b.count));

  const onAct = (taskId: string, action: StageAction) => {
    void run(taskId, action.target, action.key === "rework" ? { block_reason: "Operator-Nacharbeit aus dem Fleet-Tab" } : undefined);
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
        {pipeline.active.length === 0 ? (
          <FleetEmptyState ok title={de.fleet.pipelineEmptyTitle} desc={de.fleet.pipelineEmptyDesc} />
        ) : (
          <ul className="space-y-2">
            {pipeline.active.map((task) => (
              <ActionRow
                key={task.id}
                task={task as BoardTask}
                worker={workersByTask.get(task.id)}
                now={now}
                busy={busyId === task.id}
                error={errorById[task.id] || undefined}
                onAct={(action) => onAct(task.id, action)}
                onClearError={() => clearError(task.id)}
                onSelectTask={onSelectTask}
              />
            ))}
          </ul>
        )}
      </div>
    </FleetPanel>
  );
}
