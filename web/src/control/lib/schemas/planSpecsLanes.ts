import { z } from "zod";
import {
  nullableEpochSeconds,
} from "./common";

export const PlanSpecRecordSchema = z.object({
  path: z.string().catch(""),
  agent: z.string().catch(""),
  filename: z.string().catch(""),
  topic: z.string().catch(""),
  status: z.string().catch(""),
  freigabe: z.string().catch(""),
  live_test_depth: z.string().nullable().catch(null),
  binding: z.boolean().catch(false),
  subtask_count: z.coerce.number().catch(0),
  valid: z.boolean().catch(false),
  open: z.boolean().catch(false),
  closed_reason: z.string().nullable().catch(null),
  closed_at: z.union([z.string(), z.number()]).nullable().optional().catch(undefined),
  closed_by: z.string().nullable().optional().catch(undefined),
  receipt: z.string().nullable().optional().catch(undefined),
  release_evidence: z.string().nullable().optional().catch(undefined),
  kanban_root_task_id: z.string().nullable().catch(null),
  kanban_root_status: z.string().nullable().catch(null),
  kanban_state: z.enum(["not_ingested", "queued", "running", "blocked", "completed", "done", "archived", "unknown"]).catch("not_ingested"),
  kanban_child_total: z.coerce.number().catch(0),
  kanban_child_done: z.coerce.number().catch(0),
  kanban_child_blocked: z.coerce.number().catch(0),
  kanban_child_running: z.coerce.number().catch(0),
  kanban_ingested_at: nullableEpochSeconds,
  ingest_disposition: z.string().catch("not_ingestable"),
  ingest_would_block: z.coerce.boolean().catch(true),
  ingest_findings: z.array(z.string()).catch([]),
  errors: z.array(z.string()).catch([]),
  action_state: z.enum([
    "draft", "ready", "held", "handed_off", "running", "blocked", "completed", "archived",
  ]).optional().catch(undefined),
  action_reason: z.string().optional().catch(undefined),
  next_action: z.enum([
    "none", "edit", "review_findings", "preview_ingest", "release", "open_task", "open_result",
  ]).optional().catch(undefined),
  dependency_count: z.coerce.number().int().nonnegative().optional().catch(undefined),
  finding_count: z.coerce.number().int().nonnegative().optional().catch(undefined),
  target_board: z.string().nullable().optional().catch(undefined),
});

export const PlanSpecSummarySchema = z.object({
  draft: z.coerce.number().int().nonnegative().catch(0),
  ready: z.coerce.number().int().nonnegative().catch(0),
  held: z.coerce.number().int().nonnegative().catch(0),
  handed_off: z.coerce.number().int().nonnegative().catch(0),
  running: z.coerce.number().int().nonnegative().catch(0),
  blocked: z.coerce.number().int().nonnegative().catch(0),
  completed: z.coerce.number().int().nonnegative().catch(0),
  archived: z.coerce.number().int().nonnegative().catch(0),
  total_matching: z.coerce.number().int().nonnegative().catch(0),
  observed_at: z.coerce.number().catch(0),
});

export const PlanSpecsResponseSchema = z.object({
  planspecs: z.array(PlanSpecRecordSchema),
  count: z.coerce.number().catch(0),
  summary: PlanSpecSummarySchema.catch({
    draft: 0,
    ready: 0,
    held: 0,
    handed_off: 0,
    running: 0,
    blocked: 0,
    completed: 0,
    archived: 0,
    total_matching: 0,
    observed_at: 0,
  }),
});
export type PlanSpecsResponse = z.infer<typeof PlanSpecsResponseSchema>;
// PlanSpec detail drawer (E2): full structured payload from
// GET /planspecs/detail?path=…. Every field is optional/tolerant so a
// partially-written PlanSpec file still opens without throwing.
const PlanSpecDetailSubtaskSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  lane: z.string().catch(""),
  review_tier: z.string().nullable().optional().catch(undefined),
  deps: z.array(z.string()).catch([]),
  scope_files: z.array(z.string()).optional().catch(undefined),
  body: z.string().optional().catch(undefined),
  instructions: z.string().optional().catch(undefined),
  kind: z.string().nullable().optional().catch(undefined),
  max_iterations: z.coerce.number().int().positive().nullable().optional().catch(undefined),
  acceptance_criteria: z.array(z.unknown()).optional().catch(undefined),
}).passthrough();

