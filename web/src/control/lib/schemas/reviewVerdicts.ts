import { z } from "zod";
import {
  epochSeconds,
  nullableEpochSeconds,
  VerificationStateSchema,
  VerifierVerdictSchema,
} from "./common";

export const KanbanReviewSchema = z.object({
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("review"),
  task_assignee: z.string().catch("hermes"),
  created_at: epochSeconds,
  submitted_at: nullableEpochSeconds,
  run_id: z.coerce.string().nullable().catch(null),
  reviewer_profile: z.string().nullable().catch(null),
  summary_preview: z.string().catch(""),
  verification_state: VerificationStateSchema.catch("pending"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verifier_evidence: z.array(z.string()).catch([]),
  active_verifier: z.boolean().catch(false),
  active_run_id: z.coerce.string().nullable().catch(null),
  review_run_state: z.enum(["active", "approved", "request_changes", "pending"]).catch("pending"),
  review_run_source: z.enum(["claimed_event", "latest_ended_run"]).nullable().catch(null),
});

export const ReviewVerdictsResponseSchema = z.object({
  reviews: z.array(KanbanReviewSchema),
  count: z.coerce.number().catch(0),
  checked_at: epochSeconds,
  limit: z.coerce.number().catch(12),
});
