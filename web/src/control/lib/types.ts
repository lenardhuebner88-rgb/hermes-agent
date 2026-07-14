// Profile sind operator-definiert (profiles/* + Lanes-Presets) — ein hartes
// Union-Enum etikettierte echte Lanes (coder-claude, premium, reviewer, …)
// per zod-catch als "default"/„Standard". Anzeige-Labels: lib/tones.ts.
export type WorkerProfile = string;

export type TaskStatus =
  | "triage" | "todo" | "scheduled" | "ready" | "running"
  | "blocked" | "review" | "done" | "archived";

export type KnownRunStatus =
  | "running" | "done" | "blocked" | "crashed" | "timed_out" | "failed" | "released";

/** Open DB vocabulary. Known values aid autocomplete; unknown values stay lossless. */
export type RunStatus = KnownRunStatus | (string & {});

export type KnownRunOutcome =
  | "completed" | "blocked" | "crashed" | "timed_out" | "spawn_failed"
  | "gave_up" | "reclaimed" | "iteration_budget_exhausted";

export type RunOutcome = KnownRunOutcome | (string & {});
export type ModelRouteState = "planned" | "in_flight" | "confirmed" | "unknown";

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
  /** Additive frontend attribution when Fleet merges multiple board responses. */
  board_slug?: string;
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
  /** Phase B (Live-Telemetrie): Schritt-Key aus dem laufenden Run. */
  step_key?: string | null;
  /** Phase B: Modell-Override falls gesetzt (sonst null = aus Lane geerbt). */
  model_override?: string | null;
  /** Compatibility alias derived only from persisted run telemetry. */
  effective_model?: string | null;
  requested_provider?: string | null;
  requested_model?: string | null;
  active_provider?: string | null;
  active_model?: string | null;
  model_state?: ModelRouteState | null;
  model_source?: string | null;
  model_observed_at?: number | null;
  /** Phase B: Live-Input-Token-Zähler — nur Hermes-Runtime-Lanes, sonst null. */
  input_tokens?: number | null;
  /** Phase B: Live-Output-Token-Zähler — nur Hermes-Runtime-Lanes, sonst null. */
  output_tokens?: number | null;
  /** Live-Token-Status: live counters, partial sample, or explicit no-data reason. */
  token_status?: "live" | "partial" | "no_live_sample" | null;
  token_status_reason?: string | null;
  /** S2: additiver Run-Fortschritt 0..1 (elapsed/max_runtime). null → etaFraction-Fallback. */
  run_progress?: number | null;
  /** S1 (Puls-Leitstand): Heartbeat-Zeitstempel (Unix-Sek, chronologisch, Cap 20)
   * für die Swimlane-Band-Ticks. Fehlt bei alten Payloads. */
  heartbeat_ticks?: number[];
}

export interface WorkersResponse {
  workers: Worker[];
  count: number;
  /** Round C: kanban.max_in_progress — null when not configured. */
  cap: number | null;
  checked_at: number;
}

/** Ein Cross-Worker-Ereignis aus GET /runs/live-events (Puls-Leitstand-Ticker). */
export interface LiveEvent {
  id: number;
  /** Board owning this event when Fleet aggregates multiple board DBs. */
  board_slug?: string | null;
  run_id: number | null;
  task_id: string | null;
  task_title: string | null;
  profile: string | null;
  kind: string;
  note: string | null;
  /** Unix-Sekunden. */
  at: number;
}

export interface LiveEventsResponse {
  events: LiveEvent[];
  count: number;
  /** Höchste Event-ID der Antwort — Cursor für den since_id-Inkrement-Poll. */
  latest_id: number | null;
  checked_at: number;
}

export interface AccountUsageWindow {
  label: string;
  window_key: string | null;
  used_percent: number | null;
  reset_at: string | null;
  detail: string | null;
}

