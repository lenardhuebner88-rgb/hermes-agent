import { z } from "zod";
import { fetchJSON } from "@/lib/api";
import { parseOrThrow } from "../lib/schemas";
import { usePolling } from "./internal";

// Zum S6-Read-Vertrag `usage-facts.v1` (docs/observability/usage-facts-read-contract.md,
// ausführbare Fixture tests/fixtures/usage_facts_readmodel/s7_payload_example.json).
// Das Schema lebt bewusst hier und nicht im schemas-Barrel — der Hook ist die einzige
// Konsumentin, und das Barrel wird von parallelen Sessions editiert.

export const USAGE_FACTS_CONTRACT_VERSION = "usage-facts.v1";

const TokenStatusSchema = z
  .enum(["exact", "lower_bound", "unavailable"])
  .catch("unavailable");

const TokenFieldSchema = z.object({
  tokens: z.coerce.number().nullable().catch(null),
  status: TokenStatusSchema,
});

const NormalizedTokensSchema = z.object({
  input_semantics: z.string().catch("unknown"),
  context_input: TokenFieldSchema,
  new_input: TokenFieldSchema,
  cache_read_tokens: z.coerce.number().nullable().catch(null),
  cache_write_tokens: z.coerce.number().nullable().catch(null),
  output_tokens: z.coerce.number().nullable().catch(null),
});

const TokenTotalsSchema = z.object({
  context_input_tokens: z.coerce.number().catch(0),
  new_input_tokens: z.coerce.number().catch(0),
  cache_read_tokens: z.coerce.number().catch(0),
  cache_write_tokens: z.coerce.number().catch(0),
  output_tokens: z.coerce.number().catch(0),
  has_lower_bounds: z.boolean().catch(false),
});

const PriceSchema = z.object({
  known_amount_usd: z.string().catch("0.000000"),
  status: z.enum(["complete", "partial", "unknown"]).catch("unknown"),
  priced_breakdowns: z.coerce.number().catch(0),
  unpriced_breakdowns: z.coerce.number().catch(0),
  lower_bound_breakdowns: z.coerce.number().catch(0),
});

// Kategorie-spezifische Felder (metered_usd / marginal_usd+list_equivalent_usd /
// reason) sind je Topf unterschiedlich besetzt und bleiben optional — die View
// entscheidet anhand ihrer Anwesenheit, nicht anhand des Topf-Namens.
const BillingCategorySchema = z.object({
  fact_rows: z.coerce.number().catch(0),
  tokens: TokenTotalsSchema,
  metered_usd: PriceSchema.optional(),
  marginal_usd: z.string().optional(),
  list_equivalent_usd: PriceSchema.optional(),
  reason: z.string().optional(),
});

const BillingSchema = z.object({
  metered: BillingCategorySchema,
  quota: BillingCategorySchema,
  unclassified: BillingCategorySchema,
});

const GroupSchema = z.object({
  key: z.object({
    origin: z.string(),
    profile: z.string().nullable().catch(null),
    lane: z.string().nullable().catch(null),
    model: z.string().nullable().catch(null),
    model_label: z.string().catch("nicht_zuordenbar"),
  }),
  fact_rows: z.coerce.number().catch(0),
  tokens: NormalizedTokensSchema,
  billing: z.record(z.string(), BillingCategorySchema).catch({}),
});

const KanbanProjectionSchema = z.object({
  available: z.boolean().catch(false),
  reason: z.string().optional(),
  total_runs: z.coerce.number().catch(0),
  provider_classification: z.record(z.string(), z.coerce.number()).catch({}),
  usage_coverage: z
    .object({
      token_bearing_runs: z.coerce.number().catch(0),
      provider_only_fact_runs: z.coerce.number().catch(0),
      runs_without_fact: z.coerce.number().catch(0),
      state: z.string().catch("unknown"),
    })
    .optional(),
});

export const UsageFactsResponseSchema = z.object({
  contract_version: z.string(),
  generated_at: z.string().catch(""),
  scope: z
    .object({
      origins: z.array(z.string()).nullable().catch(null),
      captured_from: z.string().nullable().catch(null),
      captured_to: z.string().nullable().catch(null),
    })
    .optional(),
  normalization: z.object({
    version: z.string().catch(""),
    origins: z.record(
      z.string(),
      z.object({ input_semantics: z.string().catch("unknown") }).catch({ input_semantics: "unknown" }),
    ).catch({}),
  }),
  summary: z.object({
    group_count: z.coerce.number().catch(0),
    fact_rows: z.coerce.number().catch(0),
    raw_input_tokens: z.coerce.number().catch(0),
    tokens: TokenTotalsSchema,
    billing: BillingSchema,
  }),
  groups: z.array(GroupSchema).catch([]),
  unattributed: z.object({
    label: z.string().catch("nicht_zuordenbar"),
    reason: z.string().catch("model_missing"),
    fact_rows: z.coerce.number().catch(0),
    tokens: TokenTotalsSchema,
    by_origin: z
      .array(
        z.object({
          origin: z.string(),
          fact_rows: z.coerce.number().catch(0),
          tokens: TokenTotalsSchema,
        }),
      )
      .catch([]),
  }),
  kanban: KanbanProjectionSchema,
});

export type UsageFactsTokenTotals = z.infer<typeof TokenTotalsSchema>;
export type UsageFactsBillingCategory = z.infer<typeof BillingCategorySchema>;
export type UsageFactsGroup = z.infer<typeof GroupSchema>;
export type UsageFactsResponse = z.infer<typeof UsageFactsResponseSchema>;

// Voraggregierter Rollup (~1 s Serverseite auf 96k Zeilen): langsam pollen wie
// die anderen Stats-Hooks. Die View konsumiert `summary` und faltet `groups`
// ausschließlich über die Hebel-Achsen origin/model (Brief §3a) — sie summiert
// keine quellennativen `input_tokens` neu.
export function useUsageFacts() {
  return usePolling<UsageFactsResponse>(
    "stats/usage-facts",
    async () =>
      parseOrThrow(
        UsageFactsResponseSchema,
        await fetchJSON<unknown>("/api/plugins/kanban/stats/usage-facts"),
        "usage-facts",
      ),
    60000,
  );
}
