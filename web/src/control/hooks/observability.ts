import { fetchJSON } from "@/lib/api";
import {
  ObservabilityStatsResponseSchema,
  type ObservabilityStatsResponse,
} from "../lib/schemas/observability";
import { parseOrThrow } from "../lib/schemas";
import { usePolling } from "./internal";

export function useObservabilityStats(days = 7) {
  const safeDays = Math.max(1, Math.min(30, Math.round(days)));
  return usePolling<ObservabilityStatsResponse>(
    `stats/observability:${safeDays}`,
    async () => parseOrThrow(
      ObservabilityStatsResponseSchema,
      await fetchJSON<unknown>(`/api/plugins/kanban/stats/observability?days=${safeDays}`),
      "stats/observability",
    ),
    60000,
  );
}
