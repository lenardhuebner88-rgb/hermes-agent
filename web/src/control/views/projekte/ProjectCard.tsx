import type { KeyboardEvent } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Card } from "../../components/primitives";
import { Led } from "../../components/atoms";
import { SignalLabel } from "../../components/leitstand";
import { fmtRelativeTime } from "../../lib/derive";
import type { ProjectAgent, ProjectEntry } from "../../lib/schemas";
import { de } from "../../i18n/de";
import { AGENT_KIND_STYLES, AGENTS_CHIP_MAX_VISIBLE, agentChipText } from "./agentKinds";

const t = de.projekte;

export interface ProjectCardProps {
  project: ProjectEntry;
  /** Agents assigned to this project (from groupAgentsByProject). Empty = idle. */
  agents: ReadonlyArray<ProjectAgent>;
  /** Anzeigename des Elternprojekts, falls `project.parent` gesetzt ist. */
  parentName: string | null;
  now: number;
  /** Opens the project detail drawer (Stufe 6). */
  onOpen: () => void;
}

/** Eine Karte pro Projekt — die Grundeinheit des Projekte-Tabs (Stufe 4/5/6).
 *  Klick / Enter / Space öffnet den Detail-Drawer; Agent-Chip-Tooltips bleiben
 *  erhalten (title auf den Chips, kein stopPropagation nötig). */
export function ProjectCard({ project, agents, parentName, now, onOpen }: ProjectCardProps) {
  const commit = project.last_commit;
  const kanban = project.kanban;
  const loopsActive = project.loops?.active ?? 0;
  const hasErrors = project.errors.length > 0;
  const agentCount = agents.length;
  const visibleAgents = agents.slice(0, AGENTS_CHIP_MAX_VISIBLE);
  const overflow = agentCount - visibleAgents.length;

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onOpen();
    }
  };

  return (
    <Card
      surface="card"
      interactive
      className="flex h-full flex-col gap-3 p-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-bronze"
      ariaLabel={t.detailOpenAria(project.name)}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={onKeyDown}
    >
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h3 className="truncate text-sec font-semibold text-ink">{project.name}</h3>
          {parentName ? <p className="mt-0.5 truncate text-micro text-ink-3">{t.partOf(parentName)}</p> : null}
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
          <span>{t.kanbanOpen} {kanban.open}</span>
          <span>{t.kanbanRunning} {kanban.running}</span>
          {kanban.blocked > 0 ? (
            <SignalLabel tone="warn" label={`${t.kanbanBlocked} ${kanban.blocked}`} />
          ) : (
            <span>{t.kanbanBlocked} {kanban.blocked}</span>
          )}
          <span>{t.kanbanReview} {kanban.review}</span>
          <span>{t.kanbanDone7d} {kanban.done_7d}</span>
        </div>
      ) : null}

      <footer className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-2.5">
        <div
          className={cn(
            "flex min-w-0 flex-1 flex-wrap items-center gap-1.5 text-micro",
            agentCount > 0 ? "text-ink" : "text-ink-3",
          )}
        >
          <Led kind={agentCount > 0 ? "live" : "idle"} size={7} />
          {agentCount === 0 ? (
            <span>{t.agentsCount(0)}</span>
          ) : (
            <>
              {visibleAgents.map((agent, index) => {
                const style = AGENT_KIND_STYLES[agent.kind] ?? AGENT_KIND_STYLES.unknown;
                const Icon = style.icon;
                const text = agentChipText(agent);
                return (
                  <span
                    key={`${agent.kind}:${agent.label}:${index}`}
                    className={cn(
                      "inline-flex max-w-[7.5rem] items-center gap-1 rounded-card border border-line bg-surface-2 px-1.5 py-0.5",
                      style.tone,
                    )}
                    title={agent.task ? `${text} — ${agent.task}` : text}
                  >
                    <Icon className="h-3 w-3 shrink-0" aria-hidden />
                    <span className="truncate">{text}</span>
                  </span>
                );
              })}
              {overflow > 0 ? (
                <span className="shrink-0 font-data text-ink-3">{t.agentsOverflow(overflow)}</span>
              ) : null}
            </>
          )}
        </div>
        {loopsActive > 0 ? (
          <span className="inline-flex shrink-0 items-center gap-1.5 text-micro text-ink-2">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t.loopsActive(loopsActive)}
          </span>
        ) : null}
      </footer>
    </Card>
  );
}
