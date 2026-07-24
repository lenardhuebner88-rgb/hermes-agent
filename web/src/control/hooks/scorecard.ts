import { fetchJSON } from "@/lib/api";
import { parseOrThrow, ScorecardResponseSchema, type ScorecardResponse } from "../lib/schemas";
import { withBoardParam } from "../lib/multiBoard";
import { usePolling } from "./internal";

export function useScorecard(board: string | null = null) {
  const url = withBoardParam("/api/plugins/kanban/scorecard", board);
  return usePolling<ScorecardResponse>(
    board ? `scorecard:${board}` : "scorecard",
    async () => parseOrThrow(ScorecardResponseSchema, await fetchJSON<unknown>(url), "scorecard"),
    20_000,
  );
}