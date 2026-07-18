import { z } from "zod";
import {
  epochSeconds,
  VerifierVerdictSchema,
} from "./common";

// N-E1/E2: consolidated kanban decision queue. Each row is one operator
// decision. Unknown `kind`s coerce to "sticky_blocked" so a backend that grows
// a new decision kind degrades gracefully instead of dropping the whole list.
export const KanbanDecisionKindSchema = z
  .enum([
    "review_rejected",
    "budget_held",
    "role_fit_held",
    "sticky_blocked",
    "decompose_failed",
    "stranded_by_stuck_parent",
    "stranded_decompose_root_branch",
    "operator_escalation",
    "integration_parked",
    "rate_limited_loop",
    "release_gate_parked",
    "tree_root_woke",
    "deliverable_posted_not_completed",
    "disposition_risk",
    "disposition_stale",
  ])
  .catch("sticky_blocked");

const OperatorEscalationTaskSchema = z.object({
  id: z.string().catch(""),
  title: z.string().nullable().catch(null).optional(),
  status: z.string().nullable().catch(null).optional(),
  assignee: z.string().nullable().catch(null).optional(),
});

export const OperatorEscalationPayloadSchema = z.object({
  task: OperatorEscalationTaskSchema.catch({ id: "" }),
  source: z.string().nullable().catch(null).optional(),
  signal_key: z.string().nullable().catch(null).optional(),
  why_now: z.string().catch(""),
  attempts_already_made: z.coerce.number().catch(0),
  evidence: z.record(z.string(), z.unknown()).catch({}),
  recommended_human_action: z.string().catch(""),
  blocked_action_boundary: z.array(z.string()).catch([]),
}).nullable().catch(null);

export const ReleaseGatePayloadSchema = z.object({
  root_id: z.string().nullable().catch(null).optional(),
  source_task_id: z.string().nullable().catch(null).optional(),
  merge_commit: z.string().nullable().catch(null).optional(),
  commands: z.array(z.string()).catch([]),
  suggested_command: z.string().nullable().catch(null).optional(),
}).nullable().catch(null);

export const KanbanDecisionSchema = z.object({
  kind: KanbanDecisionKindSchema,
  task_id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  reason: z.string().catch(""),
  age_seconds: z.coerce.number().nullable().catch(null),
  suggested_command: z.string().nullable().catch(null),
  // disposition_risk trägt die Anzahl offener Risiko-Items (Block 8, kanban_db)
  // top-level mit — die Karte macht damit die Provenienz ("N offene Risiken aus
  // Abschluss") explizit statt den Task als neu blockiert erscheinen zu lassen.
  risk_count: z.coerce.number().nullable().catch(null).optional(),
  operator_escalation: OperatorEscalationPayloadSchema.optional(),
  release_gate: ReleaseGatePayloadSchema.optional(),
});

export const DecisionQueueResponseSchema = z.object({
  decisions: z.array(KanbanDecisionSchema),
  count: z.coerce.number().catch(0),
  checked_at: epochSeconds,
});
export const BlockedCompletionSchema = z.object({
  event_id: z.coerce.number().catch(0),
  run_id: z.coerce.string().nullable().catch(null).optional(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("blocked"),
  assignee: z.string().catch("hermes"),
  kind: z.enum(["completion_blocked_hallucination", "suspected_hallucinated_references", "verifier_request_changes"]).catch("completion_blocked_hallucination"),
  created_at: epochSeconds,
  summary_preview: z.string().nullable().catch(null),
  phantom: z.array(z.string()).catch([]),
  reviewer_profile: z.string().nullable().catch(null).optional(),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null).optional(),
  failure_output: z.array(z.string()).catch([]),
  fix_summary: z.string().nullable().catch(null).optional(),
});

export const BlockedCompletionsResponseSchema = z.object({
  blocked: z.array(BlockedCompletionSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: epochSeconds,
  since_hours: z.coerce.number().catch(48),
});
export type KanbanDecision = z.infer<typeof KanbanDecisionSchema>;
export type KanbanDecisionKind = z.infer<typeof KanbanDecisionKindSchema>;
export type DecisionQueueResponse = z.infer<typeof DecisionQueueResponseSchema>;
