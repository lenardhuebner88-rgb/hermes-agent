export type WorkerProfile =
  | "default" | "admin" | "coder" | "devpower" | "dispatcher"
  | "kanbanops" | "planner" | "research" | "critic" | "verifier";

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
  /** Backend-Begründung bei alive=false (z.B. "no worker_pid recorded"). */
  reason?: string | null;
}

export interface Worker {
  run_id: string;
  task_id: string;
  task_title: string;
  task_status: TaskStatus;
  task_assignee: string;
  profile: WorkerProfile;
  /** null bei claude-cli-Lanes ohne greifbaren Prozess (PID nicht erfasst). */
  worker_pid: number | null;
  started_at: number;
  claim_lock: string;
  claim_expires: number;
  last_heartbeat_at: number;
  max_runtime_seconds: number;
  run_status: RunStatus;
  run_outcome: RunOutcome | null;
  block_reason?: string | null;
  inspect?: RunInspect | null;
  /** Phase A (Fortschritt): jüngste Heartbeat-Note („macht gerade: X"). */
  last_heartbeat_note?: string | null;
  last_heartbeat_note_at?: number | null;
  /** Phase A: ehrliche ETA — p50/p90 abgeschlossener Runs des Profils. */
  eta_p50_seconds?: number | null;
  eta_p90_seconds?: number | null;
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
  profile: string | null;
  run_role: RunRole;
  run_role_label: string;
  run_role_source: RunRoleSource;
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
  verification_state?: VerificationState;
  verifier_verdict?: VerifierVerdict | null;
  verifier_evidence?: string[];
  result_quality: ResultQualityBadge;
  deliverables?: TaskDeliverable[];
  residual_risk?: string | null;
}

export interface TaskDeliverable {
  filename: string;
  relative_path: string;
  size: number;
  mtime: number;
  content_type: string;
  url: string;
}

export type VerifierVerdict = "APPROVED" | "REQUEST_CHANGES";
export type VerificationState = "approved" | "request_changes" | "pending" | "ungated";
export type ResultQualityState = "verifier_approved" | "ungated" | "rejected_needs_work" | "unknown_legacy";
export interface ResultQualityBadge {
  state: ResultQualityState;
  label: string;
  tone: ToneName;
  description: string;
}
export type RunRole = "implementation" | "verification" | "legacy_unknown";
export type RunRoleSource = "claimed_event" | "missing_claim_event";

export interface KanbanReview {
  task_id: string;
  task_title: string;
  task_status: TaskStatus;
  task_assignee: string;
  created_at: number;
  submitted_at: number | null;
  run_id: string | null;
  reviewer_profile: string | null;
  summary_preview: string;
  verification_state: VerificationState;
  verifier_verdict: VerifierVerdict | null;
  verifier_evidence: string[];
}

export interface RecentResultsResponse {
  results: KanbanResult[];
  count: number;
  checked_at: number;
  limit: number;
  since_hours: number;
  outcome: string;
}

export interface TodayDigestItem {
  run_id: string;
  task_id: string;
  task_title: string;
  task_summary: string;
  ended_at: number;
  profile: string | null;
  run_role: RunRole;
  run_role_label: string;
  verification_state: VerificationState;
  verifier_verdict: VerifierVerdict | null;
  verdict_label: string;
  result_quality: ResultQualityBadge;
  gate_evidence: string[];
  deliverable: TaskDeliverable | null;
  deliverable_excerpt: string | null;
  residual_risk?: string | null;
}

export interface TodayDigestResponse {
  schema: string;
  items: TodayDigestItem[];
  count: number;
  checked_at: number;
  day_start: number;
  timezone: string;
  limit: number;
}

export interface ReviewVerdictsResponse {
  reviews: KanbanReview[];
  count: number;
  checked_at: number;
  limit: number;
}

export type BlockedCompletionKind = "completion_blocked_hallucination" | "suspected_hallucinated_references" | "verifier_request_changes";

export interface BlockedCompletion {
  event_id: number;
  run_id?: string | null;
  task_id: string;
  task_title: string;
  task_status: TaskStatus;
  assignee: string;
  kind: BlockedCompletionKind;
  created_at: number;
  summary_preview: string | null;
  phantom: string[];
  reviewer_profile?: string | null;
  verifier_verdict?: VerifierVerdict | null;
  failure_output: string[];
  fix_summary?: string | null;
}

export interface BlockedCompletionsResponse {
  blocked: BlockedCompletion[];
  count: number;
  checked_at: number;
  since_hours: number;
}

