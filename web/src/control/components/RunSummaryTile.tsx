import { de } from "../i18n/de";
import { fmtAge } from "../lib/derive";
import type { RunSummaryResponse } from "../lib/schemas";
import { FleetEmptyState, FleetPanel, FleetPod } from "./fleet/atoms";
import { ToneCallout } from "./atoms";

function fmtCost(value: number | null): string {
  if (value == null) return "—";
  if (value === 0) return "$0.00";
  if (value < 0.01) return `<$0.01`;
  return `$${value.toFixed(2)}`;
}

function fmtDuration(seconds: number | null): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/**
 * K7 RunSummaryTile — root-grouped throughput / cost / cycle-time over the
 * last window plus the most recent roots. Pure presentational: a 404 (or any
 * parse failure) lands here as ``data == null`` and renders a quiet empty
 * state — never a crash. ``error`` surfaces a calm callout above the panel.
 */
export function RunSummaryTile({
  data,
  now,
  error,
  loading,
}: {
  data: RunSummaryResponse | null;
  now: number;
  error?: string | null;
  loading?: boolean;
}) {
  const roots = data?.roots ?? [];
  const meta = data ? de.runSummary.meta(data.since_hours) : de.runSummary.meta(24);

  return (
    <FleetPanel eyebrow={de.runSummary.eyebrow} meta={meta}>
      {error ? (
        <div className="mb-3">
          <ToneCallout tone="amber">{de.runSummary.error}<br />{error}</ToneCallout>
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <FleetPod label={de.runSummary.podCompleted} value={loading && !data ? "—" : (data?.completed_roots ?? 0)} />
        <FleetPod label={de.runSummary.podCost} value={loading && !data ? "—" : fmtCost(data?.total_cost_usd ?? null)} />
        <FleetPod label={de.runSummary.podP50} value={loading && !data ? "—" : fmtDuration(data?.cycle_time_p50_seconds ?? null)} />
        <FleetPod label={de.runSummary.podP90} value={loading && !data ? "—" : fmtDuration(data?.cycle_time_p90_seconds ?? null)} />
      </div>

      <div className="mt-3">
        {roots.length === 0 ? (
          <FleetEmptyState title={de.runSummary.emptyTitle} desc={de.runSummary.emptyDesc} />
        ) : (
          <ul className="space-y-2">
            {roots.map((root) => (
              <li
                key={root.id}
                className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 rounded-lg border border-white/10 bg-black/15 px-3 py-2"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-white">{root.title || root.id}</p>
                  <p className="hc-mono text-xs hc-dim">
                    {root.id}
                    {root.subtask_count > 0 ? ` · ${de.runSummary.subtasks(root.subtask_count)}` : ""}
                    {root.completed_at != null ? ` · vor ${fmtAge(root.completed_at, now)}` : ""}
                  </p>
                </div>
                <div className="flex shrink-0 items-baseline gap-3 hc-mono text-xs">
                  <span className="text-zinc-200">{fmtDuration(root.cycle_time_seconds)}</span>
                  <span className="text-emerald-200">{fmtCost(root.cost_usd)}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </FleetPanel>
  );
}
