import { describe, it, expect } from "vitest";
import {
  computeNextFoTaskId,
  buildFoCommissionPrompt,
  filterFoItems,
  foHealthStripCounts,
  nextActionForFoItem,
  ownerLoadSummary,
  qualityFlagsForFoItem,
  queueStateForFoItem,
  rankFoItems,
  sortFoItems,
  staleSignalForFoItem,
} from "./foBacklog";
import type { BacklogItem, BacklogDetail } from "./schemas";

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
    ...overrides,
  };
}

function detail(overrides: Partial<BacklogDetail> & { id: string }): BacklogDetail {
  return {
    title: `Task ${overrides.id}`,
    status: "now",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    body: "",
    decision: [],
    acceptance_criteria: [],
    proofs: [],
    blockers: [],
    next_action: "",
    source_path: "",
    source_ref: "",
    links: [],
    ...overrides,
  };
}

// --- computeNextFoTaskId ---

describe("computeNextFoTaskId", () => {
  it("returns null for empty list", () => {
    expect(computeNextFoTaskId([])).toBeNull();
  });

  it("picks the only now item", () => {
    const items = [item({ id: "0001", status: "now", updated: "2026-06-01" })];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("prefers now over next", () => {
    const items = [
      item({ id: "0002", status: "next", updated: "2026-01-01" }),
      item({ id: "0001", status: "now", updated: "2026-06-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("among multiple now items picks oldest updated", () => {
    const items = [
      item({ id: "0002", status: "now", updated: "2026-06-02" }),
      item({ id: "0001", status: "now", updated: "2026-05-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("falls back to next when no now items", () => {
    const items = [
      item({ id: "0003", status: "later", updated: "2026-01-01" }),
      item({ id: "0002", status: "next", updated: "2026-06-01" }),
      item({ id: "0001", status: "next", updated: "2026-05-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0001");
  });

  it("skips done/later/in_progress for next-pick", () => {
    const items = [
      item({ id: "0001", status: "done" }),
      item({ id: "0002", status: "later" }),
      item({ id: "0003", status: "in_progress" }),
    ];
    expect(computeNextFoTaskId(items)).toBeNull();
  });

  it("prefers non-stale over stale within same status", () => {
    const items = [
      item({ id: "0001", status: "now", stale: true, updated: "2020-01-01" }),
      item({ id: "0002", status: "now", stale: false, updated: "2026-06-01" }),
    ];
    expect(computeNextFoTaskId(items)).toBe("0002");
  });

  it("uses queueState semantics and ignores unknown-status drift", () => {
    const items = [
      item({ id: "0001", status: "readyish" as BacklogItem["status"], updated: "2026-01-01" }),
      item({ id: "0002", status: "next", updated: "2026-06-01" }),
    ];
    expect(queueStateForFoItem(items[0]).state).toBe("drift");
    expect(computeNextFoTaskId(items)).toBe("0002");
  });
});

describe("FO queue intelligence", () => {
  it("classifies all canonical queue states without remapping unknown statuses", () => {
    expect(queueStateForFoItem(item({ id: "n", status: "now" })).state).toBe("now");
    expect(queueStateForFoItem(item({ id: "x", status: "readyish" as BacklogItem["status"] }))).toEqual({
      state: "drift",
      reason: "unknown_status",
    });
  });

  it("marks stale proof/update separately from fresh and missing update states", () => {
    expect(staleSignalForFoItem(item({ id: "old", status: "in_progress", updated: "2026-05-20" }), 1780524000).state).toBe("stale");
    expect(staleSignalForFoItem(item({ id: "none", status: "next", updated: "" }), 1780524000).state).toBe("missing_update");
    expect(staleSignalForFoItem(item({ id: "done", status: "done", updated: "2026-05-20" }), 1780524000).state).toBe("fresh");
  });

  it("summarizes active owner load with high-risk and stale pressure", () => {
    const summary = ownerLoadSummary([
      item({ id: "a", owner: "unassigned", risk: "high", status: "next" }),
      item({ id: "b", owner: "claude", risk: "medium", status: "in_progress", stale: true }),
      item({ id: "c", owner: "claude", risk: "low", status: "done" }),
    ]);
    expect(summary).toEqual([
      { owner: "claude", total: 1, highRisk: 0, stale: 1, unready: 0 },
      { owner: "unassigned", total: 1, highRisk: 1, stale: 0, unready: 1 },
    ]);
  });

  it("ranks risk, age, and business impact ahead of low-impact later work", () => {
    const ranked = rankFoItems([
      item({ id: "low", status: "later", risk: "low", area: "process", updated: "2026-06-01" }),
      item({ id: "high", status: "next", risk: "high", area: "db", updated: "2026-06-03" }),
      item({ id: "old", status: "next", risk: "medium", area: "lists", updated: "2026-05-01" }),
    ]);
    expect(ranked.map((it) => it.id)).toEqual(["high", "old", "low"]);
  });

  it("derives next action and task-quality flags without mutating backlog data", () => {
    const weak = item({
      id: "0009",
      title: "Fix",
      owner: "unassigned",
      status: "next",
      updated: "2026-05-01",
      stale: true,
    });
    const d = detail({
      id: "0009",
      title: "Fix",
      owner: "unassigned",
      status: "next",
      body: "## Kontext\n\n" + "- Punkt\n".repeat(12),
    });

    expect(nextActionForFoItem(weak, d)).toBe("Akzeptanzkriterien und konkreten nächsten Schritt klären.");
    expect(qualityFlagsForFoItem(weak, d).map((flag) => flag.kind)).toEqual([
      "weak_title",
      "missing_acceptance",
      "unclear_owner",
      "stale_update",
      "large_scope",
      "missing_next_action",
    ]);
  });

  it("computes health strip counts from items plus endpoint contract health", () => {
    const counts = foHealthStripCounts(
      [
        item({ id: "n", status: "now", risk: "low" }),
        item({ id: "x", status: "next", risk: "high", owner: "unassigned" }),
        item({ id: "b", status: "blocked", risk: "medium", stale: true }),
      ],
      {
        source_count: 3,
        counted_sum: 2,
        unknown_statuses: [{ status: "readyish", count: 1, ids: ["d"] }],
        invalid_risk_count: 1,
        invalid_owner_count: 0,
        unowned_count: 1,
        stale_count: 1,
        missing_acceptance_count: 2,
        missing_next_action_count: 1,
        invalid_area_count: 0,
      },
    );

    expect(counts).toEqual({
      now: 1,
      nextReady: 1,
      blocked: 1,
      unowned: 1,
      stale: 1,
      highRisk: 1,
      contractDrift: 2,
      missingAcceptance: 2,
    });
  });
});

// --- buildFoCommissionPrompt ---

describe("buildFoCommissionPrompt", () => {
  it("includes task title and id in the prompt", () => {
    const d = detail({ id: "0001", title: "Add shopping list feature" });
    const prompt = buildFoCommissionPrompt(d);
    expect(prompt).toContain("0001");
    expect(prompt).toContain("Add shopping list feature");
  });

  it("references the correct spec path", () => {
    const d = detail({ id: "0042", title: "X" });
    const prompt = buildFoCommissionPrompt(d);
    expect(prompt).toContain("~/projects/family-organizer/backlog/items/0042.md");
  });

  it("includes the gate command", () => {
    const d = detail({ id: "0001", title: "X" });
    expect(buildFoCommissionPrompt(d)).toContain("npm run gate:e2e");
  });

  it("is a non-empty string", () => {
    const d = detail({ id: "0001", title: "X" });
    expect(buildFoCommissionPrompt(d).length).toBeGreaterThan(50);
  });
});

// --- filterFoItems ---

describe("filterFoItems", () => {
  const items = [
    item({ id: "0001", title: "Groceries list", owner: "piet", risk: "low", area: "kitchen" }),
    item({ id: "0002", title: "Calendar sync", owner: "claude", risk: "medium", area: "planning" }),
    item({ id: "0003", title: "School schedule", owner: "piet", risk: "high", area: "education", stale: true }),
  ];

  it("returns all when no filter", () => {
    expect(filterFoItems(items, "", {})).toHaveLength(3);
  });

  it("filters by query against title", () => {
    expect(filterFoItems(items, "groceries", {})).toHaveLength(1);
    expect(filterFoItems(items, "groceries", {})[0].id).toBe("0001");
  });

  it("filters by query against owner", () => {
    expect(filterFoItems(items, "claude", {})).toHaveLength(1);
  });

  it("filters by query against area", () => {
    expect(filterFoItems(items, "planning", {})).toHaveLength(1);
  });

  it("filters by owner", () => {
    expect(filterFoItems(items, "", { owner: "piet" })).toHaveLength(2);
  });

  it("filters by risk", () => {
    expect(filterFoItems(items, "", { risk: "high" })).toHaveLength(1);
  });

  it("filters by stale=true", () => {
    expect(filterFoItems(items, "", { stale: true })).toHaveLength(1);
    expect(filterFoItems(items, "", { stale: true })[0].id).toBe("0003");
  });

  it("combines query and filter", () => {
    expect(filterFoItems(items, "schedule", { owner: "piet" })).toHaveLength(1);
    expect(filterFoItems(items, "schedule", { owner: "claude" })).toHaveLength(0);
  });
});

// --- sortFoItems ---

describe("sortFoItems", () => {
  const items = [
    item({ id: "0001", risk: "low", updated: "2026-06-01", status: "next" }),
    item({ id: "0002", risk: "high", updated: "2026-03-01", status: "now" }),
    item({ id: "0003", risk: "medium", updated: "2026-01-01", status: "later" }),
  ];

  it("sort by risk: high first", () => {
    const sorted = sortFoItems(items, "risk");
    expect(sorted[0].id).toBe("0002");
    expect(sorted[1].id).toBe("0003");
    expect(sorted[2].id).toBe("0001");
  });

  it("sort by age: oldest updated first", () => {
    const sorted = sortFoItems(items, "age");
    expect(sorted[0].id).toBe("0003");
    expect(sorted[2].id).toBe("0001");
  });

  it("sort by status: now < next < later", () => {
    const sorted = sortFoItems(items, "status");
    expect(sorted[0].id).toBe("0002"); // now
    expect(sorted[1].id).toBe("0001"); // next
    expect(sorted[2].id).toBe("0003"); // later
  });

  it("does not mutate original array", () => {
    const orig = [...items];
    sortFoItems(items, "risk");
    expect(items.map((it) => it.id)).toEqual(orig.map((it) => it.id));
  });
});
