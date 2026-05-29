import { AlertTriangle, Eye, Lock, RotateCw, Send, Zap } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import {
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

interface Props {
  worker: Worker;
  health: WorkerHealth;
  density: Density;
  now: number;
  inspectLoading?: boolean;
  onInspect: (runId: string) => void;
}

function actionIcon(key: string) {
  if (key === "unlock") return <Lock className="h-4 w-4" />;
  if (key === "restart") return <RotateCw className="h-4 w-4" />;
  if (key === "dispatch") return <Send className="h-4 w-4" />;
  return <Zap className="h-4 w-4" />;
}

export function WorkerCard({ worker, health, density, now, inspectLoading, onInspect }: Props) {
  const inspect = worker.inspect ?? null;
  const remaining = workerRemaining(worker, now);
  const heartbeatAge = workerHeartbeatAge(worker, now);
  const primary = health.key === "blocked" ? "unlock" : health.key === "stuck" ? "nudge" : health.key === "offline" ? "restart" : "dispatch";
  const primaryLabel = de.worker.actions[primary];
  const problemText = worker.block_reason || (health.key === "offline" ? de.worker.offlineReason : health.key === "stuck" ? de.worker.stuckReason(fmtDur(heartbeatAge)) : null);

  return (
    <article className={cn("hc-card space-y-4 p-4", density === "compact" && "p-3")}>
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
        <Metric label={de.worker.runtime} value={fmtDur(workerRuntime(worker, now))} />
        <Metric label={de.worker.heartbeat} value={fmtDur(heartbeatAge)} warn={heartbeatAge > 90} />
        <Metric label={de.worker.remaining} value={remaining <= 0 ? "0s" : fmtDur(remaining)} warn={remaining <= 0} />
        <Metric label="PID" value={String(worker.worker_pid || "-")} />
      </div>

      {inspect ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <MeterBar label="CPU" value={inspect.cpu_percent} max={100} tone={inspect.cpu_percent > 80 ? "amber" : "cyan"} />
          <MeterBar label="RAM" value={inspect.rss / 1048576} max={2048} tone={inspect.rss > 1536 * 1048576 ? "amber" : "cyan"} />
          <p className="text-xs hc-soft sm:col-span-2">{de.worker.process}: {inspect.status} ? {fmtMB(inspect.rss)} ? {inspect.num_threads} Threads ? {inspect.num_fds} FDs</p>
        </div>
      ) : null}

      {problemText ? <ToneCallout tone={health.tone}><AlertTriangle className="mr-2 inline h-4 w-4" />{problemText}</ToneCallout> : null}

      <div className="flex flex-wrap gap-2">
        <Button outlined size="sm" onClick={() => onInspect(worker.run_id)} disabled={inspectLoading} prefix={inspectLoading ? <Spinner /> : <Eye className="h-4 w-4" />}>
          {de.worker.actions.inspect}
        </Button>
        <Button outlined size="sm" disabled prefix={<Eye className="h-4 w-4" />}>{de.worker.actions.details}</Button>
        <Button outlined size="sm" disabled prefix={actionIcon(primary)}>{primaryLabel} ? TODO</Button>
      </div>
    </article>
  );
}

function Metric({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return <div className={cn("rounded-lg border border-white/10 bg-white/[.03] px-3 py-2", warn && "border-amber-500/30 bg-amber-500/10 text-amber-100")}><p className="text-xs hc-dim">{label}</p><p className="hc-mono text-sm font-semibold">{value}</p></div>;
}
