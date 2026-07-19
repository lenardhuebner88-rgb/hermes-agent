/**
 * useProjectNames — slug → Anzeigename für Projekt-Chips der S3.10-Panels
 * (AktivitaetPanel/SessionsPanel). Exakt dieselbe Ableitung wie
 * buildProjectsOverview.projectNames (direkte Map über die Projekt-Liste in
 * views/projekte/derive.ts) und derselbe Polling-Key ("projects/list") wie
 * ProjekteView/ProjektePanel — der pollingStore dedupliziert, kein zweiter
 * Fetch.
 */
import { useMemo } from "react";

import { useProjects } from "../hooks/useControlData";

export function useProjectNames(): Record<string, string> {
  const projects = useProjects();
  return useMemo(() => {
    const names: Record<string, string> = {};
    for (const project of projects.data?.projects ?? []) {
      names[project.slug] = project.name;
    }
    return names;
  }, [projects.data]);
}
