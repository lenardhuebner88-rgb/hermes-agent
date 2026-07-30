import { describe, expect, it } from "vitest";
import {
  USAGE_FACTS_CONTRACT_VERSION,
  UsageFactsResponseSchema,
} from "./usageFacts";

// Strukturgetreu nach der ausführbaren S6-Fixture
// tests/fixtures/usage_facts_readmodel/s7_payload_example.json (die Datei liegt
// außerhalb von web/src und kann vom tsconfig-include nicht importiert werden).
const contractShapedPayload = {
  contract_version: "usage-facts.v1",
  generated_at: "2026-07-27T15:36:00+00:00",
  scope: { origins: null, captured_from: null, captured_to: null },
  normalization: {
    version: "origin-input.v1",
    fields: { context_input: "…", new_input: "…", uncached_input: "…" },
    origins: {
      claude_code: {
        input_semantics: "cache_exclusive",
        context_input: "input_tokens + cache_read_tokens + cache_write_tokens",
        new_input: "input_tokens + cache_write_tokens",
        uncached_input: "input_tokens",
      },
    },
  },
  summary: {
    group_count: 1,
    fact_rows: 1,
    raw_input_tokens: 6,
    tokens: {
      context_input_tokens: 104682,
      new_input_tokens: 3815,
      cache_read_tokens: 100867,
      cache_write_tokens: 3809,
      output_tokens: 409,
      has_lower_bounds: false,
    },
    billing: {
      metered: {
        fact_rows: 0,
        tokens: {
          context_input_tokens: 0,
          new_input_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          output_tokens: 0,
          has_lower_bounds: false,
        },
        metered_usd: {
          known_amount_usd: "0.000000",
          status: "complete",
          priced_breakdowns: 0,
          unpriced_breakdowns: 0,
          lower_bound_breakdowns: 0,
        },
      },
      quota: {
        fact_rows: 0,
        tokens: {
          context_input_tokens: 0,
          new_input_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          output_tokens: 0,
          has_lower_bounds: false,
        },
        marginal_usd: "0",
        list_equivalent_usd: {
          known_amount_usd: "0.000000",
          status: "complete",
          priced_breakdowns: 0,
          unpriced_breakdowns: 0,
          lower_bound_breakdowns: 0,
        },
      },
      unclassified: {
        fact_rows: 1,
        tokens: {
          context_input_tokens: 104682,
          new_input_tokens: 3815,
          cache_read_tokens: 100867,
          cache_write_tokens: 3809,
          output_tokens: 409,
          has_lower_bounds: false,
        },
        reason: "billing_mode_unknown",
      },
    },
  },
  groups: [
    {
      key: {
        origin: "claude_code",
        profile: null,
        lane: "redacted-claude-session",
        model: "test-model-preview",
        model_label: "test-model-preview",
      },
      fact_rows: 1,
      tokens: {
        input_semantics: "cache_exclusive",
        raw_input_tokens: 6,
        context_input: { tokens: 104682, status: "exact" },
        new_input: { tokens: 3815, status: "exact" },
        uncached_input: { tokens: 6, status: "exact" },
        cache_read_tokens: 100867,
        cache_write_tokens: 3809,
        output_tokens: 409,
        reasoning_tokens: null,
        observed_rows: { token_rows: 1, input: 1, cache_read: 1, cache_write: 1, output: 1, reasoning: 0 },
      },
      billing: {
        unclassified: {
          fact_rows: 1,
          tokens: {
            context_input_tokens: 104682,
            new_input_tokens: 3815,
            cache_read_tokens: 100867,
            cache_write_tokens: 3809,
            output_tokens: 409,
            has_lower_bounds: false,
          },
          reason: "billing_mode_unknown",
        },
      },
    },
  ],
  unattributed: {
    label: "nicht_zuordenbar",
    reason: "model_missing",
    fact_rows: 0,
    tokens: {
      context_input_tokens: 0,
      new_input_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      output_tokens: 0,
      has_lower_bounds: false,
    },
    by_origin: [],
  },
  kanban: {
    available: true,
    scope: "all_board_runs",
    total_runs: 8284,
    provider_classification: { nie_gelaufen: 2592, unbekannt: 3750, rekonstruiert: 638, gestempelt: 1304 },
    reconstruction_sources: { cli_raw: 3, "metadata.model": 595, profile_state_db: 40 },
    by_classification_and_provider: [],
    usage_coverage: { token_bearing_runs: 3, provider_only_fact_runs: 638, runs_without_fact: 7643, state: "thin" },
  },
  database: { run_usage_facts_rows: 96026, selected_token_rows: 95388, rows_without_tokens: 638 },
  timing_ms: { facts_query: 0, kanban_projection: 0, total: 0 },
};

