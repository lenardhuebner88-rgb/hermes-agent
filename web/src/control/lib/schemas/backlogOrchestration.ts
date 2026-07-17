import { z } from "zod";
import {
  epochSeconds,
  nullableNumber,
} from "./common";

const OperatorInventoryLeverSchema = z.object({
  action: z.string().catch("observe"),
  label: z.string().catch("Alles ruhig"),
  detail: z.string().catch("Keine Inventar-Hebel erkannt."),
  tone: z.enum(["emerald", "cyan", "sky", "indigo", "amber", "rose", "red", "zinc", "violet"]).catch("zinc"),
  count: z.coerce.number().catch(0),
  target: z.string().catch("/control/system"),
  mutation: z.literal("none").catch("none"),
});

const OperatorInventorySummarySchema = z.object({
  worktrees_total: z.coerce.number().catch(0),
  worktrees_locked: z.coerce.number().catch(0),
  worktrees_dirty: z.coerce.number().catch(0),
  worktrees_prunable: z.coerce.number().catch(0),
  worktrees_orphaned: z.coerce.number().catch(0),
  worktrees_status_unknown: z.coerce.number().catch(0),
  actors_total: z.coerce.number().catch(0),
  actors_canonical: z.coerce.number().catch(0),
});

const OperatorInventoryWorktreeSchema = z.object({
  id: z.string().catch("unknown"),
  path_label: z.string().catch("unknown"),
  branch: z.string().catch("unknown"),
  head: z.string().nullable().catch(null),
  relation: z.string().catch("manual"),
  task_hint: z.string().nullable().catch(null),
  state: z.enum(["clean", "dirty", "locked", "prunable", "unknown"]).catch("unknown"),
  locked: z.boolean().catch(false),
  prunable: z.boolean().catch(false),
  detached: z.boolean().catch(false),
  dirty_count: z.coerce.number().nullable().catch(null),
  untracked_count: z.coerce.number().nullable().catch(null),
  status_checked: z.boolean().catch(false),
  orphaned: z.boolean().catch(false),
});

const OperatorInventoryActorSchema = z.object({
  role: z.string().catch("unknown"),
  label: z.string().catch("Actor"),
  count: z.coerce.number().catch(0),
  cpu_percent: nullableNumber,
  rss_mb: nullableNumber,
  oldest_age_seconds: z.coerce.number().nullable().catch(null),
  source: z.enum(["canonical", "process"]).catch("process"),
  confidence: z.string().catch("medium"),
  stale_count: z.coerce.number().catch(0),
  target: z.string().catch("/control/system"),
  controllable: z.boolean().catch(false),
});

export const OperatorInventoryResponseSchema = z.object({
  schema: z.string().catch("hermes-operator-inventory-v1"),
  checked_at: epochSeconds,
  summary: OperatorInventorySummarySchema,
  next_lever: OperatorInventoryLeverSchema,
  levers: z.array(OperatorInventoryLeverSchema).catch([]),
  worktrees: z.array(OperatorInventoryWorktreeSchema).catch([]),
  actors: z.array(OperatorInventoryActorSchema).catch([]),
  errors: z.array(z.string()).catch([]),
});
// Read-only family-organizer backlog board. Mirrors the frontmatter "Feld-Vertrag"
// (family-organizer backlog/README.md) served by GET /api/family-organizer/backlog.
// Tolerant (.catch defaults) so a partial/stale payload still renders the board.
// v2 per-item facts (deterministic, server-computed). Optional + tolerant so a v1
// payload (fields absent) still parses and the client falls back to its own heuristics.
export const BacklogQualityIssueSchema = z.object({
  code: z.string().catch(""),
  severity: z.string().catch("warn"),
});

export const BacklogItemSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: z.string().catch(""),
  owner: z.string().catch("unassigned"),
  risk: z.string().catch(""),
  area: z.string().catch(""),
  updated: z.string().catch(""),
  lane: z.string().nullable().catch(null),
  result: z.string().nullable().catch(null),
  stale: z.boolean().catch(false),
  excerpt: z.string().optional().catch(undefined),
  source_path: z.string().optional().catch(undefined),
  missing_acceptance: z.boolean().optional().catch(undefined),
  missing_next_action: z.boolean().optional().catch(undefined),
  age_days: z.number().nullable().optional().catch(undefined),
  freshness: z.string().optional().catch(undefined),
  quality_issues: z.array(BacklogQualityIssueSchema).optional().catch(undefined),
  readiness: z.string().optional().catch(undefined),
});

export const BacklogDetailSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  status: z.string().catch(""),
  owner: z.string().catch(""),
  risk: z.string().catch(""),
  area: z.string().catch(""),
  updated: z.string().catch(""),
  lane: z.string().nullable().catch(null),
  result: z.string().nullable().catch(null),
  stale: z.boolean().catch(false),
  age_days: z.number().nullable().optional().catch(undefined),
  freshness: z.string().optional().catch(undefined),
  quality_issues: z.array(BacklogQualityIssueSchema).optional().catch(undefined),
  readiness: z.string().optional().catch(undefined),
  missing_acceptance: z.boolean().optional().catch(undefined),
  missing_next_action: z.boolean().optional().catch(undefined),
  body: z.string().catch(""),
  decision: z.array(z.string()).catch([]),
  acceptance_criteria: z.array(z.string()).catch([]),
  proofs: z.array(z.string()).catch([]),
  blockers: z.array(z.string()).catch([]),
  next_action: z.string().catch(""),
  source_path: z.string().catch(""),
  source_ref: z.string().catch(""),
  links: z.array(z.object({
    label: z.string().catch(""),
    href: z.string().catch(""),
  })).catch([]),
  error: z.string().optional(),
});

