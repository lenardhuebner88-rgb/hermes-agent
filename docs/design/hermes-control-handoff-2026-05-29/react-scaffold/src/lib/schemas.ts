/**
 * Laufzeit-Validierung der API-Antworten mit zod.
 * Verwende `WorkersResponseSchema.parse(json)` an der fetch-Grenze, damit kaputte
 * Backend-Antworten früh und laut scheitern statt still die UI zu verbiegen.
 */
import { z } from 'zod';

export const WorkerProfileSchema = z.enum([
  'default', 'admin', 'coder', 'devpower', 'dispatcher',
  'kanbanops', 'planner', 'research', 'critic',
]);

export const TaskStatusSchema = z.enum([
  'triage', 'todo', 'scheduled', 'ready', 'running',
  'blocked', 'review', 'done', 'archived',
]);

export const RunStatusSchema = z.enum([
  'running', 'done', 'blocked', 'crashed', 'timed_out', 'failed', 'released',
]);

export const RunOutcomeSchema = z.enum([
  'completed', 'blocked', 'crashed', 'timed_out', 'spawn_failed',
  'gave_up', 'reclaimed', 'iteration_budget_exhausted',
]);

export const RunInspectSchema = z.object({
  cpu_percent: z.number(),
  rss: z.number(),
  num_threads: z.number(),
  num_fds: z.number(),
  status: z.string(),
  create_time: z.number().optional(),
  cmdline: z.array(z.string()).optional(),
  alive: z.boolean(),
});

export const WorkerSchema = z.object({
  run_id: z.string(),
  task_id: z.string(),
  task_title: z.string(),
  task_status: TaskStatusSchema,
  task_assignee: z.string(),
  profile: WorkerProfileSchema,
  worker_pid: z.number(),
  started_at: z.number(),
  claim_lock: z.string(),
  claim_expires: z.number(),
  last_heartbeat_at: z.number(),
  max_runtime_seconds: z.number(),
  run_status: RunStatusSchema,
  run_outcome: RunOutcomeSchema.nullable(),
  block_reason: z.string().optional(),
  inspect: RunInspectSchema,
});

export const WorkersResponseSchema = z.object({
  workers: z.array(WorkerSchema),
  count: z.number(),
  checked_at: z.number(),
});

export const AgentStatusSchema = z.enum(['active', 'monitoring', 'ready', 'idle', 'offline']);
export const AgentIdSchema = z.enum([
  'main', 'sre-expert', 'frontend-guru', 'efficiency-auditor', 'spark', 'james',
]);
export const PrioritySchema = z.enum(['high', 'med', 'low']);

export const AgentTaskSchema = z.object({
  id: z.string(),
  title: z.string(),
  priority: PrioritySchema,
  progressPercent: z.number(),
});

export const FleetHealthSchema = z.object({
  currentTask: z.string(),
  heartbeat: z.number().nullable(),
  throughput: z.string(),
  currentTool: z.string(),
  lastOutput: z.string(),
});

export const AgentLiveSchema = z.object({
  id: AgentIdSchema,
  name: z.string(),
  emoji: z.string(),
  status: AgentStatusSchema,
  model: z.string(),
  lastActive: z.number(),
  tasks: z.object({
    queued: z.array(AgentTaskSchema),
    active: z.array(AgentTaskSchema),
    review: z.array(AgentTaskSchema),
    recentDone: z.array(AgentTaskSchema),
  }),
  stuckSignal: z.boolean(),
  activityPulse: z.number(),
  fleetHealth: FleetHealthSchema,
  roleLabel: z.string(),
  roleSummary: z.string(),
  escalationNote: z.string().nullable(),
});

export const AgentsResponseSchema = z.object({
  agents: z.array(AgentLiveSchema),
  updatedAt: z.number(),
});

export const AutoresearchStateSchema = z.enum(['idle', 'running', 'stopping', 'crashed']);

export const AutoresearchStatusSchema = z.object({
  state: AutoresearchStateSchema,
  pid: z.number().nullable(),
  request_id: z.string().nullable(),
  iteration: z.number(),
  max: z.number(),
  last_step: z.string(),
  last_eval: z.string(),
  route_status: z.string(),
  heartbeat_age_s: z.number(),
  heartbeat_fresh: z.boolean(),
  last_receipt: z.string().nullable(),
  last_run: z.number().nullable(),
  note: z.string(),
});

export const DiffLineSchema = z.object({
  type: z.enum(['ctx', 'add', 'del']),
  text: z.string(),
});

export const ProposalSchema = z.object({
  id: z.string(),
  target: z.string(),
  section: z.string(),
  new_text: z.string().optional(),
  rationale_plain: z.string(),
  diff_before_after: z.array(DiffLineSchema),
  mode: z.enum(['skill', 'code']),
  status: z.enum(['proposed', 'applied', 'skipped']),
  result: z.string().optional(),
  applied_at: z.number().optional(),
});

export const ProposalsResponseSchema = z.array(ProposalSchema);

/**
 * Bequemer Helfer: validiert und liefert typsicher zurück.
 * Wirft mit klarer Fehlermeldung, wenn das Backend vom Vertrag abweicht.
 */
export function parseOrThrow<T>(schema: z.ZodType<T>, data: unknown, label: string): T {
  const r = schema.safeParse(data);
  if (!r.success) {
    throw new Error(`[Hermes] ${label} entspricht nicht dem Vertrag: ${r.error.message}`);
  }
  return r.data;
}
