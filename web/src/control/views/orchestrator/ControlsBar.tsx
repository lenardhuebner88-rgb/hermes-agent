import { Columns3, List } from "lucide-react";

import { cn } from "@/lib/utils";
import { Card } from "../../components/primitives";
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
      <input
        type="search"
        value={q}
        onChange={(event) => onQ(event.target.value)}
        placeholder={de.orchestrator.searchPlaceholder}
        className="w-full rounded-md border border-white/10 bg-white/[.04] px-3 py-2 text-sm text-white placeholder:text-zinc-500 focus:border-cyan-400/50 focus:outline-none focus:ring-2 focus:ring-cyan-400/30"
      />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {(["", "high", "medium", "low"] as const).map((priority) => (
          <button
            key={priority || "all-prio"}
            type="button"
            onClick={() => onFilterPriority(priority)}
            className={cn(
              "rounded-md border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
              filterPriority === priority
                ? "border-cyan-400/50 bg-cyan-500/15 text-cyan-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {priority || de.orchestrator.filterAll}
          </button>
        ))}

        <button
          type="button"
          onClick={() => onFilterPlanGate(filterPlanGate === "true" ? "" : "true")}
          className={cn(
            "rounded-md border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
            filterPlanGate === "true"
              ? "border-indigo-400/50 bg-indigo-400/15 text-indigo-200"
              : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
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
              "rounded-md border px-2.5 py-1 text-xs font-medium transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
              filterReadiness === readiness
                ? readiness === "ready"
                  ? "border-emerald-500/50 bg-emerald-500/15 text-emerald-200"
                  : "border-red-500/50 bg-red-500/15 text-red-200"
                : "border-white/10 text-zinc-400 hover:border-white/20 hover:text-zinc-200",
            )}
          >
            {readiness === "ready" ? de.orchestrator.filterReady : de.orchestrator.filterBlocked}
          </button>
        ))}

        {projects.length > 1 ? (
          <select
            value={filterProject}
            onChange={(event) => onFilterProject(event.target.value)}
            className="rounded-md border border-white/10 bg-white/[.04] px-2.5 py-1 text-xs text-zinc-200 focus:outline-none focus:ring-2 focus:ring-cyan-400/50"
          >
            <option value="">{de.orchestrator.filterAll} Projekt</option>
            {projects.map((project) => <option key={project} value={project}>{project}</option>)}
          </select>
        ) : null}

        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          <span className="text-xs hc-dim">{de.orchestrator.sortLabel}:</span>
          {(["priority", "age", "readiness"] as SortKey[]).map((key) => (
            <button
              key={key}
              type="button"
              onClick={() => onSort(key)}
              className={cn(
                "rounded-md border px-2 py-1 text-xs transition focus:outline-none focus:ring-2 focus:ring-cyan-400/50",
                sortKey === key
                  ? "border-cyan-400/40 bg-cyan-500/10 text-cyan-200"
                  : "border-white/10 text-zinc-400 hover:text-zinc-200",
              )}
            >
              {key === "priority" ? de.orchestrator.sortPriority : key === "age" ? de.orchestrator.sortAge : de.orchestrator.sortReadiness}
            </button>
          ))}
          <div className="ml-1 inline-flex rounded-md border border-white/10 bg-black/20 p-0.5" aria-label="Ansicht">
            <button
              type="button"
              onClick={() => onViewMode("queue")}
              className={cn("grid h-8 w-8 place-items-center rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-cyan-400/50", viewMode === "queue" ? "bg-cyan-500/15 text-cyan-200" : "hc-soft hover:bg-white/5")}
              title={de.orchestrator.queueView}
            >
              <List className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={() => onViewMode("board")}
              className={cn("grid h-8 w-8 place-items-center rounded-md text-xs focus:outline-none focus:ring-2 focus:ring-cyan-400/50", viewMode === "board" ? "bg-cyan-500/15 text-cyan-200" : "hc-soft hover:bg-white/5")}
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
