import { z } from "zod";
import {
  epochSeconds,
  ModelRouteStateSchema,
  nullableEpochSeconds,
  RunOutcomeSchema,
  RunStatusSchema,
  TaskStatusSchema,
} from "./common";

const FlowGateReleaseLevelSchema = z.enum(["merge", "live"]).catch("merge");

const FlowGateRiskSchema = z.object({
  tone: z.enum(["low", "medium", "high"]).catch("low"),
  reasons: z.array(z.string()).catch([]),
}).catch({ tone: "low", reasons: [] });

const FlowGateChildSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: TaskStatusSchema,
  assignee: z.string().nullable().catch(null),
  parents: z.array(z.string()).catch([]),
  risk: FlowGateRiskSchema,
  created_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
});

const FlowGateLaneSchema = z.object({
  id: z.string().nullable().catch(null),
  name: z.string().catch("Profile"),
  active: z.boolean().catch(false),
  profiles: z.array(z.string()).catch([]),
});

const FlowGateCostItemSchema = z.object({
  task_id: z.string().catch(""),
  profile: z.string().catch("default"),
  estimated_tokens: z.coerce.number().catch(0),
  estimated_cost_usd: z.coerce.number().catch(0),
  token_source: z.string().catch("unknown"),
  cost_source: z.string().catch("unknown"),
});

const FlowGateCostEstimateSchema = z.object({
  estimated_tokens: z.coerce.number().catch(0),
  estimated_cost_usd: z.coerce.number().catch(0),
  soft_limit_usd: z.coerce.number().catch(1),
  warning: z.boolean().catch(false),
  items: z.array(FlowGateCostItemSchema).catch([]),
});

export const FlowGateResponseSchema = z.object({
  root_id: z.string().catch(""),
  root_status: TaskStatusSchema,
  children: z.array(FlowGateChildSchema).catch([]),
  held_count: z.coerce.number().catch(0),
  release_levels: z.array(FlowGateReleaseLevelSchema).catch(["merge", "live"]),
  timeout_seconds: z.coerce.number().catch(1800),
  timeout_at: nullableEpochSeconds,
  auto_dispatch_eligible: z.boolean().catch(false),
  lanes: z.array(FlowGateLaneSchema).catch([]),
  cost_estimate: FlowGateCostEstimateSchema,
});
export type FlowGateResponse = z.infer<typeof FlowGateResponseSchema>;

export const FlowSizingResponseSchema = z.object({
  ok: z.boolean().catch(false),
  task_id: z.string().catch(""),
  action: z.enum(["merge", "split"]).catch("merge"),
  kept_id: z.string().optional(),
  archived_id: z.string().optional(),
  source_id: z.string().optional(),
  new_id: z.string().optional(),
  gate: FlowGateResponseSchema,
});

export const FlowReleaseResponseSchema = z.object({
  ok: z.boolean().catch(false),
  task_id: z.string().catch(""),
  released: z.coerce.number().catch(0),
  released_ids: z.array(z.string()).catch([]),
  release_level: FlowGateReleaseLevelSchema,
  assignee_overrides: z.record(z.string(), z.string().nullable()).catch({}),
  // Phase C: echoed levers (additiv; ältere Server liefern sie nicht → null).
  review_tier: z.enum(["standard", "review", "critical"]).nullable().catch(null),
  scout_id: z.string().nullable().catch(null),
});

export const FlowTimeoutSweepResponseSchema = z.object({
  ok: z.boolean().catch(false),
  timeout_seconds: z.coerce.number().catch(1800),
  released_roots: z.array(z.object({
    task_id: z.string().catch(""),
    released: z.coerce.number().catch(0),
    released_ids: z.array(z.string()).catch([]),
    release_level: FlowGateReleaseLevelSchema,
  })).catch([]),
  released: z.coerce.number().catch(0),
});
const ChainGraphRunSchema = z.object({
  id: z.coerce.number().catch(0),
  profile: z.string().nullable().catch(null),
  status: RunStatusSchema,
  outcome: RunOutcomeSchema,
  started_at: nullableEpochSeconds,
  ended_at: nullableEpochSeconds,
  last_heartbeat_at: nullableEpochSeconds,
  runtime_seconds: z.coerce.number().nullable().catch(null),
  heartbeat_age_seconds: z.coerce.number().nullable().catch(null),
  // S2: additiver Run-Fortschritt 0..1 (elapsed/max_runtime).
  run_progress: z.coerce.number().min(0).max(1).nullable().catch(null),
  requested_provider: z.string().nullable().catch(null),
  requested_model: z.string().nullable().catch(null),
  active_provider: z.string().nullable().catch(null),
  active_model: z.string().nullable().catch(null),
  model_state: ModelRouteStateSchema,
  model_source: z.string().nullable().catch(null),
  model_observed_at: nullableEpochSeconds,
  effective_model: z.string().nullable().catch(null),
});

const ChainGraphNodeSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: TaskStatusSchema,
  assignee: z.string().nullable().catch(null),
  level: z.coerce.number().catch(0),
  parents: z.array(z.string()).catch([]),
  children: z.array(z.string()).catch([]),
  created_at: epochSeconds,
  started_at: nullableEpochSeconds,
  completed_at: nullableEpochSeconds,
  last_heartbeat_at: nullableEpochSeconds,
  runtime_seconds: z.coerce.number().nullable().catch(null),
  progress: z.object({ done: z.coerce.number().catch(0), total: z.coerce.number().catch(0) }).nullable().catch(null),
  latest_run: ChainGraphRunSchema.nullable().catch(null),
  // FIX-5: Review-Rollen-Track — ALLE task_runs des Node-Tasks (nicht nur
  // latest_run). Additiv; ältere Payloads ohne dieses Feld liefern [].
  review_roles: z.array(z.object({
    profile: z.string(),
    status: z.string(),
    verdict: z.string().nullable().catch(null),
  })).catch([]),
  // K7 cost fields — additiv; ältere Payloads ohne diese Felder liefern 0.
  cost_usd: z.coerce.number().catch(0),
  input_tokens: z.coerce.number().catch(0),
  output_tokens: z.coerce.number().catch(0),
  // Geschätzter API-Gegenwert für Abo-Runs (alle Abo-Lanes: claude & Codex gestempelt).
  cost_usd_equivalent: z.coerce.number().catch(0),
  cost_effective_usd: z.coerce.number().catch(0),
});

export const ChainGraphResponseSchema = z.object({
  schema: z.string().catch("kanban-chain-graph-v1"),
  root_id: z.string(),
  checked_at: epochSeconds,
  nodes: z.array(ChainGraphNodeSchema).catch([]),
  edges: z.array(z.object({ from: z.string().catch(""), to: z.string().catch("") })).catch([]),
});
