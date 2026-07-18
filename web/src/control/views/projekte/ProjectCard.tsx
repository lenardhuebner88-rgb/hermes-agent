import type { KeyboardEvent, ReactNode } from "react";
import { AlertTriangle, RefreshCw, X } from "lucide-react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Card } from "../../components/primitives";
import { Led } from "../../components/atoms";
import { SectionHeader, SignalLabel } from "../../components/leitstand";
import { fmtAge, fmtRelativeTime } from "../../lib/derive";
import type { ProjectAgent, ProjectEntry } from "../../lib/schemas";
import type { TaskStatus } from "../../lib/types";
import { de } from "../../i18n/de";
import { AGENT_KIND_STYLES } from "./agentKinds";
import {
  attentionTone,
  killTarget,
  splitAgentsBySource,
  type AttentionReason,
  type ProjectAttention,
  type ProjectAttentionResult,
} from "./derive";

const t = de.projekte;

/** Board slug from registry (`kanban_project`), empty/whitespace → null. */
function projectKanbanBoard(project: ProjectEntry): string | null {
  const raw = project.kanban_project;
  if (typeof raw !== "string") return null;
  const slug = raw.trim();
  return slug.length > 0 ? slug : null;
}

/**
 * Honest chip → Fleet deep-link mapping (see projects_overview._kanban_counts):
 * - open: aggregates triage/todo/scheduled/ready → board only, no status
 * - running / blocked / review: exact BoardTab TaskStatus → board + status
 * - done_7d: 7-day window, not BoardTab "done" → no link (static)
 * - needs_input: no chip in this stage
 */
function fleetChipHref(board: string, status?: TaskStatus): string {
  const params = new URLSearchParams({ board });
  if (status) params.set("status", status);
  return `/control/fleet?${params.toString()}`;
}

/** Touch-friendly chip link; stopPropagation so the card drawer stays closed. */
function KanbanChipLink({
  to,
  children,
  className,
  ariaLabel,
}: {
  to: string;
  children: ReactNode;
  className?: string;
  ariaLabel: string;
}) {
  return (
    <Link
      to={to}
      aria-label={ariaLabel}
      onClick={(event) => {
        event.stopPropagation();
      }}
      onKeyDown={(event) => {
        // Nested interactive inside role=button card: keep Enter/Space from opening drawer.
        event.stopPropagation();
      }}
      className={cn(
        "inline-flex min-h-11 min-w-11 items-center rounded-card px-1 -mx-0.5 tab:min-h-7 tab:min-w-7",
        "text-ink-2 underline-offset-2 hover:text-ink hover:underline",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze",
        className,
      )}
    >
      {children}
    </Link>
  );
}

/** Left-edge accent bar for the attention ampel. Rendered as an absolute child
 *  rather than a `border-l-*` utility: the Card's own `.hc-surface-card` border
 *  shorthand outranks Tailwind border utilities in the cascade, so a utility
 *  border silently loses. Tokens only — no hardcoded colors. */
const ATTENTION_ACCENT: Record<ProjectAttention, string> = {
  alert: "bg-status-alert",
  active: "bg-status-warn",
  quiet: "bg-ink-3/40",
};

const ATTENTION_DOT: Record<ProjectAttention, string> = {
  alert: "bg-status-alert",
  active: "bg-status-warn",
  quiet: "bg-ink-3",
};

/** Badge surface for the v2 Ampel (tokens only). Quiet is never rendered. */
const ATTENTION_BADGE: Record<Exclude<ProjectAttention, "quiet">, string> = {
  alert: "border-status-alert/40 bg-status-alert/10 text-status-alert",
  active: "border-bronze/40 bg-bronze/10 text-bronze-hi",
};

function reasonChipLabel(reason: AttentionReason): string {
  switch (reason.kind) {
    case "needs_input":
      return t.reasonNeedsInput(reason.count);
    case "blocked":
      return t.reasonBlocked(reason.count);
    case "stale_sessions":
      return t.reasonStale(reason.count);
    case "loop_red":
      return t.reasonLoopRed;
  }
}

