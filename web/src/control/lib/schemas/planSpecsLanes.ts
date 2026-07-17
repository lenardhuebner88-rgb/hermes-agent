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
});

export const PlanSpecsResponseSchema = z.object({
  planspecs: z.array(PlanSpecRecordSchema),
  count: z.coerce.number().catch(0),
});
export type PlanSpecsResponse = z.infer<typeof PlanSpecsResponseSchema>;
// PlanSpec detail drawer (E2): full structured payload from
// GET /planspecs/detail?path=…. Every field is optional/tolerant so a
// partially-written PlanSpec file still opens without throwing.
const PlanSpecDetailSubtaskSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  lane: z.string().catch(""),
  deps: z.array(z.string()).catch([]),
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
  subtasks: z.array(PlanSpecDetailSubtaskSchema).catch([]),
  // Additive: only present for a dashboard prose-plan source (no YAML
  // frontmatter — see hermes_cli.planspecs.parse_prose_plan_detail);
  // absent/undefined for every binding PlanSpec, unchanged.
  prose_plan: z.boolean().optional(),
  full_text: z.string().optional(),
});
export type PlanSpecDetailResponse = z.infer<typeof PlanSpecDetailResponseSchema>;
export type PlanSpecDetailSubtask = z.infer<typeof PlanSpecDetailSubtaskSchema>;
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
