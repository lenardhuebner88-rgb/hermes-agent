import { z } from "zod";
import {
  ModelRouteStateSchema,
  nullableEpochSeconds,
  TaskDeliverableSchema,
  TaskLinksSchema,
  VaultMemoryLinkSchema,
} from "./common";

// Fleet Ketten-Detail-Drawer: GET /tasks/{id}/deliverables
// Separate Deliverables-Antwort (wird nur bei offenem Drawer geladen).
export const TaskDeliverablesResponseSchema = z.object({
  task_id: z.coerce.string().catch(""),
  deliverables: z.array(TaskDeliverableSchema).catch([]),
});
export type TaskDeliverablesResponse = z.infer<typeof TaskDeliverablesResponseSchema>;
export type TaskDeliverable = z.infer<typeof TaskDeliverableSchema>;

// Fleet Ketten-Detail-Drawer: GET /tasks/{id} — Body + Acceptance-Criteria
// für die Übersicht-Tab. Wir picken hier nur die für den Drawer relevanten Felder.
// (Vollständig: TaskDetailResponseSchema oben — derselbe Endpoint.)
export const TaskBodySchema = z.object({
  task: z.object({
    id: z.coerce.string().catch(""),
    title: z.string().catch(""),
    body: z.string().nullable().catch(null),
    status: z.string().catch(""),
    assignee: z.string().nullable().catch(null),
    block_reason: z.string().nullable().catch(null),
    operator_question: z.boolean().catch(false),
    created_at: nullableEpochSeconds,
    started_at: nullableEpochSeconds,
    completed_at: nullableEpochSeconds,
    priority: z.number().nullable().catch(null),
    result: z.string().nullable().catch(null),
    latest_summary: z.string().nullable().catch(null),
    summary: z.string().nullable().catch(null),
    closure: z.string().nullable().catch(null),
    cost_usd: z.coerce.number().nullable().catch(null),
    planspec_source: z.string().nullable().catch(null),
    diagnostics: z.array(z.object({
      kind: z.string().catch(""),
      severity: z.string().nullable().catch(null),
      title: z.string().catch(""),
      detail: z.string().nullable().catch(null),
    }).passthrough()).catch([]),
    vault_memory_links: z.array(VaultMemoryLinkSchema).catch([]),
    archived_at: nullableEpochSeconds,
    due_at: nullableEpochSeconds,
    last_heartbeat_at: nullableEpochSeconds,
    review_tier: z.enum(["standard", "review", "critical"]).nullable().catch(null),
    branch_name: z.string().nullable().catch(null),
    model_override: z.string().nullable().catch(null),
    // Real API payload exposes workspace_kind / workspace_path (not "workspace")
    workspace_kind: z.string().nullable().catch(null),
    workspace_path: z.string().nullable().catch(null),
    // Acceptance-Criteria: der Drawer zeigt sie als Liste; raw text oder strukturiert.
    acceptance_criteria: z.union([
      z.array(z.string()),
      z.array(z.object({ statement: z.string().catch("") }).passthrough()),
      z.string(),
    ]).nullable().catch(null),
  }).nullable().catch(null),
  // Laufzeit aus dem jüngsten Run (für den Übersicht-Tab)
  runs: z.array(z.object({
    id: z.coerce.string(),
    profile: z.string().nullable().catch(null),
    status: z.string().catch(""),
    outcome: z.string().nullable().catch(null),
    summary: z.string().nullable().catch(null),
    error: z.string().nullable().catch(null),
    started_at: nullableEpochSeconds,
    ended_at: nullableEpochSeconds,
    runtime_seconds: z.coerce.number().nullable().catch(null),
    input_tokens: z.coerce.number().nullable().catch(null),
    output_tokens: z.coerce.number().nullable().catch(null),
    cost_usd: z.coerce.number().nullable().catch(null),
    requested_provider: z.string().nullable().catch(null),
    requested_model: z.string().nullable().catch(null),
    active_provider: z.string().nullable().catch(null),
    active_model: z.string().nullable().catch(null),
    model_state: ModelRouteStateSchema,
    model_source: z.string().nullable().catch(null),
    model_observed_at: nullableEpochSeconds,
    worker_session_id: z.string().nullable().catch(null),
  })).catch([]),
  comments: z.array(z.object({
    id: z.coerce.number().catch(0),
    author: z.string().nullable().catch(null),
    body: z.string().catch(""),
    created_at: nullableEpochSeconds,
  }).passthrough()).catch([]),
  events: z.array(z.object({
    id: z.coerce.number().catch(0),
    kind: z.string().catch(""),
    created_at: nullableEpochSeconds,
    payload: z.record(z.string(), z.unknown()).nullable().catch(null),
  }).passthrough()).catch([]),
  child_results: z.array(z.object({
    id: z.coerce.string().catch(""),
    title: z.string().catch(""),
    status: z.string().catch(""),
    latest_summary: z.string().nullable().catch(null),
    result: z.string().nullable().catch(null),
  }).passthrough()).catch([]),
  deliverables: z.array(TaskDeliverableSchema).catch([]),
  links: TaskLinksSchema.default({ parents: [], children: [], parent_states: [], child_states: [] }),
}).passthrough().catch({
  task: null,
  runs: [],
  comments: [],
  events: [],
  child_results: [],
  deliverables: [],
  links: { parents: [], children: [], parent_states: [], child_states: [] },
});
export type TaskBodyResponse = z.infer<typeof TaskBodySchema>;
