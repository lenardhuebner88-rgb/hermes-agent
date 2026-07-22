import { fetchJSON } from "@/lib/api";
import { usePolling } from "../../hooks/internal";
import { parseOrThrow, RunsIssuesResponseSchema, type RunsIssuesResponse } from "../../lib/schemas";

export function useStartIssues(days: 1 | 3 | 7 = 7) {
  return usePolling<RunsIssuesResponse>(
    `start/runs/issues:${days}:200`,
    async () => parseOrThrow(
      RunsIssuesResponseSchema,
      await fetchJSON<unknown>(`/api/plugins/kanban/runs/issues?days=${days}&limit=200`),
      "start/runs/issues",
    ),
    60000,
  );
}
