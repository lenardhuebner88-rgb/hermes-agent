import { z } from "zod";
import {
  ChainCostsLaneSchema,
  epochSeconds,
  nullableNumber,
} from "./common";

const AccountUsageWindowSchema = z.object({
  label: z.string().catch("Limit"),
  window_key: z.string().nullable().catch(null),
  used_percent: z.coerce.number().nullable().catch(null),
  reset_at: z.string().nullable().catch(null),
  detail: z.string().nullable().catch(null),
});

const AccountUsageProviderSchema = z.object({
  provider: z.string().catch("unknown"),
  available: z.boolean().catch(false),
  source: z.string().nullable().catch(null),
  fetched_at: z.string().nullable().catch(null),
  signal_at: z.string().nullable().catch(null),
  title: z.string().catch("Account limits"),
  plan: z.string().nullable().catch(null),
  windows: z.array(AccountUsageWindowSchema).catch([]),
  details: z.array(z.string()).catch([]),
  unavailable_reason: z.string().nullable().catch(null),
  cached: z.boolean().catch(false),
  fallback: z.boolean().catch(false),
});

export const AccountUsageResponseSchema = z.object({
  providers: z.array(AccountUsageProviderSchema),
  cache_ttl_seconds: z.coerce.number().catch(60),
});
export type AccountUsageWindow = z.infer<typeof AccountUsageWindowSchema>;
// signal_at optional on the TS surface so pre-existing Partial fixtures keep
// typechecking (8-file slice); runtime parse always materializes string|null via .catch.
export type AccountUsageProvider = Omit<z.infer<typeof AccountUsageProviderSchema>, "signal_at" | "fallback"> & {
  signal_at?: string | null;
  fallback?: boolean;
};
export const ChainCostsResponseSchema = z.object({
  schema: z.string().catch("kanban-chain-costs-v1"),
  root_id: z.string(),
  // Canon 00-Canon/decisions/2026-07-27 (Kosten-SSOT im Lesepfad, Regel 3):
  // unknown bleibt unknown — Summen und Run-Zähler tragen KEIN .catch(0) und
  // das Totals-Objekt keinen Voll-Fallback mehr. Der Endpunkt liefert alle
  // Felder aus demselben Rollup; fehlt eines, ist der Vertrag gebrochen, der
  // Parse wirft und die Fläche rendert "—"/Fehler statt "0 = keine Aktivität".
  totals: z.object({
    input_tokens: z.coerce.number(),
    output_tokens: z.coerce.number(),
    cost_usd: nullableNumber,
    actual_cost_usd: nullableNumber,
    run_count: z.coerce.number(),
    // Geschätzter API-Gegenwert für Abo-Runs (additiv; ältere Payloads liefern 0).
    cost_usd_equivalent: nullableNumber,
    api_equivalent_usd: nullableNumber,
    cost_effective_usd: nullableNumber,
    billing_neuralwatt_kwh: z.coerce.number(),
    billing_neuralwatt_cost_usd: z.coerce.number(),
    // Coverage-Zähler sind jünger als der Vertrag: ältere Backends lassen sie
    // weg (optional), ein anwesender aber gebrochener Wert wirft.
    cost_usd_known_runs: z.coerce.number().optional(),
    cost_usd_total_runs: z.coerce.number().optional(),
    cost_usd_coverage: nullableNumber.optional(),
    cost_usd_state: z.enum(["empty", "complete", "partial", "unknown"]).catch("unknown").optional(),
    cost_usd_equivalent_candidate_runs: z.coerce.number().optional(),
    cost_usd_equivalent_known_runs: z.coerce.number().optional(),
    cost_usd_equivalent_state: z.enum(["not_applicable", "complete", "partial", "unknown"]).catch("unknown").optional(),
  }),
  // absteigend nach cost_usd (Backend-Garantie)
  by_lane: z.array(ChainCostsLaneSchema).catch([]),
});
export type ChainCostsLane = z.infer<typeof ChainCostsLaneSchema>;
export type ChainCostsResponse = z.infer<typeof ChainCostsResponseSchema>;
// F4 (Statistik): Kosten heute/Fenster + Top-Profile (vom Backend nach Burn
// sortiert). cost_usd = echte $ (Subscription-Lanes ehrliche 0, K17),
// cost_usd_equivalent = API-Äquivalent aus der Run-Metadata — getrennt halten.
const CostBucketSchema = z.object({
  // Run-Zähler ohne .catch(0) (Canon Regel 3): das Backend liefert `runs` aus
  // demselben Kosten-Select wie die Beträge; ein gebrochener Bucket wirft
  // statt "0 Runs" zu behaupten. Coverage-Zähler bleiben optional (ältere
  // Backends lassen sie weg), aber ein anwesender gebrochener Wert wirft.
  runs: z.coerce.number(),
  cost_usd: nullableNumber,
  cost_usd_equivalent: nullableNumber,
  api_equivalent_usd: nullableNumber,
  actual_cost_usd: nullableNumber,
  billing_neuralwatt_kwh: nullableNumber,
  billing_neuralwatt_charged_kwh: nullableNumber,
  billing_neuralwatt_usd_per_kwh: nullableNumber,
  billing_neuralwatt_cost_usd: nullableNumber,
  input_tokens: nullableNumber,
  output_tokens: nullableNumber,
  cached_tokens: nullableNumber.optional(),
  total_tokens: nullableNumber.optional(),
  equivalent_cost_known_runs: z.coerce.number().optional(),
  equivalent_cost_total_runs: z.coerce.number().optional(),
  equivalent_cost_coverage: nullableNumber.optional(),
  equivalent_cost_state: z.enum(["not_applicable", "complete", "partial", "unknown"]).catch("unknown").optional(),
  cost_usd_known_runs: z.coerce.number().optional(),
  cost_usd_total_runs: z.coerce.number().optional(),
  cost_usd_coverage: nullableNumber.optional(),
  cost_usd_state: z.enum(["empty", "complete", "partial", "unknown"]).catch("unknown").optional(),
});
const CostProfileRowSchema = CostBucketSchema.extend({
  profile: z.string().catch("unbekannt"),
  // Paid-subscription lane the profile dispatches through, resolved server-side
  // from the profile's runtime/provider config (NOT its name): "chatgpt"
  // (ChatGPT/Codex), "claude" (Claude Max), "kimi", or null for API-billed
  // lanes (openrouter, gemini, …). Drives the Abo-Tokenverbrauch panel.
  subscription: z.enum(["chatgpt", "claude", "kimi"]).nullable().catch(null),
});
// S1B: Review-Wert je Stufe (verifier/reviewer/critic) über dasselbe Fenster
// wie die Kosten-Sicht. findings_* / tokens_per_finding sind NULL, wenn kein
// Lauf der Stufe das metadata.review_findings-Feld trägt (gesamter Altbestand);
// tokens_per_finding bleibt auch bei nachweislich 0 Funden NULL.
const ReviewValueRowSchema = z.object({
  profile: z.string().catch("unbekannt"),
  // Zähler ohne .catch(0) (Canon Regel 3) — dieselbe Begründung wie oben.
  runs: z.coerce.number(),
  approved: z.coerce.number(),
  request_changes: z.coerce.number(),
  findings_blocking: nullableNumber,
  findings_observations: nullableNumber,
  input_tokens: nullableNumber,
  tokens_per_finding: nullableNumber,
  // Scout ist read-only Recon, keine Verdikt-Stufe: sein Wert sind gelesene
  // Evidenz-Items (read_items) und die Kosten je Item (tokens_per_read_item).
  // NULL für Verdict-Stufen ohne Read-Metadaten und für Altbestand-Backends,
  // die das Feld gar nicht senden — nie ein Fehler.
  read_items: nullableNumber,
  tokens_per_read_item: nullableNumber,
});
export const RunsCostsResponseSchema = z.object({
  days: z.coerce.number().catch(7),
  now: epochSeconds,
  today: CostBucketSchema,
  window: CostBucketSchema,
  profiles: z.array(CostProfileRowSchema).catch([]),
  review_value: z.array(ReviewValueRowSchema).catch([]),
});
export const RunsCostsSeriesPointSchema = CostBucketSchema.extend({
  day: z.string().catch(""),
  token_known_runs: z.coerce.number().optional(),
  token_total_runs: z.coerce.number().optional(),
  token_coverage: nullableNumber.optional(),
  token_state: z.enum(["empty", "complete", "partial", "unknown"]).catch("unknown").optional(),
  equivalent_cost_known_runs: z.coerce.number().optional(),
  equivalent_cost_total_runs: z.coerce.number().optional(),
  equivalent_cost_coverage: nullableNumber.optional(),
  equivalent_cost_state: z.enum(["not_applicable", "complete", "partial", "unknown"]).catch("unknown").optional(),
});
export const RunsCostsSeriesResponseSchema = z.object({
  days: z.coerce.number().catch(7),
  now: epochSeconds,
  series: z.array(RunsCostsSeriesPointSchema).catch([]),
  field_sources: z.record(z.string(), z.string()).optional(),
});
export type CostBucket = z.infer<typeof CostBucketSchema>;
export type CostProfileRow = z.infer<typeof CostProfileRowSchema>;
export type RunsCostsSeriesPoint = z.infer<typeof RunsCostsSeriesPointSchema>;
export type RunsCostsSeriesResponse = z.infer<typeof RunsCostsSeriesResponseSchema>;
export type ReviewValueRow = z.infer<typeof ReviewValueRowSchema>;
export type RunsCostsResponse = z.infer<typeof RunsCostsResponseSchema>;

