/**
 * Hermes Control — Domänen-Typen
 * Verbindliche Verträge. Feldnamen & Enum-Werte exakt wie in der API.
 * Quelle: GET /api/plugins/kanban/workers/active · GET /api/openclaw/agents ·
 *         GET /autoresearch/status · GET /autoresearch/proposals
 */

/* ── Hermes-Worker ─────────────────────────────────────────────────────── */

export type WorkerProfile =
  | 'default' | 'admin' | 'coder' | 'devpower' | 'dispatcher'
  | 'kanbanops' | 'planner' | 'research' | 'critic';

export type TaskStatus =
  | 'triage' | 'todo' | 'scheduled' | 'ready' | 'running'
  | 'blocked' | 'review' | 'done' | 'archived';

export type RunStatus =
  | 'running' | 'done' | 'blocked' | 'crashed' | 'timed_out' | 'failed' | 'released';

export type RunOutcome =
  | 'completed' | 'blocked' | 'crashed' | 'timed_out' | 'spawn_failed'
  | 'gave_up' | 'reclaimed' | 'iteration_budget_exhausted';

/** Live-Prozessdetail — GET /runs/{run_id}/inspect */
export interface RunInspect {
  cpu_percent: number;
  /** entspricht memory_info.rss in Bytes */
  rss: number;
  num_threads: number;
  num_fds: number;
  status: string;       // running | sleeping | zombie | …
  create_time?: number; // epoch s
  cmdline?: string[];
  alive: boolean;
}

export interface Worker {
  run_id: string;
  task_id: string;
  task_title: string;
  task_status: TaskStatus;
  task_assignee: string;
  profile: WorkerProfile;
  worker_pid: number;
  started_at: number;          // epoch s
  claim_lock: string;
  claim_expires: number;       // epoch s
  last_heartbeat_at: number;   // epoch s
  max_runtime_seconds: number;
  run_status: RunStatus;
  run_outcome: RunOutcome | null;
  /** optionaler Klartext-Grund bei blocked/timed_out */
  block_reason?: string;
  inspect: RunInspect;
}

export interface WorkersResponse {
  workers: Worker[];
  count: number;
  checked_at: number; // epoch s
}

/* ── OpenClaw-Agenten ──────────────────────────────────────────────────── */

export type AgentStatus = 'active' | 'monitoring' | 'ready' | 'idle' | 'offline';
export type AgentId =
  | 'main' | 'sre-expert' | 'frontend-guru' | 'efficiency-auditor' | 'spark' | 'james';
export type Priority = 'high' | 'med' | 'low';

export interface AgentTask {
  id: string;
  title: string;
  priority: Priority;
  progressPercent: number;
}

export interface FleetHealth {
  currentTask: string;
  heartbeat: number | null; // epoch s
  throughput: string;
  currentTool: string;
  lastOutput: string;
}

export interface AgentLive {
  id: AgentId;
  name: string;
  emoji: string;
  status: AgentStatus;
  model: string;
  lastActive: number; // epoch s
  tasks: {
    queued: AgentTask[];
    active: AgentTask[];
    review: AgentTask[];
    recentDone: AgentTask[];
  };
  stuckSignal: boolean;
  activityPulse: number; // 0..1
  fleetHealth: FleetHealth;
  roleLabel: string;
  roleSummary: string;
  escalationNote: string | null;
}

export interface AgentsResponse {
  agents: AgentLive[];
  updatedAt: number; // epoch s
}

/* ── Autoresearch ──────────────────────────────────────────────────────── */

export type AutoresearchState = 'idle' | 'running' | 'stopping' | 'crashed';

/** Schema: autoresearch-runner-status-v1 */
export interface AutoresearchStatus {
  state: AutoresearchState;
  pid: number | null;
  request_id: string | null;
  iteration: number;
  max: number;
  last_step: string;
  last_eval: string;
  route_status: string; // 'configured' u.a.
  heartbeat_age_s: number;
  heartbeat_fresh: boolean;
  last_receipt: string | null;
  last_run: number | null; // epoch s
  note: string;
}

export type ProposalMode = 'skill' | 'code';
export type ProposalStatus = 'proposed' | 'applied' | 'skipped';

/** Eine Diff-Zeile (Render-Modell). 'ctx' = Kontext, 'add' = grün, 'del' = rot. */
export type DiffLineType = 'ctx' | 'add' | 'del';
export interface DiffLine {
  type: DiffLineType;
  text: string;
}

export interface Proposal {
  id: string;
  target: string;   // Skill-Name oder Code-Pfad
  section: string;
  new_text?: string;
  rationale_plain: string;
  diff_before_after: DiffLine[];
  mode: ProposalMode;
  status: ProposalStatus;
  /** Ergebnis nach Apply, z.B. "✓ übernommen — Skill: eval grün" */
  result?: string;
  applied_at?: number; // epoch s
}

/* ── Sonstiges ─────────────────────────────────────────────────────────── */

export interface ActivityEntry {
  at: number; // epoch s
  text: string;
  tone: ToneName;
}

export interface NavItem {
  id: string;
  label: string;
  icon: string; // lucide-Name
}

/* ── Töne (Status-Farbsystem) ──────────────────────────────────────────── */

export type ToneName =
  | 'emerald' | 'cyan' | 'sky' | 'indigo' | 'amber'
  | 'rose' | 'red' | 'zinc' | 'violet';

/** Abgeleiteter Gesundheitszustand eines Hermes-Workers (siehe derive.ts) */
export type WorkerHealthKey = 'healthy' | 'stuck' | 'blocked' | 'offline';
export interface WorkerHealth {
  key: WorkerHealthKey;
  tone: ToneName;
  label: string;       // 'Läuft' | 'Stuck' | 'Blockiert' | 'Offline'
  dot: 'live' | 'warn' | 'error' | 'idle' | 'offline' | 'ready';
}
