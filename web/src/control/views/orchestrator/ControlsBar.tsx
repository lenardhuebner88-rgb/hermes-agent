import { Columns3, List } from "lucide-react";

import { cn } from "@/lib/utils";
import { Card, Eyebrow } from "../../components/primitives";
import { de } from "../../i18n/de";
import type { SortKey } from "../../lib/orchestration";
import type { ViewMode } from "./shared";

export function ControlsBar({
  q,
  onQ,
  filterPriority,
  onFilterPriority,
  filterProject,
  onFilterProject,
  filterPlanGate,
  onFilterPlanGate,
  filterReadiness,
  onFilterReadiness,
  sortKey,
  onSort,
  projects,
  viewMode,
  onViewMode,
}: {
  q: string;
  onQ: (v: string) => void;
  filterPriority: string;
  onFilterPriority: (v: string) => void;
  filterProject: string;
  onFilterProject: (v: string) => void;
  filterPlanGate: string;
  onFilterPlanGate: (v: string) => void;
  filterReadiness: string;
  onFilterReadiness: (v: string) => void;
  sortKey: SortKey;
  onSort: (v: SortKey) => void;
  projects: string[];
  viewMode: ViewMode;
  onViewMode: (v: ViewMode) => void;
}) {
  return (
    <Card surface="card" className="p-3">
      <label htmlFor="orch-backlog-search" className="mb-2 block">
        <Eyebrow>Backlog durchsuchen</Eyebrow>
      </label>
      <input
        id="orch-backlog-search"
        type="search"
        value={q}
        onChange={(event) => onQ(event.target.value)}
        placeholder={de.orchestrator.searchPlaceholder}
        className="min-h-12 w-full rounded-card border border-line bg-surface-2 px-3 text-sec text-ink placeholder:text-ink-3 focus:border-live focus:outline-none focus:ring-2 focus:ring-live/30"
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {(["", "high", "medium", "low"] as const).map((priority) => (
          <button
            key={priority || "all-prio"}
            type="button"
            onClick={() => onFilterPriority(priority)}
            className={cn(
              "inline-flex min-h-12 items-center rounded-card border px-3 text-sec font-medium transition focus:outline-none focus:ring-2 focus:ring-live/50",
              filterPriority === priority
                ? "border-live bg-live/10 text-bronze-hi"
                : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink",
            )}
          >
            {priority || de.orchestrator.filterAll}
          </button>
        ))}

        <button
          type="button"
          onClick={() => onFilterPlanGate(filterPlanGate === "true" ? "" : "true")}
          className={cn(
            "inline-flex min-h-12 items-center rounded-card border px-3 text-sec font-medium transition focus:outline-none focus:ring-2 focus:ring-live/50",
            filterPlanGate === "true"
              ? "border-live bg-live/10 text-bronze-hi"
              : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink",
          )}
        >
          {de.orchestrator.filterPlanGate}
        </button>

        {(["ready", "blocked"] as const).map((readiness) => (
          <button
            key={readiness}
            type="button"
            onClick={() => onFilterReadiness(filterReadiness === readiness ? "" : readiness)}
            className={cn(
              "inline-flex min-h-12 items-center rounded-card border px-3 text-sec font-medium transition focus:outline-none focus:ring-2 focus:ring-live/50",
              filterReadiness === readiness
                ? "border-live bg-live/10 text-bronze-hi"
                : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink",
            )}
          >
            {readiness === "ready" ? de.orchestrator.filterReady : de.orchestrator.filterBlocked}
          </button>
        ))}

        {projects.length > 1 ? (
          <select
            value={filterProject}
            onChange={(event) => onFilterProject(event.target.value)}
            aria-label="Projekt filtern"
            className="min-h-12 rounded-card border border-line bg-surface-2 px-3 text-sec text-ink-2 focus:outline-none focus:ring-2 focus:ring-live/50"
          >
            <option value="">{de.orchestrator.filterAll} Projekt</option>
            {projects.map((project) => <option key={project} value={project}>{project}</option>)}
          </select>
        ) : null}

        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          <Eyebrow>{de.orchestrator.sortLabel}:</Eyebrow>
          {(["priority", "age", "readiness"] as SortKey[]).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onSort(key)}
              className={cn(
                "inline-flex min-h-12 items-center rounded-card border px-3 text-sec transition focus:outline-none focus:ring-2 focus:ring-live/50",
                sortKey === key
                  ? "border-live bg-live/10 text-bronze-hi"
                  : "border-line text-ink-2 hover:bg-surface-3 hover:text-ink",
              )}
            >
              {key === "priority" ? de.orchestrator.sortPriority : key === "age" ? de.orchestrator.sortAge : de.orchestrator.sortReadiness}
            </button>
          ))}
          <div className="ml-1 inline-flex rounded-card border border-line bg-surface-1 p-0.5" aria-label="Ansicht">
            <button
              type="button"
              onClick={() => onViewMode("queue")}
              aria-label={de.orchestrator.queueView}
              aria-pressed={viewMode === "queue"}
              className={cn("grid size-12 place-items-center rounded-card focus:outline-none focus:ring-2 focus:ring-live/50", viewMode === "queue" ? "bg-live/10 text-bronze-hi" : "text-ink-2 hover:bg-surface-3 hover:text-ink")}
              title={de.orchestrator.queueView}
            >
              <List className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => onViewMode("board")}
              aria-label={de.orchestrator.boardView}
              aria-pressed={viewMode === "board"}
              className={cn("grid size-12 place-items-center rounded-card focus:outline-none focus:ring-2 focus:ring-live/50", viewMode === "board" ? "bg-live/10 text-bronze-hi" : "text-ink-2 hover:bg-surface-3 hover:text-ink")}
              title={de.orchestrator.boardView}
            >
              <Columns3 className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      </div>
    </Card>
  );
}
