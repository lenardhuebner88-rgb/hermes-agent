// Pure derivation logic for the Projekte-Tab card grid + agents rail +
// detail drawer (Stufe 4/5/6). No React/no fetch — unit-tested in
// derive.test.ts against the real /api/projects payload shapes.
import type { ProjectAgent, ProjectAgentKind, ProjectEntry } from "../../lib/schemas";
import type { SignalTone } from "../../components/leitstand";
import { PROJECT_AGENT_KIND_ORDER } from "./agentKinds";

/** Agents grouped by project slug. Agents without a resolved project
 *  (`project: null`) are omitted — they still appear in the kind rail. */
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

/** Agents grouped by kind in rail display order. Only non-empty groups are
 *  returned. Unassigned agents (project null) stay in their kind group. */
export function groupAgentsByKind(
  agents: ReadonlyArray<ProjectAgent>,
): Array<[ProjectAgentKind, ProjectAgent[]]> {
  const buckets = new Map<ProjectAgentKind, ProjectAgent[]>();
  for (const agent of agents) {
    const list = buckets.get(agent.kind);
    if (list) list.push(agent);
    else buckets.set(agent.kind, [agent]);
  }
  const ordered: Array<[ProjectAgentKind, ProjectAgent[]]> = [];
  for (const kind of PROJECT_AGENT_KIND_ORDER) {
    const list = buckets.get(kind);
    if (list && list.length > 0) ordered.push([kind, list]);
  }
  return ordered;
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

// ── Stufe 7 — Attention (Ampel) ────────────────────────────────────────────

/** Per-card attention state for the Projekte grid (operator "where does it hang"). */
export type ProjectAttention = "alert" | "active" | "quiet";

const ATTENTION_RANK: Record<ProjectAttention, number> = {
  alert: 0,
  active: 1,
  quiet: 2,
};

/**
 * Derive attention for one project card.
 * - alert: blocked tasks or operator-waiting (needs_input)
 * - active: agents running or loops active
 * - quiet: neither
 */
export function computeAttention(
  project: Pick<ProjectEntry, "kanban" | "loops">,
  agentCount: number,
): ProjectAttention {
  const kanban = project.kanban;
  if (kanban && (kanban.blocked > 0 || kanban.needs_input > 0)) return "alert";
  if (agentCount > 0 || (project.loops?.active ?? 0) > 0) return "active";
  return "quiet";
}

/**
 * Stable sort: alert → active → quiet; within a bucket keep registry order
 * (Array.prototype.sort is stable; equal ranks preserve input index).
 */
export function sortProjectsByAttention(
  projects: ReadonlyArray<ProjectEntry>,
  agentCountBySlug: Readonly<Record<string, number>>,
): ProjectEntry[] {
  return projects
    .map((project, index) => ({
      project,
      index,
      rank: ATTENTION_RANK[
        computeAttention(project, agentCountBySlug[project.slug] ?? 0)
      ],
    }))
    .sort((a, b) => a.rank - b.rank || a.index - b.index)
    .map(({ project }) => project);
}

/** Map attention to an existing Leitstand SignalTone (alert is loudest). */
export function attentionTone(a: ProjectAttention): SignalTone {
  if (a === "alert") return "alert";
  if (a === "active") return "warn";
  return "neutral";
}
