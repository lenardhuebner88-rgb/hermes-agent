import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ChainSelector } from "./ChainSelector";
import { buildChainOptionLabel } from "./chainSelectorUtils";
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

// B2: pure label-builder — no separator glue, separator is ` · `
describe("buildChainOptionLabel", () => {
  it("running only: title · N läuft · total Tasks", () => {
    const chain = chains[0]; // runningCount=1, blockedCount=0
    const label = buildChainOptionLabel(chain);
    expect(label).toBe("Ship feature · 1 läuft · 3 Tasks");
  });

  it("blocked only: title · N blockiert · total Tasks", () => {
    const chain = chains[1]; // runningCount=0, blockedCount=1, root=null → uses rootId
    const label = buildChainOptionLabel(chain);
    expect(label).toBe("root_b · 1 blockiert · 2 Tasks");
  });

  it("neither running nor blocked: title · status (deutsch) · total Tasks", () => {
    const scheduledChain: ChainModel<BoardTask> = {
      ...chains[0],
      rootId: "root_c",
      root: { ...chains[0].root!, id: "root_c", status: "scheduled" } as BoardTask,
      runningCount: 0,
      blockedCount: 0,
      total: 1,
    };
    const label = buildChainOptionLabel(scheduledChain);
    expect(label).toBe("Ship feature · Geplant · 1 Tasks");
  });

  it("done status shows Fertig", () => {
    const doneChain: ChainModel<BoardTask> = {
      ...chains[0],
      rootId: "root_d",
      root: { ...chains[0].root!, id: "root_d", status: "done" } as BoardTask,
      runningCount: 0,
      blockedCount: 0,
      total: 2,
    };
    const label = buildChainOptionLabel(doneChain);
    expect(label).toBe("Ship feature · Fertig · 2 Tasks");
  });
});

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

  // B3: meta line uses "fertig" not "done"
  it("meta line shows 'fertig' not 'done'", () => {
    const html = renderToStaticMarkup(
      <ChainSelector chains={chains} selectedRootId="root_a" onSelect={vi.fn()} />,
    );
    expect(html).not.toContain(" done");
    expect(html).toContain("fertig");
  });

  // B2: option text is concatenated with · separator (no concatenation glue)
  it("option labels are separated by ' · ' with no glue", () => {
    const html = renderToStaticMarkup(
      <ChainSelector chains={chains} selectedRootId="root_a" onSelect={vi.fn()} />,
    );
    // Must have separator; must NOT have "läuft1" or "blockiert1" (old glue bug)
    expect(html).not.toMatch(/läuft\d/);
    expect(html).not.toMatch(/blockiert\d/);
    expect(html).toContain(" · ");
  });
});
