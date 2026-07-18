import { z } from "zod";

import { epochSeconds, invalidEpochSeconds, nullableEpochSeconds, nullableString } from "./common";

// ─── Projekte-Tab (GET /api/projects, GET /api/projects/agents) ────────────
// Beide Endpunkte antworten laut Backend-Vertrag (hermes_cli/projects_overview.py)
// immer mit gültigem JSON, nie einem 500 — jede Quelle (git/kanban/loops/tmux/
// coordination) ist dort schon isoliert. Die Schemas bleiben trotzdem defensiv
// (jedes Feld catch()t auf einen neutralen Default), damit ein zusätzlicher
// unbekannter Schlüssel/Wert nie die ganze Karte oder den ganzen Tab reißt.

const ProjectLinkSchema = z.object({
  label: z.string().catch(""),
  url: z.string().catch(""),
}).passthrough();

export const ProjectCommitAttributionSchema = z.object({
  kind: z.enum(["direct", "loop", "kanban", "wip", "merge", "revert"]).catch("direct"),
  pack: nullableString,
  task_id: nullableString,
  lane: nullableString,
  model: nullableString,
  label: nullableString,
}).passthrough().nullable().optional().catch(null);
export type ProjectCommitAttribution = z.infer<typeof ProjectCommitAttributionSchema>;

const ProjectLastCommitSchema = z.object({
  hash: z.string().catch(""),
  message: z.string().catch(""),
  author: z.string().catch(""),
  committed_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
  attribution: ProjectCommitAttributionSchema,
}).passthrough().nullable().catch(null);

const ProjectKanbanCountsSchema = z.object({
  open: z.coerce.number().catch(0),
  running: z.coerce.number().catch(0),
  blocked: z.coerce.number().catch(0),
  review: z.coerce.number().catch(0),
  done_7d: z.coerce.number().catch(0),
  needs_input: z.coerce.number().catch(0),
}).passthrough().nullable().catch(null);

// Shared loop-ledger outcome shape for list packs AND detail packs
// ({verdict, phase, reason, plan, ts} | null). Defined once so the Karten-
// Ampel and the Detail-Drawer never drift. Missing field / null → null.
const ProjectLoopOutcomeSchema = z.object({
  verdict: z.string().catch(""),
  phase: nullableString,
  reason: nullableString,
  plan: nullableString,
  ts: nullableEpochSeconds,
}).passthrough().nullable().catch(null);
export type ProjectLoopOutcome = z.infer<typeof ProjectLoopOutcomeSchema>;

const ProjectLoopPackStatusSchema = z.object({
  name: z.string().catch(""),
  running: z.boolean().catch(false),
  last_heartbeat_at: nullableEpochSeconds,
  // Backend additive (Karten-Ampel-Quelle): same shape as detail last_outcome.
  // Older list payloads omit it → null via .catch(null) on the shared schema.
  last_outcome: ProjectLoopOutcomeSchema,
}).passthrough();

const ProjectLoopsSchema = z.object({
  active: z.coerce.number().catch(0),
  packs: z.array(ProjectLoopPackStatusSchema).catch([]),
}).passthrough().nullable().catch(null);

const ProjectEntrySchema = z.object({
  slug: z.string().catch(""),
  name: z.string().catch(""),
  repo_path: z.string().catch(""),
  parent: nullableString,
  // Board-Slug aus der Registry (Fleet-Deep-Link der Chips); optional, weil
  // ältere Backends das Feld noch nicht emittieren.
  kanban_project: z.string().nullable().optional().catch(null),
  links: z.array(ProjectLinkSchema).catch([]),
  last_commit: ProjectLastCommitSchema,
  kanban: ProjectKanbanCountsSchema,
  loops: ProjectLoopsSchema,
  errors: z.array(z.string()).catch([]),
}).passthrough();
export type ProjectEntry = z.infer<typeof ProjectEntrySchema>;