export interface ProjectCardProps {
  project: ProjectEntry;
  /** Agents assigned to this project (from groupAgentsByProject). Empty = idle. */
  agents: ReadonlyArray<ProjectAgent>;
  /** Anzeigename des Elternprojekts, falls `project.parent` gesetzt ist. */
  parentName: string | null;
  /** Stufe 7/2.3 attention (level + intervention reasons). */
  attention: ProjectAttentionResult;
  now: number;
  /** Opens the project detail drawer (Stufe 6). */
  onOpen: () => void;
  /** Opens the kill-confirmation sheet for one live (tmux) session row. */
  onKillSession: (agent: ProjectAgent) => void;
}

/** Eine Karte pro Projekt — die Grundeinheit des Projekte-Tabs.
 *  Klick / Enter / Space öffnet den Detail-Drawer. Der frühere Footer mit
 *  anonymen Agent-Chips ist seit 2026-07-17 in zwei Sektionen aufgeteilt:
 *  SESSIONS (echte laufende tmux-Prozesse, mit Laufzeit + ✕-Kill) und
 *  CHECK-INS (Vault-Claims mit Task-Text, bewusst NICHT killbar). Der Kill-
 *  Button stoppt die Propagation, damit nicht der Drawer aufgeht. */
export function ProjectCard({ project, agents, parentName, attention, now, onOpen, onKillSession }: ProjectCardProps) {
  const commit = project.last_commit;
  const kanban = project.kanban;
  const kanbanBoard = projectKanbanBoard(project);
  const loopsActive = project.loops?.active ?? 0;
  const hasErrors = project.errors.length > 0;
  const { live, claims } = splitAgentsBySource(agents);
  const level = attention.level;
  const tone = attentionTone(level);
  const attentionLabel = t.attentionLabel[level];
  const showBadge = level !== "quiet";
  const reasons = attention.reasons;

  const onKeyDown = (event: KeyboardEvent) => {
    // Only the card itself opens the drawer: Enter/Space on a NESTED control
    // (the kill button) must not bubble up and open the drawer alongside the
    // sheet (Fable review 2026-07-17, obs. 1).
    if (event.target !== event.currentTarget) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
  };

  return (
    <Card
      surface="card"
      interactive
      className="relative overflow-hidden flex h-full flex-col gap-3 p-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze"
      ariaLabel={t.detailOpenAria(project.name)}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={onKeyDown}
    >
      <span
        aria-hidden
        className={cn("pointer-events-none absolute inset-y-0 left-0 w-0.5", ATTENTION_ACCENT[level])}
      />
      <header className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            {/* Existing attention marker — v2 keeps data-attention / tone for
                tests and the left accent; badge is additive, not a replacement. */}
            <span
              aria-label={attentionLabel}
              title={attentionLabel}
              data-attention={level}
              data-tone={tone}
              className={cn("size-1.5 shrink-0 rounded-full", ATTENTION_DOT[level])}
            />
            <h3 className="min-w-0 truncate text-sec font-semibold text-ink">{project.name}</h3>
            {showBadge ? (
              <span
                data-attention-badge={level}
                className={cn(
                  "inline-flex shrink-0 items-center rounded-card border px-1.5 py-0.5 font-data text-micro",
                  ATTENTION_BADGE[level],
                )}
              >
                {t.attentionBadge[level]}
              </span>
            ) : null}
          </div>
          {parentName ? <p className="mt-0.5 truncate text-micro text-ink-3">{t.partOf(parentName)}</p> : null}
          {reasons.length > 0 ? (
            <div
              data-attention-reasons=""
              className="mt-1.5 flex min-w-0 flex-nowrap items-center gap-1 overflow-x-auto [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
            >
              {reasons.map((reason) => (
                <span
                  key={reason.kind}
                  data-reason={reason.kind}
                  className="inline-flex shrink-0 items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 font-data text-micro text-ink-2"
                >
                  {reasonChipLabel(reason)}
                </span>
              ))}
            </div>
          ) : null}
        </div>
        {hasErrors ? (
          <span
            title={project.errors.join("\n")}
            aria-label={t.cardErrorsTooltip(project.errors.length)}
            className="shrink-0 text-status-warn"
          >
            <AlertTriangle className="h-4 w-4" aria-hidden />
          </span>
        ) : null}
      </header>

      {commit ? (
        <div className="min-w-0 text-micro">
          <p className="truncate text-ink-2">{commit.message || t.noCommitMessage}</p>
          <p className="mt-0.5 flex items-center gap-1.5 font-data text-ink-3">
            {commit.author ? <span className="truncate text-ink-2">{commit.author}</span> : null}
            {commit.author ? <span aria-hidden>·</span> : null}
            <span>{commit.hash}</span>
            <span aria-hidden>·</span>
            <span>{fmtRelativeTime(commit.committed_at, now)}</span>
          </p>
        </div>
      ) : (
        <p className="text-micro text-ink-3">{t.noCommit}</p>
      )}

      {kanban ? (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-data text-micro tabular-nums text-ink-2">
          {kanbanBoard ? (
            <KanbanChipLink
              to={fleetChipHref(kanbanBoard)}
              ariaLabel={`${t.kanbanOpen} ${kanban.open} — Fleet-Board öffnen`}
            >
              {t.kanbanOpen} {kanban.open}
            </KanbanChipLink>
          ) : (
            <span>{t.kanbanOpen} {kanban.open}</span>
          )}
          {kanbanBoard ? (
            <KanbanChipLink
              to={fleetChipHref(kanbanBoard, "running")}
              ariaLabel={`${t.kanbanRunning} ${kanban.running} — Fleet-Board öffnen`}
            >
              {t.kanbanRunning} {kanban.running}
            </KanbanChipLink>
          ) : (
            <span>{t.kanbanRunning} {kanban.running}</span>
          )}
          {kanban.blocked > 0 ? (
            kanbanBoard ? (
              <KanbanChipLink
                to={fleetChipHref(kanbanBoard, "blocked")}
                ariaLabel={`${t.kanbanBlocked} ${kanban.blocked} — Fleet-Board öffnen`}
                className="text-status-warn hover:text-status-warn"
              >
                <SignalLabel tone="warn" label={`${t.kanbanBlocked} ${kanban.blocked}`} />
              </KanbanChipLink>
            ) : (
              <SignalLabel tone="warn" label={`${t.kanbanBlocked} ${kanban.blocked}`} />
            )
          ) : kanbanBoard ? (
            <KanbanChipLink
              to={fleetChipHref(kanbanBoard, "blocked")}
              ariaLabel={`${t.kanbanBlocked} ${kanban.blocked} — Fleet-Board öffnen`}
            >
              {t.kanbanBlocked} {kanban.blocked}
            </KanbanChipLink>
          ) : (
            <span>{t.kanbanBlocked} {kanban.blocked}</span>
          )}
          {kanbanBoard ? (
            <KanbanChipLink
              to={fleetChipHref(kanbanBoard, "review")}
              ariaLabel={`${t.kanbanReview} ${kanban.review} — Fleet-Board öffnen`}
            >
              {t.kanbanReview} {kanban.review}
            </KanbanChipLink>
          ) : (
            <span>{t.kanbanReview} {kanban.review}</span>
          )}
          {/* done_7d is a 7-day window, not BoardTab status=done — stay static (honesty). */}
          <span>{t.kanbanDone7d} {kanban.done_7d}</span>
        </div>
      ) : null}

      {live.length === 0 && claims.length === 0 ? (
        <p className="text-micro text-ink-3">{t.agentsCount(0)}</p>
      ) : null}

      {live.length > 0 ? (
        <section aria-label={t.sessionsSection} className="min-w-0 space-y-1.5">
          <SectionHeader
            label={
              <span className="inline-flex items-center gap-1.5">
                <Led kind="live" size={7} />
                {t.sessionsSection}
              </span>
            }
            meta={t.liveCount(live.length)}
            rule={false}
          />
          <ul className="space-y-1">
            {live.map((agent, index) => (
              <LiveSessionRow
                key={`${agent.tmux_session ?? ""}:${agent.tmux_window ?? ""}:${agent.label}:${index}`}
                agent={agent}
                now={now}
                onKillSession={onKillSession}
              />
            ))}
          </ul>
        </section>
      ) : null}

      {claims.length > 0 ? (
        <section aria-label={t.checkinsSection} className="min-w-0 space-y-1.5">
          <SectionHeader label={t.checkinsSection} meta={claims.length} rule={false} />
          <ul className="space-y-1">
            {claims.map((agent, index) => (
              <ClaimRow key={`${agent.kind}:${agent.label}:${index}`} agent={agent} now={now} />
            ))}
          </ul>
        </section>
      ) : null}

      {loopsActive > 0 ? (
        <footer className="mt-auto flex items-center justify-end gap-2 border-t border-line pt-2.5">
          <span className="inline-flex shrink-0 items-center gap-1.5 text-micro text-ink-2">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t.loopsActive(loopsActive)}
          </span>
        </footer>
      ) : null}
    </Card>
  );
}

