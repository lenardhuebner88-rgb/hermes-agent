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
  alive: z.boolean().catch(true),
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
    openclaw: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    autoresearch: SubsystemHealthSchema.catch(defaultSubsystemHealth),
    kanban_db: SubsystemHealthSchema.catch(defaultSubsystemHealth),
  }).catch({
    gateway: defaultSubsystemHealth,
    openclaw: defaultSubsystemHealth,
    autoresearch: defaultSubsystemHealth,
    kanban_db: defaultSubsystemHealth,
  }),
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
  lane: z.enum(["skill", "code"]).catch("skill"),
  request_id: z.string().nullable().catch(null),
  tokens: z.coerce.number().catch(0),
  proposed: z.coerce.number().catch(0),
  errors: z.coerce.number().catch(0),
  scanned: z.coerce.number().catch(0),
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
  rationale_plain: z.string().catch(""),
  diff_before_after: z.string().catch(""),
  rank_score: z.coerce.number().nullable().catch(null).optional(),
  mode: z.enum(["skill", "code"]).catch("skill"),
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

const AgentTaskSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  priority: z.enum(["high", "med", "low"]).catch("med"),
  progressPercent: z.coerce.number().catch(0),
});

const AgentTasksSchema = z.object({
  queued: z.array(AgentTaskSchema).catch([]),
  active: z.array(AgentTaskSchema).catch([]),
  review: z.array(AgentTaskSchema).catch([]),
  recentDone: z.array(AgentTaskSchema).catch([]),
});

const FleetHealthSchema = z.object({
  currentTask: z.string().catch("Keine aktive Aufgabe"),
  heartbeat: nullableNumber,
  throughput: z.string().catch("0/h"),
  currentTool: z.string().catch("-"),
  lastOutput: z.string().catch(""),
});

export const DrilldownSchema = z.object({
  decisions: z.array(z.object({
    id: z.string().optional(),
    label: z.string().catch(""),
    detail: z.string().catch(""),
  })).catch([]),
  artifacts: z.array(z.object({
    label: z.string().catch(""),
    value: z.string().catch(""),
    source: z.string().optional(),
  })).catch([]),
  timeline: z.array(z.object({
    id: z.string().optional(),
    at: z.string().catch(""),
    kind: z.string().optional(),
    label: z.string().catch(""),
    detail: z.string().optional(),
  })).catch([]),
  highlights: z.array(z.string()).catch([]),
  sources: z.array(z.string()).catch([]),
}).catch({ decisions: [], artifacts: [], timeline: [], highlights: [], sources: [] });

export const AgentLiveSchema = z.object({
  id: z.enum(["main", "sre-expert", "frontend-guru", "efficiency-auditor", "spark", "james"]).catch("main"),
  name: z.string().catch("OpenClaw"),
  emoji: z.string().catch("⚙️"),
  status: z.enum(["active", "monitoring", "ready", "idle", "offline"]).catch("idle"),
  model: z.string().catch("unbekannt"),
  lastActive: z.coerce.number().catch(0),
  tasks: AgentTasksSchema.catch({ queued: [], active: [], review: [], recentDone: [] }),
  stuckSignal: z.boolean().catch(false),
  activityPulse: z.coerce.number().catch(0),
  fleetHealth: FleetHealthSchema.catch({ currentTask: "Keine aktive Aufgabe", heartbeat: null, throughput: "0/h", currentTool: "-", lastOutput: "" }),
  roleLabel: z.string().catch("Agent"),
  roleSummary: z.string().catch("OpenClaw-Agent"),
  escalationNote: z.string().nullable().catch(null),
  // E4: MC-parity enrichment from the read-only proxy (optional, degrades to defaults).
  load: z.coerce.number().catch(0),
  loadSource: z.string().nullable().optional(),
  heartbeatTruth: z.string().nullable().optional(),
  throughputTruth: z.string().nullable().optional(),
  currentToolTruth: z.string().nullable().optional(),
  currentTaskTruth: z.string().nullable().optional(),
  drilldown: DrilldownSchema.optional(),
});

export const AgentsResponseSchema = z.object({
  agents: z.array(AgentLiveSchema).catch([]),
  updatedAt: nullableNumber,
  error: z.string().nullable().optional(),
});

// Glue #3: read-only list of dispatched ``openclaw:<agent>`` kanban tasks with
// their latest run's MC correlation. Tolerant (.catch defaults) so a partial /
// stale backend payload still renders the panel instead of blanking it.
export const OpenClawDispatchedTaskSchema = z.object({
  id: z.coerce.string().catch(""),
  title: z.string().catch("Ohne Titel"),
  agent: z.string().nullable().catch(null),
  status: z.string().catch("unknown"),
  mc_task_id: z.string().nullable().catch(null),
  workflow_id: z.string().nullable().catch(null),
  poll_state: z.string().nullable().catch(null),
  result_summary: z.string().nullable().catch(null),
  updated_at: z.coerce.number().catch(0),
});

export const OpenClawDispatchedResponseSchema = z.object({
  tasks: z.array(OpenClawDispatchedTaskSchema).catch([]),
  stale: z.string().optional(),
});

export type OpenClawDispatchedTask = z.infer<typeof OpenClawDispatchedTaskSchema>;
export type OpenClawDispatchedResponse = z.infer<typeof OpenClawDispatchedResponseSchema>;

export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new Error(`[Hermes Control] ${label} entspricht nicht dem Vertrag: ${result.error.message}`);
  }
  return result.data;
}
