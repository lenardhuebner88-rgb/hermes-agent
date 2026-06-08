import { z } from "zod";

const nullableNumber = z.number().nullable().catch(null);
const nullableString = z.string().nullable().catch(null);
const LastOutcomeSchema = z.enum(["applied", "reverted_no_improvement"]).nullable().catch(null);
const VerifierVerdictSchema = z.enum(["APPROVED", "REQUEST_CHANGES"]);
const VerificationStateSchema = z.enum(["approved", "request_changes", "pending", "ungated"]);
const ResultQualityBadgeSchema = z.object({
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
const RunRoleSchema = z.enum(["implementation", "verification", "legacy_unknown"]);
const RunRoleSourceSchema = z.enum(["claimed_event", "missing_claim_event"]);

export const RunInspectSchema = z.object({
  cpu_percent: z.coerce.number().catch(0),
  memory_info: z.object({ rss: z.coerce.number().catch(0) }).optional(),
  rss: z.coerce.number().optional(),
  num_threads: z.coerce.number().catch(0),
  num_fds: z.coerce.number().catch(0),
  status: z.string().catch("unknown"),
  create_time: z.coerce.number().optional(),
  cmdline: z.array(z.string()).optional(),
  alive: z.boolean().catch(false),
}).transform((v) => ({
  cpu_percent: v.cpu_percent,
  rss: v.rss ?? v.memory_info?.rss ?? 0,
  num_threads: v.num_threads,
  num_fds: v.num_fds,
  status: v.status,
  create_time: v.create_time,
  cmdline: v.cmdline,
  alive: v.alive,
}));

export const WorkerSchema = z.object({
  // The backend sends run_id as an integer (task_runs.id); the SPA treats it
  // as a string (React key, URL param, inspect map). Without coercion a numeric
  // id fails validation and — because the array has .catch([]) — silently
  // empties the ENTIRE worker list (count > 0 but zero cards rendered).
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("running"),
  task_assignee: z.string().catch("hermes"),
  profile: z.enum(["default", "admin", "coder", "devpower", "dispatcher", "kanbanops", "planner", "research", "critic", "verifier"]).catch("default"),
  worker_pid: z.coerce.number().catch(0),
  started_at: z.coerce.number().catch(0),
  claim_lock: z.string().catch(""),
  claim_expires: z.coerce.number().catch(0),
  last_heartbeat_at: z.coerce.number().catch(0),
  max_runtime_seconds: z.coerce.number().catch(0),
  run_status: z.enum(["running", "done", "blocked", "crashed", "timed_out", "failed", "released"]).catch("running"),
  run_outcome: z.enum(["completed", "blocked", "crashed", "timed_out", "spawn_failed", "gave_up", "reclaimed", "iteration_budget_exhausted"]).nullable().catch(null),
  block_reason: z.string().nullable().optional(),
  inspect: RunInspectSchema.nullable().optional(),
});

export const WorkersResponseSchema = z.object({
  workers: z.array(WorkerSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
});

const TaskStatusSchema = z
  .enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"])
  .catch("todo");

export const BoardTaskSchema = z.object({
  id: z.coerce.string(),
  title: z.string().catch("Ohne Titel"),
  status: TaskStatusSchema,
  assignee: z.string().nullable().catch(null),
  priority: z.coerce.number().catch(0),
  created_at: z.coerce.number().catch(0),
  started_at: z.coerce.number().nullable().catch(null),
  completed_at: z.coerce.number().nullable().catch(null),
  branch_name: z.string().nullable().catch(null),
  latest_summary: z.string().nullable().catch(null),
  link_counts: z.object({ parents: z.coerce.number().catch(0), children: z.coerce.number().catch(0) }).catch({ parents: 0, children: 0 }),
  comment_count: z.coerce.number().catch(0),
  progress: z.object({ done: z.coerce.number().catch(0), total: z.coerce.number().catch(0) }).nullable().catch(null),
  age: z
    .object({
      created_age_seconds: z.coerce.number().nullable().catch(null),
      started_age_seconds: z.coerce.number().nullable().catch(null),
      time_to_complete_seconds: z.coerce.number().nullable().catch(null),
    })
    .nullable()
    .catch(null),
});

export const BoardResponseSchema = z.object({
  columns: z.array(z.object({ name: z.string(), tasks: z.array(BoardTaskSchema).catch([]) })).catch([]),
  tenants: z.array(z.string()).catch([]),
  assignees: z.array(z.string()).catch([]),
  latest_event_id: z.coerce.number().catch(0),
  now: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
});


const TaskDeliverableSchema = z.object({
  filename: z.string().catch(""),
  relative_path: z.string().catch(""),
  size: z.coerce.number().catch(0),
  mtime: z.coerce.number().catch(0),
  content_type: z.string().catch("application/octet-stream"),
  url: z.string().catch(""),
});

export const KanbanResultSchema = z.object({
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("done"),
  task_assignee: z.string().catch("hermes"),
  profile: z.string().nullable().catch(null),
  run_role: RunRoleSchema.catch("legacy_unknown"),
  run_role_label: z.string().catch("Unknown / legacy run"),
  run_role_source: RunRoleSourceSchema.catch("missing_claim_event"),
  status: z.enum(["running", "done", "blocked", "crashed", "timed_out", "failed", "released"]).catch("done"),
  outcome: z.enum(["completed", "blocked", "crashed", "timed_out", "spawn_failed", "gave_up", "reclaimed", "iteration_budget_exhausted"]).nullable().catch("completed"),
  started_at: z.coerce.number().catch(0),
  ended_at: z.coerce.number().catch(0),
  duration_seconds: z.coerce.number().catch(0),
  summary: z.string().catch(""),
  summary_preview: z.string().catch(""),
  followups: z.array(z.string()).catch([]),
  artifacts: z.array(z.string()).catch([]),
  verification: z.array(z.string()).catch([]),
  verification_state: VerificationStateSchema.catch("ungated"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verifier_evidence: z.array(z.string()).catch([]),
  result_quality: ResultQualityBadgeSchema,
  deliverables: z.array(TaskDeliverableSchema).catch([]),
  residual_risk: z.string().nullable().optional(),
});

export const TodayDigestItemSchema = z.object({
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_summary: z.string().catch(""),
  ended_at: z.coerce.number().catch(0),
  profile: z.string().nullable().catch(null),
  run_role: RunRoleSchema.catch("legacy_unknown"),
  run_role_label: z.string().catch("Unknown / legacy run"),
  verification_state: VerificationStateSchema.catch("ungated"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verdict_label: z.string().catch("Not independently verified"),
  result_quality: ResultQualityBadgeSchema,
  gate_evidence: z.array(z.string()).catch([]),
  deliverable: TaskDeliverableSchema.nullable().catch(null),
  deliverable_excerpt: z.string().nullable().catch(null),
  residual_risk: z.string().nullable().optional(),
});

export const TodayDigestResponseSchema = z.object({
  schema: z.string().catch("kanban-today-digest-v1"),
  items: z.array(TodayDigestItemSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  day_start: z.coerce.number().catch(0),
  timezone: z.string().catch("local"),
  limit: z.coerce.number().catch(12),
});

export const KanbanReviewSchema = z.object({
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("review"),
  task_assignee: z.string().catch("hermes"),
  created_at: z.coerce.number().catch(0),
  submitted_at: z.coerce.number().nullable().catch(null),
  run_id: z.coerce.string().nullable().catch(null),
  reviewer_profile: z.string().nullable().catch(null),
  summary_preview: z.string().catch(""),
  verification_state: VerificationStateSchema.catch("pending"),
  verifier_verdict: VerifierVerdictSchema.nullable().catch(null),
  verifier_evidence: z.array(z.string()).catch([]),
});

export const RecentResultsResponseSchema = z.object({
  results: z.array(KanbanResultSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  limit: z.coerce.number().catch(12),
  since_hours: z.coerce.number().catch(48),
  outcome: z.string().catch("completed"),
});

export const ReviewVerdictsResponseSchema = z.object({
  reviews: z.array(KanbanReviewSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  limit: z.coerce.number().catch(12),
});

export const BlockedCompletionSchema = z.object({
  event_id: z.coerce.number().catch(0),
  run_id: z.coerce.string().nullable().catch(null).optional(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("blocked"),
  assignee: z.string().catch("hermes"),
  kind: z.enum(["completion_blocked_hallucination", "suspected_hallucinated_references", "verifier_request_changes"]).catch("completion_blocked_hallucination"),
  created_at: z.coerce.number().catch(0),
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
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  since_hours: z.coerce.number().catch(48),
});

const HealthStatusSchema = z.enum(["healthy", "degraded", "offline"]);
const SubsystemHealthSchema = z.object({
  status: HealthStatusSchema.catch("offline"),
  detail: z.string().catch(""),
  error: z.string().nullable().catch(null),
  latency_ms: z.coerce.number().catch(0).optional(),
  heartbeat_age_s: z.coerce.number().nullable().catch(null).optional(),
});
const defaultSubsystemHealth = { status: "offline" as const, detail: "", error: null };

export const SystemHealthResponseSchema = z.object({
  schema: z.string().catch("hermes-health-v1"),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  overall: HealthStatusSchema.catch("offline"),
  subsystems: z.object({
    gateway: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    autoresearch: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    kanban_db: SubsystemHealthSchema.catch(defaultSubsystemHealth),
  }).catch({
    gateway: defaultSubsystemHealth,
    autoresearch: defaultSubsystemHealth,
    kanban_db: defaultSubsystemHealth,
  }),
});

export const VaultProvenanceResponseSchema = z.object({
  schema: z.string().catch("hermes-vault-provenance-v1"),
  error: z.string().nullable().catch(null),
  stale_count: z.coerce.number().catch(0),
  open_sessions: z.array(z.object({
    agent: z.string().catch("?"),
    started: z.string().catch("?"),
    task: z.string().catch(""),
    path: z.string().catch(""),
    age_hours: z.number().nullable().catch(null),
    stale: z.boolean().catch(false),
  })).catch([]),
  recent_receipts: z.array(z.object({
    when: z.string().catch(""),
    agent: z.string().catch("?"),
    file: z.string().catch(""),
    path: z.string().catch(""),
  })).catch([]),
});

const CronLatestOutputSchema = z.object({
  filename: z.string().nullable().catch(null),
  mtime: z.coerce.number().nullable().catch(null),
  size_bytes: z.coerce.number().nullable().catch(null),
  run_count: z.coerce.number().catch(0),
});

// next_run_at / last_run_at / paused_at arrive as ISO-8601 strings from the cron
// normalizer (e.g. "2026-06-03T07:30:00+02:00"), but may be epoch numbers in
// other paths — accept both without coercion (coerce.number would turn an ISO
// string into NaN, which slips past .catch). The view normalizes to a Date.
const CronTimestampSchema = z.union([z.number(), z.string()]).nullable().catch(null);

export const CronJobSchema = z.object({
  id: z.coerce.string().catch(""),
  name: z.string().catch(""),
  enabled: z.boolean().catch(false),
  state: z.string().catch(""),
  paused_at: CronTimestampSchema,
  paused_reason: z.string().nullable().catch(null),
  schedule_display: z.string().catch(""),
  // repeat may be a string ("daily") or an object ({times, completed}); only
  // schedule_display is surfaced, so tolerate anything and drop it.
  repeat: z.unknown().nullable().catch(null),
  next_run_at: CronTimestampSchema,
  last_run_at: CronTimestampSchema,
  last_status: z.string().nullable().catch(null),
  last_error: z.string().nullable().catch(null),
  last_delivery_error: z.string().nullable().catch(null),
  deliver: z.string().nullable().catch(null),
  skill: z.string().nullable().catch(null),
  model: z.string().nullable().catch(null),
  profile: z.string().catch("default"),
  is_default_profile: z.boolean().catch(true),
  has_script: z.boolean().catch(false),
  has_prompt: z.boolean().catch(false),
  latest_output: CronLatestOutputSchema.nullable().catch(null),
});

export const CronObservabilityResponseSchema = z.object({
  schema: z.string().catch("hermes-cron-obs-v1"),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  gateway: z.object({
    running: z.boolean().catch(false),
    pids: z.array(z.coerce.number()).catch([]),
    error: z.string().nullable().catch(null).optional(),
  }).catch({ running: false, pids: [] }),
  // A single malformed job must never empty the whole list (WorkerSchema lesson).
  jobs: z.array(CronJobSchema).catch([]),
  error: z.string().nullable().catch(null).optional(),
});

const MetricsGroupSchema = z.object({
  count: z.coerce.number().catch(0),
  error_count: z.coerce.number().catch(0),
  error_rate: z.coerce.number().catch(0),
  p50_ms: z.coerce.number().catch(0),
  p95_ms: z.coerce.number().catch(0),
});

export const MetricsLiteResponseSchema = z.object({
  schema: z.string().catch("hermes-metrics-lite-v1"),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  uptime_seconds: z.coerce.number().catch(0),
  // A malformed group degrades to defaults rather than emptying the record.
  groups: z.record(z.string(), MetricsGroupSchema).catch({}),
  error: z.string().nullable().catch(null).optional(),
});

export const CronOutputSchema = z.object({
  job_id: z.string().catch(""),
  filename: z.string().nullable().catch(null),
  text: z.string().nullable().catch(null),
  truncated: z.boolean().catch(false),
  mtime: z.coerce.number().nullable().catch(null),
});

export const AutoresearchStatusSchema = z.object({
  schema: z.string().optional(),
  state: z.enum(["idle", "running", "stopping", "crashed"]).catch("idle"),
  pid: nullableNumber,
  request_id: nullableString,
  iteration: z.coerce.number().catch(0),
  max: z.coerce.number().catch(0),
  last_step: nullableString,
  last_eval: nullableString,
  route_status: nullableString,
  heartbeat_age_s: nullableNumber,
  heartbeat_fresh: z.boolean().catch(false),
  last_receipt: nullableString,
  last_run: z.unknown().nullable().catch(null),
  note: nullableString,
});

export const AutoresearchRunSchema = z.object({
  at: z.string().catch(""),
  lane: z.enum(["skill", "code", "deep-audit", "test"]).catch("skill"),
  request_id: z.string().nullable().catch(null),
  tokens: z.coerce.number().catch(0),
  proposed: z.coerce.number().catch(0),
  errors: z.coerce.number().catch(0),
  vetoed: z.coerce.number().catch(0).optional(),
  scanned: z.coerce.number().catch(0),
  model: z.string().nullable().catch(null).optional(),
});

export const AutoresearchRunsResponseSchema = z.object({
  schema: z.string().optional(),
  runs: z.array(AutoresearchRunSchema).catch([]),
});

export const ProposalSchema = z.object({
  id: z.string(),
  target: z.string().catch(""),
  section: z.string().nullable().catch(null),
  title: z.string().nullable().optional(),
  category: z.string().nullable().catch(null).optional(),
  severity: z.enum(["critical", "high", "medium", "low"]).nullable().catch(null).optional(),
  evidence: z.string().nullable().catch(null).optional(),
  new_text: z.string().nullable().optional(),
  proposal_type: z.string().nullable().catch(null).optional(),
  rationale_plain: z.string().catch(""),
  diff_before_after: z.string().catch(""),
  rank_score: z.coerce.number().nullable().catch(null).optional(),
  mode: z.enum(["skill", "code", "test"]).catch("skill"),
  status: z.enum(["proposed", "testing", "applied", "skipped"]).catch("proposed"),
  last_outcome: LastOutcomeSchema.optional(),
  result: z.string().nullable().optional(),
  created_at: z.union([z.number(), z.string()]).nullable().optional(),
  applied_at: z.union([z.number(), z.string()]).nullable().optional(),
  gate: z.object({
    phase: z.enum(["running", "passed", "failed", "crashed"]).catch("running"),
    started_at: nullableString.optional(),
    finished_at: nullableString.optional(),
    returncode: nullableNumber.optional(),
    summary: nullableString.optional(),
  }).nullable().optional(),
});

export const ProposalsResponseSchema = z.object({
  schema: z.string().optional(),
  count: z.coerce.number().catch(0),
  open_count: z.coerce.number().catch(0),
  reverted_count: z.coerce.number().catch(0),
  testing_count: z.coerce.number().catch(0),
  applied_count: z.coerce.number().catch(0),
  skipped_count: z.coerce.number().catch(0),
  proposals: z.array(ProposalSchema).catch([]),
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
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
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
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
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

// GET /tasks/{id} — the live receipt rail for the Flow board: the task's runs
// (the receipt chain), events (the live feed) and deliverables. Declared here,
// after TaskDeliverableSchema, which it reuses.
const TaskRunSchema = z.object({
  id: z.coerce.string(),
  profile: z.string().nullable().catch(null),
  status: z.string().catch(""),
  outcome: z.string().nullable().catch(null),
  summary: z.string().nullable().catch(null),
  error: z.string().nullable().catch(null),
  started_at: z.coerce.number().nullable().catch(null),
  ended_at: z.coerce.number().nullable().catch(null),
  run_role: z.string().nullable().catch(null),
  run_role_label: z.string().nullable().catch(null),
});
const TaskEventSchema = z.object({
  id: z.coerce.number().catch(0),
  kind: z.string().catch(""),
  created_at: z.coerce.number().catch(0),
  run_id: z.coerce.string().nullable().catch(null),
});
const TaskDetailTaskSchema = z.object({
  id: z.coerce.string().catch(""),
  title: z.string().catch(""),
  status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("todo"),
  assignee: z.string().nullable().catch(null),
  latest_summary: z.string().nullable().catch(null),
}).partial().catch({});
export const TaskDetailResponseSchema = z.object({
  task: TaskDetailTaskSchema.nullable().catch(null),
  runs: z.array(TaskRunSchema).catch([]),
  events: z.array(TaskEventSchema).catch([]),
  deliverables: z.array(TaskDeliverableSchema).catch([]),
});
export type TaskRun = z.infer<typeof TaskRunSchema>;
export type TaskEvent = z.infer<typeof TaskEventSchema>;
export type TaskDetailResponse = z.infer<typeof TaskDetailResponseSchema>;

export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new Error(`[Hermes Control] ${label} entspricht nicht dem Vertrag: ${result.error.message}`);
  }
  return result.data;
}
