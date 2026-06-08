import { useMemo } from "react";
import { AlertTriangle, Bot, Check, Clock, RotateCcw, SkipForward } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { useCronObservability, useHermesRecentResults } from "../hooks/useControlData";
import { buildPulse, groupPulseByDay, summarizePulse, type PulseEvent, type PulseKind } from "../lib/pulse";
import { fmtAge, fmtClockTime, freshness, nowSec } from "../lib/derive";
import { TONE_HEX } from "../lib/tones";
import { FleetPanel, FleetPod, FleetEmptyState } from "../components/fleet/atoms";
import { SkeletonCard } from "../components/primitives";
import { de } from "../i18n/de";
import type { Proposal } from "../lib/types";

interface Props {
  proposals: Proposal[];
  proposalsLastUpdated?: number | null;
}

const kindMeta: Record<PulseKind, { icon: React.ComponentType<{ className?: string }>; label: string }> = {
  run: { icon: Bot, label: de.pulse.kindRun },
  applied: { icon: Check, label: de.pulse.kindApplied },
  reverted: { icon: RotateCcw, label: de.pulse.kindReverted },
  skipped: { icon: SkipForward, label: de.pulse.kindSkipped },
  "cron-ok": { icon: Clock, label: de.pulse.kindCron },
  "cron-error": { icon: AlertTriangle, label: de.pulse.kindCronError },
};

// Fenster des Stroms: die Quellen liefern selbst ~48h (recent-results) bzw. den
// letzten Lauf je Cron — wir zeigen, was da ist, und benennen das Fenster ehrlich.
const WINDOW_HOURS = 48;

export function PulseView({ proposals, proposalsLastUpdated }: Props) {
  const navigate = useNavigate();
  const results = useHermesRecentResults();
  const crons = useCronObservability();
  const now = nowSec();

  const events = useMemo(
    () =>
      buildPulse({
        results: results.data?.results ?? [],
        proposals,
        crons: crons.data?.jobs ?? [],
        sinceSec: now - WINDOW_HOURS * 3600,
        nowSec: now,
      }),
    [results.data, proposals, crons.data, now],
  );
  const summary = useMemo(() => summarizePulse(events), [events]);
  const days = useMemo(() => groupPulseByDay(events, now), [events, now]);

  // Frische: der älteste der drei Ströme bestimmt, wie aktuell der Puls ist.
  const fresh = freshness(
    Math.min(results.lastUpdated ?? now, crons.lastUpdated ?? now, proposalsLastUpdated ?? now),
    20000,
    now,
  );
  const loading = results.loading && crons.loading && events.length === 0;
  const error = results.error && crons.error ? (results.error ?? crons.error) : null;

  return (
    <div className="space-y-5">
      <FleetPanel
        eyebrow={de.pulse.eyebrow}
        meta={
          <span className={cn(fresh.stale ? "text-amber-200" : undefined)} title={fresh.stale ? de.pulse.stalePaused : undefined}>
            {error ? de.pulse.sourceError : fresh.stale ? de.pulse.staleWarn(fresh.label.replace("vor ", "")) : fresh.label}
          </span>
        }
      >
        <div>
          <h2 className="text-2xl font-semibold tracking-normal text-white sm:text-3xl">{de.pulse.title}</h2>
          <p className="mt-2 max-w-2xl hc-soft">{de.pulse.subtitle(WINDOW_HOURS)}</p>
        </div>

        {/* Tally: eine ehrliche Zeile darüber, was die Maschine geleistet hat. */}
        <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
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
      </FleetPanel>

      {loading ? (
        <SkeletonCard rows={4} />
      ) : events.length === 0 ? (
        <FleetEmptyState ok title={de.pulse.empty} desc={de.pulse.emptyHint(WINDOW_HOURS)} />
      ) : (
        <div className="space-y-5">
          {days.map((day) => (
            <FleetPanel key={day.key} eyebrow={dayLabel(day.daysAgo, day.events[0].at)} meta={de.pulse.dayCount(day.events.length)}>
              <ol className="space-y-2">
                {day.events.map((event) => (
                  <EventRow key={event.id} event={event} now={now} onOpen={() => navigate(event.tab)} />
                ))}
              </ol>
            </FleetPanel>
          ))}
        </div>
      )}
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
            <span className="shrink-0 rounded-full border border-white/10 px-1.5 py-0.5 text-[10px] hc-soft">{Meta.label}</span>
          </span>
          {event.detail ? <span className="mt-0.5 line-clamp-1 block text-xs hc-soft">{event.detail}</span> : null}
        </span>
        <span className="shrink-0 text-right">
          <span className="hc-mono block text-xs text-white/80">{fmtClockTime(event.at)}</span>
          <span className="hc-mono block text-[10px] hc-dim">{de.pulse.ago(fmtAge(event.at, now))}</span>
        </span>
      </button>
    </li>
  );
}
