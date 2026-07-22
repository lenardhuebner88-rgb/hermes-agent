import { z } from "zod";
import {
  ChainCostsLaneSchema,
  epochSeconds,
  nullableEpochSeconds,
  nullableNumber,
  ResultQualityBadgeSchema,
  RunRoleSchema,
  RunRoleSourceSchema,
  RunOutcomeSchema,
  RunStatusSchema,
  TaskArtifactLinkSchema,
  TaskDeliverableSchema,
  VerificationStateSchema,
  VerifierVerdictSchema,
} from "./common";

// GET /runs/windowed-rollup — completed Mother roots with Worker + Läufer cost detail.
const WindowedRollupWorkerSchema = ChainCostsLaneSchema.extend({
  provider: z.string().nullable().catch(null),
  model: z.string().nullable().catch(null),
  provider_model_source: z.enum(["run_metadata", "session_log", "lane_current_fallback", "unknown"]).catch("unknown"),
  unknown_run_count: z.coerce.number().catch(0),
  // Das Backend kodiert „keine Preis-Evidenz" bewusst als null (nicht als
  // falsche 0) — genau wie die Root-Felder unten. Die geerbten
  // z.coerce.number()-Felder hätten null still zu 0 gemacht
  // (Number(null) === 0), sodass „unbekannt" und „bestätigt kostenlos"
  // ununterscheidbar wurden.
  cost_usd: nullableNumber,
  cost_usd_equivalent: nullableNumber,
  cost_effective_usd: nullableNumber,
});

const WindowedRollupRunnerSchema = z.object({
  id: z.coerce.number().catch(0),
  task_id: z.coerce.string().catch(""),
  profile: z.string().catch("unbekannt"),
  provider: z.string().nullable().catch(null),
  model: z.string().nullable().catch(null),
  provider_model_source: z.enum(["run_metadata", "session_log", "lane_current_fallback", "unknown"]).catch("unknown"),
  input_tokens: z.coerce.number().nullable().catch(null),
  output_tokens: z.coerce.number().nullable().catch(null),
  cost_usd: nullableNumber,
  cost_usd_equivalent: nullableNumber,
  cost_effective_usd: nullableNumber,
  billing_mode: z.string().nullable().catch(null),
  neuralwatt: z.unknown().nullable().catch(null),
  started_at: nullableEpochSeconds,
  ended_at: nullableEpochSeconds,
  runtime_seconds: z.coerce.number().nullable().catch(null),
});

const WindowedRollupRootSchema = z.object({
  id: z.coerce.string().catch(""),
  title: z.string().nullable().catch(null),
  status: z.string().nullable().catch(null),
  assignee: z.string().nullable().catch(null),
  created_at: nullableEpochSeconds,
  started_at: nullableEpochSeconds,
  completed_at: nullableEpochSeconds,
  ended_at: nullableEpochSeconds,
  providers: z.array(z.string()).catch([]),
  cost_usd: nullableNumber,
  cost_usd_equivalent: nullableNumber,
  cost_effective_usd: nullableNumber,
  unknown_run_count: z.coerce.number().catch(0),
  billing_mode: z.string().nullable().catch(null),
  neuralwatt: z.unknown().nullable().catch(null),
  runtime_seconds: z.coerce.number().nullable().catch(null),
  workers: z.array(WindowedRollupWorkerSchema).catch([]),
  runners: z.array(WindowedRollupRunnerSchema).catch([]),
});

