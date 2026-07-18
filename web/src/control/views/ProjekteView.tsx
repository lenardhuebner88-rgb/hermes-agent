import { useState } from "react";
import { AlertTriangle, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../components/primitives";
import { FleetEmptyState } from "../components/leitstand";
import { useProjectAgents, useProjectCommits, useProjectReceipts, useProjectSessions, useProjects } from "../hooks/useControlData";
import { de } from "../i18n/de";
import { nowSec } from "../lib/derive";
import type { ProjectAgent } from "../lib/schemas";
import {
  computeAttention,
  countAgentsByProject,
  countOpenSessions,
  countStaleSessionsByProject,
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
import { ReceiptsFeed } from "./projekte/ReceiptsFeed";

const t = de.projekte;

/** Projekte-Tab — der Operator-Blick auf "wer arbeitet gerade wirklich woran":
 *  Summary-Strip (live/Check-ins/offene Sessions/blockiert) → LiveBoard
 *  (Agents nach Projekt gruppiert, mit Lane/Operator, tmux killbar) →
 *  Karten-Grid (Attention-sortiert, Klick = Drilldown) → Ergebnisse (Cross-
 *  Agent Receipt-Feed, Zeilenklick = Lese-Sheet) → Offene Sessions
 *  (Spawn-Baum aus state.db) → Alle Commits (projektübergreifender Feed).
 *  Daten: GET /api/projects + /agents + /sessions + /commits + /receipts.
 *  Mobile (<tab 600px, sessions-first): der Strip wird zur sticky,
 *  horizontal scrollbaren Zeile direkt unter dem App-Header; die Reihenfolge
 *  Strip → LiveBoard → Karten → Sessions → Commits bleibt (CSS-only, kein
 *  matchMedia); der Commit-Feed steckt hinter einer "Commits anzeigen"-
 *  Disclosure (default zu). Ab tab: exakt das Desktop-Layout von jeher. */
export function ProjekteView() {
  const projects = useProjects();
  const agents = useProjectAgents();
  const sessions = useProjectSessions();
  const commits = useProjectCommits();
  const receipts = useProjectReceipts();
  const now = nowSec();
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [killAgent, setKillAgent] = useState<ProjectAgent | null>(null);
  // Commits-Disclosure ist nur <tab sichtbar/bedienbar (Button tab:hidden,
  // Feed tab:block); der State beeinflusst das Desktop-Layout nie.
  const [commitsOpen, setCommitsOpen] = useState(false);

  const list = projects.data?.projects ?? [];
  const registryErrors = projects.data?.registry_errors ?? [];
  const agentList = agents.data?.agents ?? [];
  const sessionList = sessions.data?.sessions ?? [];
  const commitList = commits.data?.commits ?? [];
  const receiptList = receipts.data?.receipts ?? [];
  const agentsByProject = groupAgentsByProject(agentList);
  const agentCountBySlug = countAgentsByProject(agentList);
  // Stale-open sessions → per-card Ampel source (client aggregate; sessions
  // payload is already loaded for the Sessions section).
  const staleBySlug = countStaleSessionsByProject(sessionList);
  const sortedList = sortProjectsByAttention(list, agentCountBySlug, staleBySlug);
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
      </header>

      {/* Mobile (<tab): kompakter Sticky-Strip direkt unter dem App-Header —
          volle Breite (-mx-4/px-4 über den main-Gutter), eine horizontal
          scrollbare Chip-Zeile auf der Page-Canvas (surface-0/95 + Blur,
          FleetCard-Sticky-Idiom). Ab tab: die bisherige Header-Chip-Zeile.
          MUSS direktes Kind der Root-Section sein: position:sticky pinnt nur
          innerhalb des Eltern-Blocks — im <header> (nur so hoch wie der Strip
          selbst) war der Pin-Bereich null (ui-verify-Fund 2026-07-18). */}
      {agents.data !== null ? (
        <div className="sticky top-0 z-10 -mx-4 flex flex-nowrap items-center gap-1.5 overflow-x-auto border-b border-line-soft bg-surface-0/95 px-4 py-2 text-micro backdrop-blur [scrollbar-width:none] [&::-webkit-scrollbar]:hidden tab:static tab:z-auto tab:mx-0 tab:flex-wrap tab:overflow-visible tab:border-b-0 tab:bg-transparent tab:px-0 tab:py-0 tab:backdrop-blur-none">
          <span className="inline-flex shrink-0 items-center gap-1.5 rounded-card border border-bronze/40 bg-bronze/10 px-2 py-0.5 font-data text-bronze-hi">
            {t.summaryLive(liveTotal)}
          </span>
          <span className="inline-flex shrink-0 items-center rounded-card border border-line bg-surface-1 px-2 py-0.5 font-data text-ink-2">
            {t.summaryCheckins(claimsTotal)}
          </span>
          {sessions.data !== null ? (
            <span className="inline-flex shrink-0 items-center rounded-card border border-line bg-surface-1 px-2 py-0.5 font-data text-ink-2">
              {t.summaryOpenSessions(openSessionsTotal)}
            </span>
          ) : null}
          {blockedTotal > 0 ? (
            <span className="inline-flex shrink-0 items-center rounded-card border border-status-alert/40 bg-status-alert/10 px-2 py-0.5 font-data text-status-alert">
              {t.summaryBlocked(blockedTotal)}
            </span>
          ) : null}
          {needsInputTotal > 0 ? (
            <span className="inline-flex shrink-0 items-center rounded-card border border-status-warn/40 bg-status-warn/10 px-2 py-0.5 font-data text-status-warn">
              {t.summaryNeedsInput(needsInputTotal)}
            </span>
          ) : null}
        </div>
      ) : null}

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
            const staleCount = staleBySlug[project.slug] ?? 0;
            return (
              <ProjectCard
                key={project.slug}
                project={project}
                agents={agentsByProject[project.slug] ?? []}
                parentName={parentDisplayName(project.parent, list)}
                attention={computeAttention(project, agentCount, staleCount)}
                now={now}
                onOpen={() => setSelectedSlug(project.slug)}
                onKillSession={setKillAgent}
              />
            );
          })}
        </div>
      ) : null}

      {/* Sichtbar sobald der erste Fetch beantwortet ist (Daten ODER Fehler) —
          nie mehr komplett fehlend bei Fehler; während des initialen Ladens
          aber kein falscher "Noch keine Receipts"-Zustand. */}
      {receipts.data !== null || receipts.error ? (
        <ReceiptsFeed
          receipts={receiptList}
          projectNames={projectNames}
          now={now}
          error={Boolean(receipts.error)}
        />
      ) : null}

      {sessions.data !== null ? (
        <SessionsSection sessions={sessionList} projectNames={projectNames} now={now} />
      ) : null}

      {commits.data !== null ? (
        <>
          {/* Mobile (<tab): Feed hinter einer Disclosure (default zu); der
              Toggle ist ein 44px-Ziel und verschwindet ab tab. Desktop sieht
              den Feed wie bisher direkt (tab:block) — derselbe DOM-Baum. */}
          <button
            type="button"
            aria-expanded={commitsOpen}
            aria-controls="projekte-commits-feed"
            onClick={() => setCommitsOpen((open) => !open)}
            className="flex min-h-11 w-full items-center gap-2 rounded-card border border-line bg-surface-1 px-3 py-2 text-left text-sec text-ink-2 hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-bronze tab:hidden"
          >
            <ChevronRight
              aria-hidden
              className={cn(
                "h-4 w-4 shrink-0 text-ink-3 transition-transform duration-150 ease-out motion-reduce:transition-none",
                commitsOpen ? "rotate-90" : "",
              )}
            />
            {commitsOpen ? t.commitsHide : t.commitsShow}
          </button>
          <div
            id="projekte-commits-feed"
            className={cn("tab:block", commitsOpen ? "block" : "hidden")}
          >
            <CommitsFeed commits={commitList} now={now} />
          </div>
        </>
      ) : null}

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
