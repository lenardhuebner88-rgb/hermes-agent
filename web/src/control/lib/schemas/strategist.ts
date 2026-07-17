import { z } from "zod";

export const StrategistCountSchema = z.object({ count: z.number() }).passthrough();

export const StrategistLastRunsSchema = z.object({
  harvest: z.object({ ts: z.number(), receipts: z.number().optional(), candidates: z.number().optional() }).passthrough().nullable(),
  propose: z.object({ ts: z.number(), candidates: z.number().optional(), ingested: z.number().optional() }).passthrough().nullable(),
});
export type StrategistLastRuns = z.infer<typeof StrategistLastRunsSchema>;

// ── Lever-Outcomes (Ziel-2 Ledger → Ziel-4 Wirkungs-Historie) ──────────────
export const LeverOutcomeSchema = z.object({
  lever_key: z.string().nullable().catch(null),
  root_task_id: z.string().nullable().catch(null),
  proposed_at: z.number().nullable().catch(null),
  baseline: z.record(z.string(), z.unknown()).nullable().catch(null),
  metric_key: z.string().nullable().catch(null),
  shipped_at: z.number().nullable().catch(null),
  measured_at: z.number().nullable().catch(null),
  current: z.number().nullable(),
  delta: z.number().nullable(),
  verdict: z.string().nullable().catch(null),
  status: z.string().nullable().catch(null),
}).passthrough();
export type LeverOutcome = z.infer<typeof LeverOutcomeSchema>;

export const StrategistOutcomesResponseSchema = z.object({
  outcomes: z.array(LeverOutcomeSchema).catch([]),
  generated_at: z.number(),
});
export type StrategistOutcomesResponse = z.infer<typeof StrategistOutcomesResponseSchema>;
