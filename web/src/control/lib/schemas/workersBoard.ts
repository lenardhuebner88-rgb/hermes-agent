import { z } from "zod";
import {
  epochSeconds,
  invalidEpochSeconds,
  ModelRouteStateSchema,
  nullableEpochSeconds,
  RunOutcomeSchema,
  RunStatusSchema,
  TaskStatusSchema,
  VaultMemoryLinkSchema,
} from "./common";

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
  // alive=false trägt eine Backend-Begründung (z.B. "no worker_pid recorded"
  // bei claude-cli-Lanes) — die UI zeigt sie statt irreführender Null-Meter.
  reason: z.string().nullable().catch(null),
}).transform((v) => ({
  cpu_percent: v.cpu_percent,
  rss: v.rss ?? v.memory_info?.rss ?? 0,
  num_threads: v.num_threads,
  num_fds: v.num_fds,
  status: v.status,
  create_time: v.create_time,
  cmdline: v.cmdline,
  alive: v.alive,
  reason: v.reason,
}));

export const WorkerSchema = z.object({
  // The backend sends run_id as an integer (task_runs.id); the SPA treats it
  // as a string (React key, URL param, inspect map). Without coercion a numeric
  // id fails validation and — because the array has .catch([]) — silently
  // empties the ENTIRE worker list (count > 0 but zero cards rendered).
  run_id: z.coerce.string(),
  board_slug: z.string().optional(),
  task_id: z.string().catch(""),
  task_title: z.string().catch("Ohne Titel"),
  task_status: z.enum(["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done", "archived"]).catch("running"),
  task_assignee: z.string().catch("hermes"),
  // Profile sind operator-definiert (Lanes!) — kein Enum: das stempelte echte
  // claude-cli-Lanes (coder-claude, premium, reviewer) zu "default" um.
  profile: z.string().catch("default"),
  // claude-cli-Lanes laufen ohne greifbaren Prozess — pid bleibt dort ehrlich null.
  worker_pid: z.coerce.number().nullable().catch(null),
  started_at: epochSeconds,
  claim_lock: z.string().catch(""),
  claim_expires: epochSeconds,
  last_heartbeat_at: epochSeconds,
  max_runtime_seconds: z.coerce.number().catch(0),
  run_status: RunStatusSchema,
  run_outcome: RunOutcomeSchema,
  block_reason: z.string().nullable().optional(),
  inspect: RunInspectSchema.nullable().optional(),
  // Phase A (Fortschritt): Tätigkeits-Note + ehrliche ETA (p50/p90).
  last_heartbeat_note: z.string().nullable().catch(null),
  last_heartbeat_note_at: nullableEpochSeconds,
  eta_p50_seconds: z.coerce.number().nullable().catch(null),
  eta_p90_seconds: z.coerce.number().nullable().catch(null),
  // Phase B (Live-Telemetrie): Schritt-Key, Modell-Override, effektives Modell,
  // Live-Token-Zähler. Alle nullable — nur Hermes-Runtime-Lanes liefern Tokens live.
  step_key: z.string().nullable().catch(null),
  model_override: z.string().nullable().catch(null),
  effective_model: z.string().nullable().catch(null),
  requested_provider: z.string().nullable().catch(null),
  requested_model: z.string().nullable().catch(null),
  active_provider: z.string().nullable().catch(null),
  active_model: z.string().nullable().catch(null),
  model_state: ModelRouteStateSchema,
  model_source: z.string().nullable().catch(null),
  model_observed_at: nullableEpochSeconds,
  input_tokens: z.coerce.number().nullable().catch(null),
  output_tokens: z.coerce.number().nullable().catch(null),
  // S2: additiver Run-Fortschritt 0..1 (elapsed/max_runtime_seconds).
  // null bei fehlendem Cap → UI nutzt etaFraction-Heuristik weiter.
  run_progress: z.coerce.number().min(0).max(1).nullable().catch(null),
  // S1 (Puls-Leitstand): Heartbeat-Zeitstempel (Unix-Sek, chronologisch, Cap 20)
  // für die Swimlane-Band-Ticks. Tolerant: fehlt bei alten Payloads → [].
  heartbeat_ticks: z.array(z.coerce.number().catch(invalidEpochSeconds)).catch([]),
});

