import { z } from "zod";

export const nullableNumber = z.number().nullable().catch(null);
export const nullableString = z.string().nullable().catch(null);
// A malformed timestamp is materially different from an absent timestamp.
// Preserve contamination as NaN so guarded renderers can say "Zeit ungültig"
// instead of inventing epoch zero or silently omitting the field.
export const invalidEpochSeconds = Number.NaN;
export const epochSeconds = z.coerce.number().catch(invalidEpochSeconds);
export const nullableEpochSeconds = z.coerce.number().nullable().default(null).catch(invalidEpochSeconds);
export const LastOutcomeSchema = z.string().nullable().catch(null);
export const VerifierVerdictSchema = z.enum(["APPROVED", "REQUEST_CHANGES"]);
export const VerificationStateSchema = z.enum(["approved", "request_changes", "pending", "ungated"]);
export const ResultQualityBadgeSchema = z.object({
  state: z.enum(["verifier_approved", "ungated", "rejected_needs_work", "unknown_legacy"]).catch("unknown_legacy"),
  label: z.string().catch("Unknown legacy"),
  tone: z.enum(["emerald", "cyan", "sky", "indigo", "amber", "rose", "red", "zinc", "violet"]).catch("zinc"),
  description: z.string().catch("Legacy run has no verifier metadata or profile lineage."),
}).catch({
  state: "unknown_legacy",
  label: "Unknown legacy",
  tone: "zinc",
  description: "Legacy run has no verifier metadata or profile lineage.",
});
export const RunRoleSchema = z.enum(["implementation", "verification", "legacy_unknown"]);
export const RunRoleSourceSchema = z.enum(["claimed_event", "missing_claim_event"]);
// task_runs.status/outcome are open lifecycle vocabularies. The live DB carries
// states beyond the old seven-value UI enum (review, reclaimed, spawn_failed,
// integration_parked, transient_retry, ...). Preserve every non-empty backend
// value; only malformed/empty input becomes an explicit unknown/null.
export const RunStatusSchema = z.string().trim().min(1).catch("unknown");
export const RunOutcomeSchema = z.string().trim().min(1).nullable().catch(null);
export const ModelRouteStateSchema = z.enum(["planned", "in_flight", "confirmed", "unknown"]).nullable().catch(null);

export const TaskStatusSchema = z
  .enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"])
  .catch("todo");

const TaskDeliverableSchema = z.object({
  filename: z.string().catch(""),
  relative_path: z.string().catch(""),
  size: z.coerce.number().catch(0),
  mtime: z.coerce.number().catch(0),
  content_type: z.string().catch("application/octet-stream"),
  url: z.string().catch(""),
});

const TaskArtifactLinkSchema = TaskDeliverableSchema.extend({
  path: z.string().catch(""),
  source: z.enum(["metadata.artifacts", "deliverables_preserved"]).catch("metadata.artifacts"),
});

export { TaskDeliverableSchema, TaskArtifactLinkSchema };

export const VaultMemoryLinkSchema = z.object({
  kind: z.enum(["vault", "memory"]).catch("vault"),
  label: z.string().catch(""),
  target: z.string().catch(""),
  source: z.string().catch(""),
  path: z.string().nullable().catch(null),
  display_path: z.string().catch(""),
  exists: z.boolean().nullable().catch(null),
  obsidian_url: z.string().nullable().catch(null),
  url: z.string().nullable().catch(null),
});

// GET /tasks/{task_id}/chain-costs — Kosten-Rollup je Kette + by_lane-Aufschlüsselung.
export const ChainCostsLaneSchema = z.object({
  profile: z.string().catch("unbekannt"),
  input_tokens: z.coerce.number().catch(0),
  output_tokens: z.coerce.number().catch(0),
  cost_usd: z.coerce.number().catch(0),
  actual_cost_usd: z.coerce.number().catch(0),
  run_count: z.coerce.number().catch(0),
  // Geschätzter API-Gegenwert für Abo-Runs (additiv; ältere Payloads liefern 0).
  cost_usd_equivalent: z.coerce.number().catch(0),
  api_equivalent_usd: z.coerce.number().catch(0),
  cost_effective_usd: z.coerce.number().catch(0),
  billing_neuralwatt_kwh: z.coerce.number().catch(0),
  billing_neuralwatt_cost_usd: z.coerce.number().catch(0),
});

const LinkedTaskStateSchema = z.object({
  id: z.coerce.string(),
  title: z.string().catch("Ohne Titel"),
  status: TaskStatusSchema,
});
const TaskLinksSchema = z.object({
  parents: z.array(z.coerce.string()).catch([]),
  children: z.array(z.coerce.string()).catch([]),
  parent_states: z.array(LinkedTaskStateSchema).catch([]),
  child_states: z.array(LinkedTaskStateSchema).catch([]),
}).catch({ parents: [], children: [], parent_states: [], child_states: [] });

export { LinkedTaskStateSchema, TaskLinksSchema };

export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new Error(`[Hermes Control] ${label} entspricht nicht dem Vertrag: ${result.error.message}`);
  }
  return result.data;
}
