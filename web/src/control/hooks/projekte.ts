import { fetchJSON } from "@/lib/api";

import {
  ProjectDetailResponseSchema,
  ProjectsAgentsResponseSchema,
  ProjectSessionsResponseSchema,
  ProjectsCommitsResponseSchema,
  ProjectsResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type {
  ProjectDetail,
  ProjectSessionsResponse,
  ProjectsAgentsResponse,
  ProjectsCommitsResponse,
  ProjectsResponse,
} from "../lib/schemas";
import { usePolling } from "./internal";

// ─── Projekte-Tab (Stufe 4): GET /api/projects + GET /api/projects/agents ───
// Beide read-only, quellenisoliert (siehe Backend-Kommentar) — 12s Poll passt
// zur "10-15s" Vorgabe und dedupliziert automatisch über mehrere Karten/Views
// hinweg (gleicher Poll-Key = ein Subscriber der Polling-Store).
export function useProjects() {
  return usePolling<ProjectsResponse>(
    "projects/list",
    async () => parseOrThrow(ProjectsResponseSchema, await fetchJSON<unknown>("/api/projects"), "projects/list"),
    12000,
  );
}

export function useProjectAgents() {
  return usePolling<ProjectsAgentsResponse>(
    "projects/agents",
    async () => parseOrThrow(ProjectsAgentsResponseSchema, await fetchJSON<unknown>("/api/projects/agents"), "projects/agents"),
    12000,
  );
}

/** Project drilldown (Stufe 6). Only mounted while the detail drawer is open
 *  (`key` includes the slug), so polling naturally pauses when the drawer closes. */
export function useProjectDetail(slug: string) {
  return usePolling<ProjectDetail>(
    `projects/detail/${slug}`,
    async () =>
      parseOrThrow(
        ProjectDetailResponseSchema,
        await fetchJSON<unknown>(`/api/projects/${encodeURIComponent(slug)}`),
        `projects/detail/${slug}`,
      ),
    8000,
  );
}

/** Offene Sessions + Spawn-Baum (Stage 10): state.db-gestützt, 12s Poll wie
 *  die Agents-Liste — beide beantworten zusammen "wer arbeitet gerade". */
export function useProjectSessions() {
  return usePolling<ProjectSessionsResponse>(
    "projects/sessions",
    async () =>
      parseOrThrow(
        ProjectSessionsResponseSchema,
        await fetchJSON<unknown>("/api/projects/sessions"),
        "projects/sessions",
      ),
    12000,
  );
}

/** Projektübergreifender Commit-Feed (Stage 11). Commits ändern sich
 *  deutlich langsamer als Agent-Belegung — 30s Poll reicht. */
export function useProjectCommits() {
  return usePolling<ProjectsCommitsResponse>(
    "projects/commits",
    async () =>
      parseOrThrow(
        ProjectsCommitsResponseSchema,
        await fetchJSON<unknown>("/api/projects/commits"),
        "projects/commits",
      ),
    30000,
  );
}
