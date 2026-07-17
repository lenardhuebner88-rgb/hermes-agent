import { fetchJSON } from "@/lib/api";
import { ReviewVerdictsResponseSchema, parseOrThrow } from "../lib/schemas";
import type { ReviewVerdictsResponse } from "../lib/types";
import { withBoardParam } from "../lib/multiBoard";
import { usePolling } from "./internal";

export const HERMES_REVIEW_VERDICTS_URL = "/api/plugins/kanban/tasks/review-verdicts?limit=50";


export function useHermesReviewVerdicts(board: string | null = null) {
  const url = withBoardParam(HERMES_REVIEW_VERDICTS_URL, board);
  return usePolling<ReviewVerdictsResponse>(
    board ? `tasks/review-verdicts:${board}` : "tasks/review-verdicts",
    async () => parseOrThrow(
      ReviewVerdictsResponseSchema,
      await fetchJSON<unknown>(url),
      "tasks/review-verdicts",
    ),
    20000,
  );
}
