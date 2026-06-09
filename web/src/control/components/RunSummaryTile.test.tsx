import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RunSummaryTile } from "./RunSummaryTile";
import type { RunSummaryResponse } from "../lib/schemas";

const summary: RunSummaryResponse = {
  since_hours: 24,
  now: 1780000600,
  completed_roots: 1,
  total_cost_usd: 0.42,
  cycle_time_p50_seconds: 1800,
  cycle_time_p90_seconds: 3600,
  roots: [
    {
      id: "t_root",
      title: "Ship the feature",
      status: "done",
      assignee: "orchestrator",
      completed_at: 1780000000,
      cost_usd: 0.42,
      cycle_time_seconds: 1800,
      subtask_count: 3,
    },
  ],
};

describe("RunSummaryTile", () => {
  it("renders throughput, cost, cycle-time and the recent root", () => {
    const html = renderToStaticMarkup(<RunSummaryTile data={summary} now={1780000600} />);
    expect(html).toContain("Ship the feature");
    expect(html).toContain("t_root");
    expect(html).toContain("$0.42");
    expect(html).toContain("3 Teilaufgaben");
  });

  it("renders a quiet empty state on a 404 (data null) without crashing", () => {
    const html = renderToStaticMarkup(<RunSummaryTile data={null} now={1780000600} />);
    expect(html).toContain("Noch keine abgeschlossenen Aufträge");
    // The KPI pods still render (with em-dash placeholders), no throw.
    expect(html).toContain("Abgeschlossen");
  });

  it("surfaces a calm error callout above the panel", () => {
    const html = renderToStaticMarkup(
      <RunSummaryTile data={null} now={1780000600} error="boom" />,
    );
    expect(html).toContain("boom");
    expect(html).toContain("ruhiger Leerzustand");
  });
});
