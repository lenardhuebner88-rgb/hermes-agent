import { AlertTriangle, ExternalLink, FolderGit2 } from "lucide-react";
import { DrawerShell, SectionHeader, SignalChip } from "../../components/leitstand";
import { Led } from "../../components/atoms";
import { SkeletonCard } from "../../components/primitives";
import { useProjectDetail } from "../../hooks/useControlData";
import { de } from "../../i18n/de";
import { fmtRelativeTime, nowSec } from "../../lib/derive";
import type { ProjectDetail } from "../../lib/schemas";
import { AGENT_KIND_STYLES, agentChipText } from "./agentKinds";
import { kanbanTaskTone, loopOutcomeTone } from "./derive";
import { cn } from "@/lib/utils";

const t = de.projekte;

export function ProjectDetailDrawer({
  slug,
  parentName,
  onClose,
}: {
  slug: string;
  /** Display name of parent project when this is a sub-project. */
  parentName?: string | null;
  onClose: () => void;
}) {
  const detail = useProjectDetail(slug);
  const data = detail.data;
  const now = nowSec();

  return (
    <DrawerShell
      eyebrow={t.detailEyebrow}
      title={data?.name || slug}
      icon={FolderGit2}
      onClose={onClose}
      ariaLabel={data?.name ? t.detailOpenAria(data.name) : t.detailEyebrow}
      closeLabel={t.detailClose}
      headerExtra={
        <div className="mt-2 min-w-0 space-y-1">
          {parentName ? <p className="truncate text-micro text-ink-3">{t.partOf(parentName)}</p> : null}
          {data?.repo_path ? (
            <p className="truncate font-data text-micro text-ink-3" title={data.repo_path}>
              {data.repo_path}
            </p>
          ) : null}
        </div>
      }
      widthClassName="tab:w-[min(480px,70vw)]"
    >
      {detail.loading && !data ? <SkeletonCard rows={5} /> : null}
      {detail.error ? (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-card border border-status-alert/30 bg-status-alert/10 px-3 py-2 text-sec text-status-alert"
        >
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.detailError}
        </div>
      ) : null}
      {data?.error ? (
        <div
          role="alert"
          className="mb-3 flex items-start gap-2 rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn"
        >
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0" />
          {t.detailUnknown}
        </div>
      ) : null}
      {data && !data.error ? <ProjectDetailBody data={data} now={now} /> : null}
    </DrawerShell>
  );
}

