import { z } from "zod";
import { nullableEpochSeconds, nullableString } from "./common";

// ── Funnel-Drafts (Demand-Funnel Freigabe-Queue) ─────────────────────────────
// Spiegelt funnel.draft_dict() (hermes_cli/funnel.py) — die Felder, die der
// GET /funnel/drafts-Handler pro Draft liefert.
export const FunnelDraftSchema = z.object({
  id: z.string(),
  title: z.string().catch(""),
  created_by: z.string().catch(""),
  assignee: nullableString,
  completed_at: nullableEpochSeconds,
  draft_excerpt: nullableString,
  draft_text: nullableString,
  operator_edited: z.boolean().catch(false),
  revision_of: nullableString,
  spend_alert: z.boolean().catch(false),
});
export type FunnelDraft = z.infer<typeof FunnelDraftSchema>;

export const FunnelDraftListResponseSchema = z.object({
  drafts: z.array(FunnelDraftSchema).catch([]),
});
export type FunnelDraftListResponse = z.infer<typeof FunnelDraftListResponseSchema>;

// ── Disposition-Items (FRD Phase 3b) ────────────────────────────────────────
export const DispositionItemSchema = z.object({
  id: z.string(),
  source_task_id: z.string(),
  typ: z.enum(["risk", "follow_up", "still_open"]).catch("still_open" as const),
  disposition: z.enum(["done", "delegate", "defer", "drop"]).catch("done" as const),
  next_action: z.string().nullable().catch(null),
  severity: z.enum(["real-risk", "scope-note", "none"]).catch("none" as const),
  evidence: z.string().nullable().catch(null),
  status: z.enum(["open", "accepted", "task_created", "dismissed", "superseded"]).catch("open" as const),
  supersedes_id: z.string().nullable().catch(null),
  created_at: z.number(),
  decided_at: z.number().nullable().catch(null),
  decided_by: z.string().nullable().catch(null),
});
export type DispositionItem = z.infer<typeof DispositionItemSchema>;

export const DispositionListResponseSchema = z.object({
  items: z.array(DispositionItemSchema).catch([]),
});
export type DispositionListResponse = z.infer<typeof DispositionListResponseSchema>;
