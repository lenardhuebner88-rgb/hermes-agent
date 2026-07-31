import { z } from "zod";

// ── Loop-Runner (/control Loops-Tab) — Vertrag: hermes_cli/control_loops.py ──
export const LoopPhaseSchema = z.object({
  engine: z.string().catch(""),
  model: z.string().catch(""),
  timeout: z.coerce.number().catch(0),
  // Reasoning-Effort dieser Phase; null = Engine-Default (kein CLI-Flag).
  effort: z.string().nullable().catch(null),
  // Stufen, die die Engine dieser Phase transportieren kann; [] = kein
  // Control (dieselbe Konvention wie `reasoning_support` im Lanes-Tab).
  effort_support: z.array(z.string()).catch([]),
});

// ManifestError-Fall: Backend liefert nur {name, error} statt der vollen Summary.
export const LoopPackErrorSchema = z.object({
  name: z.string(),
  error: z.string(),
});

export const LoopHeartbeatCurrentSchema = z.object({
  phase: z.string().catch(""),
  engine: z.string().catch(""),
  model: z.string().catch(""),
  started_at: z.string().catch(""),
  timeout: z.coerce.number().catch(0),
  round: z.coerce.number().int().positive().optional().catch(undefined),
});

export const LoopHeartbeatHistoryEntrySchema = z.object({
  phase: z.string().catch(""),
  engine: z.string().catch(""),
  model: z.string().catch(""),
  secs: z.coerce.number().catch(0),
  rc: z.coerce.number().catch(0),
  at: z.string().catch(""),
  round: z.coerce.number().int().positive().optional().catch(undefined),
});

export const LoopHeartbeatSchema = z.object({
  current: LoopHeartbeatCurrentSchema.nullable().catch(null),
  last: z.array(LoopHeartbeatHistoryEntrySchema).catch([]),
});

// ── Landing-Pack (deterministic) — Vertrag: control_loops._landing_control_payload
// Alle Felder additiv-optional: ältere Backends liefern sie nicht, das Schema
// darf das Pack deshalb nicht verwerfen.
export const LoopLandingQueueSummarySchema = z.object({
  total: z.coerce.number().optional(),
  landed: z.coerce.number().optional(),
  cleaned: z.coerce.number().optional(),
  parked: z.coerce.number().optional(),
});

export const LoopLandingCollectionWindowSchema = z.object({
  opened_at: z.string().nullable().optional(),
  closes_at: z.string().nullable().optional(),
});

export const LoopLandingCandidateSchema = z.object({
  branch: z.string(),
  head: z.string().nullable().optional(),
  ahead: z.coerce.number().optional(),
  behind: z.coerce.number().optional(),
});

export const LoopLandingRecoveryState = z.enum(["requested", "started", "exhausted"]);

export const LoopLandingRecoveryEntrySchema = z.object({
  task_id: z.string(),
  fingerprint: z.string().optional(),
  failure_class: z.string().optional(),
  failing_gate: z.string().optional(),
  candidate_commit: z.string().optional(),
  state: LoopLandingRecoveryState,
  requested_at: z.string().nullable().optional(),
  started_at: z.string().nullable().optional(),
});

export const LoopLandingPreviewSchema = z.object({
  running: z.boolean().optional(),
  started_at: z.string().nullable().optional(),
  finished_at: z.string().nullable().optional(),
  rc: z.coerce.number().nullable().optional(),
  output_tail: z.string().optional(),
});

