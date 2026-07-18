// Pure derivation logic for the Projekte-Tab card grid + live board +
// sessions/commits sections + detail drawer. No React/no fetch — unit-tested
// in derive.test.ts against the real /api/projects payload shapes.
import type { ProjectAgent, ProjectEntry, ProjectSession } from "../../lib/schemas";
import type { SignalTone } from "../../components/leitstand";

/** Agents grouped by project slug (Karten-Grid). Agents ohne zugeordnetes
 *  Projekt (`project: null`) werden hier weggelassen — auf dem LiveBoard
 *  erscheinen sie in der "Unzugeordnet"-Gruppe (liveBoardGroups). */
export function groupAgentsByProject(
  agents: ReadonlyArray<ProjectAgent>,
): Record<string, ProjectAgent[]> {
  const groups: Record<string, ProjectAgent[]> = {};
  for (const agent of agents) {
    if (!agent.project) continue;
    (groups[agent.project] ??= []).push(agent);
  }
  return groups;
}

/** Wie viele Agents (tmux/Koordination/Kanban/Loop) aktuell je Projekt-Slug
 *  laufen — einmal über die flache Agents-Liste aggregiert statt pro Karte
 *  neu zu filtern. Agents ohne zugeordnetes Projekt (`project: null`, z. B.
 *  ein Terminal außerhalb eines bekannten Repo-Pfads) zählen nirgends mit. */
export function countAgentsByProject(
  agents: ReadonlyArray<Pick<ProjectAgent, "project">>,
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const agent of agents) {
    if (!agent.project) continue;
    counts[agent.project] = (counts[agent.project] ?? 0) + 1;
  }
  return counts;
}

// ── LiveBoard ("Wer arbeitet gerade", 2026-07-17) ──────────────────────────
//
// Der LiveBoard ersetzt die alte Kind-Rail: gruppiert wird nach PROJEKT (die
// Operator-Frage ist "wer arbeitet woran", nicht "welche Engine läuft"), mit
// einer "Unzugeordnet"-Gruppe zuletzt. Innerhalb einer Gruppe: echte Prozesse
// (tmux) zuerst, dann Kanban-Tasks, Loops, Check-ins; gleiche Quelle = älteste
// zuerst (am längsten laufend = relevantestes Signal).

/** Display rank of an agent source: real processes first, claims last. */
export function agentSourceRank(source: string): number {
  switch (source) {
    case "tmux":
      return 0;
    case "kanban":
      return 1;
    case "loop":
      return 2;
    case "coordination":
      return 3;
    default:
      return 4;
  }
}

function compareAgentsForBoard(a: ProjectAgent, b: ProjectAgent): number {
  const rankDelta = agentSourceRank(a.source) - agentSourceRank(b.source);
  if (rankDelta !== 0) return rankDelta;
  // Oldest first within one source; unknown start times sort last.
  const aSince = a.since != null && Number.isFinite(a.since) ? a.since : Number.POSITIVE_INFINITY;
  const bSince = b.since != null && Number.isFinite(b.since) ? b.since : Number.POSITIVE_INFINITY;
  if (aSince !== bSince) return aSince - bSince;
  return a.label.localeCompare(b.label);
}

export interface LiveBoardGroup {
  /** Project slug, or null for the trailing "Unzugeordnet" group. */
  slug: string | null;
  agents: ProjectAgent[];
}

/** Agents grouped by project for the live board. Groups order by their most
 *  "alive" agent (a group with a running process outranks a claims-only one);
 *  unassigned agents always trail as one "Unzugeordnet" group. */
export function liveBoardGroups(agents: ReadonlyArray<ProjectAgent>): LiveBoardGroup[] {
  const byProject = new Map<string, ProjectAgent[]>();
  const unassigned: ProjectAgent[] = [];
  for (const agent of agents) {
    if (agent.project == null) {
      unassigned.push(agent);
      continue;
    }
    const list = byProject.get(agent.project);
    if (list) list.push(agent);
    else byProject.set(agent.project, [agent]);
  }

  const groups: LiveBoardGroup[] = [];
  for (const [slug, list] of byProject) {
    groups.push({ slug, agents: [...list].sort(compareAgentsForBoard) });
  }
  groups.sort((a, b) => {
    const rankA = agentSourceRank(a.agents[0]?.source ?? "");
    const rankB = agentSourceRank(b.agents[0]?.source ?? "");
    if (rankA !== rankB) return rankA - rankB;
    return (a.slug ?? "").localeCompare(b.slug ?? "");
  });
  if (unassigned.length > 0) {
    groups.push({ slug: null, agents: [...unassigned].sort(compareAgentsForBoard) });
  }
  return groups;
}