export const PlanSpecDetailResponseSchema = z.object({
  goal: z.string(),
  acceptance_criteria: z.array(
    z.object({ id: z.string().optional(), statement: z.string().optional() })
      .passthrough()
      .catch({}),
  ).catch([]),
  anti_scope: z.array(z.string()).catch([]),
  evidence_required: z.array(z.string()).catch([]),
  freigabe: z.string().catch(""),
  live_test_depth: z.string().catch(""),
  target_board: z.string().nullable().optional().catch(undefined),
  dependency_count: z.coerce.number().int().nonnegative().optional().catch(undefined),
  finding_count: z.coerce.number().int().nonnegative().optional().catch(undefined),
  subtasks: z.array(PlanSpecDetailSubtaskSchema).catch([]),
  source_state: z.enum(["binding", "draft", "closed", "invalid"]).optional().catch(undefined),
  source_findings: z.array(z.string()).optional().catch(undefined),
  // Additive: only present for a dashboard prose-plan source (no YAML
  // frontmatter — see hermes_cli.planspecs.parse_prose_plan_detail);
  // absent/undefined for every binding PlanSpec, unchanged.
  prose_plan: z.boolean().optional(),
  full_text: z.string().optional(),
});
export type PlanSpecDetailResponse = z.infer<typeof PlanSpecDetailResponseSchema>;
export type PlanSpecDetailSubtask = z.infer<typeof PlanSpecDetailSubtaskSchema>;

export const PlanSpecTransitionPreviewSchema = z.object({
  source_digest: z.string().length(64),
  action: z.literal("create_held_chain"),
  target_board: z.string(),
  root_cards_created: z.coerce.number().int().nonnegative(),
  child_tasks_created: z.coerce.number().int().nonnegative(),
  starts_worker: z.boolean(),
  live_test_depth: z.string(),
  workspace_mode: z.string(),
  review_floor: z.enum(["standard", "review", "critical"]),
  auto_scout_tasks: z.coerce.number().int().nonnegative(),
  lanes_resolvable: z.boolean(),
  blocking_findings: z.array(z.string()),
  warnings: z.array(z.string()),
  can_ingest: z.boolean(),
});
export type PlanSpecTransitionPreview = z.infer<typeof PlanSpecTransitionPreviewSchema>;
// ─── Lane-Preset-Catalog (GET /api/plugins/kanban/lanes) ──────────────────────
// Benötigt für das Plan-Cockpit: Modell-Optionen je Lane-Profil.
// Tolerant: ältere Server ohne models-Feld liefern leere Liste.

const LaneModelOptionSchema = z.object({
  id: z.string().catch(""),
  label: z.string().catch(""),
  runtime: z.string().catch("hermes"),
  provider: z.string().nullable().catch(null),
  group: z.string().catch(""),
  locked: z.boolean().catch(false),
  source: z.string().optional(),
});

const LaneCatalogProfileSchema = z.object({
  name: z.string().catch(""),
  worker_runtime: z.string().catch("hermes"),
  default_model: z.string().nullable().catch(null),
  default_provider: z.string().nullable().catch(null),
  description: z.string().catch(""),
  locked: z.boolean().catch(false),
  locked_reason: z.string().nullable().catch(null),
}).passthrough();

export const LanesCatalogResponseSchema = z.object({
  lanes: z.array(z.unknown()),
  count: z.coerce.number().catch(0),
  active_id: z.string().nullable().catch(null),
  profiles: z.array(LaneCatalogProfileSchema).catch([]),
  models: z.array(LaneModelOptionSchema).catch([]),
}).passthrough();

export type LaneCatalogProfile = z.infer<typeof LaneCatalogProfileSchema>;
export type LaneModelOptionRecord = z.infer<typeof LaneModelOptionSchema>;
export type LanesCatalogResponse = z.infer<typeof LanesCatalogResponseSchema>;
