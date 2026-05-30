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


export interface KanbanResult {
  run_id: string;
  task_id: string;
  task_title: string;
  task_status: TaskStatus;
  task_assignee: string;
  profile: WorkerProfile;
  status: RunStatus;
  outcome: RunOutcome | null;
  started_at: number;
  ended_at: number;
  duration_seconds: number;
  summary: string;
  summary_preview: string;
  followups: string[];
  artifacts: string[];
  verification: string[];
  residual_risk?: string | null;
}

export interface RecentResultsResponse {
  results: KanbanResult[];
  count: number;
  checked_at: number;
  limit: number;
  since_hours: number;
  outcome: string;
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

export interface DrilldownDecision {
  id?: string;
  label: string;
  detail: string;
}

export interface DrilldownArtifact {
  label: string;
  value: string;
  source?: string;
}

export interface DrilldownTimelineItem {
  id?: string;
  at: string;
  kind?: string;
  label: string;
  detail?: string;
}

export interface Drilldown {
  decisions: DrilldownDecision[];
  artifacts: DrilldownArtifact[];
  timeline: DrilldownTimelineItem[];
  highlights: string[];
  sources: string[];
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
  /** E4: MC-parity enrichment (queue depth + heartbeat provenance). Optional. */
  load?: number;
  loadSource?: string | null;
  heartbeatTruth?: string | null;
  /** F1: per-metric provenance flags for throughput/tool/currentTask. Optional. */
  throughputTruth?: string | null;
  currentToolTruth?: string | null;
  currentTaskTruth?: string | null;
  drilldown?: Drilldown;
}

export interface AgentsResponse {
  agents: AgentLive[];
  updatedAt: number | null;
  error?: string | null;
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
  last_run: unknown | null;
  note: string | null;
}

export type ProposalMode = "skill" | "code";
export type ProposalStatus = "proposed" | "testing" | "applied" | "skipped";
export type ProposalLastOutcome = "applied" | "reverted_no_improvement" | null;

export type GatePhase = "running" | "passed" | "failed" | "crashed";

/** A3 test-suite gate state, present on code proposals once apply has run. */
export interface ProposalGate {
  phase: GatePhase;
  started_at?: string | null;
  finished_at?: string | null;
  returncode?: number | null;
  summary?: string | null;
}

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
  category?: string | null;
  evidence?: string | null;
  new_text?: string | null;
  rationale_plain: string;
  diff_before_after: string;
  rank_score?: number | null;
  mode: ProposalMode;
  status: ProposalStatus;
  last_outcome?: ProposalLastOutcome;
  result?: string | null;
  created_at?: number | string | null;
  applied_at?: number | string | null;
  gate?: ProposalGate | null;
}

export interface ProposalsResponse {
  schema?: string;
  count: number;
  open_count: number;
  reverted_count?: number;
  testing_count?: number;
  applied_count?: number;
  skipped_count?: number;
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
