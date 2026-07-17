import { cn } from "@/lib/utils";
import { Eyebrow } from "../../components/primitives";
import { fmtRelativeTime } from "../../lib/derive";
import type { ProjectAgent } from "../../lib/schemas";
import { de } from "../../i18n/de";
import { AGENT_KIND_STYLES } from "./agentKinds";
import { groupAgentsByKind } from "./derive";

const t = de.projekte;

export interface AgentsRailProps {
  agents: ReadonlyArray<ProjectAgent>;
  /** slug → display name for the project tag; missing slugs fall back to the raw slug. */
  projectNames: Readonly<Record<string, string>>;
  now: number;
}

/** "Alle Agents" rail below the project card grid (Stufe 5) — agents grouped by
 *  kind, unassigned rows tagged "Unzugeordnet" inside their kind group. No
 *  click/drilldown (Stufe 6). */
export function AgentsRail({ agents, projectNames, now }: AgentsRailProps) {
  const groups = groupAgentsByKind(agents);

  return (
    <section aria-label={t.agentsRail} className="space-y-3">
      <header>
        <Eyebrow>{t.agentsRailEyebrow}</Eyebrow>
        <h3 className="mt-1 font-display text-sec font-semibold text-ink">{t.agentsRail}</h3>
      </header>

      {groups.length === 0 ? (
        <p className="text-sec text-ink-3">{t.agentsRailEmpty}</p>
      ) : (
        <div className="space-y-4">
          {groups.map(([kind, list]) => {
            const style = AGENT_KIND_STYLES[kind];
            const Icon = style.icon;
            return (
              <div key={kind} className="space-y-1.5">
                <div className={cn("flex items-center gap-1.5 text-micro font-medium", style.tone)}>
                  <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span>{style.label}</span>
                  <span className="font-data tabular-nums text-ink-3">{list.length}</span>
                </div>
                <ul className="space-y-1">
                  {list.map((agent, index) => (
                    <AgentRailRow
                      key={`${kind}:${agent.label}:${agent.project ?? ""}:${agent.since ?? ""}:${index}`}
                      agent={agent}
                      projectNames={projectNames}
                      now={now}
                    />
                  ))}
                </ul>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function AgentRailRow({
  agent,
  projectNames,
  now,
}: {
  agent: ProjectAgent;
  projectNames: Readonly<Record<string, string>>;
  now: number;
}) {
  const projectTag =
    agent.project == null
      ? t.unassigned
      : (projectNames[agent.project] ?? agent.project);

  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-line-soft bg-surface-2 px-2.5 py-1.5 text-micro">
      <span className="min-w-0 max-w-full truncate font-data text-ink" title={agent.label}>
        {agent.label || "—"}
      </span>
      {agent.task ? (
        <span className="min-w-0 max-w-full truncate text-ink-2" title={agent.task}>
          {agent.task}
        </span>
      ) : null}
      <span className="inline-flex max-w-full shrink-0 items-center rounded-card border border-line bg-surface-1 px-1.5 py-0.5 text-micro text-ink-3">
        <span className="truncate">{projectTag}</span>
      </span>
      {agent.since != null ? (
        <span className="shrink-0 font-data tabular-nums text-ink-3">
          {fmtRelativeTime(agent.since, now)}
        </span>
      ) : null}
    </li>
  );
}