export const WindowedRollupResponseSchema = z.object({
  schema: z.string().catch("kanban-windowed-rollup-v1"),
  since_hours: z.coerce.number().catch(168),
  now: epochSeconds,
  completed_roots: z.coerce.number().catch(0),
  roots: z.array(WindowedRollupRootSchema).catch([]),
});
export type WindowedRollupWorker = z.infer<typeof WindowedRollupWorkerSchema>;
export type WindowedRollupRunner = z.infer<typeof WindowedRollupRunnerSchema>;
export type WindowedRollupRoot = z.infer<typeof WindowedRollupRootSchema>;
export type WindowedRollupResponse = z.infer<typeof WindowedRollupResponseSchema>;
export const KanbanResultSchema = z.object({
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("done"),
  task_assignee: z.string().catch("hermes"),
  profile: z.string().nullable().catch(null),
  run_role: RunRoleSchema.catch("legacy_unknown"),
  run_role_label: z.string().catch("Unknown / legacy run"),
  run_role_source: RunRoleSourceSchema.catch("missing_claim_event"),
  status: RunStatusSchema,
  outcome: RunOutcomeSchema,
  started_at: epochSeconds,
  ended_at: epochSeconds,
  duration_seconds: z.coerce.number().catch(0),
  summary: z.string().catch(""),
  summary_preview: z.string().catch(""),
  followups: z.array(z.string()).catch([]),
  artifacts: z.array(z.string()).catch([]),
  artifact_links: z.array(TaskArtifactLinkSchema).catch([]),
  verification: z.array(z.string()).catch([]),
  verification_state: VerificationStateSchema.catch("ungated"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verifier_evidence: z.array(z.string()).catch([]),
  result_quality: ResultQualityBadgeSchema,
  deliverables: z.array(TaskDeliverableSchema).catch([]),
  residual_risk: z.string().nullable().optional(),
});

export const TodayDigestItemSchema = z.object({
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_summary: z.string().catch(""),
  ended_at: epochSeconds,
  profile: z.string().nullable().catch(null),
  run_role: RunRoleSchema.catch("legacy_unknown"),
  run_role_label: z.string().catch("Unknown / legacy run"),
  verification_state: VerificationStateSchema.catch("ungated"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verdict_label: z.string().catch("Not independently verified"),
  result_quality: ResultQualityBadgeSchema,
  gate_evidence: z.array(z.string()).catch([]),
  deliverable: TaskDeliverableSchema.nullable().catch(null),
  deliverable_excerpt: z.string().nullable().catch(null),
  residual_risk: z.string().nullable().optional(),
});

export const TodayDigestResponseSchema = z.object({
  schema: z.string().catch("kanban-today-digest-v1"),
  items: z.array(TodayDigestItemSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: epochSeconds,
  day_start: z.coerce.number().catch(0),
  timezone: z.string().catch("local"),
  limit: z.coerce.number().catch(12),
});
export const RecentResultsResponseSchema = z.object({
  results: z.array(KanbanResultSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: epochSeconds,
  limit: z.coerce.number().catch(12),
  since_hours: z.coerce.number().catch(48),
  outcome: z.string().catch("completed"),
});
// K7: root-grouped run summary (throughput / cost / cycle-time + recent roots).
const RunSummaryRootSchema = z.object({
  id: z.coerce.string().catch(""),
  title: z.string().nullable().catch(null),
  status: z.string().nullable().catch(null),
  assignee: z.string().nullable().catch(null),
  completed_at: nullableEpochSeconds,
  cost_usd: nullableNumber,
  // Geschätzter API-Gegenwert + effektive Kosten (additiv; ältere Payloads → null via .catch).
  cost_effective_usd: nullableNumber,
  cycle_time_seconds: nullableNumber,
  subtask_count: z.coerce.number().catch(0),
});
export const RunSummaryResponseSchema = z.object({
  since_hours: z.coerce.number().catch(24),
  now: epochSeconds,
  completed_roots: z.coerce.number().catch(0),
  total_cost_usd: nullableNumber,
  // Geschätzter API-Gegenwert Gesamt (additiv; ältere Payloads → null).
  total_cost_effective_usd: nullableNumber,
  cycle_time_p50_seconds: nullableNumber,
  cycle_time_p90_seconds: nullableNumber,
  roots: z.array(RunSummaryRootSchema).catch([]),
});
export type RunSummaryRoot = z.infer<typeof RunSummaryRootSchema>;
export type RunSummaryResponse = z.infer<typeof RunSummaryResponseSchema>;

// Phase 3 (Statistik): per-Profil-Verlässlichkeit + Tages-Zeitreihe.
const ReliabilityProfileSchema = z.object({
  profile: z.string().catch("unbekannt"),
  runs: z.coerce.number().catch(0),
  tasks: z.coerce.number().catch(0),
  outcomes: z.record(z.string(), z.coerce.number()).catch({}),
  completed_rate: nullableNumber,
  failed_rate: nullableNumber,
  retries: z.coerce.number().catch(0),
  retry_rate: nullableNumber,
  judged: z.coerce.number().catch(0),
  approved: z.coerce.number().catch(0),
  rejected: z.coerce.number().catch(0),
  // null = unter dem min-n-Gate (roster-stats damping) — keine Behauptung.
  approve_rate: nullableNumber,
  low_sample: z.boolean().catch(false),
});
export const ReliabilityResponseSchema = z.object({
  since_hours: z.coerce.number().catch(168),
  baseline_hours: z.coerce.number().catch(720),
  min_n: z.coerce.number().catch(5),
  now: epochSeconds,
  profiles: z.array(ReliabilityProfileSchema),
  baseline: z.array(ReliabilityProfileSchema).catch([]),
});
export type ReliabilityProfile = z.infer<typeof ReliabilityProfileSchema>;
export type ReliabilityResponse = z.infer<typeof ReliabilityResponseSchema>;

// T5 (Wert-Bilanz): wofür wurde geliefert — Klasse je Root via created_by
// (Funnel-Quellen → nutzer, Review-/Verifier-Ketten → haertung, Rest → meta).
const ValueClassBreakdownSchema = z
  .object({
    nutzer: z.coerce.number().catch(0),
    haertung: z.coerce.number().catch(0),
    meta: z.coerce.number().catch(0),
  })
  .catch({ nutzer: 0, haertung: 0, meta: 0 });

const RunsDailyPointSchema = z.object({
  date: z.string().catch(""),
  done_roots: z.coerce.number().catch(0),
  done_roots_by_class: ValueClassBreakdownSchema,
  done_tasks: z.coerce.number().catch(0),
  cost_usd: nullableNumber,
  input_tokens: nullableNumber,
  output_tokens: nullableNumber,
  runs_completed: z.coerce.number().catch(0),
  runs_failed: z.coerce.number().catch(0),
  cycle_time_p50_seconds: nullableNumber,
});
export const RunsDailyResponseSchema = z.object({
  days: z.coerce.number().catch(30),
  now: epochSeconds,
  series: z.array(RunsDailyPointSchema),
});
export type RunsDailyPoint = z.infer<typeof RunsDailyPointSchema>;
export type RunsDailyResponse = z.infer<typeof RunsDailyResponseSchema>;
// ST5 (Effizienz): die zwei ST2-Aggregate, die die Flotten-Effizienz-Karte
// braucht. chain_completion_rate = done-Roots, deren Abhängigkeits-Leaves alle
// done sind, / done-Roots (eigener Endpunkt /stats/chain-completion).
export const ChainCompletionResponseSchema = z.object({
  done_roots: z.coerce.number().catch(0),
  completed_done_roots: z.coerce.number().catch(0),
  chain_completion_rate: nullableNumber,
});
export type ChainCompletionResponse = z.infer<typeof ChainCompletionResponseSchema>;
// ST4 (Statistik-Broadsheet): wiederkehrende Fehler, gruppiert je
// (Signatur + Profil), gespiegelt aus /runs/issues. Die `outcomes` sind
// ausschließlich Harness-Lifecycle-Endzustände (crashed/timed_out/spawn_failed/
// gave_up/iteration_budget_exhausted/blocked) — daher die Fehler-Taxonomie.
const IssueGroupSchema = z.object({
  signature: z.string().catch(""),
  profile: z.string().catch("unbekannt"),
  cause_key: z.string().catch("other"),
  cause_label: z.string().catch("Sonstige"),
  cause_hint: z.string().catch("Noch keiner belastbaren Ursachenklasse zugeordnet."),
  count: z.coerce.number().catch(0),
  first_seen: nullableNumber,
  last_seen: nullableNumber,
  outcomes: z.record(z.string(), z.coerce.number()).catch({}),
  example_run_id: nullableNumber,
  example_task_id: z.coerce.string().catch(""),
  example_task_title: z.string().catch(""),
  example_assignee: z.string().catch(""),
  example_block_kind: z.string().nullable().catch(null),
  example_text: z.string().catch(""),
});
export const RunsIssuesResponseSchema = z.object({
  days: z.coerce.number().catch(30),
  now: epochSeconds,
  total_failed_runs: z.coerce.number().catch(0),
  group_count: z.coerce.number().catch(0),
  truncated: z.boolean().catch(false),
  issues: z.array(IssueGroupSchema).catch([]),
});
type ParsedIssueGroup = z.infer<typeof IssueGroupSchema>;
export type IssueGroup = Omit<ParsedIssueGroup, "cause_key" | "cause_label" | "cause_hint" | "example_task_title" | "example_assignee" | "example_block_kind"> & {
  cause_key?: string;
  cause_label?: string;
  cause_hint?: string;
  example_task_title?: string;
  example_assignee?: string;
  example_block_kind?: string | null;
};
export type RunsIssuesResponse = Omit<z.infer<typeof RunsIssuesResponseSchema>, "issues"> & { issues: IssueGroup[] };
