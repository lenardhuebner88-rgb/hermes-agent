export type WorkerProfile =
  | "default" | "admin" | "coder" | "devpower" | "dispatcher"
  | "kanbanops" | "planner" | "research" | "critic";

export type TaskStatus =
  | "triage" | "todo" | "scheduled" | "ready" | "running"
  | "blocked" | "review" | "done" | "archived";

export type RunStatus =
  | "running" | "done" | "blocked" | "crashed" | "timed_out" | "failed" | "released";

export type RunOutcome =
  | "completed" | "blocked" | "crashed" | "timed_out" | "spawn_failed"
  | "gave_up" | "reclaimed" | "iteration_budget_exhausted";

export interface RunInspect {
  cpu_percent: number;
  rss: number;
  num_threads: number;
  num_fds: number;
  status: string;
  create_time?: number;
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
  started_at: number;
  claim_lock: string;
  claim_expires: number;
  last_heartbeat_at: number;
  max_runtime_seconds: number;
  run_status: RunStatus;
  run_outcome: RunOutcome | null;
  block_reason?: string | null;
  inspect?: RunInspect | null;
}

export interface WorkersResponse {
  workers: Worker[];
  count: number;
  checked_at: number;
}

export type AgentStatus = "active" | "monitoring" | "ready" | "idle" | "offline";
export type AgentId =
  | "main" | "sre-expert" | "frontend-guru" | "efficiency-auditor" | "spark" | "james";
export type Priority = "high" | "med" | "low";

export interface AgentTask {
  id: string;
  title: string;
  priority: Priority;
  progressPercent: number;
}

export interface FleetHealth {
  currentTask: string;
  heartbeat: number | null;
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
  lastActive: number;
  tasks: {
    queued: AgentTask[];
    active: AgentTask[];
    review: AgentTask[];
    recentDone: AgentTask[];
  };
  stuckSignal: boolean;
  activityPulse: number;
  fleetHealth: FleetHealth;
  roleLabel: string;
  roleSummary: string;
  escalationNote: string | null;
}

export interface AgentsResponse {
  agents: AgentLive[];
  updatedAt: number;
}

export type AutoresearchState = "idle" | "running" | "stopping" | "crashed";

export interface AutoresearchStatus {
  schema?: string;
  state: AutoresearchState;
  pid: number | null;
  request_id: string | null;
  iteration: number;
  max: number;
  last_step: string | null;
  last_eval: string | null;
  route_status: string | null;
  heartbeat_age_s: number | null;
  heartbeat_fresh: boolean;
  last_receipt: string | null;
  last_run: number | null;
  note: string | null;
}

export type ProposalMode = "skill" | "code";
export type ProposalStatus = "proposed" | "applied" | "skipped";

export type DiffLineType = "ctx" | "add" | "del";
export interface DiffLine {
  type: DiffLineType;
  text: string;
}

export interface Proposal {
  id: string;
  target: string;
  section: string | null;
  title?: string | null;
  new_text?: string | null;
  rationale_plain: string;
  diff_before_after: string;
  mode: ProposalMode;
  status: ProposalStatus;
  result?: string | null;
  created_at?: number | string | null;
  applied_at?: number | string | null;
}

export interface ProposalsResponse {
  schema?: string;
  count: number;
  open_count: number;
  proposals: Proposal[];
}

export interface ActivityEntry {
  at: number;
  text: string;
  tone: ToneName;
}

export interface NavItem {
  id: string;
  label: string;
  icon: string;
}

export type ToneName =
  | "emerald" | "cyan" | "sky" | "indigo" | "amber"
  | "rose" | "red" | "zinc" | "violet";

export type WorkerHealthKey = "healthy" | "stuck" | "blocked" | "offline";
export interface WorkerHealth {
  key: WorkerHealthKey;
  tone: ToneName;
  label: string;
  dot: "live" | "warn" | "error" | "idle" | "offline" | "ready";
}