export const LoopPackSummarySchema = z.object({
  name: z.string(),
  type: z.enum(["pipeline", "sweep", "deterministic"]),
  // Infrastruktur-Fehler einer Teil-Probe (z.B. git-Timeout beim commits_ahead-
  // Zählen, control_loops.py). Die Summary bleibt vollständig — nur diese eine
  // Zahl ist unzuverlässig. Ohne dieses Feld würde zod den Schlüssel still
  // verwerfen und der Fehler wäre nirgends sichtbar.
  error: z.string().optional(),
  // "repo" = kuratiertes Manifest, "custom" = per Werkstatt dupliziert (control_loops.py:220).
  source: z.enum(["repo", "custom"]).optional(),
  repo: z.string().catch(""),
  // Existiert der repo-Pfad noch? Ein Pack mit totem Repo sah im Tab normal aus
  // und starb erst beim Start (control_loops.py, gleiche Semantik wie der
  // Runner-Check). `catch(true)` ist die sichere Vorgabe: ein altes Backend ohne
  // das Feld darf keine Pack-Karte fälschlich als kaputt markieren.
  repo_exists: z.boolean().catch(true),
  base_branch: z.string().catch("main"),
  land_remote: z.string().catch("piet-fork"),
  land_push: z.boolean().catch(true),
  land_gates: z.array(z.string()).nullable().catch(null),
  // Manifest-Zustand (Vertrags-Autoland, Allowlist + SHA).
  autoland: z.boolean().catch(false),
  // Darf der Operator Autoland für einen Lauf einschalten? = pipeline-Pack.
  autoland_capable: z.boolean().catch(false),
  description: z.string().catch(""),
  stability: z.string().catch("experimental"),
  phases: z.record(z.string(), LoopPhaseSchema).catch({}),
  stop: z.record(z.string(), z.coerce.number()).catch({}),
  params: z.record(z.string(), z.string()).catch({}),
  running: z.boolean().catch(false),
  heartbeat: LoopHeartbeatSchema.nullable().catch(null),
  stop_requested: z.boolean().catch(false),
  queue: z.record(z.string(), z.coerce.number()).nullable().catch(null),
  commits_ahead: z.coerce.number().catch(0),
  timer_enabled: z.boolean().catch(false),
  timer_schedule: z.string().regex(/^(?:[01]\d|2[0-3]):[0-5]\d$/).catch("23:37"),
  timer_next_run: z.string().nullable().catch(null),
  // Landing-Pack (deterministic) — additiv, siehe _landing_control_payload.
  automation_enabled: z.boolean().optional(),
  baseline_sha: z.string().nullable().optional(),
  baseline_ok: z.boolean().nullable().optional(),
  queue_summary: LoopLandingQueueSummarySchema.optional(),
  next_trigger_at: z.string().nullable().optional(),
  last_result: z.string().nullable().optional(),
  collection_window: LoopLandingCollectionWindowSchema.nullable().optional(),
  candidates: z.array(LoopLandingCandidateSchema).optional(),
  token_usage: z.object({
    total_tokens: z.coerce.number().nullable().catch(null),
    metered_cost_eur: z.coerce.number().nullable().catch(null),
    billing: z.enum(["subscription", "mixed", "unknown"]).catch("unknown"),
  }).optional(),
});

// Reihenfolge irrelevant für die Auflösung (Summary verlangt "type", Error hat
// es nicht — die fehlenden Pflichtfelder entscheiden, welche Variante matcht).
export const LoopPackSchema = z.union([LoopPackSummarySchema, LoopPackErrorSchema]);

export const LoopsResponseSchema = z.object({
  packs: z.array(LoopPackSchema).catch([]),
});

export const LoopEngineCatalogSchema = z.object({
  label: z.string().catch(""),
  models: z.array(z.string()).catch([]),
  // Aus der Engine-Registrierung, nicht aus models.yaml — [] = kein Control.
  effort_levels: z.array(z.string()).catch([]),
});

export const LoopModelsResponseSchema = z.object({
  engines: z.record(z.string(), LoopEngineCatalogSchema).catch({}),
});

export const LoopDetailResponseSchema = LoopPackSummarySchema.extend({
  ledger_tail: z.array(z.string()).catch([]),
  queue_entries: z.record(z.string(), z.array(z.string())).nullable().catch(null),
  commits: z.array(z.string()).catch([]),
  overrides: z.record(z.string(), z.string()).catch({}),
  phase_usage: z.array(z.object({
    ts: z.string(),
    round: z.coerce.number().int().positive().optional(),
    phase: z.string(),
    engine: z.string(),
    model: z.string(),
    total_tokens: z.coerce.number().optional(),
    input_tokens: z.coerce.number().optional(),
    cached_input_tokens: z.coerce.number().optional(),
    output_tokens: z.coerce.number().optional(),
    reasoning_tokens: z.coerce.number().optional(),
    billing: z.enum(["subscription", "unknown"]).catch("unknown"),
    metered_cost_eur: z.coerce.number().optional(),
  })).catch([]),
  // Landing-Pack Detail — additiv (control_loops.pack_detail, LL2-S5).
  gate_stages: z.array(z.string()).optional(),
  trigger_history: z.array(z.string()).optional(),
  followup_pending: z.boolean().optional(),
  recovery: z.array(LoopLandingRecoveryEntrySchema).optional(),
  // Backend sendet null, solange nie eine Vorschau lief.
  preview: LoopLandingPreviewSchema.nullable().optional(),
});

export const LoopQueueFileResponseSchema = z.object({
  pack: z.string(),
  stage: z.string(),
  filename: z.string(),
  content: z.string().catch(""),
});

// Werkstatt: Pack-Dateien lesen/schreiben + Pack duplizieren + landen.
export const LoopFileSchema = z.object({
  name: z.string(),
  content: z.string().catch(""),
  editable: z.boolean().catch(false),
});

export const LoopFilesResponseSchema = z.object({
  pack: z.string(),
  source: z.enum(["repo", "custom"]),
  files: z.array(LoopFileSchema).catch([]),
});

export const LoopFileSaveResultSchema = z.object({
  saved: z.boolean().catch(false),
  pack: z.string(),
  file: z.string(),
});

export const LoopDuplicateResultSchema = z.object({
  created: z.string(),
  source: z.string(),
});

export const LoopLandResultSchema = z.object({
  land_started: z.boolean().catch(false),
  pack: z.string(),
  log: z.string().catch(""),
  note: z.string().catch(""),
});

export const LoopNightOverridesResponseSchema = z.object({
  pack: z.string(),
  overrides: z.record(z.string(), z.string()).catch({}),
  ok: z.boolean().optional(),
});
