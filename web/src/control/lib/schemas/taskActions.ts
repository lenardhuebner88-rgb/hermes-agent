import { z } from "zod";
import {
  epochSeconds,
  ModelRouteStateSchema,
  nullableEpochSeconds,
  TaskDeliverableSchema,
  TaskLinksSchema,
  VaultMemoryLinkSchema,
} from "./common";

// Worker-Drawer-Steuerung (Gap 1) — POST /workers/{run_id}/action's response.
// A guard refusal (e.g. "confirm required", "no active claim") comes back as
// `ok: false` at HTTP 200, NOT a thrown error — see plugin_api.py's own doc
// comment on the endpoint. `task_id` is absent for action="dispatch" (a bare
// dispatcher tick has no single task); every other action carries it.
export const WorkerActionResponseSchema = z.object({
  ok: z.boolean().catch(false),
  action: z.string().optional(),
  run_id: z.coerce.string().optional(),
  task_id: z.string().nullable().optional(),
  detail: z.string().optional(),
});
export type WorkerActionResponse = z.infer<typeof WorkerActionResponseSchema>;

// POST /runs/{run_id}/terminate's response — 404/409 raise HTTPException (fetchJSON
// throws before this ever parses); 200 is always ok:true.
export const TerminateRunResponseSchema = z.object({
  ok: z.boolean().catch(false),
  run_id: z.coerce.string().optional(),
  task_id: z.string().optional(),
});
export type TerminateRunResponse = z.infer<typeof TerminateRunResponseSchema>;
const TaskRunSchema = z.object({
  id: z.coerce.string(),
  profile: z.string().nullable().catch(null),
  status: z.string().catch(""),
  outcome: z.string().nullable().catch(null),
  summary: z.string().nullable().catch(null),
  error: z.string().nullable().catch(null),
  started_at: nullableEpochSeconds,
  ended_at: nullableEpochSeconds,
  run_role: z.string().nullable().catch(null),
  run_role_label: z.string().nullable().catch(null),
  requested_provider: z.string().nullable().catch(null),
  requested_model: z.string().nullable().catch(null),
  active_provider: z.string().nullable().catch(null),
  active_model: z.string().nullable().catch(null),
  model_state: ModelRouteStateSchema,
  model_source: z.string().nullable().catch(null),
  model_observed_at: nullableEpochSeconds,
  effective_model: z.string().nullable().catch(null),
});
const TaskEventSchema = z.object({
  id: z.coerce.number().catch(0),
  kind: z.string().catch(""),
  created_at: epochSeconds,
  run_id: z.coerce.string().nullable().catch(null),
  // Free-form event payload. The Flow rail reads `decomposed.child_ids`
  // (the subtask group) and `flow_plan.spec` (the Vault plan-spec link).
  payload: z.record(z.string(), z.unknown()).nullable().catch(null),
});
const TaskCommentSchema = z.object({
  id: z.coerce.number().catch(0),
  task_id: z.coerce.string().catch(""),
  author: z.string().nullable().catch(null),
  body: z.string().catch(""),
  created_at: epochSeconds,
  kind: z.string().nullable().catch(null),
});
const TaskDiagnosticActionSchema = z.object({
  kind: z.string().catch(""),
  label: z.string().catch(""),
  payload: z.record(z.string(), z.unknown()).nullable().catch(null),
  suggested: z.boolean().nullable().catch(null),
}).passthrough();
const TaskDiagnosticSchema = z.object({
  kind: z.string().catch(""),
  severity: z.string().nullable().catch(null),
  title: z.string().catch(""),
  detail: z.string().nullable().catch(null),
  actions: z.array(TaskDiagnosticActionSchema).catch([]),
  first_seen_at: nullableEpochSeconds,
  last_seen_at: nullableEpochSeconds,
  count: z.coerce.number().nullable().catch(null),
  run_id: z.coerce.string().nullable().catch(null),
  data: z.record(z.string(), z.unknown()).nullable().catch(null),
}).passthrough();
const TaskDiagnosticWarningsSchema = z.object({
  count: z.coerce.number().catch(0),
  kinds: z.record(z.string(), z.coerce.number()).catch({}),
  latest_at: nullableEpochSeconds,
  highest_severity: z.string().nullable().catch(null),
}).passthrough();
const TaskDetailTaskSchema = z.object({
  id: z.coerce.string().catch(""),
  title: z.string().catch(""),
  body: z.string().nullable().catch(null),
  status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("todo"),
  assignee: z.string().nullable().catch(null),
  latest_summary: z.string().nullable().catch(null),
  result: z.string().nullable().catch(null),
  summary: z.string().nullable().catch(null),
  closure: z.string().nullable().catch(null),
  block_reason: z.string().nullable().catch(null),
  operator_question: z.boolean().catch(false),
  diagnostics: z.array(TaskDiagnosticSchema).catch([]),
  warnings: TaskDiagnosticWarningsSchema.nullable().catch(null),
  vault_memory_links: z.array(VaultMemoryLinkSchema).catch([]),
}).partial().catch({});
export const TaskDetailResponseSchema = z.object({
  task: TaskDetailTaskSchema.nullable().catch(null),
  comments: z.array(TaskCommentSchema).catch([]),
  runs: z.array(TaskRunSchema).catch([]),
  events: z.array(TaskEventSchema).catch([]),
  deliverables: z.array(TaskDeliverableSchema).catch([]),
  links: TaskLinksSchema.default({ parents: [], children: [], parent_states: [], child_states: [] }),
});
export type TaskRun = z.infer<typeof TaskRunSchema>;
export type TaskEvent = z.infer<typeof TaskEventSchema>;
export type TaskComment = z.infer<typeof TaskCommentSchema>;
export type TaskDiagnostic = z.infer<typeof TaskDiagnosticSchema>;
export type TaskDetailResponse = z.infer<typeof TaskDetailResponseSchema>;
// POST /api/plugins/kanban/tasks/{id}/reassign.
// 409 guards throw before parsing; 200 carries the new nullable assignee.
export const TaskReassignResponseSchema = z.object({
  ok: z.boolean().catch(false),
  task_id: z.string().catch(""),
  assignee: z.string().nullable().catch(null),
}).passthrough();
export type TaskReassignResponse = z.infer<typeof TaskReassignResponseSchema>;
