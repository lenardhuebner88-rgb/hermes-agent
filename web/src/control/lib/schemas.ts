import { z } from "zod";

const nullableNumber = z.number().nullable().catch(null);
const nullableString = z.string().nullable().catch(null);
const LastOutcomeSchema = z.enum(["applied", "reverted_no_improvement"]).nullable().catch(null);

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
  profile: z.enum(["default", "admin", "coder", "devpower", "dispatcher", "kanbanops", "planner", "research", "critic"]).catch("default"),
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


export const KanbanResultSchema = z.object({
  run_id: z.coerce.string(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("done"),
  task_assignee: z.string().catch("hermes"),
  profile: z.enum(["default", "admin", "coder", "devpower", "dispatcher", "kanbanops", "planner", "research", "critic"]).catch("default"),
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
  residual_risk: z.string().nullable().optional(),
});

export const RecentResultsResponseSchema = z.object({
  results: z.array(KanbanResultSchema).catch([]),
  count: z.coerce.number().catch(0),
  checked_at: z.coerce.number().catch(() => Math.floor(Date.now() / 1000)),
  limit: z.coerce.number().catch(12),
  since_hours: z.coerce.number().catch(48),
  outcome: z.string().catch("completed"),
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
export const BacklogItemSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: z.enum(["now", "next", "later", "in_progress", "blocked", "done"]).catch("now"),
  owner: z.string().catch("unassigned"),
  risk: z.enum(["low", "medium", "high"]).catch("low"),
  area: z.string().catch(""),
  updated: z.string().catch(""),
  lane: z.string().nullable().catch(null),
  result: z.string().nullable().catch(null),
  stale: z.boolean().catch(false),
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
  body: z.string().catch(""),
  error: z.string().optional(),
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
  source: z.object({
    dir: z.string().catch(""),
    count: z.coerce.number().catch(0),
  }).catch({ dir: "", count: 0 }),
  error: z.string().nullable().catch(null),
});

export type BacklogItem = z.infer<typeof BacklogItemSchema>;
export type BacklogDetail = z.infer<typeof BacklogDetailSchema>;
export type BacklogResponse = z.infer<typeof BacklogResponseSchema>;

// Read-only Orchestrator backlog board. Mirrors the Backlog.md-style frontmatter
// (status/priority/dependsOn/planGate/created) served by GET /api/orchestration/backlog.
// Deliberately a separate schema/view from the family-organizer board (different
// contract — no premature generalisation). Tolerant (.catch defaults) so a
// partial/stale payload still renders the board.
export const OrchestrationItemSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  status: z.enum(["backlog", "todo", "doing", "review", "done"]).catch("backlog"),
  priority: z.enum(["low", "medium", "high"]).catch("medium"),
  dependsOn: z.array(z.string()).catch([]),
  planGate: z.boolean().catch(false),
  created: z.string().catch(""),
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

export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new Error(`[Hermes Control] ${label} entspricht nicht dem Vertrag: ${result.error.message}`);
  }
  return result.data;
}
