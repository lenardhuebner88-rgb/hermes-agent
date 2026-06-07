import { useState } from "react";
import { AlertTriangle, Eye, Lock, RotateCw, Send, Zap } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import {
  STUCK_HEARTBEAT_S,
  fmtDur,
  fmtMB,
  workerHeartbeatAge,
  workerRemaining,
  workerRuntime,
} from "../lib/derive";
import { profileLabel, taskStatusLabel } from "../lib/tones";
import { de } from "../i18n/de";
import type { Density } from "../hooks/useDensity";
import type { Worker, WorkerHealth } from "../lib/types";
import { MeterBar, StatusPill, ToneCallout } from "./atoms";
import { Stat, Text } from "./primitives";

interface Props {
  worker: Worker;
  health: WorkerHealth;
  density: Density;
  now: number;
  inspectLoading?: boolean;
  onInspect: (runId: string) => void;
  onAction?: (runId: string, action: string) => void | Promise<void>;
  actionBusy?: boolean;
}

function actionIcon(key: string) {
  if (key === "unlock") return <Lock className="h-4 w-4" />;
  if (key === "restart") return <RotateCw className="h-4 w-4" />;
  if (key === "dispatch") return <Send className="h-4 w-4" />;
  return <Zap className="h-4 w-4" />;
}

export function WorkerCard({ worker, health, density, now, inspectLoading, onInspect, onAction, actionBusy }: Props) {
  const [confirming, setConfirming] = useState(false);
  const inspect = worker.inspect ?? null;
  const remaining = workerRemaining(worker, now);
  const hasHeartbeat = worker.last_heartbeat_at > 0;
  const heartbeatAge = workerHeartbeatAge(worker, now);
  const primary = health.key === "blocked" ? "unlock" : health.key === "stuck" ? "nudge" : health.key === "offline" ? "restart" : "dispatch";
  const primaryLabel = de.worker.actions[primary];
  const stuckReason = hasHeartbeat ? de.worker.stuckReason(fmtDur(heartbeatAge)) : de.worker.expiredReason;
  const problemText = worker.block_reason || (health.key === "offline" ? de.worker.offlineReason : health.key === "stuck" ? stuckReason : null);

  return (
    <article className={cn("hc-surface-card space-y-4 p-4", density === "compact" && "p-3")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded-full border border-[var(--hc-accent-border)] bg-[var(--hc-accent-wash)] px-2 py-0.5 text-xs text-[var(--hc-accent-text)]">{profileLabel[worker.profile] ?? worker.profile}</span>
            <span className="rounded-full border border-white/10 px-2 py-0.5 text-xs hc-soft">{taskStatusLabel[worker.task_status] ?? worker.task_status}</span>
          </div>
          <h3 className="line-clamp-2 text-base font-semibold leading-snug text-white">{worker.task_title}</h3>
        </div>
        <StatusPill tone={health.tone} label={health.label} dot={health.dot} />
      </div>

      <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
        <Stat label={de.worker.runtime} value={fmtDur(workerRuntime(worker, now))} />
        <Stat label={de.worker.heartbeat} value={hasHeartbeat ? fmtDur(heartbeatAge) : "—"} tone={hasHeartbeat && heartbeatAge > STUCK_HEARTBEAT_S ? "amber" : undefined} />
        <Stat label={de.worker.remaining} value={remaining <= 0 ? "0s" : fmtDur(remaining)} tone={remaining <= 0 ? "amber" : undefined} />
        <Stat label="PID" value={String(worker.worker_pid || "-")} />
      </div>

      {inspect ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <MeterBar label="CPU" value={inspect.cpu_percent} max={100} tone={inspect.cpu_percent > 80 ? "amber" : "cyan"} />
          <MeterBar label="RAM" value={inspect.rss / 1048576} max={2048} tone={inspect.rss > 1536 * 1048576 ? "amber" : "cyan"} />
          <Text variant="label" className="hc-soft sm:col-span-2">{de.worker.process}: {inspect.status} · {fmtMB(inspect.rss)} · {inspect.num_threads} Threads · {inspect.num_fds} FDs</Text>
        </div>
      ) : null}

      {problemText ? <ToneCallout tone={health.tone}><AlertTriangle className="mr-2 inline h-4 w-4" />{problemText}</ToneCallout> : null}

      <div className="flex flex-wrap gap-2">
        <Button outlined size="sm" onClick={() => onInspect(worker.run_id)} disabled={inspectLoading} prefix={inspectLoading ? <Spinner /> : <Eye className="h-4 w-4" />}>
          {de.worker.actions.inspect}
        </Button>
        <Button outlined size="sm" disabled prefix={<Eye className="h-4 w-4" />}>{de.worker.actions.details}</Button>
        {onAction && !confirming ? (
          <Button outlined size="sm" disabled={actionBusy} onClick={() => setConfirming(true)} prefix={actionBusy ? <Spinner /> : actionIcon(primary)}>
            {primaryLabel}
          </Button>
        ) : null}
        {onAction && confirming ? (
          <>
            <Button
              size="sm"
              disabled={actionBusy}
              onClick={async () => { await onAction(worker.run_id, primary); setConfirming(false); }}
              prefix={actionBusy ? <Spinner /> : actionIcon(primary)}
            >
              {primaryLabel} · {de.worker.actions.confirm}
            </Button>
            <Button outlined size="sm" disabled={actionBusy} onClick={() => setConfirming(false)}>
              {de.worker.actions.cancel}
            </Button>
          </>
        ) : null}
      </div>
      {onAction && confirming ? (
        <Text variant="label" className="hc-soft">{primary === "restart" ? de.worker.restartHint : de.worker.confirmHint}</Text>
      ) : null}
    </article>
  );
}
