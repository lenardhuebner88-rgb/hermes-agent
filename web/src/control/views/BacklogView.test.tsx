import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FoBacklogQueueTable, FoHealthStrip } from "./BacklogView";
import type { BacklogItem, BacklogContractHealth } from "../lib/schemas";

function item(overrides: Partial<BacklogItem> & { id: string }): BacklogItem {
  return {
    title: `Task ${overrides.id}`,
    status: "next",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: undefined,
    source_path: `backlog/items/${overrides.id}-task.md`,
    ...overrides,
  };
}

const health: BacklogContractHealth = {
  source_count: 3,
  counted_sum: 2,
  unknown_statuses: [{ status: "readyish", count: 1, ids: ["0099"] }],
  invalid_risk_count: 0,
  invalid_owner_count: 0,
  unowned_count: 1,
  stale_count: 1,
  missing_acceptance_count: 2,
  missing_next_action_count: 1,
  invalid_area_count: 0,
};

describe("Family Organizer queue-first view pieces", () => {
  it("renders the contract-health strip with operator queue counts", () => {
    const html = renderToStaticMarkup(
      <FoHealthStrip
        items={[
          item({ id: "0001", status: "now" }),
          item({ id: "0002", status: "next", risk: "high", owner: "unassigned" }),
          item({ id: "0003", status: "blocked", stale: true }),
        ]}
        contractHealth={health}
      />,
    );

    expect(html).toContain("Now");
    expect(html).toContain("Next Ready");
    expect(html).toContain("Contract Drift");
    expect(html).toContain("Missing Acceptance");
  });

  it("renders a dense queue table with next action, source/id, and quality lens", () => {
    const html = renderToStaticMarkup(
      <FoBacklogQueueTable
        items={[
          item({ id: "0002", title: "Ready task", status: "next", risk: "high", area: "db" }),
          item({ id: "0003", title: "Fix", owner: "unassigned", stale: true, updated: "2026-05-01", missing_acceptance: true, missing_next_action: true }),
        ]}
        nowSec={1770000000}
        nextTaskId="0002"
        onOpen={() => undefined}
      />,
    );

    expect(html).toContain("<table");
    expect(html).toContain("Next Action");
    expect(html).toContain("Source/Id");
    expect(html).toContain("Ready task");
    expect(html).toContain("Akzeptanz");
    expect(html).toContain("backlog/items/0002-task.md");
  });
});
