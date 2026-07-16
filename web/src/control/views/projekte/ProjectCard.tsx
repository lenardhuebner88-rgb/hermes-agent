import { AlertTriangle, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Card } from "../../components/primitives";
import { Led } from "../../components/atoms";
import { SignalLabel } from "../../components/leitstand";
import { fmtRelativeTime } from "../../lib/derive";
import type { ProjectEntry } from "../../lib/schemas";
import { de } from "../../i18n/de";

const t = de.projekte;

export interface ProjectCardProps {
  project: ProjectEntry;
  /** Aus der Agents-Liste vorab aggregiert (derive.ts) — 0 ist ein gültiger,
   *  ruhiger Zustand, kein Fehler. */
  agentCount: number;
  /** Anzeigename des Elternprojekts, falls `project.parent` gesetzt ist. */
  parentName: string | null;
  now: number;
}

/** Eine Karte pro Projekt — die Grundeinheit des Projekte-Tabs (Stufe 4).
 *  Bewusst ohne Klick-/Drilldown-Verhalten (kommt in Stufe 6); die Struktur
 *  hält sich an die Leitstand-Bausteine, damit spätere Stufen (Agents-Rail,
 *  Attention-Sortierung) hier andocken können, ohne die Karte neu zu bauen. */
export function ProjectCard({ project, agentCount, parentName, now }: ProjectCardProps) {
  const commit = project.last_commit;
  const kanban = project.kanban;
  const loopsActive = project.loops?.active ?? 0;
  const hasErrors = project.errors.length > 0;

  return (
    <Card surface="card" className="flex h-full flex-col gap-3 p-4" ariaLabel={project.name}>
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
        <span className={cn("inline-flex items-center gap-1.5 text-micro", agentCount > 0 ? "text-ink" : "text-ink-3")}>
          <Led kind={agentCount > 0 ? "live" : "idle"} size={7} />
          {t.agentsCount(agentCount)}
        </span>
        {loopsActive > 0 ? (
          <span className="inline-flex items-center gap-1.5 text-micro text-ink-2">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            {t.loopsActive(loopsActive)}
          </span>
        ) : null}
      </footer>
    </Card>
  );
}
