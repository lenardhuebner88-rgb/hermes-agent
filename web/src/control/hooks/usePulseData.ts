import { useMemo } from "react";
import { useCronObservability } from "./cron";
import { useHermesRecentResults } from "./runsDigestRollup";
import { buildPulse, groupPulseByDay, summarizePulse, type PulseDay, type PulseEvent, type PulseSummary } from "../lib/pulse";
import { freshness, nowSec } from "../lib/derive";
import type { Proposal } from "../lib/types";

// Fenster des Stroms: die Quellen liefern selbst ~48h (recent-results) bzw. den
// letzten Lauf je Cron — wir zeigen, was da ist, und benennen das Fenster ehrlich.
export const PULSE_WINDOW_HOURS = 48;

export interface PulseData {
  events: PulseEvent[];
  summary: PulseSummary;
  days: PulseDay[];
  fresh: ReturnType<typeof freshness>;
  loading: boolean;
  error: string | null;
  now: number;
  windowHours: number;
  results: ReturnType<typeof useHermesRecentResults>;
  crons: ReturnType<typeof useCronObservability>;
}

export interface PulseSource {
  proposals: Proposal[];
  proposalsLastUpdated?: number | null;
}

/**
 * usePulseData — die 48h-Aktivität aus den drei Strömen (recent-results,
 * Proposals, Cron-Observability) zusammengeführt. Geteilt zwischen der
 * eigenständigen PulseView und dem fusionierten System-Kopf (S1), damit der
 * Puls nur an EINER Stelle abgeleitet wird. Eigene Datei (kein Komponenten-
 * Modul), weil react-refresh keine Hook-Exporte neben Komponenten duldet.
 */
export function usePulseData({ proposals, proposalsLastUpdated }: PulseSource): PulseData {
  const results = useHermesRecentResults();
  const crons = useCronObservability();
  const now = nowSec();

  const events = useMemo(
    () =>
      buildPulse({
        results: results.data?.results ?? [],
        proposals,
        crons: crons.data?.jobs ?? [],
        sinceSec: now - PULSE_WINDOW_HOURS * 3600,
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

  return { events, summary, days, fresh, loading, error, now, windowHours: PULSE_WINDOW_HOURS, results, crons };
}