export interface AccountUsageProvider {
  provider: string;
  available: boolean;
  source: string | null;
  fetched_at: string | null;
  title: string;
  plan: string | null;
  windows: AccountUsageWindow[];
  details: string[];
  unavailable_reason: string | null;
  cached: boolean;
}

export interface AccountUsageResponse {
  providers: AccountUsageProvider[];
  cache_ttl_seconds: number;
}

export interface PlanSpecRecord {
  path: string;
  agent: string;
  filename: string;
  topic: string;
  status: string;
  freigabe: string;
  live_test_depth: string | null;
  binding: boolean;
  subtask_count: number;
  valid: boolean;
  open: boolean;
  closed_reason: string | null;
  kanban_root_task_id: string | null;
  kanban_root_status: string | null;
  kanban_state: "not_ingested" | "queued" | "running" | "blocked" | "completed" | "done" | "archived" | "unknown";
  kanban_child_total: number;
  kanban_child_done: number;
  kanban_child_blocked: number;
  kanban_child_running: number;
  kanban_ingested_at: number | null;
  ingest_disposition: string;
  ingest_would_block: boolean;
  ingest_findings: string[];
  errors: string[];
}

export interface PlanSpecsResponse {
  planspecs: PlanSpecRecord[];
  count: number;
}

export interface PlanSpecIngestResponse {
  ok: boolean;
  path: string;
  root_task_id: string;
  child_ids: string[];
  freigabe: string;
  live_test_depth: string;
  subtask_count: number;
}

export interface PlanSpecPromptResponse {
  path: string;
  prompt: string;
}

export interface PlanSpecCloseResponse {
  ok: boolean;
  path: string;
  status: string;
  closed_reason: string;
}

export type FlowGateReleaseLevel = "merge" | "live";

export interface FlowGateRisk {
  tone: "low" | "medium" | "high";
  reasons: string[];
}

export interface FlowGateChild {
  id: string;
  title: string;
  status: TaskStatus;
  assignee: string | null;
  parents: string[];
  risk: FlowGateRisk;
  created_at: number;
  age_seconds: number;
}

export interface FlowGateLane {
  id: string | null;
  name: string;
  active: boolean;
  profiles: string[];
}

export interface FlowGateCostItem {
  task_id: string;
  profile: string;
  estimated_tokens: number;
  estimated_cost_usd: number;
  token_source: string;
  cost_source: string;
}

export interface FlowGateCostEstimate {
  estimated_tokens: number;
  estimated_cost_usd: number;
  soft_limit_usd: number;
  warning: boolean;
  items: FlowGateCostItem[];
}

export interface FlowGateResponse {
  root_id: string;
  root_status: TaskStatus;
  children: FlowGateChild[];
  held_count: number;
  release_levels: FlowGateReleaseLevel[];
  timeout_seconds: number;
  timeout_at: number | null;
  auto_dispatch_eligible: boolean;
  lanes: FlowGateLane[];
  cost_estimate: FlowGateCostEstimate;
}

export type ReviewTier = "standard" | "review" | "critical";

// Live-Stage-Pill: the review profile currently running for a task in `review`
// status (the staged gate's verifier→reviewer→critic). Distinct from the
// CONFIGURED `ReviewTier` — this is the stage actually executing right now.
// The names come from kanban.verifier_profile/review_profile/critic_profile
// in config.yaml, so custom lane names are legal — `string & {}` keeps
// autocomplete for the defaults while accepting any configured profile.
export type ActiveReviewStage = "verifier" | "reviewer" | "critic" | (string & {});

export interface FlowReleaseOptions {
  assignee_overrides?: Record<string, string | null>;
  release_level?: FlowGateReleaseLevel;
  /** Phase C: chain-wide staged-review tier stamped on every released child. */
  review_tier?: ReviewTier;
  /** Phase C: prepend one read-only scout recon task before the entry children. */
  inject_scout?: boolean;
}

export interface FlowSizingResponse {
  ok: boolean;
  task_id: string;
  action: "merge" | "split";
  kept_id?: string;
  archived_id?: string;
  source_id?: string;
  new_id?: string;
  gate: FlowGateResponse;
}

