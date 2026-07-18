import { z } from "zod";

// ── Loop-Runner (/control Loops-Tab) — Vertrag: hermes_cli/control_loops.py ──
export const LoopPhaseSchema = z.object({
  engine: z.string().catch(""),
  model: z.string().catch(""),
  timeout: z.coerce.number().catch(0),
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

export const LoopPackSummarySchema = z.object({
  name: z.string(),
  type: z.enum(["pipeline", "sweep"]),
  // "repo" = kuratiertes Manifest, "custom" = per Werkstatt dupliziert (control_loops.py:220).
  source: z.enum(["repo", "custom"]).optional(),
  repo: z.string().catch(""),
  base_branch: z.string().catch("main"),
  land_remote: z.string().catch("piet-fork"),
  land_push: z.boolean().catch(true),
  land_gates: z.array(z.string()).nullable().catch(null),
  autoland: z.boolean().catch(false),
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
