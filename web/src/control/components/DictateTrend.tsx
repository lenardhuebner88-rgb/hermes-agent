import { Sparkline } from "./charts/charts";
import type { SeriesPoint } from "./charts/charts";
import { FleetEmptyState, FleetPanel } from "./leitstand";
import type { DictateHistoryDay } from "../lib/schemas";

/** Em dash for null/missing, matching DictateStatusTile's own convention. */
function fmtPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Math.round(value * 10) / 10}%`;
}

function fmtMs(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return "—";
  return `${Math.round(value)} ms`;
}

interface TrendRow extends DictateHistoryDay {
  isToday: boolean;
}

/** A day only counts as "active" (worth showing without history) if it has
 * at least one dictation/failure — mirrors the server's own history-append
 * rule (delta dictations+failures > 0), so a fresh empty `today` doesn't
 * suppress the empty state. */
function hasActivity(day: DictateHistoryDay | null | undefined): day is DictateHistoryDay {
  return Boolean(day) && ((day as DictateHistoryDay).dictations > 0 || (day as DictateHistoryDay).failures > 0);
}

/**
 * Diktat Stufe 11 — daily metric trend ("Verlauf"). Reuses the shared
 * chart-primitive idiom (`Sparkline` from `components/charts/charts.tsx`,
 * already established by CommandHome's StatsPulse) for the at-a-glance
 * success-rate line, plus a compact readable per-day list (mono data values)
 * for the exact numbers a chart tooltip alone wouldn't surface accessibly.
 * No polling — `history`/`today` ride along on the existing `useDictateStatus` poll.
 */
export function DictateTrend({
  history,
  today,
}: {
  history: DictateHistoryDay[] | undefined;
  today: DictateHistoryDay | null | undefined;
}) {
  const days = history ?? [];
  const activeToday = hasActivity(today) ? today : null;

  if (days.length === 0 && !activeToday) {
    return (
      <FleetPanel eyebrow="Verlauf (30 Tage)">
        <FleetEmptyState title="Noch keine Tagesdaten" desc="kommt mit dem ersten aktiven Diktat-Tag" />
      </FleetPanel>
    );
  }

  const rows: TrendRow[] = [
    ...days.map((day) => ({ ...day, isToday: false })),
    ...(activeToday ? [{ ...activeToday, isToday: true }] : []),
  ];

  const points: SeriesPoint[] = rows.map((row) => ({
    label: row.isToday ? "heute" : row.date,
    value: row.success_rate_percent ?? 0,
  }));

  return (
    <FleetPanel
      eyebrow="Verlauf (30 Tage)"
      meta={`${rows.length} ${rows.length === 1 ? "Tag" : "Tage"}`}
    >
      {points.length > 1 ? (
        <div className="mb-3">
          <Sparkline points={points} valueFmt={(v) => `${v}%`} />
        </div>
      ) : null}
      <ul className="grid grid-cols-1 gap-1.5">
        {rows
          .slice()
          .reverse()
          .map((row) => (
            <li
              key={`${row.date}-${row.isToday ? "today" : "history"}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-card border border-line bg-surface-2 px-3 py-1.5 text-xs"
            >
              <span className="flex items-center gap-1.5">
                <span className="font-data text-ink">{row.date}</span>
                {row.isToday ? <span className="text-live">heute</span> : null}
              </span>
              <span className="font-data tabular-nums text-ink-2">
                {fmtPercent(row.success_rate_percent)} · p50 {fmtMs(row.latency_p50_ms)} · p95 {fmtMs(row.latency_p95_ms)}
              </span>
            </li>
          ))}
      </ul>
    </FleetPanel>
  );
}
