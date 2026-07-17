import { useMemo } from "react";
import { useBoard } from "./workersBoard";

// Derive which FO backlog items are already visible on the board (status IN
// ready/running/blocked/review/triage/scheduled) via idempotency_key matching.
// Returns a map { foItemId → FoBoardStatus } for fast O(1) lookup per row.
// Uses the shared useBoard() poll — no extra request.
export type FoBoardStatus = {
  /** Board task id (not the FO backlog id). */
  taskId: string;
  /** Raw kanban status string. */
  status: string;
  /** Human-readable label for the badge in the FO tab. */
  label: string;
};


const FO_BOARD_STATUSES = new Set(["ready", "running", "blocked", "review", "triage", "scheduled"]);

const FO_STATUS_LABEL: Record<string, string> = {
  running: "läuft",
  ready: "wartet",
  triage: "wartet",
  scheduled: "wartet",
  blocked: "blockiert",
  review: "in Review",
};


// Pure helper: extract the FO backlog item id from a kanban idempotency_key.
// Returns null for null/empty keys or keys without the "fo-backlog:" prefix.
// Exported for unit tests — no behaviour change to useFoBoardStatus.
export function extractFoIdFromIdempotencyKey(key: string | null | undefined): string | null {
  if (!key) return null;
  if (!key.startsWith("fo-backlog:")) return null;
  const foId = key.slice("fo-backlog:".length);
  return foId || null;
}


export function useFoBoardStatus(): Record<string, FoBoardStatus> {
  const board = useBoard();
  return useMemo(() => {
    const data = board.data;
    if (!data) return {};
    const result: Record<string, FoBoardStatus> = {};
    for (const col of data.columns) {
      if (!FO_BOARD_STATUSES.has(col.name)) continue;
      for (const task of col.tasks) {
        const foId = extractFoIdFromIdempotencyKey(task.idempotency_key);
        if (!foId) continue;
        result[foId] = {
          taskId: task.id,
          status: task.status,
          label: FO_STATUS_LABEL[task.status] ?? task.status,
        };
      }
    }
    return result;
  }, [board.data]);
}
