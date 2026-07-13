import { StaleBadge } from "../../components/atoms";
import type { StructuredError } from "../../hooks/pollingStore";

export interface FleetFreshnessSource {
  label: string;
  error?: string | null;
  errorObj?: StructuredError | null;
  isStale?: boolean;
  lastUpdated?: number | null;
}

export function FleetSourceFreshness({ sources }: { sources: FleetFreshnessSource[] }) {
  const affected = sources.filter((source) => source.isStale || source.error || source.errorObj);
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
            errorObj={source.errorObj}
            error={source.error}
          />
        </div>
      ))}
    </section>
  );
}
