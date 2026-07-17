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
