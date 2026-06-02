import { useState } from "react";
import { Clock, FileText, Pause, Play, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useCronObservability, useCronOutput } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { Led, StatusPill, ToneCallout } from "../components/atoms";
import { fmtAge, fmtClock, nowSec } from "../lib/derive";
import type { CronJob, ToneName } from "../lib/types";
import type { DotKind } from "../lib/tones";
import type { Density } from "../hooks/useDensity";

const t = de.crons;

/** Cron timestamps arrive as ISO-8601 strings or epoch seconds → epoch seconds. */
function toEpoch(value: number | string | null): number | null {
  if (value == null) return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : null;
}

function fmtWhen(value: number | string | null): string {
  const epoch = toEpoch(value);
  return epoch == null ? t.never : fmtClock(epoch);
}

type JobTone = { tone: ToneName; dot: DotKind; label: string };

export function jobTone(job: CronJob): JobTone {
  if (job.last_delivery_error) return { tone: "red", dot: "error", label: t.deliveryError };
  if (job.last_error) return { tone: "red", dot: "error", label: t.runError };
  if (!job.enabled) return { tone: "amber", dot: "warn", label: t.disabled };
  if (job.state === "paused" || job.paused_at) return { tone: "amber", dot: "warn", label: t.paused };
  return { tone: "emerald", dot: "live", label: job.last_status || t.scheduled };
}

function CronJobCard({ job, controls, output }: {
  job: CronJob;
  controls: ReturnType<typeof useCronObservability>;
  output: ReturnType<typeof useCronOutput>;
}) {
  const [open, setOpen] = useState(false);
  const status = jobTone(job);
  const now = nowSec();
  const lastRunEpoch = toEpoch(job.last_run_at);
  const busy = controls.busyJob === job.id;
  const ref = { id: job.id, profile: job.profile };
  const loaded = output.outputById[job.id];
  const outErr = output.errorById[job.id];
  const outLoading = output.loadingId === job.id;
  const runCount = job.latest_output?.run_count ?? 0;

  const toggleOutput = () => {
    const next = !open;
    setOpen(next);
    if (next && !loaded && !outLoading) void output.load(ref);
  };

  return (
    <div className="rounded-xl border border-[var(--hc-border)] bg-[var(--hc-panel)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Led kind={status.dot} />
            <h3 className="truncate text-sm font-semibold text-white">{job.name || job.id}</h3>
            <StatusPill tone={status.tone} label={status.label} size="sm" />
          </div>
          <p className="mt-1 text-xs hc-soft">
            {t.schedule}: <span className="hc-mono">{job.schedule_display || "—"}</span>
            {job.profile && job.profile !== "default" ? <span className="ml-2 rounded bg-white/5 px-1.5 py-0.5">{job.profile}</span> : null}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {job.enabled && job.state !== "paused" && !job.paused_at ? (
            <Button size="xs" ghost disabled={busy} onClick={() => { if (window.confirm(t.confirmPause)) void controls.pause(ref); }}>
              <Pause className="h-3.5 w-3.5" />{t.actions.pause}
            </Button>
          ) : (
            <Button size="xs" ghost disabled={busy} onClick={() => { if (window.confirm(t.confirmResume)) void controls.resume(ref); }}>
              <Play className="h-3.5 w-3.5" />{t.actions.resume}
            </Button>
          )}
          <Button size="xs" disabled={busy} onClick={() => { if (window.confirm(t.confirmTrigger)) void controls.trigger(ref); }}>
            {t.actions.trigger}
          </Button>
        </div>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 text-xs hc-soft sm:grid-cols-3">
        <div><span className="hc-dim">{t.nextRun}</span><br /><span className="hc-mono">{fmtWhen(job.next_run_at)}</span></div>
        <div><span className="hc-dim">{t.lastRun}</span><br /><span className="hc-mono">{lastRunEpoch != null ? `${fmtClock(lastRunEpoch)} (${fmtAge(lastRunEpoch, now)})` : t.never}</span></div>
        <div><span className="hc-dim">{t.deliver}</span><br /><span className="hc-mono">{job.deliver || "—"}</span></div>
      </div>

      {job.last_error ? <div className="mt-2"><ToneCallout tone="red">{job.last_error}</ToneCallout></div> : null}
      {job.last_delivery_error ? <div className="mt-2"><ToneCallout tone="red">{t.deliveryError}: {job.last_delivery_error}</ToneCallout></div> : null}

      <div className="mt-3 border-t border-[var(--hc-border)] pt-3">
        <button type="button" onClick={toggleOutput} className="inline-flex items-center gap-2 text-xs hc-soft hover:text-white">
          <FileText className="h-3.5 w-3.5" />
          {t.lastResult}
          {runCount > 0 ? <span className="hc-dim">· {t.runCount(runCount)}</span> : <span className="hc-dim">· {t.noOutput}</span>}
        </button>
        {open ? (
          <div className="mt-2">
            {outLoading ? <p className="text-xs hc-dim">{t.loading}</p> : null}
            {outErr ? <ToneCallout tone="red">{t.outputError}</ToneCallout> : null}
            {loaded && !outLoading ? (
              loaded.text ? (
                <div className="rounded-lg border border-[var(--hc-border)] bg-black/25">
                  <div className="flex items-center justify-between border-b border-[var(--hc-border)] px-3 py-2 text-xs hc-soft">
                    <span className="hc-mono">{loaded.filename}{loaded.truncated ? ` ${t.outputTruncated}` : ""}</span>
                    <button type="button" aria-label={t.close} onClick={() => setOpen(false)}><X className="h-3.5 w-3.5" /></button>
                  </div>
                  <pre className="max-h-96 max-w-full overflow-auto whitespace-pre-wrap break-words p-3 text-xs leading-5 hc-mono text-zinc-200">{loaded.text}</pre>
                </div>
              ) : <p className="text-xs hc-dim">{t.noOutput}</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}

export function CronView(_props: { density: Density }) {
  const controls = useCronObservability();
  const output = useCronOutput();
  const data = controls.data;
  const jobs = data?.jobs ?? [];
  const gatewayRunning = data?.gateway.running ?? false;

  return (
    <div className="space-y-4">
      <header>
        <p className="hc-eyebrow">{t.eyebrow}</p>
        <h2 className="mt-1 text-xl font-semibold text-white">{t.title}</h2>
        <p className="mt-1 text-sm hc-soft">{t.subtitle}</p>
      </header>

      {!gatewayRunning ? <ToneCallout tone="red">{t.gatewayDown}</ToneCallout> : null}
      {controls.error ? <ToneCallout tone="amber">{t.error}</ToneCallout> : null}
      {controls.actionError ? <ToneCallout tone="red">{t.actionFailed}: {controls.actionError}</ToneCallout> : null}

      <div className="flex items-center gap-2 text-xs hc-soft">
        <Clock className="h-4 w-4" />
        {t.jobCount(jobs.length)}
      </div>

      {jobs.length === 0 && !controls.loading ? (
        <ToneCallout tone="zinc">{t.empty}</ToneCallout>
      ) : (
        <div className="space-y-3">
          {jobs.map((job) => (
            <CronJobCard key={`${job.profile}:${job.id}`} job={job} controls={controls} output={output} />
          ))}
        </div>
      )}
    </div>
  );
}
