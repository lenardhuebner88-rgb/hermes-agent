import { describe, expect, it } from "vitest";
import { ObservabilityStatsResponseSchema } from "./observability";

/** The read model emits null for token sums it cannot know
 *  (hermes_cli/usage_facts_readmodel.py: "without manufacturing zeroes for
 *  missing data"). The schema must not coerce that null into 0 — "unbekannt"
 *  and "null Tokens" are different statements. */
const payload = {
  contract_version: "langfuse-hermes-read.v1",
  window_days: 7,
  generated_at: "2026-08-05T00:00:00Z",
  langfuse: { available: false, state: "absent" },
  usage: {
    available: true,
    state: "fresh",
    reason: null,
    captured_at: "2026-08-05T00:00:00Z",
    summary: {
      fact_rows: 12,
      context_input_tokens: null,
      new_input_tokens: null,
      output_tokens: 4200,
      cache_read_tokens: null,
      cache_write_tokens: null,
      known_metered_usd: null,
      price_status: "unknown",
      priced_breakdowns: 0,
      unpriced_breakdowns: 0,
      unclassified_fact_rows: 0,
    },
    origins: [{
      name: "kanban",
      fact_rows: 12,
      context_input_tokens: null,
      output_tokens: 4200,
      cache_read_tokens: null,
      cache_write_tokens: null,
      model_count: 1,
    }],
    models: [{ name: "opus", fact_rows: 12, context_input_tokens: null, output_tokens: 4200, origin_count: 1 }],
    daily_imports: [],
    coverage: { available: true, reason: null },
  },
  run_duration: {},
  checked_at: 1_800_000_000,
};

describe("ObservabilityStatsResponseSchema", () => {
  it("keeps unknown token sums null instead of rendering them as zero", () => {
    const parsed = ObservabilityStatsResponseSchema.parse(payload);

    expect(parsed.usage.summary.context_input_tokens).toBeNull();
    expect(parsed.usage.summary.cache_read_tokens).toBeNull();
    expect(parsed.usage.summary.output_tokens).toBe(4200);
    expect(parsed.usage.origins[0].context_input_tokens).toBeNull();
    expect(parsed.usage.models[0].context_input_tokens).toBeNull();
    // Row counts are always emitted as int and keep their 0 default.
    expect(parsed.usage.summary.fact_rows).toBe(12);
  });

  it("still coerces numeric strings", () => {
    const parsed = ObservabilityStatsResponseSchema.parse({
      ...payload,
      usage: {
        ...payload.usage,
        summary: { ...payload.usage.summary, context_input_tokens: "1234" },
      },
    });

    expect(parsed.usage.summary.context_input_tokens).toBe(1234);
  });
});