export interface FlowReleaseResponse {
  ok: boolean;
  task_id: string;
  released: number;
  released_ids: string[];
  release_level: FlowGateReleaseLevel;
  assignee_overrides: Record<string, string | null>;
  /** Phase C: the chain-wide tier applied (null when not set). */
  review_tier?: ReviewTier | null;
  /** Phase C: the injected scout task id (null when no scout was prepended). */
  scout_id?: string | null;
}

export interface FlowTimeoutSweepResponse {
  ok: boolean;
  timeout_seconds: number;
  released_roots: Array<{ task_id: string; released: number; released_ids: string[]; release_level: FlowGateReleaseLevel }>;
  released: number;
}

export interface ChainGraphRun {
  id: number;
  profile: string | null;
  status: RunStatus;
  outcome: RunOutcome | null;
  started_at: number | null;
  ended_at: number | null;
  last_heartbeat_at: number | null;
  runtime_seconds: number | null;
  heartbeat_age_seconds: number | null;
  /** S2: additiver Run-Fortschritt 0..1 (elapsed/max_runtime). null → DAG/ETA-Fallback. */
  run_progress?: number | null;
  requested_provider?: string | null;
  requested_model?: string | null;
  active_provider?: string | null;
  active_model?: string | null;
  model_state?: ModelRouteState | null;
  model_source?: string | null;
  model_observed_at?: number | null;
  effective_model?: string | null;
}

export interface ChainGraphNode {
  id: string;
  title: string;
  status: TaskStatus;
  assignee: string | null;
  level: number;
  parents: string[];
  children: string[];
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  last_heartbeat_at: number | null;
  runtime_seconds: number | null;
  /** Subtask rollup for the progress bar; null when the node has no children
   *  or the backend does not (yet) emit it — the card degrades gracefully. */
  progress: { done: number; total: number } | null;
  latest_run: ChainGraphRun | null;
  /** FIX-5: Review-Rollen-Track — ALLE task_runs des Node-Tasks (nicht nur
   *  latest_run). Additiv; ältere Payloads liefern [] via zod-catch. */
  review_roles: Array<{ profile: string; status: string; verdict: string | null }>;
  /** Kosten-Felder — additiv (K7); ältere Payloads liefern 0 via zod-catch. */
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  /** Geschätzter API-Gegenwert für Abo-Runs (alle Abo-Lanes: claude & Codex gestempelt); 0 wenn nicht verfügbar. */
  cost_usd_equivalent: number;
  /** Effektive Kosten: cost_usd + cost_usd_equivalent; 0 wenn nicht verfügbar. */
  cost_effective_usd: number;
}

export interface ChainCostsLane {
  profile: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  /** Tatsächliche Kosten inkl. kWh-basierter Neuralwatt-Abrechnung. */
  actual_cost_usd: number;
  run_count: number;
  /** Geschätzter API-Gegenwert für Abo-Runs; 0 wenn nicht gestempelt. */
  cost_usd_equivalent: number;
  /** Alias für cost_usd_equivalent, damit echte Kosten und API-Wert klar getrennt bleiben. */
  api_equivalent_usd: number;
  /** Effektive Kosten; 0 wenn nicht gestempelt. */
  cost_effective_usd: number;
  billing_neuralwatt_kwh: number;
  billing_neuralwatt_cost_usd: number;
}

export interface ChainCostsResponse {
  schema: string;
  root_id: string;
  totals: {
    input_tokens: number;
    output_tokens: number;
    cost_usd: number;
    actual_cost_usd: number;
    run_count: number;
    /** Geschätzter API-Gegenwert für Abo-Runs; 0 wenn nicht gestempelt. */
    cost_usd_equivalent: number;
    api_equivalent_usd: number;
    /** Effektive Kosten; 0 wenn nicht gestempelt. */
    cost_effective_usd: number;
    billing_neuralwatt_kwh: number;
    billing_neuralwatt_cost_usd: number;
  };
  by_lane: ChainCostsLane[];
}