export const ProjectsResponseSchema = z.object({
  generated_at: epochSeconds,
  registry_errors: z.array(z.string()).catch([]),
  projects: z.array(ProjectEntrySchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, registry_errors: [], projects: [] });
export type ProjectsResponse = z.infer<typeof ProjectsResponseSchema>;

// Known agent kinds get their own icon/label; a future kind added server-side
// (e.g. a new coding CLI) must never fail the whole payload — it degrades to
// "unknown" instead. z.enum(...).catch() would already do this per-value, but
// only for a genuinely invalid type; an unrecognised-but-valid string needs an
// explicit narrowing step so it doesn't leak an arbitrary label into the UI.
const KNOWN_PROJECT_AGENT_KINDS = new Set([
  "claude", "codex", "kimi", "grok", "hermes", "kanban", "loop", "unknown",
]);
export type ProjectAgentKind = "claude" | "codex" | "kimi" | "grok" | "hermes" | "kanban" | "loop" | "unknown";
const ProjectAgentKindSchema: z.ZodType<ProjectAgentKind> = z
  .string()
  .catch("unknown")
  .transform((value): ProjectAgentKind => (KNOWN_PROJECT_AGENT_KINDS.has(value) ? (value as ProjectAgentKind) : "unknown"));

const ProjectAgentSchema = z.object({
  kind: ProjectAgentKindSchema,
  label: z.string().catch(""),
  task: nullableString,
  project: nullableString,
  since: nullableEpochSeconds,
  source: z.string().catch(""),
  // Structured kill target, present only on source==="tmux" rows (backend
  // additive 2026-07-17); coordination/kanban/loop rows omit them → null.
  tmux_session: nullableString,
  tmux_window: nullableString,
  // Terminal-Deep-Link-Ziel (backend additive 2026-07-18): agent-terminals
  // adressiert Fenster über den NAMEN, tmux_window (Index) bleibt fürs
  // Terminate-API. Ältere Backends ohne das Feld → null.
  tmux_window_name: z.string().nullable().optional().catch(null),
  // Lane/assignee of a running kanban task; only source==="kanban" rows carry
  // it (backend additive 2026-07-17). Operator of a coordination claim; only
  // source==="coordination" rows can carry it. Both absent → null.
  assignee: nullableString,
  operator: nullableString,
  // Task correlation (backend additive 2026-07-17): tmux rows resolve them
  // from the tmux options @hermes_session_id/@hermes_task_id, coordination
  // rows from optional claim frontmatter keys. Old unmarked processes omit
  // them → null, and the row renders exactly as before.
  session_id: nullableString,
  task_id: nullableString,
}).passthrough();
export type ProjectAgent = z.infer<typeof ProjectAgentSchema>;

export const ProjectsAgentsResponseSchema = z.object({
  generated_at: epochSeconds,
  errors: z.array(z.string()).catch([]),
  agents: z.array(ProjectAgentSchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, errors: [], agents: [] });
export type ProjectsAgentsResponse = z.infer<typeof ProjectsAgentsResponseSchema>;

// ─── Projekte-Tab Stage 12 — GET /api/projects/receipts ────────────────────
// Cross-Agent Receipt-Feed (neueste zuerst, Backend-Cap 30) aus den Vault-
// Receipt-Verzeichnissen (hermes_cli/projects_overview.build_receipts_payload).
// Steht bewusst VOR dem Detail-Schema: detail.receipts bettet dieselbe
// Zeilenform ein (≤5, projekt-gefiltert), und ein const darf seine
// Abhängigkeit nicht nach sich selbst definieren (TDZ).
// mtime ist laut Vertrag ein ISO-String (kein Epoch) — die UI rechnet die
// Epoch für fmtRelativeTime selbst (receiptEpoch in views/projekte/derive).

const ProjectReceiptEntrySchema = z.object({
  agent: z.string().catch(""),
  filename: z.string().catch(""),
  title: z.string().catch(""),
  mtime: z.string().catch(""),
  age_seconds: z.coerce.number().catch(0),
  project: nullableString,
  excerpt: nullableString,
}).passthrough();
export type ProjectReceiptEntry = z.infer<typeof ProjectReceiptEntrySchema>;

export const ProjectsReceiptsResponseSchema = z.object({
  generated_at: epochSeconds,
  receipts: z.array(ProjectReceiptEntrySchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, receipts: [] });
export type ProjectsReceiptsResponse = z.infer<typeof ProjectsReceiptsResponseSchema>;

// Einzel-Receipt-Inhalt (GET /api/projects/receipts/{agent}/{filename}):
// unbekanntes/ungültiges Receipt antwortet 404 — fetchJSON wirft, der Hook
// zeigt den Fehlerzustand; das Schema modelliert nur den Erfolgsbody.
export const ProjectReceiptContentSchema = z.object({
  agent: z.string().catch(""),
  filename: z.string().catch(""),
  title: z.string().catch(""),
  mtime: z.string().catch(""),
  truncated: z.boolean().catch(false),
  markdown: z.string().catch(""),
}).passthrough();
export type ProjectReceiptContent = z.infer<typeof ProjectReceiptContentSchema>;

// ─── Projekte-Tab Stage 6 — GET /api/projects/{slug} detail drilldown ──────
// Frozen shape from hermes_cli/projects_overview.build_project_detail. Unknown
// slug answers 404 with {error, slug}; the schema still accepts that body so
// the loader can surface it without a hard parse crash. Every field catch()es
// to a neutral default; unknown extra keys pass through.

const ProjectDetailCommitSchema = z.object({
  hash: z.string().catch(""),
  message: z.string().catch(""),
  author: z.string().catch(""),
  committed_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
  attribution: ProjectCommitAttributionSchema,
}).passthrough();

const ProjectDetailKanbanTaskSchema = z.object({
  id: z.string().catch(""),
  title: z.string().catch(""),
  status: z.string().catch(""),
  block_kind: nullableString,
  priority: z.coerce.number().catch(0),
  created_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
}).passthrough();

// Alias: detail packs reuse the same outcome schema as list packs (no drift).
const ProjectDetailLoopOutcomeSchema = ProjectLoopOutcomeSchema;

const ProjectDetailLoopSchema = z.object({
  name: z.string().catch(""),
  running: z.boolean().catch(false),
  last_heartbeat_at: nullableEpochSeconds,
  last_outcome: ProjectDetailLoopOutcomeSchema,
}).passthrough();

// Detail agents drop the project field (already scoped to this slug).
const ProjectDetailAgentSchema = z.object({
  kind: ProjectAgentKindSchema,
  label: z.string().catch(""),
  task: nullableString,
  since: nullableEpochSeconds,
  source: z.string().catch(""),
  assignee: nullableString,
  operator: nullableString,
  // Same task-correlation fields as ProjectAgent — the detail endpoint passes
  // them through identically (backend additive 2026-07-17). Absent → null.
  session_id: nullableString,
  task_id: nullableString,
}).passthrough();

export const ProjectDetailResponseSchema = z.object({
  // Present only on the 404 unknown-slug body (or a soft error field).
  error: z.string().optional().catch(undefined),
  generated_at: epochSeconds,
  slug: z.string().catch(""),
  name: z.string().catch(""),
  repo_path: z.string().catch(""),
  parent: nullableString,
  links: z.array(ProjectLinkSchema).catch([]),
  recent_commits: z.array(ProjectDetailCommitSchema).catch([]),
  // Stage 12: neueste Receipts dieses Projekts (≤5, gleiche Zeilenform wie
  // der Feed). Ältere Backends ohne das Feld → leere Liste.
  receipts: z.array(ProjectReceiptEntrySchema).catch([]),
  kanban_tasks: z.array(ProjectDetailKanbanTaskSchema).nullable().catch(null),
  loops: z.array(ProjectDetailLoopSchema).catch([]),
  agents: z.array(ProjectDetailAgentSchema).catch([]),
  errors: z.array(z.string()).catch([]),
}).passthrough();
export type ProjectDetail = z.infer<typeof ProjectDetailResponseSchema>;

// ─── Projekte-Tab Stage 10 — GET /api/projects/sessions ────────────────────
// Offene Sessions + Spawn-Baum aus state.db (hermes_state sessions table).
// spawn_kind: delegate (Subagent via _delegate_from), branch (/branch),
// compression (Fortsetzung derselben Konversation), child (generischer Kind-
// Link), null (Wurzel). Unbekannte künftige Werte degradieren zu "child" —
// nie die ganze Zeile reißen (gleiche Narrowing-Doktrin wie ProjectAgentKind).

const KNOWN_SESSION_SPAWN_KINDS = new Set(["delegate", "branch", "compression", "child"]);
export type ProjectSessionSpawnKind = "delegate" | "branch" | "compression" | "child";
const ProjectSessionSpawnKindSchema: z.ZodType<ProjectSessionSpawnKind | null> = z
  .string()
  .nullable()
  .catch(null)
  .transform((value): ProjectSessionSpawnKind | null =>
    value != null && KNOWN_SESSION_SPAWN_KINDS.has(value) ? (value as ProjectSessionSpawnKind) : null,
  );

const ProjectSessionSchema = z.object({
  id: z.string().catch(""),
  label: z.string().catch(""),
  source: z.string().catch(""),
  model: nullableString,
  started_at: nullableEpochSeconds,
  ended_at: nullableEpochSeconds,
  end_reason: nullableString,
  is_open: z.boolean().catch(false),
  is_active: z.boolean().catch(false),
  // Open but inactive for ≥24h — the never-closed graveyard bucket (backend
  // additive 2026-07-17). Older payloads omit it → false (= "fresh open").
  stale_open: z.boolean().catch(false),
  last_active: nullableEpochSeconds,
  message_count: z.coerce.number().catch(0),
  tokens: z.coerce.number().catch(0),
  project: nullableString,
  spawn_kind: ProjectSessionSpawnKindSchema,
  spawned_by_id: nullableString,
  spawned_by_label: nullableString,
  // Terminal-Deep-Link: das Backend annotiert Session-Zeilen mit tmux-Herkunft
  // (Join über @hermes_session_id in build_sessions_payload); optional statt
  // nullableString, damit alte Fixtures/Payloads ohne die Schlüssel unverändert
  // parsen. Gesetzt → die Zeile bekommt die "Terminal öffnen"-Affordance.
  tmux_session: z.string().nullable().optional().catch(null),
  tmux_window: z.string().nullable().optional().catch(null),
  tmux_window_name: z.string().nullable().optional().catch(null),
}).passthrough();
export type ProjectSession = z.infer<typeof ProjectSessionSchema>;

export const ProjectSessionsResponseSchema = z.object({
  generated_at: epochSeconds,
  errors: z.array(z.string()).catch([]),
  sessions: z.array(ProjectSessionSchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, errors: [], sessions: [] });
export type ProjectSessionsResponse = z.infer<typeof ProjectSessionsResponseSchema>;

// ─── Projekte-Tab Stage 11 — GET /api/projects/commits ─────────────────────
// Projektübergreifender Commit-Feed (neueste zuerst, Backend-Cap 30).

const ProjectCommitFeedEntrySchema = z.object({
  project: z.string().catch(""),
  project_name: z.string().catch(""),
  hash: z.string().catch(""),
  message: z.string().catch(""),
  author: z.string().catch(""),
  committed_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
  attribution: ProjectCommitAttributionSchema,
}).passthrough();
export type ProjectCommitFeedEntry = z.infer<typeof ProjectCommitFeedEntrySchema>;

export const ProjectsCommitsResponseSchema = z.object({
  generated_at: epochSeconds,
  errors: z.array(z.string()).catch([]),
  commits: z.array(ProjectCommitFeedEntrySchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, errors: [], commits: [] });
export type ProjectsCommitsResponse = z.infer<typeof ProjectsCommitsResponseSchema>;
