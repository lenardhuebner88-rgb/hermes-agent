import { RefreshCw } from "lucide-react";
import { StaleBadge } from "../../components/atoms";
import type { StructuredError } from "../../hooks/pollingStore";
import { useClientNowSeconds } from "../../lib/clock";
import { freshness } from "../../lib/derive";
import { de } from "../../i18n/de";

export interface FleetFreshnessSource {
  label: string;
  error?: string | null;
  errorObj?: StructuredError | null;
  isStale?: boolean;
  lastUpdated?: number | null;
  pollIntervalMs?: number;
  /** Hook-Reload (usePolling u. a.); Rückgabewert ist hier irrelevant. */
  reload?: () => unknown;
}

export function FleetSourceFreshness({ sources }: { sources: FleetFreshnessSource[] }) {
  const now = useClientNowSeconds();
  const affected = sources.filter((source) => source.isStale || source.error || source.errorObj || (source.lastUpdated != null && freshness(source.lastUpdated, source.pollIntervalMs ?? 10000, now).stale));
  if (affected.length === 0) return null;

  // Age-Stale ohne Fehler ist im Hintergrund-Tab ERWARTBAR (usePolling pausiert
  // bei document.hidden — by design): Ursache offen ausweisen statt verstecken.
  const ageStaleWithoutError = affected.some(
    (source) => !source.error && !source.errorObj
      && (source.isStale || (source.lastUpdated != null && freshness(source.lastUpdated, source.pollIntervalMs ?? 10000, now).stale)),
  );
  const reloadable = affected.filter((source) => typeof source.reload === "function");

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
      {reloadable.length > 0 ? (
        <button
          type="button"
          className="inline-flex min-h-8 items-center gap-1.5 rounded-full border border-line bg-surface-2 px-2.5 py-1 text-micro font-medium text-ink-2 transition hover:border-live hover:text-ink"
          onClick={() => {
            for (const source of reloadable) void source.reload?.();
          }}
        >
          <RefreshCw className="h-3.5 w-3.5" aria-hidden />
          {de.fleet.freshnessReload}
        </button>
      ) : null}
      {ageStaleWithoutError ? (
        <span className="text-micro text-ink-2">{de.fleet.freshnessPausedNote}</span>
      ) : null}
    </section>
  );
}
