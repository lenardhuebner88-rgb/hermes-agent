import { z } from "zod";
import {
  nullableEpochSeconds,
  TaskDeliverableSchema,
  TaskLinksSchema,
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
    started_at: nullableEpochSeconds,
    ended_at: nullableEpochSeconds,
    runtime_seconds: z.coerce.number().nullable().catch(null),
    input_tokens: z.coerce.number().nullable().catch(null),
    output_tokens: z.coerce.number().nullable().catch(null),
    cost_usd: z.coerce.number().nullable().catch(null),
  })).catch([]),
  deliverables: z.array(TaskDeliverableSchema).catch([]),
  links: TaskLinksSchema.default({ parents: [], children: [], parent_states: [], child_states: [] }),
}).passthrough().catch({
  task: null,
  runs: [],
  deliverables: [],
  links: { parents: [], children: [], parent_states: [], child_states: [] },
});
export type TaskBodyResponse = z.infer<typeof TaskBodySchema>;
