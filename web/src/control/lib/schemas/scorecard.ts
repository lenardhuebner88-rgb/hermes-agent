import { z } from "zod";
import { epochSeconds, nullableNumber } from "./common";

const ScoreRateSchema = z.object({
  runs: z.coerce.number().catch(0),
  approved: z.coerce.number().catch(0),
  approval_rate: z.coerce.number().nullable().catch(null),
});
const ScoreGroupSchema = ScoreRateSchema.extend({ name: z.string().catch("unknown") });
const QualityCoverageSchema = z.object({
  runs: z.coerce.number().catch(0),
  review_verdicts: z.coerce.number().catch(0),
  run_outcomes: z.coerce.number().catch(0),
  review_iterations: z.coerce.number().catch(0),
});
const QualitySnapshotSchema = z.object({
  window_days: z.coerce.number().catch(7),
  overall: ScoreRateSchema,
  verdicts: z.object({
    approved: z.coerce.number().catch(0),
    rejected: z.coerce.number().catch(0),
  }),
  outcomes: z.record(z.string(), z.coerce.number()).catch({}),
  profiles: z.array(ScoreGroupSchema).catch([]),
  models: z.array(ScoreGroupSchema).catch([]),
  daily_verdicts: z.array(z.object({
    date: z.string().catch(""),
    approved: z.coerce.number().catch(0),
    rejected: z.coerce.number().catch(0),
  })).catch([]),
  review_iterations: z.object({
    average: z.coerce.number().nullable().catch(null),
    count: z.coerce.number().catch(0),
    distribution: z.record(z.string(), z.coerce.number()).catch({}),
  }),
  coverage: QualityCoverageSchema,
  source: z.string().catch("kanban.metric_scores"),
  captured_at: epochSeconds,
});
const ObservabilityHealthSchema = z.object({
  available: z.boolean().catch(false),
  state: z.enum(["fresh", "stale", "absent", "partial", "unknown"]).catch("absent"),
  reason: z.string().nullable().catch(null),
  captured_at: z.string().nullable().catch(null),
  cache: z.object({
    ttl_seconds: z.coerce.number().catch(45),
    age_seconds: z.coerce.number().nullable().catch(null),
  }).catch({ ttl_seconds: 45, age_seconds: null }),
});
/** Materialisierte numerische Serien oder kategoriale Häufigkeits-Maps aus SC-S1. */
const MaterializedScoreSchema = z.object({
  value: z.union([z.coerce.number(), z.record(z.string(), z.coerce.number())]).nullable().catch(null),
  min: z.coerce.number().nullable().catch(null).optional(),
  max: z.coerce.number().nullable().catch(null).optional(),
  sum: z.coerce.number().nullable().catch(null).optional(),
  count: z.coerce.number().catch(0),
});
export const ScorecardResponseSchema = z.object({
  contract_version: z.string().catch("hermes-scorecard.v1"),
  overall: ScoreRateSchema,
  verdicts: z.object({ approved: z.coerce.number().catch(0), rejected: z.coerce.number().catch(0) }),
  profiles: z.array(ScoreGroupSchema).catch([]),
  models: z.array(ScoreGroupSchema).catch([]),
  weeks: z.array(ScoreRateSchema.extend({ year: z.coerce.number(), week: z.coerce.number() })).catch([]),
  materialized_scores: z.record(z.string(), MaterializedScoreSchema).catch({}),
  quality: QualitySnapshotSchema.optional(),
  observability: ObservabilityHealthSchema.optional(),
  checked_at: epochSeconds,
});
export type QualitySnapshot = z.infer<typeof QualitySnapshotSchema>;
export type MaterializedScore = z.infer<typeof MaterializedScoreSchema>;
export type ScorecardResponse = z.infer<typeof ScorecardResponseSchema>;

/**
 * Wochen-Digest aus GET /api/plugins/kanban/scores/digest (SC-S4).
 * Shape gemessen 2026-07-26 gegen einen Snapshot der Live-kanban.db:
 * `cost_usd` kann je Zeile null sein und `by_model` kann den Schluessel "?"
 * (Laeufe ohne aufgezeichnetes Modell) enthalten — beides ist echt und darf
 * weder zu 0 noch zu einem Modellnamen geglattet werden. `top_findings` ist
 * aktuell immer leer, weil die Score-Materialisierung keinen Aufrufer hat.
 */
export const DigestRateBucketSchema = z.object({
  total: z.coerce.number().catch(0),
  approved: z.coerce.number().catch(0),
  approval_rate: z.coerce.number().nullable().catch(null),
});
export const DigestWeeklyRowSchema = DigestRateBucketSchema.extend({
  year: z.coerce.number().catch(0),
  week: z.coerce.number().catch(0),
  rows_total: z.coerce.number().catch(0),
  approved_rows: z.coerce.number().catch(0),
});
export const DigestRunCostSchema = z.object({
  run_id: z.coerce.number().catch(0),
  task_id: z.string().catch(""),
  cost_usd: nullableNumber,
  duration_seconds: z.coerce.number().nullable().catch(null),
});
export const DigestRetryHotspotSchema = z.object({
  task_id: z.string().catch(""),
  rejections: z.coerce.number().catch(0),
});
export const DigestFindingSchema = z.object({
  kind: z.string().catch(""),
  title: z.string().catch(""),
  summary: z.string().catch(""),
  impact_eur: z.coerce.number().catch(0),
  data_source: z.string().catch(""),
});
export const ScoreDigestSchema = z.object({
  generated_at: epochSeconds,
  weeks_requested: z.coerce.number().catch(0),
  approval_rate: z.coerce.number().nullable().catch(null),
  wow_delta: z.coerce.number().nullable().catch(null),
  approved_rows: z.coerce.number().catch(0),
  rows_total: z.coerce.number().catch(0),
  weekly: z.array(DigestWeeklyRowSchema).catch([]),
  by_profile: z.record(z.string(), DigestRateBucketSchema).catch({}),
  by_model: z.record(z.string(), DigestRateBucketSchema).catch({}),
  cost_duration_per_approved_run: z.array(DigestRunCostSchema).catch([]),
  retry_hotspots: z.array(DigestRetryHotspotSchema).catch([]),
  has_metric_scores: z.boolean().catch(false),
  approved_run_metric_coverage: z.boolean().catch(false),
  top_findings: z.array(DigestFindingSchema).catch([]),
});
export type DigestRateBucket = z.infer<typeof DigestRateBucketSchema>;
export type DigestFinding = z.infer<typeof DigestFindingSchema>;
export type ScoreDigest = z.infer<typeof ScoreDigestSchema>;
