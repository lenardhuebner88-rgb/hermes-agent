import { z } from "zod";
import { fetchJSON } from "@/lib/api";
import { parseOrThrow } from "../lib/schemas";
import { usePolling } from "./internal";

// Zum Vertrag `usage-consumption.v1` (hermes_cli/usage_consumption_readmodel.py,
// Phase C des usage-observability-Goals). Wie bei usage-facts.v1 gilt Canon
// Regel 3: Geldfelder und Raten tragen KEIN .catch(0) — fehlende Basis ist
// "not applicable" (String) oder null, niemals eine gedrückte Zahl.

export const USAGE_CONSUMPTION_CONTRACT = "usage-consumption.v1";

/** Geld- oder Anteilsfeld: Zahl oder ehrliches "not applicable". */
const MoneyOrNa = z.union([z.number(), z.literal("not applicable")]);

const RateSchema = z.object({
  value: z.union([z.number(), z.literal("not applicable")]),
  numerator: z.number(),
  denominator: z.number(),
});

const TotalsSchema = z.object({
  equivalent_usd: z.number(),
  metered_usd: z.number(),
  subscription_saving_usd: z.number(),
  metered_coverage: RateSchema,
  tokens: z.number(),
  runs: z.number(),
  cost_coverage: RateSchema,
  token_coverage: RateSchema,
});

const SeriesEntrySchema = z.object({
  key: z.string(),
  equivalent_usd: MoneyOrNa,
  metered_usd: MoneyOrNa,
  subscription_saving_usd: MoneyOrNa,
  tokens: z.number(),
  unpriced_tokens: z.number(),
  runs: z.number(),
  cost_coverage: RateSchema,
  token_coverage: RateSchema,
});

const DailyEntrySchema = z.object({
  day: z.string(),
  equivalent_usd: z.number(),
  metered_usd: z.number(),
  tokens: z.number(),
  runs: z.number(),
  token_coverage: RateSchema,
});

const ComponentShareSchema = z.object({
  tokens: z.number(),
  token_share: RateSchema,
  cost_usd: z.number().nullable(),
  cost_share: z.union([RateSchema, z.literal("not applicable")]),
});

const DistributionSchema = z.object({
  p50: z.number().nullable(),
  p90: z.number().nullable(),
  max: z.number().nullable(),
  count: z.number(),
});

const TopRunSchema = z.object({
  run_id: z.string(),
  origin: z.string().nullable(),
  model: z.string().nullable(),
  provider: z.string().nullable(),
  task_id: z.string().nullable(),
  chain_id: z.string().nullable(),
  session_id: z.string().nullable(),
  day: z.string().nullable(),
  tokens: z.number(),
  equivalent_usd: z.number(),
});

const LeverSchema = z.object({
  id: z.string(),
  title: z.string(),
  current_usd: z.number(),
  share_of_total: RateSchema,
  counterfactual_usd: z.number(),
  savings_usd: z.number(),
  assumption: z.string(),
  plausibility: z.record(z.string(), z.unknown()),
});

const TrendSchema = z.object({
  window_days: z.number(),
  equivalent_usd_per_day_full: z.number(),
  equivalent_usd_per_day_7d: z.number(),
  ratio_7d_vs_full: RateSchema,
});

const ResponseSchema = z.object({
  contract: z.string(),
  generated_at: z.string(),
  window: z.object({
    days: z.number(),
    cutoff_utc: z.string(),
    timezone: z.string(),
    granularity_note: z.string().optional(),
  }),
  confidence: z.object({ tokens: z.string(), costs: z.string() }),
  totals: TotalsSchema,
  cache_hit_rate: RateSchema,
  component_shares: z.record(z.string(), ComponentShareSchema),
  breakdown: z.object({
    dimension: z.string(),
    series: z.array(SeriesEntrySchema),
  }),
  daily: z.array(DailyEntrySchema),
  daily_by_origin: z.record(
    z.string(),
    z.array(
      z.object({
        day: z.string(),
        equivalent_usd: z.number(),
        tokens: z.number(),
        token_coverage: RateSchema,
      }),
    ),
  ),
  distributions: z.object({
    tokens_per_run: DistributionSchema,
    tokens_per_tool_call: DistributionSchema,
    calls_per_run: DistributionSchema,
  }),
  top_runs: z.array(TopRunSchema),
  trend: TrendSchema,
  levers: z.array(LeverSchema),
});

export type UsageConsumptionResponse = z.infer<typeof ResponseSchema>;
export type UsageConsumptionLever = z.infer<typeof LeverSchema>;
export type UsageConsumptionSeriesEntry = z.infer<typeof SeriesEntrySchema>;
export type UsageConsumptionDailyEntry = z.infer<typeof DailyEntrySchema>;

export type ConsumptionBreakdown =
  | "origin"
  | "model"
  | "lane"
  | "buzz_unit"
  | "provider"
  | "billing_mode";

export function useUsageConsumption(days: number, breakdown: ConsumptionBreakdown) {
  return usePolling<UsageConsumptionResponse>(
    `stats/usage-consumption:${days}:${breakdown}`,
    async () =>
      parseOrThrow(
        ResponseSchema,
        await fetchJSON<unknown>(
          `/api/plugins/kanban/stats/usage-consumption?days=${days}&breakdown=${encodeURIComponent(breakdown)}`,
        ),
        "usage-consumption",
      ),
    60000,
  );
}
