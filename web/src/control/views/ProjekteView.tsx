import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Eyebrow } from "../components/primitives";
import { FleetEmptyState } from "../components/leitstand";
import { useProjectAgents, useProjects } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { nowSec } from "../lib/derive";
import {
  computeAttention,
  countAgentsByProject,
  groupAgentsByProject,
  parentDisplayName,
  sortProjectsByAttention,
} from "./projekte/derive";
import { ProjectCard } from "./projekte/ProjectCard";
import { ProjectDetailDrawer } from "./projekte/ProjectDetailDrawer";
import { AgentsRail } from "./projekte/AgentsRail";

const t = de.projekte;

/** Projekte-Tab (Stufe 5/6/7) — Karten-Grid + Agents-Rail + Detail-Drawer:
 *  eine Karte pro registriertem Projekt (`~/.hermes/projects.yaml`), gespeist
 *  aus GET /api/projects + GET /api/projects/agents; Klick öffnet den
 *  read-only Drilldown (GET /api/projects/{slug}). Karten sind nach
 *  Attention (alert → active → quiet) sortiert. */
export function ProjekteView() {
  const projects = useProjects();
  const agents = useProjectAgents();
  const now = nowSec();
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);

  const list = projects.data?.projects ?? [];
  const registryErrors = projects.data?.registry_errors ?? [];
  const agentList = agents.data?.agents ?? [];
  const agentsByProject = groupAgentsByProject(agentList);
  const agentCountBySlug = countAgentsByProject(agentList);
  const sortedList = sortProjectsByAttention(list, agentCountBySlug);
  const projectNames: Record<string, string> = {};
  for (const project of list) {
    projectNames[project.slug] = project.name;
  }
  const selectedParentName =
    selectedSlug == null
      ? null
      : parentDisplayName(list.find((p) => p.slug === selectedSlug)?.parent ?? null, list);

  return (
    <section aria-label={t.title} className="space-y-5">
      <header>
        <Eyebrow>{t.eyebrow}</Eyebrow>
        <h2 className="mt-1 font-display text-h2 font-semibold text-ink">{t.title}</h2>
        <p className="mt-1 text-sec text-ink-2">{t.subtitle}</p>
      </header>

      {projects.error ? (
        <div className="flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert">
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.error}
        </div>
      ) : null}

      {agents.error ? (
        <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.agentsError}
        </div>
      ) : null}

      {registryErrors.length > 0 ? (
        <div className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          <p className="font-semibold">{t.registryErrors}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 font-data text-micro">
            {registryErrors.map((message, index) => (
              <li key={index}>{message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {projects.data === null && !projects.error ? <p className="text-sec text-ink-3">{t.loading}</p> : null}

      {projects.data !== null && list.length === 0 ? <FleetEmptyState title={t.empty} desc={t.emptyDesc} /> : null}

      {sortedList.length > 0 ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {sortedList.map((project) => {
            const agentCount = agentCountBySlug[project.slug] ?? 0;
            return (
              <ProjectCard
                key={project.slug}
                project={project}
                agents={agentsByProject[project.slug] ?? []}
                parentName={parentDisplayName(project.parent, list)}
                attention={computeAttention(project, agentCount)}
                now={now}
                onOpen={() => setSelectedSlug(project.slug)}
              />
            );
          })}
        </div>
      ) : null}

      {agents.data !== null || agentList.length > 0 ? (
        <AgentsRail agents={agentList} projectNames={projectNames} now={now} />
      ) : null}

      {selectedSlug ? (
        <ProjectDetailDrawer
          slug={selectedSlug}
          parentName={selectedParentName}
          onClose={() => setSelectedSlug(null)}
        />
      ) : null}
    </section>
  );
}
