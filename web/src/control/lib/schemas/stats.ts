import { z } from "zod";
import {
  nullableNumber,
} from "./common";

// board_stats (/stats) trägt seit ST2 queue_wait_p50_seconds (created_at →
// erster task_runs.started_at). Wir picken nur dieses eine Feld; zod verwirft
// den Rest des großen Board-Payloads.
export const BoardStatsResponseSchema = z.object({
  queue_wait_p50_seconds: nullableNumber,
});
export type BoardStatsResponse = z.infer<typeof BoardStatsResponseSchema>;