/** Eine laufende tmux-Session: Kind-Icon, Label, Live-LED, Laufzeit und —
 *  nur mit strukturiertem Kill-Ziel (siehe killTarget) — der ✕-Button. */
function LiveSessionRow({
  agent,
  now,
  onKillSession,
}: {
  agent: ProjectAgent;
  now: number;
  onKillSession: (agent: ProjectAgent) => void;
}) {
  const style = AGENT_KIND_STYLES[agent.kind] ?? AGENT_KIND_STYLES.unknown;
  const Icon = style.icon;
  const target = killTarget(agent);
  const label = agent.label || `${agent.tmux_session ?? "?"}:${agent.tmux_window ?? "?"}`;

  return (
    <li className="flex min-w-0 items-center gap-2 rounded-card border border-line-soft bg-surface-2 px-2 py-1.5">
      <Icon className={cn("h-3.5 w-3.5 shrink-0", style.tone)} aria-hidden />
      <span className="min-w-0 flex-1 truncate font-data text-micro text-ink" title={label}>
        {label}
      </span>
      <Led kind="live" size={7} />
      {agent.since != null && Number.isFinite(agent.since) ? (
        <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
          {fmtAge(agent.since, now)}
        </span>
      ) : null}
      {target ? (
        <button
          type="button"
          aria-label={t.killSessionAria(label)}
          title={t.killSessionAria(label)}
          onClick={(event) => {
            event.stopPropagation();
            onKillSession(agent);
          }}
          className="grid size-7 shrink-0 place-items-center rounded-card border border-line text-ink-3 hover:border-status-alert/40 hover:bg-status-alert/10 hover:text-status-alert focus-visible:outline-2 focus-visible:outline-bronze"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : null}
    </li>
  );
}

/** Ein Vault-Check-in (Claim): Task-Text + Art + Alter — bewusst ohne Kill-
 *  Button, denn ein Claim ist kein killbarer Prozess (siehe Sheet-Hinweis).
 *  Trägt die Note ein `operator:`-Feld, steht das "für wen" dabei. */
function ClaimRow({ agent, now }: { agent: ProjectAgent; now: number }) {
  const style = AGENT_KIND_STYLES[agent.kind] ?? AGENT_KIND_STYLES.unknown;

  return (
    <li className="flex min-w-0 items-center gap-2 px-2 py-1">
      <span aria-hidden className="size-1.5 shrink-0 rounded-full border border-dashed border-ink-3" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-micro text-ink-2" title={agent.task ?? agent.label}>
          {agent.task ?? agent.label}
        </p>
        <p className="truncate text-micro text-ink-3">
          {style.label} · {t.claimKind}
          {agent.operator ? ` · ${t.operatorLabel(agent.operator)}` : ""}
        </p>
      </div>
      <span className="shrink-0 rounded-card border border-dashed border-line px-1.5 py-0.5 font-data text-micro text-ink-3">
        {t.claimTag}
      </span>
      {agent.since != null && Number.isFinite(agent.since) ? (
        <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
          {fmtAge(agent.since, now)}
        </span>
      ) : null}
    </li>
  );
}
