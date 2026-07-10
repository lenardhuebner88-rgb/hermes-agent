import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { SortKey } from "../../lib/orchestration";
import { de } from "../../i18n/de";
import { ControlsBar } from "./ControlsBar";
import type { ViewMode } from "./shared";

const legacyClasses = [
  "hc-dim",
  "hc-soft",
  "cyan-",
  "emerald-",
  "sky-",
  "teal-",
  "zinc-",
  "slate-",
  "indigo-",
  "amber-",
  "red-",
  "rose-",
  "violet-",
  "white/",
  "black/",
];

function renderControls({
  filterPriority,
  filterPlanGate,
  filterReadiness,
  sortKey,
  projects,
  viewMode,
}: {
  filterPriority: string;
  filterPlanGate: string;
  filterReadiness: string;
  sortKey: SortKey;
  projects: string[];
  viewMode: ViewMode;
}) {
  return renderToStaticMarkup(
    <ControlsBar
      q=""
      onQ={vi.fn()}
      filterPriority={filterPriority}
      onFilterPriority={vi.fn()}
      filterProject=""
      onFilterProject={vi.fn()}
      filterPlanGate={filterPlanGate}
      onFilterPlanGate={vi.fn()}
      filterReadiness={filterReadiness}
      onFilterReadiness={vi.fn()}
      sortKey={sortKey}
      onSort={vi.fn()}
      projects={projects}
      viewMode={viewMode}
      onViewMode={vi.fn()}
    />,
  );
}

describe("ControlsBar sheet-A render", () => {
  it.each([
    {
      name: "unselected filters without project selector in queue view",
      props: { filterPriority: "", filterPlanGate: "", filterReadiness: "", sortKey: "priority" as const, projects: ["Hermes"], viewMode: "queue" as const },
    },
    {
      name: "selected plan/ready filters with project selector in board view",
      props: { filterPriority: "high", filterPlanGate: "true", filterReadiness: "ready", sortKey: "age" as const, projects: ["Hermes", "Vault"], viewMode: "board" as const },
    },
    {
      name: "selected blocked filter and final sort branch",
      props: { filterPriority: "low", filterPlanGate: "", filterReadiness: "blocked", sortKey: "readiness" as const, projects: ["Hermes", "Vault"], viewMode: "queue" as const },
    },
  ])("renders $name without legacy vocabulary", ({ props }) => {
    const html = renderControls(props);

    for (const legacy of legacyClasses) expect(html).not.toContain(legacy);
    expect(html).toContain("min-h-12");
    expect(html).toContain("size-12");
    expect(html).toContain('for="orch-backlog-search"');
    expect(html).toContain('id="orch-backlog-search"');
    expect(html).toContain("Backlog durchsuchen");
    expect(html).not.toContain(`aria-label="${de.orchestrator.searchPlaceholder}"`);
    expect(html).toContain('aria-label="Liste"');
    expect(html).toContain('aria-label="Board"');
  });
});