export const WorkersResponseSchema = z.object({
  // Top-level identity field is required: `{}` is a contract failure, not a
  // truthful fresh response saying that no workers exist.
  workers: z.array(WorkerSchema),
  count: z.coerce.number().catch(0),
  // Round C: kanban.max_in_progress — null when not configured.
  cap: z.coerce.number().nullable().catch(null),
  checked_at: epochSeconds,
});
// ─── Live-Ereignis-Ticker (Puls-Leitstand S2) ────────────────────────────────
// GET /runs/live-events — newest-first Cross-Worker-Events aus einer kuratierten
// Kind-Allowlist (heartbeat, claimed, completed, blocked …). Tolerant gegenüber
// neuen/fehlenden Feldern: ein einzelnes kaputtes Event darf nie die Liste leeren.
export const LiveEventSchema = z.object({
  id: z.coerce.number().catch(0),
  board_slug: z.string().nullable().catch(null),
  run_id: z.coerce.number().nullable().catch(null),
  task_id: z.string().nullable().catch(null),
  task_title: z.string().nullable().catch(null),
  profile: z.string().nullable().catch(null),
  kind: z.string().catch("unknown"),
  note: z.string().nullable().catch(null),
  at: epochSeconds,
});

export const LiveEventsResponseSchema = z.object({
  events: z.array(LiveEventSchema),
  count: z.coerce.number().catch(0),
  latest_id: z.coerce.number().nullable().catch(null),
  checked_at: epochSeconds,
});

export type LiveEvent = z.infer<typeof LiveEventSchema>;
export type LiveEventsResponse = z.infer<typeof LiveEventsResponseSchema>;
// F1: Aktivitäts-Timeline — Task-Events (neueste zuerst).
export const WorkerActivityEventSchema = z.object({
  id: z.coerce.number().catch(0),
  run_id: z.coerce.number().nullable().catch(null),
  kind: z.string().catch("unknown"),
  note: z.string().nullable().catch(null),
  at: epochSeconds,
});

export const WorkerActivityResponseSchema = z.object({
  task_id: z.string().catch(""),
  events: z.array(WorkerActivityEventSchema).catch([]),
});

export type WorkerActivityEvent = z.infer<typeof WorkerActivityEventSchema>;
export type WorkerActivityResponse = z.infer<typeof WorkerActivityResponseSchema>;
const BoardSourceErrorSchema = z.object({
  artifact: z.string().catch("kanban_board_fetch"),
  source: z.string().catch("unknown"),
  stage: z.string().catch("unknown"),
  severity: z.enum(["info", "warning", "error"]).catch("warning"),
  message: z.string().catch(""),
  db_path: z.string().nullable().catch(null),
  backup_path: z.string().nullable().catch(null),
  retry_count: z.coerce.number().catch(0),
});
export const BoardTaskSchema = z.object({
  id: z.coerce.string(),
  title: z.string().catch("Ohne Titel"),
  status: TaskStatusSchema,
  assignee: z.string().nullable().catch(null),
  priority: z.coerce.number().catch(0),
  created_at: epochSeconds,
  started_at: nullableEpochSeconds,
  completed_at: nullableEpochSeconds,
  archived_at: nullableEpochSeconds,
  due_at: nullableEpochSeconds,
  last_heartbeat_at: nullableEpochSeconds,
  branch_name: z.string().nullable().catch(null),
  latest_summary: z.string().nullable().catch(null),
  vault_memory_links: z.array(VaultMemoryLinkSchema).catch([]),
  auto_retry_count: z.coerce.number().catch(0),
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
  // Projekt-Achse + Ketten-Schlüssel (additiv; ältere Server liefern sie nicht).
  tenant: z.string().nullable().catch(null),
  root_id: z.string().nullable().catch(null),
  epic_id: z.string().nullable().catch(null),
  // Phase C: staged-review tier (Phase B column). Additiv — ältere Server / Karten
  // ohne das Feld fallen auf null zurück (= standard / kein Tier-Pill).
  review_tier: z.enum(["standard", "review", "critical"]).nullable().catch(null),
  // Slice b: die GERADE laufende Review-Stufe (verifier→reviewer→critic) eines
  // Tasks in `review`-Status — abgeleitet aus dem jüngsten submitted_for_review-
  // Event. Additiv; null wenn nicht in Review oder älterer Server.
  // String statt Enum: die Stufen-Namen kommen aus kanban.verifier_profile/
  // review_profile/critic_profile in config.yaml — ein umbenanntes Profil
  // ließ das hartkodierte Enum sonst still auf null fallen (Pill verschwand).
  active_review_stage: z.string().nullable().catch(null),
  // Stables Dedup-Feld — wird z.B. als `fo-backlog:<id>` für FO-Tasks vergeben.
  // Ältere Tasks ohne dieses Feld liefern null.
  idempotency_key: z.string().nullable().catch(null),
  // Round D: Blockier-Grund für blocked Tasks (aus letztem task_run.summary).
  // Ältere Server / nicht-blocked Tasks liefern null.
  // Enthält „operator hold" → explizit vom Operator gestoppt (Resume-Button zeigen).
  block_reason: z.string().nullable().catch(null),
  // Verdict/retry-aware backend truth. Missing on old servers fails closed.
  operator_question: z.boolean().catch(false),
  // K8 Kosten/Tokens pro Run — additiv; nur Tasks mit echtem Run tragen sie,
  // sonst null (kein Kosten-Footer auf der Karte). Spiegelt die Chain-Graph-
  // Knotenfelder: cost_effective_usd = cost_usd (metered) + cost_usd_equivalent
  // (geschätzter $-Gegenwert der Abo-Runs).
  cost_usd: z.coerce.number().nullable().catch(null),
  input_tokens: z.coerce.number().nullable().catch(null),
  output_tokens: z.coerce.number().nullable().catch(null),
  cost_usd_equivalent: z.coerce.number().nullable().catch(null),
  cost_effective_usd: z.coerce.number().nullable().catch(null),
});

