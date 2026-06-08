import { useState } from "react";
import { Clock, FileText, Pause, Play, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useCronObservability, useCronOutput } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { Led, StatusPill, ToneCallout } from "../components/atoms";
import { Disclosure, Stat } from "../components/primitives";
import { FleetEmptyState, FleetPanel } from "../components/fleet/atoms";
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

  const eyebrow = (
    <span className="inline-flex min-w-0 items-center gap-2">
      <Led kind={status.dot} />
      <span className="truncate normal-case tracking-normal text-white">{job.name || job.id}</span>
      <StatusPill tone={status.tone} label={status.label} size="sm" />
    </span>
  );
  const meta = (
    <span className="inline-flex items-center gap-2">
      <span className="hc-mono">{job.schedule_display || "—"}</span>
      {job.profile && job.profile !== "default" ? <span className="rounded bg-white/5 px-1.5 py-0.5 hc-mono">{job.profile}</span> : null}
    </span>
  );

  return (
    <FleetPanel eyebrow={eyebrow} meta={meta}>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {job.enabled && job.state !== "paused" && !job.paused_at ? (
          <Button size="xs" ghost className="min-h-11" disabled={busy} onClick={() => { if (window.confirm(t.confirmPause)) void controls.pause(ref); }}>
            <Pause className="h-3.5 w-3.5" />{t.actions.pause}
          </Button>
        ) : (
          <Button size="xs" ghost className="min-h-11" disabled={busy} onClick={() => { if (window.confirm(t.confirmResume)) void controls.resume(ref); }}>
            <Play className="h-3.5 w-3.5" />{t.actions.resume}
          </Button>
        )}
        <Button size="xs" className="min-h-11" disabled={busy} onClick={() => { if (window.confirm(t.confirmTrigger)) void controls.trigger(ref); }}>
          {t.actions.trigger}
        </Button>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
        <Stat label={t.nextRun} value={fmtWhen(job.next_run_at)} />
        <Stat label={t.lastRun} value={lastRunEpoch != null ? `${fmtClock(lastRunEpoch)} (${fmtAge(lastRunEpoch, now)})` : t.never} />
        <Stat label={t.deliver} value={job.deliver || "—"} />
      </div>

      {job.last_error ? <div className="mt-2"><ToneCallout tone="red">{job.last_error}</ToneCallout></div> : null}
      {job.last_delivery_error ? <div className="mt-2"><ToneCallout tone="red">{t.deliveryError}: {job.last_delivery_error}</ToneCallout></div> : null}

      <div className="mt-3 border-t border-[var(--hc-border)] pt-3">
        <Disclosure
          open={open}
          onToggle={toggleOutput}
          summary={
            <span className="inline-flex items-center gap-2 text-xs hc-soft">
              <FileText className="h-3.5 w-3.5" />
              {t.lastResult}
              {runCount > 0 ? <span className="hc-dim">· {t.runCount(runCount)}</span> : <span className="hc-dim">· {t.noOutput}</span>}
            </span>
          }
        >
          {outLoading ? <p className="text-xs hc-dim">{t.loading}</p> : null}
          {outErr ? <ToneCallout tone="red">{t.outputError}</ToneCallout> : null}
          {loaded && !outLoading ? (
            loaded.text ? (
              <div className="rounded-lg border border-[var(--hc-border)] bg-black/25">
                <div className="flex items-center justify-between border-b border-[var(--hc-border)] px-3 py-2 text-xs hc-soft">
                  <span className="hc-mono">{loaded.filename}{loaded.truncated ? ` ${t.outputTruncated}` : ""}</span>
                  <button type="button" aria-label={t.close} className="inline-flex min-h-11 items-center" onClick={() => setOpen(false)}><X className="h-3.5 w-3.5" /></button>
                </div>
                <pre className="max-h-96 max-w-full overflow-auto whitespace-pre-wrap break-words p-3 text-xs leading-5 hc-mono text-zinc-200">{loaded.text}</pre>
              </div>
            ) : <p className="text-xs hc-dim">{t.noOutput}</p>
          ) : null}
        </Disclosure>
      </div>
    </FleetPanel>
  );
}

export function CronView(_props: { density: Density }) {
  const controls = useCronObservability();
  const output = useCronOutput();
  const data = controls.data;
  const jobs = data?.jobs ?? [];
  const gatewayRunning = data?.gateway.running ?? false;

  return (
    <div className="space-y-5">
      <header>
        <p className="hc-eyebrow">{t.eyebrow}</p>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="hc-type-title text-white">{t.title}</h2>
          <span className="hc-mono text-sm hc-dim">{t.subtitle}</span>
        </div>
      </header>

      {!gatewayRunning ? <ToneCallout tone="red">{t.gatewayDown}</ToneCallout> : null}
      {controls.error ? <ToneCallout tone="amber">{t.error}</ToneCallout> : null}
      {controls.actionError ? <ToneCallout tone="red">{t.actionFailed}: {controls.actionError}</ToneCallout> : null}

      <div className="flex items-center gap-2 text-xs hc-soft">
        <Clock className="h-4 w-4" />
        {t.jobCount(jobs.length)}
      </div>

      {jobs.length === 0 && !controls.loading ? (
        <FleetEmptyState ok title={t.empty} desc={t.subtitle} />
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
