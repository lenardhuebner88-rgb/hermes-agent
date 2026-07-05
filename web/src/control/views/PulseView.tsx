import { AlertTriangle, Bot, Check, Clock, RotateCcw, SkipForward } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { type PulseEvent, type PulseKind, type PulseSummary } from "../lib/pulse";
import { fmtAge, fmtClockTime } from "../lib/derive";
import { TONE_HEX } from "../lib/tones";
import { FleetPanel, FleetPod, FleetEmptyState } from "../components/fleet/atoms";
import { Hero } from "../components/Hero";
import { SkeletonCard } from "../components/primitives";
import { StaleBadge } from "../components/atoms";
import { usePulseData, type PulseData, type PulseSource } from "../hooks/usePulseData";
import { de } from "../i18n/de";

const kindMeta: Record<PulseKind, { icon: React.ComponentType<{ className?: string }>; label: string }> = {
  run: { icon: Bot, label: de.pulse.kindRun },
  applied: { icon: Check, label: de.pulse.kindApplied },
  reverted: { icon: RotateCcw, label: de.pulse.kindReverted },
  skipped: { icon: SkipForward, label: de.pulse.kindSkipped },
  "cron-ok": { icon: Clock, label: de.pulse.kindCron },
  "cron-error": { icon: AlertTriangle, label: de.pulse.kindCronError },
};

/**
 * PulseTimeline — die reine 48h-Zeitleiste (Skeleton / Leerzustand / nach Tagen
 * gruppierte Ereignisliste), ohne Hero. Die PulseView setzt ihren Hero davor;
 * die System-Fusion rahmt sie als "Ereignisse"-Sektion.
 */
export function PulseTimeline({ data }: { data: PulseData }) {
  const navigate = useNavigate();
  if (data.loading) return <SkeletonCard rows={4} />;
  if (data.events.length === 0) {
    return <FleetEmptyState ok title={de.pulse.empty} desc={de.pulse.emptyHint(data.windowHours)} />;
  }
  return (
    <div className="space-y-5">
      {data.days.map((day) => (
        <FleetPanel key={day.key} eyebrow={dayLabel(day.daysAgo, day.events[0].at)} meta={de.pulse.dayCount(day.events.length)}>
          <ol className="space-y-2">
            {day.events.map((event) => (
              <EventRow key={event.id} event={event} now={data.now} onOpen={() => navigate(event.tab)} />
            ))}
          </ol>
        </FleetPanel>
      ))}
    </div>
  );
}

/** Die 48h-Bilanz als Kachel-Zeile (Läufe / Angewandt / [Reverted] / Crons).
 *  Geteilt zwischen dem Pulse-Hero und dem fusionierten System-Kopf. */
export function PulseTally({ summary }: { summary: PulseSummary }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <FleetPod label={de.pulse.statRuns} value={summary.runs} dot={summary.runs > 0 ? "live" : "idle"} />
      <FleetPod label={de.pulse.statApplied} value={summary.applied} dot={summary.applied > 0 ? "ready" : "idle"} />
      {summary.reverted > 0 ? <FleetPod label={de.pulse.statReverted} value={summary.reverted} dot="warn" /> : null}
      <FleetPod
        label={de.pulse.statCrons}
        value={summary.crons}
        suffix={summary.cronErrors > 0 ? de.pulse.cronErrorSuffix(summary.cronErrors) : undefined}
        dot={summary.cronErrors > 0 ? "error" : "live"}
      />
    </div>
  );
}

export function PulseView(props: PulseSource) {
  const data = usePulseData(props);
  const { summary, fresh, error, now, windowHours, results, crons } = data;

  return (
    <div className="space-y-5">
      <Hero
        eyebrow={de.pulse.eyebrow}
        title={de.pulse.title}
        subtitle={de.pulse.subtitle(windowHours)}
        tone={summary.cronErrors > 0 ? "amber" : "cyan"}
        status={{
          label: error ? de.pulse.sourceError : fresh.stale ? de.pulse.staleWarn(fresh.label.replace("vor ", "")) : fresh.label,
          tone: error || fresh.stale ? "amber" : "emerald",
          dot: error ? "error" : fresh.stale ? "warn" : "live",
        }}
        action={
          <div className="flex flex-wrap justify-end gap-1.5">
            <StaleBadge isStale={results.isStale} lastUpdated={results.lastUpdated} errorObj={results.errorObj} error={results.error} now={now} />
            <StaleBadge isStale={crons.isStale} lastUpdated={crons.lastUpdated} errorObj={crons.errorObj} error={crons.error} now={now} />
          </div>
        }
      >
        {/* Tally: eine ehrliche Zeile darüber, was die Maschine geleistet hat. */}
        <PulseTally summary={summary} />
      </Hero>

      <PulseTimeline data={data} />
    </div>
  );
}

function dayLabel(daysAgo: number, sampleAt: number): string {
  if (daysAgo === 0) return de.pulse.today;
  if (daysAgo === 1) return de.pulse.yesterday;
  return new Date(sampleAt * 1000).toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
}

function EventRow({ event, now, onOpen }: { event: PulseEvent; now: number; onOpen: () => void }) {
  const Meta = kindMeta[event.kind];
  const Icon = Meta.icon;
  const hex = TONE_HEX[event.tone];
  return (
    <li>
      <button type="button" onClick={onOpen} className="hc-surface-card flex min-h-11 w-full items-start gap-3 px-3 py-2.5 text-left transition hover:bg-white/[.035]">
        <span className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-full border" style={{ borderColor: `${hex}40`, background: `${hex}1a`, color: hex }}>
          <Icon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-white">{event.title}</span>
            <span className="shrink-0 rounded-full border border-white/10 px-1.5 py-0.5 hc-type-label hc-soft">{Meta.label}</span>
          </span>
          {event.detail ? <span className="mt-0.5 line-clamp-1 block text-xs hc-soft">{event.detail}</span> : null}
        </span>
        <span className="shrink-0 text-right">
          <span className="hc-mono block text-xs text-white/80">{fmtClockTime(event.at)}</span>
          <span className="hc-mono block hc-type-label hc-dim">{de.pulse.ago(fmtAge(event.at, now))}</span>
        </span>
      </button>
    </li>
  );
}