const TokenBurnBucketSchema = z.object({
  // Zähler ohne .catch(0) (Canon Regel 3) — dieselbe Begründung wie oben.
  // completed/failed/blocked und die Coverage-Zähler bleiben optional (ältere
  // Backends lassen sie weg), ein anwesender gebrochener Wert wirft.
  runs: z.coerce.number(),
  completed_runs: z.coerce.number().optional(),
  failed_runs: z.coerce.number().optional(),
  blocked_runs: z.coerce.number().optional(),
  input_tokens: nullableNumber,
  output_tokens: nullableNumber,
  total_tokens: nullableNumber,
  input_token_known_runs: z.coerce.number().optional(),
  output_token_known_runs: z.coerce.number().optional(),
  token_known_runs: z.coerce.number().optional(),
  token_total_runs: z.coerce.number().optional(),
  input_token_coverage: nullableNumber.optional(),
  output_token_coverage: nullableNumber.optional(),
  token_coverage: nullableNumber.optional(),
  token_state: z.enum(["complete", "partial", "unknown"]).optional(),
  token_semantics: z.enum(["exact", "lower_bound", "unknown"]).optional(),
});
const SubscriptionBurnLaneSchema = TokenBurnBucketSchema.extend({
  subscription: z.string().catch("unknown"),
  profile: z.string().catch("unbekannt"),
});
const SubscriptionBurnClassSchema = TokenBurnBucketSchema.extend({
  subscription: z.string().catch("unknown"),
  value_class: z.string().catch("unknown"),
});
const SubscriptionBurnDailySchema = TokenBurnBucketSchema.extend({
  subscription: z.string().catch("unknown"),
  date: z.string().catch(""),
});
const SubscriptionBurnBucketSchema = SubscriptionBurnLaneSchema.extend({
  value_class: z.string().catch("unknown"),
  date: z.string().catch(""),
});
export const SubscriptionTokenBurnResponseSchema = z.object({
  days: z.coerce.number().catch(7),
  now: epochSeconds,
  window_start: z.coerce.number().catch(0),
  totals: TokenBurnBucketSchema,
  by_lane: z.array(SubscriptionBurnLaneSchema).catch([]),
  by_class: z.array(SubscriptionBurnClassSchema).catch([]),
  daily: z.array(SubscriptionBurnDailySchema).catch([]),
  buckets: z.array(SubscriptionBurnBucketSchema).catch([]),
});
export type TokenBurnBucket = z.infer<typeof TokenBurnBucketSchema>;
export type SubscriptionBurnLane = z.infer<typeof SubscriptionBurnLaneSchema>;
export type SubscriptionBurnClass = z.infer<typeof SubscriptionBurnClassSchema>;
export type SubscriptionTokenBurnResponse = z.infer<typeof SubscriptionTokenBurnResponseSchema>;