describe("UsageFactsResponseSchema", () => {
  it("akzeptiert die Vertragsform usage-facts.v1 aus der S6-Fixture", () => {
    const parsed = UsageFactsResponseSchema.parse(contractShapedPayload);
    expect(parsed.contract_version).toBe(USAGE_FACTS_CONTRACT_VERSION);
    expect(parsed.summary.tokens.context_input_tokens).toBe(104682);
    expect(parsed.groups[0].tokens.context_input).toEqual({ tokens: 104682, status: "exact" });
    expect(parsed.summary.billing.quota.marginal_usd).toBe("0");
    expect(parsed.summary.billing.unclassified.reason).toBe("billing_mode_unknown");
    expect(parsed.kanban.usage_coverage?.state).toBe("thin");
  });

  it("übersetzt fehlende Token-Buckets zu null statt zu einer erfundenen 0", () => {
    const parsed = UsageFactsResponseSchema.parse({
      ...contractShapedPayload,
      groups: [
        {
          ...contractShapedPayload.groups[0],
          tokens: {
            input_semantics: "cache_exclusive",
            context_input: { tokens: null, status: "unavailable" },
            new_input: { tokens: 10, status: "exact" },
            cache_read_tokens: null,
            cache_write_tokens: null,
            output_tokens: null,
          },
        },
      ],
    });
    expect(parsed.groups[0].tokens.context_input).toEqual({ tokens: null, status: "unavailable" });
    expect(parsed.groups[0].tokens.cache_read_tokens).toBeNull();
  });

  it("wirft bei fehlenden Token-Summen statt sie per .catch(0) zu erfinden (Canon Regel 3)", () => {
    const { cache_read_tokens: _dropped, ...brokenTokens } = contractShapedPayload.summary.tokens;
    const result = UsageFactsResponseSchema.safeParse({
      ...contractShapedPayload,
      summary: { ...contractShapedPayload.summary, tokens: brokenTokens },
    });
    expect(result.success).toBe(false);
  });

  it("wirft bei gebrochenem Preis-Betrag statt \"0.000000\" zu füllen (Canon Regel 3)", () => {
    const { known_amount_usd: _dropped, ...brokenPrice } =
      contractShapedPayload.summary.billing.metered.metered_usd;
    const result = UsageFactsResponseSchema.safeParse({
      ...contractShapedPayload,
      summary: {
        ...contractShapedPayload.summary,
        billing: {
          ...contractShapedPayload.summary.billing,
          metered: { ...contractShapedPayload.summary.billing.metered, metered_usd: brokenPrice },
        },
      },
    });
    expect(result.success).toBe(false);
  });

  it("wirft bei fehlendem total_runs in der Kanban-Projektion statt 0 Runs zu behaupten", () => {
    const { total_runs: _dropped, ...brokenKanban } = contractShapedPayload.kanban;
    const result = UsageFactsResponseSchema.safeParse({
      ...contractShapedPayload,
      kanban: brokenKanban,
    });
    expect(result.success).toBe(false);
  });
});