// ── Offene Sessions + Spawn-Baum (2026-07-17) ──────────────────────────────

export type SessionsFilter = "open" | "active" | "stale" | "all";

/** Filter the sessions list. Default "open" answers "welche Sessions sind
 *  noch nicht geschlossen" — but HONESTLY: the never-closed zombie rows
 *  (open + ≥24h inactive, a real live-host pattern) are split into their own
 *  "stale" bucket instead of flooding the default view. "active" narrows to
 *  the 300s liveness window, "all" shows the full 36h backend window.
 *  Order is preserved. */
export function filterSessions(
  sessions: ReadonlyArray<ProjectSession>,
  filter: SessionsFilter,
): ProjectSession[] {
  if (filter === "all") return [...sessions];
  if (filter === "active") return sessions.filter((session) => session.is_active);
  if (filter === "stale") return sessions.filter((session) => session.is_open && session.stale_open);
  return sessions.filter((session) => session.is_open && !session.stale_open);
}

/** Count of not-yet-closed sessions for the summary strip. `includeStale`
 *  controls whether the never-closed zombie rows count too — the strip uses
 *  the fresh-open number (the operator-relevant one), the filter chips show
 *  both buckets separately. */
export function countOpenSessions(
  sessions: ReadonlyArray<Pick<ProjectSession, "is_open" | "stale_open">>,
  { includeStale = false }: { includeStale?: boolean } = {},
): number {
  let count = 0;
  for (const session of sessions) {
    if (!session.is_open) continue;
    if (!includeStale && session.stale_open) continue;
    count += 1;
  }
  return count;
}

export interface SessionRow {
  session: ProjectSession;
  /** 0 = root, 1 = spawned child, 2 = grandchild (deeper nesting is rare). */
  depth: number;
  /** Direct spawned children — the "wer hat wen gespawnt" answer per row. */
  childCount: number;
}

function sessionActivityKey(session: ProjectSession): number {
  const candidate = session.last_active ?? session.started_at;
  return candidate != null && Number.isFinite(candidate) ? candidate : 0;
}

function compareSessionRoots(a: ProjectSession, b: ProjectSession): number {
  // Active sessions first, then open ones, then recently ended; within a
  // bucket the most recent activity leads.
  const bucketA = a.is_active ? 0 : a.is_open ? 1 : 2;
  const bucketB = b.is_active ? 0 : b.is_open ? 1 : 2;
  if (bucketA !== bucketB) return bucketA - bucketB;
  return sessionActivityKey(b) - sessionActivityKey(a);
}

/** Flatten the spawn tree into display rows (depth-first). A row whose
 *  `spawned_by_id` is not part of the list (parent outside the 36h window or
 *  already purged) becomes a root but keeps its `spawned_by_label` for the
 *  "gespawnt von …" line. Children sort by start time (oldest spawn first);
 *  roots sort active → open → ended, then by latest activity. Cycle-safe:
 *  a corrupt parent link never loops the walk. */
export function buildSessionRows(sessions: ReadonlyArray<ProjectSession>): SessionRow[] {
  const byId = new Map<string, ProjectSession>();
  for (const session of sessions) byId.set(session.id, session);

  const childrenByParent = new Map<string, ProjectSession[]>();
  const roots: ProjectSession[] = [];
  for (const session of sessions) {
    const parentId = session.spawned_by_id;
    const parent = parentId != null ? byId.get(parentId) : undefined;
    if (parentId != null && parent !== undefined && parent.id !== session.id) {
      const list = childrenByParent.get(parentId);
      if (list) list.push(session);
      else childrenByParent.set(parentId, [session]);
    } else {
      roots.push(session);
    }
  }

  const startedKey = (session: ProjectSession): number =>
    session.started_at != null && Number.isFinite(session.started_at)
      ? session.started_at
      : Number.POSITIVE_INFINITY;
  for (const children of childrenByParent.values()) {
    children.sort((a, b) => startedKey(a) - startedKey(b));
  }
  roots.sort(compareSessionRoots);

  const rows: SessionRow[] = [];
  const visited = new Set<string>();
  const walk = (session: ProjectSession, depth: number) => {
    if (visited.has(session.id)) return;
    visited.add(session.id);
    const children = childrenByParent.get(session.id) ?? [];
    rows.push({ session, depth, childCount: children.length });
    for (const child of children) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, 0);
  return rows;
}

// ── Sessions sichtbar & killbar (2026-07-17) ───────────────────────────────