// Token-/Session-Zähler ohne .catch(0) (Canon Regel 3): der Read-Pfad liefert
// alle Felder aus demselben Aggregate-Lauf oder scheitert als Ganzes; ein
// gebrochener Wert wirft statt "0 = keine Aktivität" zu behaupten.
const HostUsageDailySchema = z.object({
  date: z.string().catch(""),
  tokens: z.coerce.number(),
  sessions: z.coerce.number(),
});
const HostUsageProviderSchema = z.object({
  provider: z.string().catch("unknown"),
  label: z.string().catch("Provider"),
  total_tokens: z.coerce.number(),
  sessions: z.coerce.number(),
  daily: z.array(HostUsageDailySchema).catch([]),
});
const HostUsageSourceSchema = z.object({
  source: z.string().catch("unknown"),
  label: z.string().catch("Quelle"),
  tokens: z.coerce.number(),
  sessions: z.coerce.number(),
});
export const HostUsageResponseSchema = z.object({
  generated_at: epochSeconds,
  days: z.coerce.number().catch(7),
  dates: z.array(z.string()).catch([]),
  total_tokens: z.coerce.number(),
  total_sessions: z.coerce.number(),
  active_tmux_panes: z.coerce.number(),
  sources: z.array(HostUsageSourceSchema).catch([]),
  providers: z.array(HostUsageProviderSchema).catch([]),
  errors: z.array(z.string()).catch([]),
  accounting_note: z.string().catch("Aktive Ein-/Ausgabe ohne Cache"),
  cached: z.boolean().catch(false),
});
export type HostUsageDaily = z.infer<typeof HostUsageDailySchema>;
export type HostUsageProvider = z.infer<typeof HostUsageProviderSchema>;
export type HostUsageSource = z.infer<typeof HostUsageSourceSchema>;
export type HostUsageResponse = z.infer<typeof HostUsageResponseSchema>;