export function ProjectDetailBody({ data, now }: { data: ProjectDetail; now: number }) {
  return (
    <div className="min-w-0 space-y-5">
      {data.errors.length > 0 ? (
        <div className="rounded-card border border-status-warn/30 bg-status-warn/10 px-3 py-2 text-sec text-status-warn">
          <p className="font-semibold">{t.detailSourceErrors}</p>
          <ul className="mt-1 list-disc space-y-0.5 pl-4 font-data text-micro">
            {data.errors.map((message, index) => (
              <li key={index} className="break-words">
                {message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="min-w-0 space-y-2">
        <SectionHeader label={t.detailLinks} meta={data.links.length || undefined} rule={false} />
        {data.links.length === 0 ? (
          <p className="text-micro text-ink-3">{t.detailNoLinks}</p>
        ) : (
          <ul className="flex min-w-0 flex-col gap-1.5">
            {data.links.map((link, index) => (
              <li key={`${link.label}:${link.url}:${index}`} className="min-w-0">
                <a
                  href={link.url}
                  target={link.url.startsWith("/") ? undefined : "_blank"}
                  rel={link.url.startsWith("/") ? undefined : "noreferrer"}
                  className="inline-flex max-w-full items-center gap-1.5 truncate text-sec text-bronze underline decoration-bronze/40 underline-offset-2 hover:text-bronze-hi"
                >
                  <ExternalLink className="h-3.5 w-3.5 shrink-0" aria-hidden />
                  <span className="truncate">{link.label}</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="min-w-0 space-y-2">
        <SectionHeader label={t.detailCommits} meta={data.recent_commits.length || undefined} />
        {data.recent_commits.length === 0 ? (
          <p className="text-micro text-ink-3">{t.detailNoCommits}</p>
        ) : (
          <ul className="min-w-0 space-y-2">
            {data.recent_commits.map((commit) => (
              <li
                key={`${commit.hash}:${commit.committed_at}`}
                className="min-w-0 rounded-card border border-line-soft bg-surface-2 px-3 py-2"
              >
                <p className="truncate text-sec text-ink">{commit.message || t.noCommitMessage}</p>
                <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 font-data text-micro text-ink-3">
                  <span>{commit.hash}</span>
                  <span aria-hidden>·</span>
                  <span>{fmtRelativeTime(commit.committed_at, now)}</span>
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="min-w-0 space-y-2">
        <SectionHeader
          label={t.detailKanban}
          meta={data.kanban_tasks == null ? undefined : data.kanban_tasks.length}
        />
        {data.kanban_tasks == null ? (
          <p className="text-micro text-ink-3">{t.detailNoKanban}</p>
        ) : data.kanban_tasks.length === 0 ? (
          <p className="text-micro text-ink-3">{t.detailKanbanEmpty}</p>
        ) : (
          <ul className="min-w-0 space-y-2">
            {data.kanban_tasks.map((task) => {
              const tone = kanbanTaskTone(task.status, task.block_kind);
              return (
                <li
                  key={task.id}
                  className={cn(
                    "min-w-0 rounded-card border px-3 py-2",
                    tone === "alert"
                      ? "border-status-alert/30 bg-status-alert/10"
                      : tone === "warn"
                        ? "border-status-warn/30 bg-status-warn/10"
                        : "border-line-soft bg-surface-2",
                  )}
                >
                  <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
                    <p className="min-w-0 flex-1 break-words text-sec font-semibold text-ink">{task.title}</p>
                    <SignalChip tone={tone} label={task.status} className="shrink-0" />
                  </div>
                  <p className="mt-1 flex flex-wrap items-center gap-x-1.5 font-data text-micro text-ink-3">
                    <span>{task.id}</span>
                    {task.block_kind ? (
                      <>
                        <span aria-hidden>·</span>
                        <span>{task.block_kind}</span>
                      </>
                    ) : null}
                    <span aria-hidden>·</span>
                    <span>{fmtRelativeTime(task.created_at, now)}</span>
                  </p>
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="min-w-0 space-y-2">
        <SectionHeader label={t.detailLoops} meta={data.loops.length || undefined} />
        {data.loops.length === 0 ? (
          <p className="text-micro text-ink-3">{t.detailNoLoops}</p>
        ) : (
          <ul className="min-w-0 space-y-2">
            {data.loops.map((pack, index) => {
              const outcome = pack.last_outcome;
              const tone = loopOutcomeTone(outcome?.verdict);
              return (
                <li
                  key={`${pack.name}:${index}`}
                  className="min-w-0 rounded-card border border-line-soft bg-surface-2 px-3 py-2"
                >
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Led kind={pack.running ? "live" : "idle"} size={7} />
                    <span className="min-w-0 truncate text-sec font-semibold text-ink">{pack.name}</span>
                    <span className="text-micro text-ink-3">
                      {pack.running ? t.detailLoopRunning : t.detailLoopIdle}
                    </span>
                  </div>
                  {outcome ? (
                    <div className="mt-1.5 min-w-0 space-y-0.5">
                      <SignalChip tone={tone} label={outcome.verdict} />
                      {(outcome.reason || outcome.plan) ? (
                        <p className="truncate text-micro text-ink-3" title={[outcome.reason, outcome.plan].filter(Boolean).join(" · ")}>
                          {[outcome.reason, outcome.plan].filter(Boolean).join(" · ")}
                        </p>
                      ) : null}
                      {outcome.ts != null && Number.isFinite(outcome.ts) ? (
                        <p className="font-data text-micro text-ink-3">{fmtRelativeTime(outcome.ts, now)}</p>
                      ) : null}
                    </div>
                  ) : (
                    <p className="mt-1 text-micro text-ink-3">{t.detailNoOutcome}</p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      <section className="min-w-0 space-y-2">
        <SectionHeader label={t.detailAgents} meta={data.agents.length || undefined} />
        {data.agents.length === 0 ? (
          <p className="text-micro text-ink-3">{t.detailNoAgents}</p>
        ) : (
          <ul className="min-w-0 space-y-2">
            {data.agents.map((agent, index) => {
              const style = AGENT_KIND_STYLES[agent.kind] ?? AGENT_KIND_STYLES.unknown;
              const Icon = style.icon;
              const text = agentChipText(agent);
              return (
                <li
                  key={`${agent.kind}:${agent.label}:${index}`}
                  className="flex min-w-0 items-start gap-2 rounded-card border border-line-soft bg-surface-2 px-3 py-2"
                >
                  <Icon className={cn("mt-0.5 h-4 w-4 shrink-0", style.tone)} aria-hidden />
                  <div className="min-w-0 flex-1">
                    <p className={cn("truncate text-sec font-semibold", style.tone)}>{text}</p>
                    {agent.task ? <p className="mt-0.5 truncate text-micro text-ink-2">{agent.task}</p> : null}
                    <p className="mt-0.5 flex flex-wrap items-center gap-x-1.5 font-data text-micro text-ink-3">
                      <span className="truncate">{agent.label}</span>
                      <span aria-hidden>·</span>
                      <span>{agent.source}</span>
                      {agent.since != null && Number.isFinite(agent.since) ? (
                        <>
                          <span aria-hidden>·</span>
                          <span>{fmtRelativeTime(agent.since, now)}</span>
                        </>
                      ) : null}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
