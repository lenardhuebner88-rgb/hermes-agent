import { useState, type ReactNode } from "react";
import { Clock, FileText, Pause, Play, TriangleAlert, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { useCronObservability, useCronOutput } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { StaleBadge } from "../components/atoms";
import { Disclosure, Eyebrow } from "../components/primitives";
import { FleetEmptyState, FleetPanel, KpiTile, SignalChip, signalToneFromLegacy } from "../components/leitstand";
import { fmtAge, fmtClock, nowSec } from "../lib/derive";
import type { CronJob } from "../lib/types";
import type { Density } from "../hooks/useDensity";
import { jobTone } from "./CronView.helpers";

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


function CronJobCard({ job, controls, output }: {
  job: CronJob;
  controls: ReturnType<typeof useCronObservability>;
  output: ReturnType<typeof useCronOutput>;
}) {
  const [open, setOpen] = useState(false);
  // Inline-Zwei-Schritt-Bestätigung (LanesView-Muster) statt window.confirm.
  // `pending` ist Komponenten-State → pro Karte gescoped: das Schärfen von Job A
  // schärft Job B nicht. Es ist immer höchstens eine Aktion gleichzeitig scharf.
  const [pending, setPending] = useState<"pause" | "resume" | "trigger" | null>(null);
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

  // Eine scharfe Aktion → Inline-Bestätigungszeile (Frage + Bestätigen + Abbrechen),
  // kein Browser-Dialog. Abbrechen setzt nur `pending` zurück und feuert keine API.
  const confirmRow = (question: string, confirmLabel: string, icon: ReactNode, run: () => void) => (
    <span className="inline-flex flex-wrap items-center justify-end gap-2">
      <span className="min-w-0 break-words text-micro text-ink-2">{question}</span>
      <button
        type="button"
        disabled={busy}
        onClick={() => { run(); setPending(null); }}
        className="inline-flex min-h-12 items-center gap-1 rounded-card border border-line bg-surface-2 px-3 text-sec text-ink disabled:opacity-40"
      >
        {icon}{busy ? "…" : confirmLabel}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => setPending(null)}
        className="inline-flex min-h-12 items-center rounded-card border border-line px-3 text-sec text-ink-2 disabled:opacity-40"
      >
        {de.worker.actions.cancel}
      </button>
    </span>
  );

  const eyebrow = (
    <span className="inline-flex min-w-0 items-center gap-2">
      <span className="truncate normal-case tracking-normal text-ink">{job.name || job.id}</span>
      <SignalChip tone={signalToneFromLegacy(status.tone)} label={status.label} />
    </span>
  );
  const meta = (
    <span className="inline-flex items-center gap-2">
      <span className="font-data tabular-nums">{job.schedule_display || "—"}</span>
      {job.profile && job.profile !== "default" ? <span className="rounded-card bg-surface-2 px-1.5 py-0.5 font-data">{job.profile}</span> : null}
    </span>
  );

  return (
    <FleetPanel eyebrow={eyebrow} meta={meta}>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {job.enabled && job.state !== "paused" && !job.paused_at ? (
          pending === "pause" ? (
            confirmRow(t.confirmPause, t.actions.pause, <Pause className="h-3.5 w-3.5" />, () => void controls.pause(ref))
          ) : (
            <Button size="xs" ghost className="min-h-12" disabled={busy} onClick={() => setPending("pause")}>
              <Pause className="h-3.5 w-3.5" />{t.actions.pause}
            </Button>
          )
        ) : (
          pending === "resume" ? (
            confirmRow(t.confirmResume, t.actions.resume, <Play className="h-3.5 w-3.5" />, () => void controls.resume(ref))
          ) : (
            <Button size="xs" ghost className="min-h-12" disabled={busy} onClick={() => setPending("resume")}>
              <Play className="h-3.5 w-3.5" />{t.actions.resume}
            </Button>
          )
        )}
        {pending === "trigger" ? (
          confirmRow(t.confirmTrigger, t.actions.trigger, null, () => void controls.trigger(ref))
        ) : (
          <Button size="xs" className="min-h-12" disabled={busy} onClick={() => setPending("trigger")}>
            {t.actions.trigger}
          </Button>
        )}
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
        <KpiTile label={t.nextRun} value={fmtWhen(job.next_run_at)} />
        <KpiTile label={t.lastRun} value={lastRunEpoch != null ? `${fmtClock(lastRunEpoch)} (${fmtAge(lastRunEpoch, now)})` : t.never} />
        <KpiTile label={t.deliver} value={job.deliver || "—"} />
      </div>

      {job.last_error ? <div className="mt-2 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{job.last_error}</div> : null}
      {job.last_delivery_error ? <div className="mt-2 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.deliveryError}: {job.last_delivery_error}</div> : null}

      <div className="mt-3 border-t border-line pt-3">
        <Disclosure
          open={open}
          onToggle={toggleOutput}
          summary={
            <span className="inline-flex items-center gap-2 text-sec text-ink-2">
              <FileText className="h-3.5 w-3.5" />
              {t.lastResult}
              {runCount > 0 ? <span className="text-ink-3">· {t.runCount(runCount)}</span> : <span className="text-ink-3">· {t.noOutput}</span>}
            </span>
          }
        >
          {outLoading ? <p className="text-sec text-ink-3">{t.loading}</p> : null}
          {outErr ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.outputError}</div> : null}
          {loaded && !outLoading ? (
            loaded.text ? (
              <div className="rounded-card border border-line bg-surface-2">
                <div className="flex items-center justify-between border-b border-line px-3 py-2 text-sec text-ink-2">
                  <span className="font-data">{loaded.filename}{loaded.truncated ? ` ${t.outputTruncated}` : ""}</span>
                  <button type="button" aria-label={t.close} className="inline-flex min-h-12 min-w-12 items-center justify-center" onClick={() => setOpen(false)}><X className="h-3.5 w-3.5" /></button>
                </div>
                <pre className="max-h-96 max-w-full overflow-auto whitespace-pre-wrap break-words p-3 font-data text-sec leading-5 text-ink">{loaded.text}</pre>
              </div>
            ) : <p className="text-sec text-ink-3">{t.noOutput}</p>
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
  const now = nowSec();

  return (
    <div className="space-y-5">
      <header>
        <Eyebrow>{t.eyebrow}</Eyebrow>
        <div className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="font-display text-h2 font-semibold text-ink">{t.title}</h2>
          <span className="text-sec text-ink-2">{t.subtitle}</span>
          <StaleBadge isStale={controls.isStale} lastUpdated={controls.lastUpdated} errorObj={controls.errorObj} error={controls.error} now={now} />
        </div>
      </header>

      {!gatewayRunning ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.gatewayDown}</div> : null}
      {controls.error ? <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.error}</div> : null}
      {controls.actionError ? <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"><TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />{t.actionFailed}: {controls.actionError}</div> : null}

      <div className="flex items-center gap-2 text-sec text-ink-2">
        <Clock className="h-4 w-4" />
        {t.jobCount(jobs.length)}
      </div>

      {jobs.length === 0 && !controls.loading ? (
        <FleetEmptyState title={t.empty} desc={t.emptyDesc} />
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