const BacklogUnknownStatusSchema = z.object({
  status: z.string().catch("(missing)"),
  count: z.coerce.number().catch(0),
  ids: z.array(z.string()).catch([]),
});

export const BacklogContractHealthSchema = z.object({
  source_count: z.coerce.number().catch(0),
  counted_sum: z.coerce.number().catch(0),
  unknown_statuses: z.array(BacklogUnknownStatusSchema).catch([]),
  invalid_risk_count: z.coerce.number().catch(0),
  invalid_owner_count: z.coerce.number().catch(0),
  unowned_count: z.coerce.number().catch(0),
  stale_count: z.coerce.number().catch(0),
  missing_acceptance_count: z.coerce.number().catch(0),
  missing_next_action_count: z.coerce.number().catch(0),
  invalid_area_count: z.coerce.number().catch(0),
});

export const BacklogResponseSchema = z.object({
  schema: z.string().catch("fo-backlog-v1"),
  checked_at: epochSeconds,
  items: z.array(BacklogItemSchema).catch([]),
  counts: z.object({
    now: z.coerce.number().catch(0),
    next: z.coerce.number().catch(0),
    in_progress: z.coerce.number().catch(0),
    blocked: z.coerce.number().catch(0),
    later: z.coerce.number().catch(0),
    done: z.coerce.number().catch(0),
  }).catch({ now: 0, next: 0, in_progress: 0, blocked: 0, later: 0, done: 0 }),
  contract_health: BacklogContractHealthSchema.optional().catch(undefined),
  source: z.object({
    dir: z.string().catch(""),
    ref: z.string().catch(""),
    count: z.coerce.number().catch(0),
  }).catch({ dir: "", ref: "", count: 0 }),
  error: z.string().nullable().catch(null),
});

export type BacklogItem = z.infer<typeof BacklogItemSchema>;
export type BacklogDetail = z.infer<typeof BacklogDetailSchema>;
export type BacklogContractHealth = z.infer<typeof BacklogContractHealthSchema>;
export type BacklogResponse = z.infer<typeof BacklogResponseSchema>;
export type BacklogQualityIssue = z.infer<typeof BacklogQualityIssueSchema>;

// Read-only Orchestrator backlog board. Mirrors the Backlog.md-style frontmatter
// (status/priority/dependsOn/planGate/created) served by GET /api/orchestration/backlog.
// Deliberately a separate schema/view from the family-organizer board (different
// contract — no premature generalisation). Unknown status/priority values stay
// visible as raw strings; contract_health carries the drift counters.
export const OrchestrationItemSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: z.string().catch(""),
  priority: z.string().catch(""),
  dependsOn: z.array(z.string()).catch([]),
  planGate: z.boolean().catch(false),
  created: z.string().catch(""),
  root: z.string().optional().catch(undefined),
  owner: z.string().optional().catch(undefined),
  source: z.string().optional().catch(undefined),
  lastProof: z.string().optional().catch(undefined),
  excerpt: z.string().optional().catch(undefined),
});

export const OrchestrationContractHealthSchema = z.object({
  source_count: z.coerce.number().catch(0),
  counted_sum: z.coerce.number().catch(0),
  unknown_statuses: z.array(z.object({
    status: z.string().catch(""),
    count: z.coerce.number().catch(0),
    ids: z.array(z.string()).catch([]),
  })).catch([]),
  invalid_priority_count: z.coerce.number().catch(0),
  missing_dep_count: z.coerce.number().catch(0),
}).catch({
  source_count: 0,
  counted_sum: 0,
  unknown_statuses: [],
  invalid_priority_count: 0,
  missing_dep_count: 0,
});

export const OrchestrationDetailSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  status: z.string().catch(""),
  priority: z.string().catch(""),
  dependsOn: z.array(z.string()).catch([]),
  planGate: z.boolean().catch(false),
  gate: z.string().catch(""),
  root: z.string().catch(""),
  owner: z.string().catch(""),
  source: z.string().catch(""),
  closed: z.string().catch(""),
  lastProof: z.string().catch(""),
  proofs: z.array(z.string()).catch([]),
  links: z.array(z.object({
    label: z.string().catch(""),
    href: z.string().catch(""),
  })).catch([]),
  created: z.string().catch(""),
  body: z.string().catch(""),
  error: z.string().optional(),
});

export const OrchestrationBacklogResponseSchema = z.object({
  schema: z.string().catch("orchestration-backlog-v1"),
  checked_at: epochSeconds,
  items: z.array(OrchestrationItemSchema).catch([]),
  counts: z.object({
    backlog: z.coerce.number().catch(0),
    todo: z.coerce.number().catch(0),
    doing: z.coerce.number().catch(0),
    review: z.coerce.number().catch(0),
    done: z.coerce.number().catch(0),
  }).catch({ backlog: 0, todo: 0, doing: 0, review: 0, done: 0 }),
  contract_health: OrchestrationContractHealthSchema,
  source: z.object({
    dir: z.string().catch(""),
    ref: z.string().catch(""),
    count: z.coerce.number().catch(0),
  }).catch({ dir: "", ref: "", count: 0 }),
  error: z.string().nullable().catch(null),
});

export type OrchestrationItem = z.infer<typeof OrchestrationItemSchema>;
export type OrchestrationDetail = z.infer<typeof OrchestrationDetailSchema>;
export type OrchestrationBacklogResponse = z.infer<typeof OrchestrationBacklogResponseSchema>;
