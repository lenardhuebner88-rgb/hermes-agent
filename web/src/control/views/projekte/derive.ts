// Pure derivation logic for the Projekte-Tab card grid (Stufe 4). No React/no
// fetch — unit-tested in derive.test.ts against the real /api/projects(/agents)
// payload shape (hermes_cli/projects_overview.py).
import type { ProjectAgent, ProjectEntry } from "../../lib/schemas";

/** Wie viele Agents (tmux/Koordination/Kanban/Loop) aktuell je Projekt-Slug
 *  laufen — einmal über die flache Agents-Liste aggregiert statt pro Karte
 *  neu zu filtern. Agents ohne zugeordnetes Projekt (`project: null`, z. B.
 *  ein Terminal außerhalb eines bekannten Repo-Pfads) zählen nirgends mit. */
export function countAgentsByProject(agents: ReadonlyArray<Pick<ProjectAgent, "project">>): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const agent of agents) {
    if (!agent.project) continue;
    counts[agent.project] = (counts[agent.project] ?? 0) + 1;
  }
  return counts;
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
