import { fetchJSON } from "@/lib/api";

import {
  ProjectDetailResponseSchema,
  ProjectReceiptContentSchema,
  ProjectsAgentsResponseSchema,
  ProjectSessionsResponseSchema,
  ProjectsCommitsResponseSchema,
  ProjectsReceiptsResponseSchema,
  ProjectsResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type {
  ProjectDetail,
  ProjectReceiptContent,
  ProjectSessionsResponse,
  ProjectsAgentsResponse,
  ProjectsCommitsResponse,
  ProjectsReceiptsResponse,
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

/** Cross-Agent Receipt-Feed (Stage 12): was haben die Agents zuletzt
 *  abgeschlossen. Datei-basiert, ändert sich im Commit-Takt — 30s wie der
 *  Commit-Feed (das Backend cached den Scan selbst mit 30s TTL). */
export function useProjectReceipts() {
  return usePolling<ProjectsReceiptsResponse>(
    "projects/receipts",
    async () =>
      parseOrThrow(
        ProjectsReceiptsResponseSchema,
        await fetchJSON<unknown>("/api/projects/receipts"),
        "projects/receipts",
      ),
    30000,
  );
}

/** Einzel-Receipt-Inhalt (Stage 12). Gemountet nur solange das Lese-Sheet
 *  offen ist — mounted-only-Doktrin wie useProjectDetail; 30s Takt wie der
 *  Feed, weil eine Receipt-Datei mit neuer mtime überschrieben werden kann. */
export function useProjectReceipt(agent: string, filename: string) {
  return usePolling<ProjectReceiptContent>(
    `projects/receipt/${agent}/${filename}`,
    async () =>
      parseOrThrow(
        ProjectReceiptContentSchema,
        await fetchJSON<unknown>(
          `/api/projects/receipts/${encodeURIComponent(agent)}/${encodeURIComponent(filename)}`,
        ),
        `projects/receipt/${agent}/${filename}`,
      ),
    30000,
  );
}
