import { fetchJSON } from "@/lib/api";
import {
  RecentResultsResponseSchema,
  TodayDigestResponseSchema,
  RunSummaryResponseSchema,
  WindowedRollupResponseSchema,
  ReliabilityResponseSchema,
  RunsDailyResponseSchema,
  ChainCompletionResponseSchema,
  RunsIssuesResponseSchema,
  BlockedCompletionsResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { RunSummaryResponse, ReliabilityResponse, RunsDailyResponse, ChainCompletionResponse, RunsIssuesResponse, WindowedRollupResponse } from "../lib/schemas";
import type { BlockedCompletionsResponse, RecentResultsResponse, TodayDigestResponse } from "../lib/types";
import { usePolling } from "./internal";

export const HERMES_RECENT_RESULTS_URL = "/api/plugins/kanban/runs/recent-results?limit=50&since_hours=48&outcome=completed";

export function useHermesRecentResults() {
  return usePolling<RecentResultsResponse>(
    "runs/recent-results",
    async () => parseOrThrow(
      RecentResultsResponseSchema,
      await fetchJSON<unknown>(HERMES_RECENT_RESULTS_URL),
      "runs/recent-results",
    ),
    20000,
  );
}


export function useHermesTodayDigest() {
  return usePolling<TodayDigestResponse>(
    "runs/today-digest",
    async () => parseOrThrow(
      TodayDigestResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/today-digest?limit=12"),
      "runs/today-digest",
    ),
    20000,
  );
}


export function useHermesRunSummary() {
  return usePolling<RunSummaryResponse>(
    "runs/summary",
    async () => parseOrThrow(
      RunSummaryResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/summary?since_hours=24"),
      "runs/summary",
    ),
    20000,
  );
}


export function useHermesWindowedRollup({ hours, limit }: { hours: number; limit: number }) {
  const safeHours = Math.max(1, Math.min(24 * 90, Math.round(hours)));
  const safeLimit = Math.max(1, Math.min(100, Math.round(limit)));
  return usePolling<WindowedRollupResponse>(
    `runs/windowed-rollup:${safeHours}:${safeLimit}`,
    async () => parseOrThrow(
      WindowedRollupResponseSchema,
      await fetchJSON<unknown>(`/api/plugins/kanban/runs/windowed-rollup?hours=${safeHours}&limit=${safeLimit}`),
      "runs/windowed-rollup",
    ),
    20000,
  );
}


// Phase 3 (Statistik): Verlässlichkeit pro Profil (7d + 30d-Baseline) und die
// Tages-Zeitreihe für die Charts. Beides langsam gepollt — Aggregate ändern
// sich im Minutentakt, nicht im Sekundentakt.
export function useHermesReliability() {
  return usePolling<ReliabilityResponse>(
    "runs/reliability",
    async () => parseOrThrow(
      ReliabilityResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/reliability?since_hours=168&baseline_hours=720&min_n=5"),
      "runs/reliability",
    ),
    60000,
  );
}


export function useHermesRunsDaily() {
  return usePolling<RunsDailyResponse>(
    "runs/daily",
    async () => parseOrThrow(
      RunsDailyResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/daily?days=30"),
      "runs/daily",
    ),
    60000,
  );
}


// ST5 (Effizienz): die zwei ST2-Aggregate für die Flotten-Effizienz-Karte —
// Ketten-Abschlussrate (eigener Endpunkt) und Queue-Wartezeit-p50 (aus dem
// board_stats /stats-Payload). Gleiche langsame Aggregate-Kadenz wie F4.
export function useChainCompletion() {
  return usePolling<ChainCompletionResponse>(
    "stats/chain-completion",
    async () => parseOrThrow(
      ChainCompletionResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/stats/chain-completion"),
      "stats/chain-completion",
    ),
    60000,
  );
}


// ST4 (Statistik-Broadsheet): wiederkehrende Fehler für die Fehler-Taxonomie —
// dieselbe Quelle wie das Issue-Board (F6), langsam gepollt (30-Tage-Aggregat).
export function useHermesRunsIssues() {
  return usePolling<RunsIssuesResponse>(
    "runs/issues",
    async () => parseOrThrow(
      RunsIssuesResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/issues?days=30&limit=50"),
      "runs/issues",
    ),
    60000,
  );
}


export function useHermesBlockedCompletions() {
  return usePolling<BlockedCompletionsResponse>(
    "runs/blocked-completions",
    async () => parseOrThrow(
      BlockedCompletionsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/blocked-completions?since_hours=48"),
      "runs/blocked-completions",
    ),
    20000,
  );
}

