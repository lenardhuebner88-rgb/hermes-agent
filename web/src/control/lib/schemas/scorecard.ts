import { z } from "zod";
import { epochSeconds } from "./common";

const ScoreRateSchema = z.object({
  runs: z.coerce.number().catch(0),
  approved: z.coerce.number().catch(0),
  approval_rate: z.coerce.number().nullable().catch(null),
});
const ScoreGroupSchema = ScoreRateSchema.extend({ name: z.string().catch("unknown") });
/** Materialisierte Event-/Usage-Scores aus SC-S1: Mittelwert (0..1) plus Row-Anzahl. */
const MaterializedScoreSchema = z.object({
  value: z.coerce.number().nullable().catch(null),
  count: z.coerce.number().catch(0),
});
export const ScorecardResponseSchema = z.object({
  overall: ScoreRateSchema,
  verdicts: z.object({ approved: z.coerce.number().catch(0), rejected: z.coerce.number().catch(0) }),
  profiles: z.array(ScoreGroupSchema).catch([]),
  models: z.array(ScoreGroupSchema).catch([]),
  weeks: z.array(ScoreRateSchema.extend({ year: z.coerce.number(), week: z.coerce.number() })).catch([]),
  materialized_scores: z.record(z.string(), MaterializedScoreSchema).catch({}),
  checked_at: epochSeconds,
});
export type MaterializedScore = z.infer<typeof MaterializedScoreSchema>;
export type ScorecardResponse = z.infer<typeof ScorecardResponseSchema>;
