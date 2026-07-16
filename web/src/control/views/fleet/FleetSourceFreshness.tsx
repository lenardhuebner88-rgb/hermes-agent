import { StaleBadge } from "../../components/atoms";
import type { StructuredError } from "../../hooks/pollingStore";
import { useClientNowSeconds } from "../../lib/clock";
import { freshness } from "../../lib/derive";

export interface FleetFreshnessSource {
  label: string;
  error?: string | null;
  errorObj?: StructuredError | null;
  isStale?: boolean;
  lastUpdated?: number | null;
  pollIntervalMs?: number;
}

export function FleetSourceFreshness({ sources }: { sources: FleetFreshnessSource[] }) {
  const now = useClientNowSeconds();
  const affected = sources.filter((source) => source.isStale || source.error || source.errorObj || (source.lastUpdated != null && freshness(source.lastUpdated, source.pollIntervalMs ?? 10000, now).stale));
  if (affected.length === 0) return null;

  return (
    <section
      aria-label="Datenfrische der sichtbaren Fleet-Quellen"
      className="mb-2 flex flex-wrap items-center gap-2 rounded-card border border-status-warn/25 bg-status-warn/5 px-3 py-2"
    >
      {affected.map((source) => (
        <div key={source.label} className="flex min-w-0 items-center gap-2">
          <span className="shrink-0 font-data text-micro text-ink-2">{source.label}</span>
          <StaleBadge
            isStale={source.isStale}
            lastUpdated={source.lastUpdated}
            pollIntervalMs={source.pollIntervalMs}
            errorObj={source.errorObj}
            error={source.error}
          />
        </div>
      ))}
    </section>
  );
}
