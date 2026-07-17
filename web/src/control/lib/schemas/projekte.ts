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

const ProjectLastCommitSchema = z.object({
  hash: z.string().catch(""),
  message: z.string().catch(""),
  committed_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
}).passthrough().nullable().catch(null);

const ProjectKanbanCountsSchema = z.object({
  open: z.coerce.number().catch(0),
  running: z.coerce.number().catch(0),
  blocked: z.coerce.number().catch(0),
  review: z.coerce.number().catch(0),
  done_7d: z.coerce.number().catch(0),
  needs_input: z.coerce.number().catch(0),
}).passthrough().nullable().catch(null);

const ProjectLoopPackStatusSchema = z.object({
  name: z.string().catch(""),
  running: z.boolean().catch(false),
  last_heartbeat_at: nullableEpochSeconds,
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
}).passthrough();
export type ProjectAgent = z.infer<typeof ProjectAgentSchema>;

export const ProjectsAgentsResponseSchema = z.object({
  generated_at: epochSeconds,
  errors: z.array(z.string()).catch([]),
  agents: z.array(ProjectAgentSchema).catch([]),
}).passthrough().catch({ generated_at: invalidEpochSeconds, errors: [], agents: [] });
export type ProjectsAgentsResponse = z.infer<typeof ProjectsAgentsResponseSchema>;

// ─── Projekte-Tab Stage 6 — GET /api/projects/{slug} detail drilldown ──────
// Frozen shape from hermes_cli/projects_overview.build_project_detail. Unknown
// slug answers 404 with {error, slug}; the schema still accepts that body so
// the loader can surface it without a hard parse crash. Every field catch()es
// to a neutral default; unknown extra keys pass through.

const ProjectDetailCommitSchema = z.object({
  hash: z.string().catch(""),
  message: z.string().catch(""),
  committed_at: epochSeconds,
  age_seconds: z.coerce.number().catch(0),
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

const ProjectDetailLoopOutcomeSchema = z.object({
  verdict: z.string().catch(""),
  phase: nullableString,
  reason: nullableString,
  plan: nullableString,
  ts: nullableEpochSeconds,
}).passthrough().nullable().catch(null);

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
  kanban_tasks: z.array(ProjectDetailKanbanTaskSchema).nullable().catch(null),
  loops: z.array(ProjectDetailLoopSchema).catch([]),
  agents: z.array(ProjectDetailAgentSchema).catch([]),
  errors: z.array(z.string()).catch([]),
}).passthrough();
export type ProjectDetail = z.infer<typeof ProjectDetailResponseSchema>;