/** Kill target for POST /api/agent-terminals/terminate — taken ONLY from the
 *  structured backend fields (`tmux_session`/`tmux_window`, tmux-source rows
 *  exclusively). The display `label` ("work:2 kimi") is never re-parsed:
 *  a destructive action must not depend on a presentation string. Anything
 *  without both fields (coordination claims, kanban/loop rows, malformed
 *  payloads) is NOT killable → null. */
export function killTarget(
  agent: Pick<ProjectAgent, "source" | "tmux_session" | "tmux_window">,
): { session: string; window: string } | null {
  if (agent.source !== "tmux") return null;
  const session = agent.tmux_session?.trim();
  const window = agent.tmux_window?.trim();
  if (!session || !window) return null;
  return { session, window };
}

/** Split one project's agent list into actually-running processes (live =
 *  tmux panes) and check-ins (coordination claims from the vault). This is the
 *  card's central answer to "welche Session läuft tatsächlich gerade" — the
 *  old chip row mixed both into indistinguishable icons.
 *  kanban/loop agents are deliberately NOT shown on the card: their state is
 *  already covered by the kanban counts line ("Läuft N") and the loops footer,
 *  and mixing them into the check-in rows would mislabel a running kanban
 *  task as "Claim, kein Prozess". They stay visible in the kind rail below.
 *  Order is preserved. */
export function splitAgentsBySource(
  agents: ReadonlyArray<ProjectAgent>,
): { live: ProjectAgent[]; claims: ProjectAgent[] } {
  const live: ProjectAgent[] = [];
  const claims: ProjectAgent[] = [];
  for (const agent of agents) {
    if (agent.source === "tmux") live.push(agent);
    else if (agent.source === "coordination") claims.push(agent);
  }
  return { live, claims };
}

/** Anzeigename des Elternprojekts für ein Unterprojekt ("Teil von X"). Fällt
 *  auf den rohen Parent-Slug zurück, wenn die Registry das Elternprojekt aus
 *  irgendeinem Grund nicht (mehr) enthält — nie eine leere/kaputte Zeile. */
export function parentDisplayName(
  parentSlug: string | null,
  projects: ReadonlyArray<Pick<ProjectEntry, "slug" | "name">>,
): string | null {
  if (!parentSlug) return null;
  return projects.find((p) => p.slug === parentSlug)?.name ?? parentSlug;
}

/** Map a loop ledger verdict to a Leitstand SignalTone.
 *  landed/passed/ok → ok; fail/stopped/bounced/blocked → warn; else neutral. */
export function loopOutcomeTone(verdict: string | null | undefined): SignalTone {
  const v = (verdict ?? "").trim().toLowerCase();
  if (v === "landed" || v === "passed" || v === "ok") return "ok";
  if (v === "fail" || v === "stopped" || v === "bounced" || v === "blocked") return "warn";
  return "neutral";
}

/** Kanban task status / block_kind → SignalTone for the detail list. */
export function kanbanTaskTone(
  status: string | null | undefined,
  blockKind: string | null | undefined,
): SignalTone {
  if (status === "blocked") {
    return blockKind === "needs_input" ? "alert" : "warn";
  }
  if (status === "running") return "ok";
  return "neutral";
}

// ── Stufe 7 / 2.3 — Attention (Ampel) ──────────────────────────────────────

/** Per-card attention level for the Projekte grid (operator "where do I intervene"). */
export type ProjectAttention = "alert" | "active" | "quiet";

/** Intervention sources that surface as reason-chips on the card. */
export type AttentionReasonKind =
  | "needs_input"
  | "blocked"
  | "stale_sessions"
  | "loop_red";

export interface AttentionReason {
  kind: AttentionReasonKind;
  count: number;
}

/** v2 result: level (sort/accent) + concrete reasons (badge chips). */
export interface ProjectAttentionResult {
  level: ProjectAttention;
  reasons: AttentionReason[];
}

const ATTENTION_RANK: Record<ProjectAttention, number> = {
  alert: 0,
  active: 1,
  quiet: 2,
};

/**
 * Count stale-open sessions per project slug (client-side aggregate of the
 * sessions payload ProjekteView already loads).
 * Rule: `stale_open === true && project === slug`. Rows with `project: null`
 * never count (unassigned graveyard — not a card signal).
 */
export function countStaleSessionsByProject(
  sessions: ReadonlyArray<Pick<ProjectSession, "project" | "stale_open">>,
): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const session of sessions) {
    if (!session.stale_open) continue;
    if (!session.project) continue;
    counts[session.project] = (counts[session.project] ?? 0) + 1;
  }
  return counts;
}

/** True when a pack's last_outcome is a fail-family verdict.
 *  Reuses the fail set from `loopOutcomeTone` (warn ⇔ fail/stopped/bounced/blocked)
 *  — never invent a second fail list. */
