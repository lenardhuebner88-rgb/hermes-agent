import { z } from "zod";
import { epochSeconds } from "./common";

const ReadMetricSchema = z.object({
  value: z.coerce.number().nullable().catch(null),
  denominator: z.coerce.number().catch(0),
  observed: z.coerce.number().catch(0),
  coverage: z.coerce.number().nullable().catch(null),
  source: z.string().catch("unknown"),
  captured_at: z.string().catch(""),
});

const ModelSignalSchema = z.object({
  name: z.string().catch("nicht_zuordenbar"),
  calls: z.coerce.number().catch(0),
  known_cost_usd: z.coerce.number().nullable().catch(null),
  cost_coverage: z.coerce.number().nullable().catch(null),
  latency_p50_ms: z.coerce.number().nullable().catch(null),
  latency_p95_ms: z.coerce.number().nullable().catch(null),
  latency_coverage: z.coerce.number().nullable().catch(null),
  ttft_p50_ms: z.coerce.number().nullable().catch(null),
  ttft_p95_ms: z.coerce.number().nullable().catch(null),
  ttft_coverage: z.coerce.number().nullable().catch(null),
});

const LangfuseProjectionSchema = z.object({
  contract_version: z.string().catch("langfuse-hermes-read.v1"),
  available: z.boolean().catch(false),
  state: z.enum(["fresh", "stale", "absent", "partial", "unknown"]).catch("absent"),
  reason: z.string().nullable().catch(null),
  window_days: z.coerce.number().catch(7),
  generated_at: z.string().catch(""),
  captured_at: z.string().nullable().catch(null),
  cache: z.object({
    ttl_seconds: z.coerce.number().catch(45),
    age_seconds: z.coerce.number().nullable().catch(null),
  }).catch({ ttl_seconds: 45, age_seconds: null }),
  metrics: z.record(z.string(), ReadMetricSchema).catch({}),
  coverage: z.record(z.string(), z.coerce.number()).catch({}),
  models: z.array(ModelSignalSchema).catch([]),
});

const UsageCoverageSchema = z.object({
  available: z.boolean().catch(false),
  reason: z.string().nullable().catch(null),
  fact_rows: z.coerce.number().catch(0),
  exact_task_run_links: z.coerce.number().catch(0),
  task_links: z.coerce.number().catch(0),
  model_known: z.coerce.number().catch(0),
  duration_known: z.coerce.number().catch(0),
  ttft_known: z.coerce.number().catch(0),
  missing_columns: z.array(z.string()).catch([]),
});

const UsageDimensionSchema = z.object({
  name: z.string().catch("unknown"),
  fact_rows: z.coerce.number().catch(0),
  context_input_tokens: z.coerce.number().catch(0),
  output_tokens: z.coerce.number().catch(0),
});

const UsageProjectionSchema = z.object({
  available: z.boolean().catch(false),
  state: z.enum(["fresh", "stale", "absent", "partial"]).catch("absent"),
  reason: z.string().nullable().catch(null),
  captured_at: z.string().nullable().catch(null),
  summary: z.object({
    fact_rows: z.coerce.number().catch(0),
    context_input_tokens: z.coerce.number().catch(0),
    new_input_tokens: z.coerce.number().catch(0),
    output_tokens: z.coerce.number().catch(0),
    cache_read_tokens: z.coerce.number().catch(0),
    cache_write_tokens: z.coerce.number().catch(0),
    known_metered_usd: z.string().nullable().catch(null),
    price_status: z.string().catch("unknown"),
    priced_breakdowns: z.coerce.number().catch(0),
    unpriced_breakdowns: z.coerce.number().catch(0),
    unclassified_fact_rows: z.coerce.number().catch(0),
  }).catch({
    fact_rows: 0,
    context_input_tokens: 0,
    new_input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    known_metered_usd: null,
    price_status: "unknown",
    priced_breakdowns: 0,
    unpriced_breakdowns: 0,
    unclassified_fact_rows: 0,
  }),
  origins: z.array(UsageDimensionSchema.extend({
    cache_read_tokens: z.coerce.number().catch(0),
    cache_write_tokens: z.coerce.number().catch(0),
    model_count: z.coerce.number().catch(0),
  })).catch([]),
  models: z.array(UsageDimensionSchema.extend({
    origin_count: z.coerce.number().catch(0),
  })).catch([]),
  daily_imports: z.array(z.object({
    date: z.string().catch(""),
    fact_rows: z.coerce.number().catch(0),
    token_rows: z.coerce.number().catch(0),
  })).catch([]),
  coverage: UsageCoverageSchema,
});

export const ObservabilityStatsResponseSchema = z.object({
  contract_version: z.string().catch("langfuse-hermes-read.v1"),
  window_days: z.coerce.number().catch(7),
  generated_at: z.string().catch(""),
  langfuse: LangfuseProjectionSchema,
  usage: UsageProjectionSchema,
  run_duration: z.object({
    p50_seconds: z.coerce.number().nullable().catch(null),
    p95_seconds: z.coerce.number().nullable().catch(null),
    count: z.coerce.number().catch(0),
    source: z.string().catch("kanban.metric_scores.run_duration_seconds"),
  }),
  checked_at: epochSeconds,
});

export type ObservabilityStatsResponse = z.infer<typeof ObservabilityStatsResponseSchema>;
export type ObservabilityUsageOrigin = z.infer<typeof UsageProjectionSchema>["origins"][number];
