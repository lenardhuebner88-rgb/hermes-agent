import { fetchJSON } from "@/lib/api";
import { StatsFieldConfigSchema, type StatsFieldConfig } from "../lib/statsFields";
import { BoardStatsResponseSchema, parseOrThrow } from "../lib/schemas";
import type { BoardStatsResponse } from "../lib/schemas";
import { usePolling } from "./internal";

// Config-driven field definitions for the Stats tab (provider/window/lane labels,
// order, visibility). Polled slowly — config changes are rare; the backend re-reads
// the YAML on mtime change, so edits reflect on the next poll. Consumers fall back to
// DEFAULT_STATS_CONFIG until this resolves (or if it errors).
export function useStatsConfig() {
  return usePolling<StatsFieldConfig>(
    "stats-config",
    async () => parseOrThrow(StatsFieldConfigSchema, await fetchJSON<unknown>("/api/stats-config"), "stats-config"),
    60000,
  );
}



export function useBoardStats() {
  return usePolling<BoardStatsResponse>(
    "stats/board",
    async () => parseOrThrow(
      BoardStatsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/stats"),
      "stats",
    ),
    60000,
  );
}
