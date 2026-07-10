import { AlertTriangle, Bot, Check, Clock, RotateCcw, SkipForward } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { type PulseEvent, type PulseKind, type PulseSummary } from "../../lib/pulse";
import { fmtAge, fmtClockTime } from "../../lib/derive";
import { FleetPanel, FleetEmptyState, KpiTile, SignalLabel, signalToneFromLegacy } from "../../components/leitstand";
import { SkeletonCard } from "../../components/primitives";
import { type PulseData } from "../../hooks/usePulseData";
import { de } from "../../i18n/de";

// PulseTally + PulseTimeline leben seit dem Abriss (S5) hier unter views/system/,
// weil die eigenständige Puls-Route zum System-Redirect wurde. Die System-View
// rahmt PulseTally im Kopf und PulseTimeline als "Ereignisse"-Sektion.

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
 * gruppierte Ereignisliste), ohne Hero. Die System-Fusion rahmt sie als
 * "Ereignisse"-Sektion.
 */
export function PulseTimeline({ data }: { data: PulseData }) {
  const navigate = useNavigate();
  if (data.loading) return <SkeletonCard rows={4} />;
  if (data.events.length === 0) {
    return <FleetEmptyState title={de.pulse.empty} desc={de.pulse.emptyHint(data.windowHours)} />;
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
      <KpiTile label={de.pulse.statRuns} value={summary.runs} dot={summary.runs > 0 ? "live" : "idle"} />
      <KpiTile label={de.pulse.statApplied} value={summary.applied} dot={summary.applied > 0 ? "ready" : "idle"} />
      {summary.reverted > 0 ? <KpiTile label={de.pulse.statReverted} value={summary.reverted} dot="warn" /> : null}
      <KpiTile
        label={de.pulse.statCrons}
        value={summary.crons}
        suffix={summary.cronErrors > 0 ? de.pulse.cronErrorSuffix(summary.cronErrors) : undefined}
        dot={summary.cronErrors > 0 ? "error" : "live"}
      />
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
  return (
    <li>
      <button type="button" onClick={onOpen} className="flex min-h-12 w-full items-start gap-3 rounded-card border border-line bg-surface-2 px-3 py-2.5 text-left transition hover:border-live hover:bg-surface-3">
        <span className="mt-0.5 grid size-8 shrink-0 place-items-center rounded-full border border-line bg-surface-1 text-brand">
          <Icon className="h-4 w-4" />
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2">
            <span className="truncate text-sec font-medium text-ink">{event.title}</span>
            <SignalLabel tone={signalToneFromLegacy(event.tone)} label={Meta.label} />
          </span>
          {event.detail ? <span className="mt-0.5 line-clamp-1 block text-sec text-ink-2">{event.detail}</span> : null}
        </span>
        <span className="shrink-0 text-right">
          <span className="block font-data text-sec tabular-nums text-ink">{fmtClockTime(event.at)}</span>
          <span className="block font-data text-micro tabular-nums text-ink-3">{de.pulse.ago(fmtAge(event.at, now))}</span>
        </span>
      </button>
    </li>
  );
}
