import { describe, it, expect } from "vitest";
import {
  computeNextFoTaskId,
  buildFoCommissionPrompt,
  filterFoItems,
  sortFoItems,
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
