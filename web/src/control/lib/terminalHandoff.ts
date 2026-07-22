// Pure helpers for the Agent-Terminals → PlanSpec/Kanban handoff (ATH-S5).
//
// These are deliberately side-effect-free: they only shape text. Capture,
// validate, ingest, task-creation and dispatch-preview are all explicit,
// separately-clicked network actions in TerminalHandoffPanel — nothing here
// touches the network or dispatches anything.

export type LiveTestDepth = "smoke" | "contract" | "ui-real";

export const LIVE_TEST_DEPTHS: readonly LiveTestDepth[] = ["smoke", "contract", "ui-real"];

// CSI escape sequences (SGR colours, cursor moves) emitted by interactive TUIs.
// tmux ``capture-pane -p`` returns raw bytes WITH these; xterm's getSelection()
// already returns plain text, but stripping twice is harmless.
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;

export function stripAnsi(text: string): string {
  return (text ?? "").replace(ANSI_RE, "");
}

/** Pick a backtick fence one longer than the longest run in ``text`` (min 3),
 *  so captured output that itself contains backticks can't break the block. */
function fenceFor(text: string): string {
  let longest = 0;
  for (const match of text.matchAll(/`+/g)) longest = Math.max(longest, match[0].length);
  return "`".repeat(Math.max(3, longest + 1));
}

function yamlQuote(value: string): string {
  return `"${(value ?? "").replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

/** Slug for the draft filename; mirrors the backend ``terminal_handoff.slugify``. */
export function defaultSlug(title: string): string {
  const slug = (title ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-+|-+$)/g, "")
    .slice(0, 60)
    .replace(/(^-+|-+$)/g, "");
  return slug || "terminal-handoff";
}

export interface PlanSpecDraftOptions {
  title?: string;
  liveTestDepth?: LiveTestDepth;
}

/**
 * Build a PlanSpec draft from captured terminal text.
 *
 * AC-3 defaults, applied unconditionally: ``freigabe: operator`` (held for the
 * operator) and an EXPLICIT ``live_test_depth``. ``taskgraph_hints`` is emitted
 * NON-binding (``binding: false``) — freeform terminal text is never auto-
 * promoted to a binding chain. The draft only becomes ingestable once the
 * operator fills in a valid binding structure, which the PlanSpec validator
 * enforces. The captured text lands in the body, never the YAML frontmatter, so
 * the frontmatter always parses.
 */
export function buildPlanSpecDraft(captured: string, opts: PlanSpecDraftOptions = {}): string {
  const title = (opts.title ?? "").trim() || "Terminal-Handoff Draft";
  const depth: LiveTestDepth = opts.liveTestDepth ?? "smoke";
  const body = stripAnsi(captured ?? "").replace(/\s+$/, "");
  const fence = fenceFor(body);
  return [
    "---",
    `title: ${yamlQuote(title)}`,
    "type: planspec",
    "agent: Hermes",
    "status: draft",
    "freigabe: operator",
    `live_test_depth: ${depth}`,
    `topic: ${yamlQuote(title)}`,
    "taskgraph_hints:",
    "  binding: false",
    "acceptance_criteria: []",
    "---",
    "",
    "## Kontext (aus Terminal übernommen)",
    "",
    fence,
    body,
    fence,
    "",
    "## Ziel / Aufgabe",
    "",
    "<!-- Beschreibe, was gebaut werden soll. -->",
    "",
    "## Subtasks aktivieren (macht den Draft erst ingestbar)",
    "",
    "<!--",
    "Ersetze den taskgraph_hints-Block oben durch eine BINDING-Struktur, z. B.:",
    "",
    "taskgraph_hints:",
    "  binding: true",
    "  subtasks:",
    "    - id: S1",
    '      title: "…"',
    "      lane: coder",
    "      deps: []",
    "-->",
    "",
  ].join("\n");
}

/** Extract ``detail.findings`` from a fetchJSON error (``"<status>: <body>"``). */
export function findingsFromError(err: unknown): string[] | null {
  const message = err instanceof Error ? err.message : String(err);
  const brace = message.indexOf("{");
  if (brace < 0) return null;
  try {
    const parsed = JSON.parse(message.slice(brace)) as { detail?: { findings?: unknown } };
    const findings = parsed?.detail?.findings;
    if (Array.isArray(findings)) return findings.map((f) => String(f));
  } catch {
    /* not JSON — caller falls back to the raw message */
  }
  return null;
}


// ---------------------------------------------------------------------------
// W3-S2 structured handoff transport (additive; not a second capsule)
// ---------------------------------------------------------------------------

export const HANDOFF_SCHEMA_VERSION = 1 as const;

export type TerminalHandoffEnvelope = {
  schema_version: number;
  source_kind: string;
  source_id: string;
  terminal_run_id?: string | null;
  correlation_id?: string | null;
  context?: Record<string, unknown> | null;
  context_fingerprint?: string | null;
  task_id?: string | null;
  run_id?: number | null;
  pane_id?: string | null;
  workspace_path?: string | null;
  agent_session?: string | null;
  native_session?: string | null;
  base_sha?: string | null;
  candidate_sha?: string | null;
  gates?: string[];
  artifact?: {
    artifact_kind: string;
    sha256: string;
    size: number;
    source_path: string;
    terminal_run_id: string;
  } | null;
  disposition?: string;
};

export type StructuredHandoffRequest = {
  schema_version: typeof HANDOFF_SCHEMA_VERSION;
  capsule: Record<string, unknown>;
  draft: {
    title?: string;
    goal?: string;
    body?: string;
    notes?: string;
    slug?: string;
    mode?: "planspec" | "direct" | "candidate";
    dry_run?: boolean;
  };
  raw: { text?: string; encoding?: string };
  session?: string;
  window?: string;
  start?: number;
  terminal_run_id?: string;
  author?: string;
};

/** Inventory gate: disable handoff without terminal_run_id/manifest. */
export function canHandoffFromInventory(windowLike: {
  terminal_run_id?: string | null;
  has_manifest?: boolean | null;
  upgrade_required?: boolean | null;
}): boolean {
  if (windowLike.upgrade_required) return false;
  if (!windowLike.terminal_run_id) return false;
  if (windowLike.has_manifest === false) return false;
  return true;
}

export function buildStructuredHandoffRequest(input: {
  session: string;
  window: string;
  title: string;
  goal?: string;
  body?: string;
  notes?: string;
  rawText: string;
  terminalRunId: string;
  capsule?: Record<string, unknown> | null;
  start?: number;
}): StructuredHandoffRequest {
  return {
    schema_version: HANDOFF_SCHEMA_VERSION,
    capsule: {
      ...(input.capsule || {}),
      terminal_run_id: input.terminalRunId,
    },
    draft: {
      title: input.title,
      goal: input.goal || input.title,
      body: input.body || "",
      notes: input.notes || "",
      mode: "planspec",
    },
    raw: { text: input.rawText, encoding: "utf-8" },
    session: input.session,
    window: input.window,
    start: input.start ?? -120,
    terminal_run_id: input.terminalRunId,
    author: "dashboard",
  };
}

/** Freeze dialog target identity for async request races. */
export function handoffRequestKey(target: {
  session?: string;
  window?: string;
  terminal_run_id?: string | null;
  requestId?: string;
}): string {
  return [
    target.session || "",
    target.window || "",
    target.terminal_run_id || "",
    target.requestId || "",
  ].join("::");
}