export function isLoopOutcomeRed(verdict: string | null | undefined): boolean {
  return loopOutcomeTone(verdict) === "warn";
}

/**
 * Derive attention for one project card (v2).
 *
 * Sources (each becomes a reason chip when count > 0):
 * 1. kanban.needs_input
 * 2. kanban.blocked
 * 3. stale_sessions (client aggregate, see countStaleSessionsByProject)
 * 4. loop_red — packs whose last_outcome fails `loopOutcomeTone` (warn set)
 *
 * Level rules:
 * - alert: any intervention reason present (needs_input | blocked | loop_red |
 *   stale_sessions). Stale alone is also alert: on mobile the question is
 *   "wo muss ich eingreifen?" and zombie open sessions are real hygiene debt
 *   the operator should see first (same rank as blocked/needs_input so the
 *   sort bubbles them). A weaker "active-only" for stale would hide them
 *   under live work.
 * - active: agents running or loops active, no intervention reasons
 * - quiet: neither
 */
export function computeAttention(
  project: Pick<ProjectEntry, "kanban" | "loops">,
  agentCount: number,
  staleCount = 0,
): ProjectAttentionResult {
  const reasons: AttentionReason[] = [];
  const needsInput = project.kanban?.needs_input ?? 0;
  const blocked = project.kanban?.blocked ?? 0;
  if (needsInput > 0) reasons.push({ kind: "needs_input", count: needsInput });
  if (blocked > 0) reasons.push({ kind: "blocked", count: blocked });
  if (staleCount > 0) reasons.push({ kind: "stale_sessions", count: staleCount });

  let loopRed = 0;
  for (const pack of project.loops?.packs ?? []) {
    // Ein LAUFENDER Pack mit altem fail-Verdict ist kein Eingriffs-Signal —
    // die Automatik (Retry-Runde) ist gerade selbst dran. Rot zählt erst,
    // wenn der Pack mit fail-Familie liegen geblieben ist (running=false).
    if (pack.running) continue;
    if (isLoopOutcomeRed(pack.last_outcome?.verdict)) loopRed += 1;
  }
  if (loopRed > 0) reasons.push({ kind: "loop_red", count: loopRed });

  if (reasons.length > 0) return { level: "alert", reasons };
  if (agentCount > 0 || (project.loops?.active ?? 0) > 0) {
    return { level: "active", reasons: [] };
  }
  return { level: "quiet", reasons: [] };
}

/**
 * Stable sort: alert → active → quiet; within a bucket keep registry order
 * (Array.prototype.sort is stable; equal ranks preserve input index).
 * `staleCountBySlug` is optional (defaults empty) so older call sites keep working.
 */
export function sortProjectsByAttention(
  projects: ReadonlyArray<ProjectEntry>,
  agentCountBySlug: Readonly<Record<string, number>>,
  staleCountBySlug: Readonly<Record<string, number>> = {},
): ProjectEntry[] {
  return projects
    .map((project, index) => ({
      project,
      index,
      rank: ATTENTION_RANK[
        computeAttention(
          project,
          agentCountBySlug[project.slug] ?? 0,
          staleCountBySlug[project.slug] ?? 0,
        ).level
      ],
    }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map(({ project }) => project);
}

/** Map attention level to an existing Leitstand SignalTone (alert is loudest). */
export function attentionTone(a: ProjectAttention): SignalTone {
  if (a === "alert") return "alert";
  if (a === "active") return "warn";
  return "neutral";
}

// ── Stage 12 — Receipts + Terminal-Deep-Link (2026-07-18) ──────────────────

/** ISO-mtime des Receipt-Vertrags → Epoch-Sekunden für fmtRelativeTime/
 *  fmtAge. Ein ungültiger String wird NaN → die Formatter sagen ehrlich
 *  "Zeit ungültig" statt eine erfundene Zeit zu zeigen. */
export function receiptEpoch(mtime: string): number {
  return Date.parse(mtime) / 1000;
}

/** In-SPA-Ziel für /control/agent-terminals (?session=&window=). Nur die
 *  strukturierten tmux-Felder — wie killTarget wird nie aus dem Anzeige-
 *  Label geparst. Das Fenster ist optional: der Deep-Link löst sonst
 *  irgendein Fenster der Session auf (pickDeepLinkedTarget); null/leer →
 *  der window-Parameter entfällt komplett. */
export function terminalDeepLink(session: string, window: string | null | undefined): string {
  const params = new URLSearchParams({ session: session.trim() });
  const trimmedWindow = window?.trim();
  if (trimmedWindow) params.set("window", trimmedWindow);
  return `/control/agent-terminals?${params.toString()}`;
}
