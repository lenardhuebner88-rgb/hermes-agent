import { useState } from "react";
import { AlertTriangle } from "lucide-react";
import { Eyebrow } from "../components/primitives";
import { FleetEmptyState } from "../components/leitstand";
import { useProjectAgents, useProjectCommits, useProjectSessions, useProjects } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { nowSec } from "../lib/derive";
import type { ProjectAgent } from "../lib/schemas";
import {
  computeAttention,
  countAgentsByProject,
  countOpenSessions,
  groupAgentsByProject,
  parentDisplayName,
  sortProjectsByAttention,
  splitAgentsBySource,
} from "./projekte/derive";
import { ProjectCard } from "./projekte/ProjectCard";
import { ProjectDetailDrawer } from "./projekte/ProjectDetailDrawer";
import { SessionKillSheet } from "./projekte/SessionKillSheet";
import { LiveBoard } from "./projekte/LiveBoard";
import { SessionsSection } from "./projekte/SessionsSection";
import { CommitsFeed } from "./projekte/CommitsFeed";

const t = de.projekte;

/** Projekte-Tab — der Operator-Blick auf "wer arbeitet gerade wirklich woran":
 *  Summary-Strip (live/Check-ins/offene Sessions/blockiert) → LiveBoard
 *  (Agents nach Projekt gruppiert, mit Lane/Operator, tmux killbar) →
 *  Karten-Grid (Attention-sortiert, Klick = Drilldown) → Offene Sessions
 *  (Spawn-Baum aus state.db) → Alle Commits (projektübergreifender Feed).
 *  Daten: GET /api/projects + /agents + /sessions + /commits. */
export function ProjekteView() {
  const projects = useProjects();
  const agents = useProjectAgents();
  const sessions = useProjectSessions();
  const commits = useProjectCommits();
  const now = nowSec();
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [killAgent, setKillAgent] = useState<ProjectAgent | null>(null);

  const list = projects.data?.projects ?? [];
  const registryErrors = projects.data?.registry_errors ?? [];
  const agentList = agents.data?.agents ?? [];
  const sessionList = sessions.data?.sessions ?? [];
  const commitList = commits.data?.commits ?? [];
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

  // Summary-Strip: echte Prozesse vs. Vault-Claims + offene Sessions +
  // offene Arbeit über alle Karten. Gleiche live/claims-Definition wie die
  // Karten-Sektionen.
  const { live: liveAgents, claims: claimAgents } = splitAgentsBySource(agentList);
  const liveTotal = liveAgents.length;
  const claimsTotal = claimAgents.length;
  const openSessionsTotal = countOpenSessions(sessionList);
  let blockedTotal = 0;
  let needsInputTotal = 0;
  for (const project of list) {
    blockedTotal += project.kanban?.blocked ?? 0;
    needsInputTotal += project.kanban?.needs_input ?? 0;
  }

  return (
    <section aria-label={t.title} className="space-y-5">
      <header>
        <Eyebrow>{t.eyebrow}</Eyebrow>
        <h2 className="mt-1 font-display text-h2 font-semibold text-ink">{t.title}</h2>
        <p className="mt-1 text-sec text-ink-2">{t.subtitle}</p>
        {agents.data !== null ? (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-micro">
            <span className="inline-flex items-center gap-1.5 rounded-card border border-bronze/40 bg-bronze/10 px-2 py-0.5 font-data text-bronze-hi">
              {t.summaryLive(liveTotal)}
            </span>
            <span className="inline-flex items-center rounded-card border border-line bg-surface-1 px-2 py-0.5 font-data text-ink-2">
              {t.summaryCheckins(claimsTotal)}
            </span>
            {sessions.data !== null ? (
              <span className="inline-flex items-center rounded-card border border-line bg-surface-1 px-2 py-0.5 font-data text-ink-2">
                {t.summaryOpenSessions(openSessionsTotal)}
              </span>
            ) : null}
            {blockedTotal > 0 ? (
              <span className="inline-flex items-center rounded-card border border-status-alert/40 bg-status-alert/10 px-2 py-0.5 font-data text-status-alert">
                {t.summaryBlocked(blockedTotal)}
              </span>
            ) : null}
            {needsInputTotal > 0 ? (
              <span className="inline-flex items-center rounded-card border border-status-warn/40 bg-status-warn/10 px-2 py-0.5 font-data text-status-warn">
                {t.summaryNeedsInput(needsInputTotal)}
              </span>
            ) : null}
          </div>
        ) : null}
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

      {sessions.error ? (
        <div className="flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.sessionsError}
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

      {agents.data !== null ? (
        <LiveBoard
          agents={agentList}
          projectNames={projectNames}
          now={now}
          onKillSession={setKillAgent}
        />
      ) : null}

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
                onKillSession={setKillAgent}
              />
            );
          })}
        </div>
      ) : null}

      {sessions.data !== null ? (
        <SessionsSection sessions={sessionList} projectNames={projectNames} now={now} />
      ) : null}

      {commits.data !== null ? <CommitsFeed commits={commitList} now={now} /> : null}

      {selectedSlug ? (
        <ProjectDetailDrawer
          slug={selectedSlug}
          parentName={selectedParentName}
          onClose={() => setSelectedSlug(null)}
        />
      ) : null}

      {killAgent ? (
        <SessionKillSheet
          agent={killAgent}
          projectName={killAgent.project ? (projectNames[killAgent.project] ?? killAgent.project) : null}
          now={now}
          onClose={() => setKillAgent(null)}
          onKilled={() => {
            void agents.reload();
          }}
        />
      ) : null}
    </section>
  );
}
