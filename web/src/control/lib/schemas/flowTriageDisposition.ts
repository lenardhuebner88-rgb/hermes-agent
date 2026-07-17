import { z } from "zod";

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
