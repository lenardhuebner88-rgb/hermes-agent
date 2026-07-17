import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { Eyebrow } from "../../components/primitives";
import { SectionHeader } from "../../components/leitstand";
import { Led } from "../../components/atoms";
import { fmtAge } from "../../lib/derive";
import type { ProjectAgent } from "../../lib/schemas";
import { de } from "../../i18n/de";
import { AGENT_KIND_STYLES } from "./agentKinds";
import { killTarget, liveBoardGroups } from "./derive";

const t = de.projekte;

export interface LiveBoardProps {
  agents: ReadonlyArray<ProjectAgent>;
  /** slug → display name for group headers; missing slugs fall back to the raw slug. */
  projectNames: Readonly<Record<string, string>>;
  now: number;
  /** Opens the kill-confirmation sheet for one live (tmux) row. */
  onKillSession: (agent: ProjectAgent) => void;
}

/** "Wer arbeitet gerade" — the one board that answers the operator's central
 *  question: grouped by PROJECT (not by engine like the old rail), every row
 *  shows who (kind), on what (task/label), from which source (process, kanban
 *  task, loop, check-in), for whom (assignee/operator) and for how long.
 *  tmux rows stay killable through the structured target only. */
export function LiveBoard({ agents, projectNames, now, onKillSession }: LiveBoardProps) {
  const groups = liveBoardGroups(agents);

  return (
    <section aria-label={t.liveBoard} className="space-y-3">
      <header>
        <Eyebrow>{t.liveBoardEyebrow}</Eyebrow>
        <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.liveBoard}</h3>
      </header>

      {groups.length === 0 ? (
        <p className="text-sec text-ink-3">{t.liveBoardEmpty}</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {groups.map((group) => (
            <div
              key={group.slug ?? "__unassigned__"}
              className="min-w-0 space-y-1.5 rounded-panel border border-line-soft bg-surface-1 p-3"
            >
              <SectionHeader
                label={group.slug == null ? t.unassigned : (projectNames[group.slug] ?? group.slug)}
                meta={group.agents.length}
                rule={false}
              />
              <ul className="space-y-1">
                {group.agents.map((agent, index) => (
                  <LiveBoardRow
                    key={`${agent.source}:${agent.kind}:${agent.label}:${index}`}
                    agent={agent}
                    now={now}
                    onKillSession={onKillSession}
                  />
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function LiveBoardRow({
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
  const isLive = agent.source === "tmux";
  const isClaim = agent.source === "coordination";
  const target = killTarget(agent);
  const headline = agent.task ?? agent.label;

  return (
    <li className="flex min-w-0 items-center gap-2 rounded-card border border-line-soft bg-surface-2 px-2.5 py-1.5">
      <Icon className={cn("h-3.5 w-3.5 shrink-0", style.tone)} aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-micro text-ink" title={headline}>
          {headline || "—"}
        </p>
        <p className="mt-0.5 flex min-w-0 flex-wrap items-center gap-x-1.5 text-micro text-ink-3">
          <span>{style.label}</span>
          <span aria-hidden>·</span>
          <span>{t.sourceLabel(agent.source)}</span>
          {agent.task && agent.label ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate font-data" title={agent.label}>
                {agent.label}
              </span>
            </>
          ) : null}
          {agent.assignee ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate" title={agent.assignee}>
                {t.assigneeLabel(agent.assignee)}
              </span>
            </>
          ) : null}
          {agent.operator ? (
            <>
              <span aria-hidden>·</span>
              <span className="truncate" title={agent.operator}>
                {t.operatorLabel(agent.operator)}
              </span>
            </>
          ) : null}
        </p>
      </div>
      {isClaim ? (
        <span className="shrink-0 rounded-card border border-dashed border-line px-1.5 py-0.5 font-data text-micro text-ink-3">
          {t.claimTag}
        </span>
      ) : null}
      {isLive ? <Led kind="live" size={7} /> : null}
      {agent.since != null && Number.isFinite(agent.since) ? (
        <span className="shrink-0 font-data text-micro tabular-nums text-ink-3">
          {fmtAge(agent.since, now)}
        </span>
      ) : null}
      {target ? (
        <button
          type="button"
          aria-label={t.killSessionAria(headline)}
          title={t.killSessionAria(headline)}
          onClick={() => onKillSession(agent)}
          className="grid size-7 shrink-0 place-items-center rounded-card border border-line text-ink-3 hover:border-status-alert/40 hover:bg-status-alert/10 hover:text-status-alert focus-visible:outline-2 focus-visible:outline-bronze"
        >
          <X className="h-3.5 w-3.5" aria-hidden />
        </button>
      ) : null}
    </li>
  );
}