export interface ChainGraphEdge {
  from: string;
  to: string;
}

export interface ChainGraphResponse {
  schema: string;
  root_id: string;
  checked_at: number;
  nodes: ChainGraphNode[];
  edges: ChainGraphEdge[];
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
  artifact_links?: TaskArtifactLink[];
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

export interface TaskArtifactLink extends TaskDeliverable {
  path: string;
  source: "metadata.artifacts" | "deliverables_preserved";
}

export interface VaultMemoryLink {
  kind: "vault" | "memory";
  label: string;
  target: string;
  source: string;
  path: string | null;
  display_path: string;
  exists: boolean | null;
  obsidian_url: string | null;
  url: string | null;
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
  active_verifier?: boolean;
  active_run_id?: string | null;
  review_run_state?: "active" | "approved" | "request_changes" | "pending";
  review_run_source?: "claimed_event" | "latest_ended_run" | null;
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
  archived_at?: number | null;
  due_at?: number | null;
  last_heartbeat_at?: number | null;
  branch_name: string | null;
  latest_summary: string | null;
  vault_memory_links?: VaultMemoryLink[];
  /** Round D: block reason for blocked tasks (latest task_run summary). "operator hold" marks an operator hold. Older payloads → undefined/null. */
  block_reason?: string | null;
  /** Dispatcher-owned classification; never infer this from block_reason prose. */
  operator_question?: boolean;
  auto_retry_count?: number;
  link_counts: { parents: number; children: number };
  comment_count: number;
  progress: { done: number; total: number } | null;
  age: { created_age_seconds: number | null; started_age_seconds: number | null; time_to_complete_seconds: number | null } | null;
  /** Projekt-Achse: the task's tenant ("family-organizer", "orchestrator", …); null = Unsortiert. */
  tenant: string | null;
  /** Chain key — the tree-sink root this task rolls up into (own id for standalone/roots). */
  root_id: string | null;
  /** Phase C: staged-review tier (Phase B column). Explicit value drives
   * verifier→reviewer→critic; null/absent = standard (auto-risk may still apply). */
  review_tier?: ReviewTier | null;
  /** Slice b: the staged-review stage actually running now (the latest
   * submitted_for_review target_profile); only present while in `review`. */
  active_review_stage?: ActiveReviewStage | null;
  /** Epic membership when assigned (first-class backend grouping). */
  epic_id: string | null;
  /** Workspace model (worker isolation): "scratch" | "dir" | "worktree".
   * Optional: older payload mocks omit it. */
  workspace_kind?: string | null;
  /** Resolved workspace path; a dispatcher-provisioned isolated worktree
   * lives under `<repo>/.worktrees/kanban/<root_id>`. */
  workspace_path?: string | null;
  /** Stable dedup key set at creation — e.g. `fo-backlog:<id>` for FO tasks.
   * Null for tasks created without one (older tasks or non-FO tasks). */
  idempotency_key?: string | null;
  /** K8 per-run cost/token roll-up for the Flow-board card footer. Only set when
   * the task actually ran (no runs → null → no footer). Mirrors the chain-graph
   * node fields; `cost_effective_usd = cost_usd + cost_usd_equivalent` (metered $
   * plus the estimated $-equivalent of subscription runs). */
  cost_usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd_equivalent?: number | null;
  cost_effective_usd?: number | null;
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

export interface BoardSourceError {
  artifact: string;
  source: string;
  stage: string;
  severity: "info" | "warning" | "error";
  message: string;
  db_path: string | null;
  backup_path: string | null;
  retry_count: number;
}

export interface BoardResponse {
  columns: BoardColumn[];
  tenants: string[];
  assignees: string[];
  latest_event_id: number;
  source_errors: BoardSourceError[];
  now: number;
}

export interface BoardArchiveResponse {
  tasks: BoardTask[];
  total_count: number;
  filtered_count: number;
  loaded_count: number;
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
  query: string;
  assignee: string | null;
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
    kanban_dispatcher: SubsystemHealth;
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

export type OperatorInventoryWorktreeState = "clean" | "dirty" | "locked" | "prunable" | "unknown";

export interface OperatorInventoryLever {
  action: string;
  label: string;
  detail: string;
  tone: ToneName;
  count: number;
  target: string;
  mutation: "none";
}

export interface OperatorInventorySummary {
  worktrees_total: number;
  worktrees_locked: number;
  worktrees_dirty: number;
  worktrees_prunable: number;
  worktrees_orphaned: number;
  worktrees_status_unknown: number;
  actors_total: number;
  actors_canonical: number;
}

export interface OperatorInventoryWorktree {
  id: string;
  path_label: string;
  branch: string;
  head: string | null;
  relation: string;
  task_hint: string | null;
  state: OperatorInventoryWorktreeState;
  locked: boolean;
  prunable: boolean;
  detached: boolean;
  dirty_count: number | null;
  untracked_count: number | null;
  status_checked: boolean;
  orphaned: boolean;
}

export interface OperatorInventoryActor {
  role: string;
  label: string;
  count: number;
  cpu_percent: number | null;
  rss_mb: number | null;
  oldest_age_seconds: number | null;
  source: "canonical" | "process";
  confidence: string;
  stale_count: number;
  target: string;
  controllable: boolean;
}

export interface OperatorInventoryResponse {
  schema: string;
  checked_at: number;
  summary: OperatorInventorySummary;
  next_lever: OperatorInventoryLever;
  levers: OperatorInventoryLever[];
  worktrees: OperatorInventoryWorktree[];
  actors: OperatorInventoryActor[];
  errors: string[];
}

export type PressureOverall = "ok" | "busy" | "saturated" | "unknown";
export type TailnetPressureState = "direct" | "relay" | "inactive" | "unknown";

export interface PressureHost {
  cpu_percent: number | null;
  load_avg: number[];
  cpu_count: number;
  memory_percent: number | null;
}

export interface PressureDashboardProcess {
  pid: number | null;
  rss_mb: number | null;
  cpu_percent: number | null;
  cpu_weight: number | null;
  cpu_quota: string;
  tasks_current: number | null;
  num_threads?: number | null;
}

export interface PressureSource {
  kind: "test" | "browser_test" | "agent" | "hermes_service" | string;
  label: string;
  count: number;
  cpu_percent: number;
  rss_mb: number;
  scope: string;
  scope_kind: string;
  throttled: boolean;
}

export interface PressureAccess {
  tailnet: TailnetPressureState;
  api_latency_ms: number | null;
  detail: string | null;
}

export interface TokenPressure {
  class: string;
  pct: number | null;
  updated_at?: string | number | null;
}

export interface PressureRecommendation {
  label: string;
  detail: string;
  tone: ToneName;
}

export interface PressureStatusResponse {
  schema: string;
  checked_at: number;
  overall: PressureOverall;
  cause: string;
  recommendation: PressureRecommendation;
  host: PressureHost;
  dashboard: PressureDashboardProcess;
  pressure_sources: PressureSource[];
  access: PressureAccess;
  token_pressure: TokenPressure;
  errors: string[];
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

// ── Loop-Runner (/control Loops-Tab) ────────────────────────────────────────
export interface LoopPhase {
  engine: string;
  model: string;
  timeout: number;
}

/** Pack-Manifest ist kaputt (ManifestError) — Backend gibt nur name+error zurück. */
export interface LoopPackError {
  name: string;
  error: string;
}

/** Aktuell laufende Phase (heartbeat.json, best effort — Telemetrie kostet nie eine Runde). */
export interface LoopHeartbeatCurrent {
  phase: string;
  engine: string;
  model: string;
  /** Eindeutiger UTC-ISO-Instant (`...Z`); ältere/kaputte Werte werden im UI abgelehnt. */
  started_at: string;
  timeout: number;
  /** Echte Runner-Runde; fehlt bei älteren Heartbeats und der Plan-Phase. */
  round?: number;
}

/** Ein abgeschlossener Phasen-Eintrag der 20er-Historie. */
export interface LoopHeartbeatHistoryEntry {
  phase: string;
  engine: string;
  model: string;
  secs: number;
  rc: number;
  at: string;
  round?: number;
}

export interface LoopHeartbeat {
  current: LoopHeartbeatCurrent | null;
  last: LoopHeartbeatHistoryEntry[];
}

export interface LoopTokenUsageSummary {
  total_tokens: number | null;
  metered_cost_eur: number | null;
  billing: "subscription" | "mixed" | "unknown";
}

export interface LoopPhaseUsage {
  ts: string;
  round?: number;
  phase: string;
  engine: string;
  model: string;
  total_tokens?: number;
  input_tokens?: number;
  cached_input_tokens?: number;
  output_tokens?: number;
  reasoning_tokens?: number;
  billing: "subscription" | "unknown";
  metered_cost_eur?: number;
}

export interface LoopPackSummary {
  name: string;
  type: "pipeline" | "sweep";
  /** "repo" = kuratiertes Manifest aus loops/packs/, "custom" = per Werkstatt dupliziert. */
  source?: "repo" | "custom";
  /** Gebundener Projektpfad aus dem Pack-Manifest; kein frei editierbares Ziel. */
  repo: string;
  base_branch: string;
  /** true = genau ein verifizierter PASS-Commit darf nach den Gates automatisch landen. */
  autoland?: boolean;
  description: string;
  stability: string;
  phases: Record<string, LoopPhase>;
  stop: Record<string, number>;
  params: Record<string, string>;
  running: boolean;
  /** null = noch nie ein Heartbeat geschrieben (kein Lauf bisher). */
  heartbeat: LoopHeartbeat | null;
  stop_requested: boolean;
  /** nur bei type=pipeline gefüllt (Stage → Anzahl Dateien); sweep hat keine Queue. */
  queue: Record<string, number> | null;
  commits_ahead: number;
  timer_enabled: boolean;
  /** Täglicher Nachtlauf in lokaler systemd-Zeit, strikt HH:MM. */
  timer_schedule: string;
  /** Von systemd gemeldeter nächster Lauf; null bei deaktiviertem/unbekanntem Timer. */
  timer_next_run: string | null;
  token_usage?: LoopTokenUsageSummary;
}

export type LoopPack = LoopPackSummary | LoopPackError;

export function isLoopPackError(pack: LoopPack): pack is LoopPackError {
  return "error" in pack;
}

export interface LoopsResponse {
  packs: LoopPack[];
}

export interface LoopEngineCatalog {
  label: string;
  models: string[];
}

export interface LoopModelsResponse {
  engines: Record<string, LoopEngineCatalog>;
}

export interface LoopDetailResponse extends LoopPackSummary {
  ledger_tail: string[];
  queue_entries: Record<string, string[]> | null;
  commits: string[];
  overrides: Record<string, string>;
  phase_usage: LoopPhaseUsage[];
}

/** Werkstatt: eine Pack-Datei (pack.yaml oder ein Prompt-*.md). */
export interface LoopFile {
  name: string;
  content: string;
  /** Repo-Packs sind kuratiert (nur via Git) — nur custom-Packs sind editierbar. */
  editable: boolean;
}

export interface LoopFilesResponse {
  pack: string;
  source: "repo" | "custom";
  files: LoopFile[];
}

export interface LoopFileSaveResult {
  saved: boolean;
  pack: string;
  file: string;
}

export interface LoopDuplicateResult {
  created: string;
  source: string;
}

export interface LoopLandResult {
  land_started: boolean;
  pack: string;
  log: string;
  note: string;
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