export const ChainSummarySchema = z.object({
  root_id: z.string().catch(""),
  root_title: z.string().catch("Ohne Titel"),
  total: z.coerce.number().int().nonnegative().catch(0),
  done: z.coerce.number().int().nonnegative().catch(0),
  status_counts: z.record(z.string(), z.coerce.number().int().nonnegative()).catch({}),
  latest_completed_at: nullableEpochSeconds,
});

export const DoneBoardPageSchema = z.object({
  total_count: z.coerce.number().int().nonnegative().catch(0),
  loaded_count: z.coerce.number().int().nonnegative().catch(0),
  limit: z.coerce.number().int().positive().catch(1),
  has_more: z.boolean().catch(false),
  next_cursor: z.string().nullable().catch(null),
});

export const BoardResponseSchema = z.object({
  columns: z.array(z.object({ name: z.string(), tasks: z.array(BoardTaskSchema).catch([]) })).catch([]),
  tenants: z.array(z.string()).catch([]),
  assignees: z.array(z.string()).catch([]),
  latest_event_id: z.coerce.number().catch(0),
  source_errors: z.array(BoardSourceErrorSchema).catch([]),
  chain_summaries: z.array(ChainSummarySchema).catch([]).optional(),
  done_page: DoneBoardPageSchema.optional(),
  now: epochSeconds,
});
export type BoardTask = z.infer<typeof BoardTaskSchema>;
export type ChainSummary = z.infer<typeof ChainSummarySchema>;
export type DoneBoardPage = z.infer<typeof DoneBoardPageSchema>;
export type BoardResponse = z.infer<typeof BoardResponseSchema>;

export const BoardArchiveResponseSchema = z.object({
  tasks: z.array(BoardTaskSchema).catch([]),
  total_count: z.coerce.number().int().nonnegative().catch(0),
  filtered_count: z.coerce.number().int().nonnegative().catch(0),
  loaded_count: z.coerce.number().int().nonnegative().catch(0),
  limit: z.coerce.number().int().positive().catch(50),
  has_more: z.boolean().catch(false),
  next_cursor: z.string().nullable().catch(null),
  query: z.string().catch(""),
  assignee: z.string().nullable().catch(null),
  assignees: z.array(z.string()).catch([]),
  latest_event_id: z.coerce.number().catch(0),
  now: epochSeconds,
});
export type BoardArchiveResponse = z.infer<typeof BoardArchiveResponseSchema>;

export const BoardsResponseSchema = z.object({
  boards: z.array(z.object({
    slug: z.string(),
    name: z.string().catch(""),
    archived: z.boolean().catch(false),
    is_current: z.boolean().catch(false),
    project_id: z.string().nullable().catch(null),
    project_slug: z.string().nullable().catch(null),
    project_name: z.string().nullable().catch(null),
    project_bound: z.boolean().catch(false),
  })).catch([]),
  current: z.string().catch("default"),
});
export type BoardsResponse = z.infer<typeof BoardsResponseSchema>;