export interface BoardTask {
  id: string;
  title: string;
  status: TaskStatus;
  assignee: string | null;
  priority: number;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  branch_name: string | null;
  latest_summary: string | null;
  link_counts: { parents: number; children: number };
  comment_count: number;
  progress: { done: number; total: number } | null;
  age: { created_age_seconds: number | null; started_age_seconds: number | null; time_to_complete_seconds: number | null } | null;
  /** Projekt-Achse: the task's tenant ("family-organizer", "orchestrator", …); null = Unsortiert. */
  tenant: string | null;
  /** Chain key — the tree-sink root this task rolls up into (own id for standalone/roots). */
  root_id: string | null;
  /** Epic membership when assigned (first-class backend grouping). */
  epic_id: string | null;
  /** Workspace model (worker isolation): "scratch" | "dir" | "worktree".
   * Optional: older payload mocks omit it. */
  workspace_kind?: string | null;
  /** Resolved workspace path; a dispatcher-provisioned isolated worktree
   * lives under `<repo>/.worktrees/kanban/<root_id>`. */
  workspace_path?: string | null;
}

/** True when the task runs in a dispatcher-provisioned isolated worktree
 * (worker isolation — kanban.worker_isolation: worktree). */
export function isIsolatedWorkspace(task: Pick<BoardTask, "workspace_path">): boolean {
  return !!task.workspace_path && task.workspace_path.includes("/.worktrees/kanban/");
}

export interface BoardColumn {
  name: string;
  tasks: BoardTask[];
}

export interface BoardResponse {
  columns: BoardColumn[];
  tenants: string[];
  assignees: string[];
  latest_event_id: number;
  now: number;
}

export type HealthStatus = "healthy" | "degraded" | "offline";

export interface SubsystemHealth {
  status: HealthStatus;
  detail: string;
  error: string | null;
  latency_ms?: number;
  heartbeat_age_s?: number | null;
}

export interface SystemHealthResponse {
  schema: string;
  checked_at: number;
  overall: HealthStatus;
  subsystems: {
    gateway: SubsystemHealth;
    autoresearch: SubsystemHealth;
    kanban_db: SubsystemHealth;
  };
}

export interface VaultProvenanceOpenSession {
  agent: string;
  started: string;
  task: string;
  path: string;
  age_hours: number | null;
  stale: boolean;
}

export interface VaultProvenanceReceipt {
  when: string;
  agent: string;
  file: string;
  path: string;
}

export interface VaultProvenanceResponse {
  schema: string;
  error: string | null;
  stale_count: number;
  open_sessions: VaultProvenanceOpenSession[];
  recent_receipts: VaultProvenanceReceipt[];
}

export interface CronLatestOutput {
  filename: string | null;
  mtime: number | null;
  size_bytes: number | null;
  run_count: number;
}

export interface CronJob {
  id: string;
  name: string;
  enabled: boolean;
  state: string;
  paused_at: number | string | null;
  paused_reason: string | null;
  schedule_display: string;
  repeat: unknown;
  next_run_at: number | string | null;
  last_run_at: number | string | null;
  last_status: string | null;
  last_error: string | null;
  last_delivery_error: string | null;
  deliver: string | null;
  skill: string | null;
  model: string | null;
  profile: string;
  is_default_profile: boolean;
  has_script: boolean;
  has_prompt: boolean;
  latest_output: CronLatestOutput | null;
}

export interface CronObservabilityResponse {
  schema: string;
  checked_at: number;
  gateway: { running: boolean; pids: number[]; error?: string | null };
  jobs: CronJob[];
  error?: string | null;
}

export interface CronOutput {
  job_id: string;
  filename: string | null;
  text: string | null;
  truncated: boolean;
  mtime: number | null;
}

export interface MetricsGroup {
  count: number;
  error_count: number;
  error_rate: number;
  p50_ms: number;
  p95_ms: number;
}

export interface MetricsLiteResponse {
  schema: string;
  checked_at: number;
  uptime_seconds: number;
  groups: Record<string, MetricsGroup>;
  error?: string | null;
}

export type Priority = "high" | "med" | "low";

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

export interface AutoresearchRun {
  at: string;
  lane: "skill" | "code" | "deep-audit" | "test";
  request_id: string | null;
  tokens: number;
  proposed: number;
  errors: number;
  vetoed?: number;
  scanned: number;
  model?: string | null;
}

export interface AutoresearchRunsResponse {
  schema?: string;
  runs: AutoresearchRun[];
}

export type ProposalMode = "skill" | "code" | "test";
export type ProposalStatus = "proposed" | "testing" | "applied" | "skipped";
export type ProposalSeverity = "critical" | "high" | "medium" | "low";
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
  severity?: ProposalSeverity | null;
  evidence?: string | null;
  new_text?: string | null;
  proposal_type?: string | null;
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
