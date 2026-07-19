/**
 * ProjektePanel — „PROJEKTE" der Jarvis-Shell (Sprint 2, Karte S2.6): die
 * Klassik-ProjectCards als schwebendes A4-Panel (Desktop) bzw. gestapelt im
 * A4-Stack (Mobil), mit echten Daten.
 *
 * Daten über EXAKT dieselben Hooks/Polling-Keys wie ProjekteView
 * (useProjects/useProjectAgents/useProjectSessions — der pollingStore
 * dedupliziert über Keys, kein zweiter Fetch) und dieselbe Ableitung
 * (buildProjectsOverview + computeAttention + splitAgentsBySource aus
 * views/projekte/derive): Logik/Hooks unverändert, nur Präsentation im
 * Jarvis-Look. Karten-Inhalt wie Klassik: Name, Attention-Ampel (Dot +
 * Badge), Grund-Chips, Kanban-Zähler, letzter Commit, live/Check-ins/Loops.
 *
 * Tap auf eine Karte führt per Link zum bisherigen Drilldown-Ziel der
 * Klassik (/control/projekte-klassisch, dort lebt der Detail-Drawer samt
 * Kill-/Aktions-Mechanik) — bewusst kein neues Navigationsmodell und keine
 * Aktionen in der Shell (Brief). Fehler inline (ReceiptsFeed-Idiom), nie
 * still; Empty-/Loading-States wie im A4.
 */
import { Link } from "react-router-dom";

import { de } from "../i18n/de";
import {
  useProjectAgents,
  useProjects,
  useProjectSessions,
} from "../hooks/useControlData";
import { fmtRelativeTime, nowSec } from "../lib/derive";
import type { ProjectAgent, ProjectEntry } from "../lib/schemas";
import {
  buildProjectsOverview,
  computeAttention,
  parentDisplayName,
  splitAgentsBySource,
  type AttentionReason,
  type ProjectAttention,
  type ProjectAttentionResult,
} from "../views/projekte/derive";

const t = de.jarvis;
const tp = de.projekte;

/** Attention-Ampel auf A4-Töne: alert = rot, active = amber, quiet = grau
 *  (neutral — dieselbe Semantik wie die Klassik-Accent-Bar). */
const ATTENTION_TONE: Record<ProjectAttention, string> = {
  alert: "jv-attn-alert",
  active: "jv-attn-active",
  quiet: "jv-attn-quiet",
};

/** Grund-Chip-Label — identisch zur Klassik (reasonChipLabel dort). */
function reasonLabel(reason: AttentionReason): string {
  switch (reason.kind) {
    case "needs_input":
      return tp.reasonNeedsInput(reason.count);
    case "blocked":
      return tp.reasonBlocked(reason.count);
    case "stale_sessions":
      return tp.reasonStale(reason.count);
    case "loop_red":
      return tp.reasonLoopRed;
  }
}

function JvProjectCard({
  project,
  agents,
  parentName,
  attention,
  now,
}: {
  project: ProjectEntry;
  agents: ReadonlyArray<ProjectAgent>;
  parentName: string | null;
  attention: ProjectAttentionResult;
  now: number;
}) {
  const kanban = project.kanban;
  const commit = project.last_commit;
  const { live, claims } = splitAgentsBySource(agents);
  const loopsActive = project.loops?.active ?? 0;
  const level = attention.level;

  const liveParts: string[] = [];
  if (live.length > 0) liveParts.push(tp.liveCount(live.length));
  if (claims.length > 0) liveParts.push(tp.summaryCheckins(claims.length));
  if (loopsActive > 0) liveParts.push(tp.loopsActive(loopsActive));

  return (
    <Link
      to="/control/projekte-klassisch"
      className={`jv-pcard ${ATTENTION_TONE[level]}`}
      data-attention={level}
      aria-label={t.projekteOpenAria(project.name)}
    >
      <span className="jv-pc-head">
        <span
          className="jv-pc-dot"
          aria-label={tp.attentionLabel[level]}
          title={tp.attentionLabel[level]}
        />
        <span className="jv-pc-name">{project.name}</span>
        {level !== "quiet" ? (
          <span className="jv-pc-badge" data-attention-badge={level}>
            {tp.attentionBadge[level]}
          </span>
        ) : null}
        {project.errors.length > 0 ? (
          <span
            className="jv-pc-err"
            title={project.errors.join("\n")}
            aria-label={tp.cardErrorsTooltip(project.errors.length)}
          >
            !
          </span>
        ) : null}
      </span>

      {parentName ? <span className="jv-pc-parent">{tp.partOf(parentName)}</span> : null}

      {attention.reasons.length > 0 ? (
        <span className="jv-pc-reasons">
          {attention.reasons.map((reason) => (
            <span className="jv-pc-chip" key={reason.kind} data-reason={reason.kind}>
              {reasonLabel(reason)}
            </span>
          ))}
        </span>
      ) : null}

      {kanban ? (
        <span className="jv-pc-kanban">
          <span>
            {tp.kanbanOpen} <b>{kanban.open}</b>
          </span>
          <span>
            {tp.kanbanRunning} <b>{kanban.running}</b>
          </span>
          <span className={kanban.blocked > 0 ? "jv-pc-warn" : undefined}>
            {tp.kanbanBlocked} <b>{kanban.blocked}</b>
          </span>
          <span>
            {tp.kanbanReview} <b>{kanban.review}</b>
          </span>
          <span>
            {tp.kanbanDone7d} <b>{kanban.done_7d}</b>
          </span>
        </span>
      ) : null}

      <span className="jv-pc-meta">
        <span className="jv-pc-commit" title={commit ? commit.message : undefined}>
          {commit
            ? `${commit.message || tp.noCommitMessage} · ${fmtRelativeTime(commit.committed_at, now)}`
            : tp.noCommit}
        </span>
        {liveParts.length > 0 ? <span className="jv-pc-live">{liveParts.join(" · ")}</span> : null}
      </span>
    </Link>
  );
}

export function ProjektePanel() {
  const projects = useProjects();
  const agents = useProjectAgents();
  const sessions = useProjectSessions();
  const now = nowSec();

  const list = projects.data?.projects ?? [];
  const { sortedList, agentsByProject, agentCountBySlug, staleBySlug } = buildProjectsOverview(
    list,
    agents.data?.agents ?? [],
    sessions.data?.sessions ?? [],
  );

  return (
    <div className="jv-float jv-projekte" role="region" aria-label={t.projekteTitle}>
      <div className="jv-ptitle">
        {t.projekteTitle}{" "}
        <span style={{ float: "right", color: "var(--faint)", letterSpacing: ".05em" }}>
          {projects.data !== null ? list.length : "—"}
        </span>
      </div>

      {projects.error ? (
        <p className="jv-qerror" role="alert">
          {t.projekteError}
        </p>
      ) : null}

      {!projects.error && projects.data === null ? (
        <p className="jv-qloading">{t.projekteLoading}</p>
      ) : null}

      {!projects.error && projects.data !== null && list.length === 0 ? (
        <div className="jv-quietempty">
          {tp.empty}
          <span>{tp.emptyDesc}</span>
        </div>
      ) : null}

      {sortedList.length > 0 ? (
        <div className="jv-projlist">
          {sortedList.map((project) => (
            <JvProjectCard
              key={project.slug}
              project={project}
              agents={agentsByProject[project.slug] ?? []}
              parentName={parentDisplayName(project.parent, list)}
              attention={computeAttention(
                project,
                agentCountBySlug[project.slug] ?? 0,
                staleBySlug[project.slug] ?? 0,
              )}
              now={now}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
