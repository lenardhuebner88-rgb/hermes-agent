import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ChainSelector } from "./ChainSelector";
import type { ChainModel } from "../../lib/fleet";
import type { BoardTask } from "../../lib/types";

const chains: ChainModel<BoardTask>[] = [
  {
    rootId: "root_a",
    root: {
      id: "root_a",
      title: "Ship feature",
      status: "running",
      assignee: "coder",
      priority: 0,
      created_at: 0,
      started_at: null,
      completed_at: null,
      branch_name: null,
      latest_summary: null,
      link_counts: { parents: 0, children: 0 },
      comment_count: 0,
      progress: null,
      age: null,
      tenant: null,
      root_id: "root_a",
      epic_id: null,
    } as BoardTask,
    members: [],
    total: 3,
    doneCount: 0,
    stageCounts: { capture: 0, plan: 0, execute: 1, verify: 0, ship: 0 },
    blockedCount: 0,
    runningCount: 1,
    reviewCount: 0,
    isDone: false,
    latestCompletedAt: null,
    tenant: null,
    epicId: null,
  },
  {
    rootId: "root_b",
    root: null,
    members: [],
    total: 2,
    doneCount: 0,
    stageCounts: { capture: 0, plan: 0, execute: 0, verify: 0, ship: 0 },
    blockedCount: 1,
    runningCount: 0,
    reviewCount: 0,
    isDone: false,
    latestCompletedAt: null,
    tenant: null,
    epicId: null,
  },
];

describe("ChainSelector", () => {
  it("renders options for each active chain", () => {
    const html = renderToStaticMarkup(
      <ChainSelector chains={chains} selectedRootId="root_a" onSelect={vi.fn()} />,
    );
    expect(html).toContain("Ship feature");
    expect(html).toContain("root_b");
    expect(html).toContain("läuft");
    expect(html).toContain("blockiert");
  });

  it("shows empty state when no chains", () => {
    const html = renderToStaticMarkup(
      <ChainSelector chains={[]} selectedRootId={null} onSelect={vi.fn()} />,
    );
    expect(html).toContain("keine aktiven Ketten");
  });
});
