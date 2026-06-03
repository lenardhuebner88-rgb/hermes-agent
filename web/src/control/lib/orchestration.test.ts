import { describe, expect, it } from "vitest";

import {
  buildCommissionPrompt,
  computeNextTaskId,
  depState,
  filterItems,
  projectFromRoot,
  readiness,
  sortItems,
} from "./orchestration";

const board = [
  { id: "a", status: "done" },
  { id: "b", status: "doing" },
  { id: "c", status: "done" },
];

describe("depState", () => {
  it("classifies done / pending / missing", () => {
    expect(depState("a", board)).toBe("done");
    expect(depState("b", board)).toBe("pending");
    expect(depState("z", board)).toBe("missing");
  });
});

describe("readiness", () => {
  it("todo with all deps done -> ready", () => {
    expect(readiness({ status: "todo", dependsOn: ["a", "c"] }, board)).toEqual({
      state: "ready",
      blockedBy: [],
    });
  });

  it("todo with a pending dep -> blocked, lists that dep", () => {
    expect(readiness({ status: "todo", dependsOn: ["a", "b"] }, board)).toEqual({
      state: "blocked",
      blockedBy: ["b"],
    });
  });

  it("todo with a missing dep -> blocked", () => {
    expect(readiness({ status: "todo", dependsOn: ["z"] }, board)).toEqual({
      state: "blocked",
      blockedBy: ["z"],
    });
  });

  it("non-todo status -> neutral even with unfinished deps", () => {
    expect(readiness({ status: "doing", dependsOn: ["b"] }, board)).toEqual({
      state: "neutral",
      blockedBy: [],
    });
  });

  it("todo with no deps -> ready", () => {
    expect(readiness({ status: "todo", dependsOn: [] }, board)).toEqual({
      state: "ready",
      blockedBy: [],
    });
  });

  it("done with no deps -> neutral", () => {
    expect(readiness({ status: "done" }, board)).toEqual({ state: "neutral", blockedBy: [] });
  });
});

// ── computeNextTaskId ─────────────────────────────────────────────────────────

const mkItem = (id: string, status: string, priority: string, created: string, dependsOn: string[] = []) => ({
  id,
  status,
  priority,
  created,
  dependsOn,
  title: id,
  planGate: false,
});

describe("computeNextTaskId", () => {
  it("returns null when no todo items", () => {
    expect(computeNextTaskId([mkItem("a", "done", "high", "2026-01-01")])).toBeNull();
  });

  it("returns null when only todo item is blocked (pending dep)", () => {
    const items = [
      mkItem("a", "doing", "high", "2026-01-01"),
      mkItem("b", "todo", "high", "2026-01-02", ["a"]),
    ];
    expect(computeNextTaskId(items)).toBeNull();
  });

  it("picks todo with missing dep (missing ≠ blocking)", () => {
    const items = [mkItem("x", "todo", "medium", "2026-01-01", ["ghost"])];
    expect(computeNextTaskId(items)).toBe("x");
  });

  it("prefers high priority over medium", () => {
    const items = [
      mkItem("a", "todo", "medium", "2026-01-01"),
      mkItem("b", "todo", "high", "2026-01-02"),
    ];
    expect(computeNextTaskId(items)).toBe("b");
  });

  it("breaks priority tie by oldest created", () => {
    const items = [
      mkItem("newer", "todo", "high", "2026-02-01"),
      mkItem("older", "todo", "high", "2026-01-01"),
    ];
    expect(computeNextTaskId(items)).toBe("older");
  });

  it("skips blocked items and picks the next unblocked one", () => {
    const items = [
      mkItem("blocked", "todo", "high", "2026-01-01", ["pending-dep"]),
      mkItem("pending-dep", "doing", "low", "2025-12-01"),
      mkItem("free", "todo", "medium", "2026-01-02"),
    ];
    expect(computeNextTaskId(items)).toBe("free");
  });
});

// ── buildCommissionPrompt ─────────────────────────────────────────────────────

describe("buildCommissionPrompt", () => {
  it("interpolates all four fields", () => {
    const detail = { title: "Mein Task", id: "f-x", root: "/home/piet/project", gate: "npm test" };
    const prompt = buildCommissionPrompt(detail);
    expect(prompt).toContain("Mein Task");
    expect(prompt).toContain("f-x");
    expect(prompt).toContain("/home/piet/project");
    expect(prompt).toContain("npm test");
    expect(prompt).toContain("~/orchestration/backlog/f-x.md");
  });

  it("contains the ABBRUCH section", () => {
    const prompt = buildCommissionPrompt({ title: "T", id: "id", root: "r", gate: "g" });
    expect(prompt).toContain("ABBRUCH");
  });
});

// ── projectFromRoot ───────────────────────────────────────────────────────────

describe("projectFromRoot", () => {
  it("maps hermes-agent path to Dashboard", () => {
    expect(projectFromRoot("/home/piet/.hermes/hermes-agent")).toBe("Dashboard");
  });
  it("maps family-organizer path", () => {
    expect(projectFromRoot("/home/piet/projects/family-organizer")).toBe("Family Organizer");
  });
  it("maps orchestration path", () => {
    expect(projectFromRoot("/home/piet/orchestration")).toBe("Orchestration");
  });
  it("falls back to basename", () => {
    expect(projectFromRoot("/home/piet/my-custom-project")).toBe("my-custom-project");
  });
  it("returns Orchestration for empty/undefined root", () => {
    expect(projectFromRoot(undefined)).toBe("Orchestration");
    expect(projectFromRoot("")).toBe("Orchestration");
  });
});

// ── filterItems ───────────────────────────────────────────────────────────────

const items = [
  { id: "f-a", title: "Dashboard Fix", status: "todo", priority: "high", created: "2026-01-01", planGate: false, root: "/home/piet/.hermes/hermes-agent" },
  { id: "f-b", title: "FO Feature", status: "todo", priority: "medium", created: "2026-01-02", planGate: true, root: "/home/piet/projects/family-organizer" },
  { id: "f-c", title: "Orchestration Sync", status: "done", priority: "low", created: "2025-12-01", planGate: false, root: "/home/piet/orchestration" },
];

describe("filterItems", () => {
  it("filters by text query on title", () => {
    const result = filterItems(items, "fix");
    expect(result.map((i) => i.id)).toEqual(["f-a"]);
  });

  it("filters by text query on id", () => {
    const result = filterItems(items, "f-b");
    expect(result.map((i) => i.id)).toEqual(["f-b"]);
  });

  it("empty query returns all", () => {
    expect(filterItems(items, "")).toHaveLength(3);
  });

  it("filters by priority", () => {
    const result = filterItems(items, "", { priority: "high" });
    expect(result.map((i) => i.id)).toEqual(["f-a"]);
  });

  it("filters by planGate=true", () => {
    const result = filterItems(items, "", { planGate: "true" });
    expect(result.map((i) => i.id)).toEqual(["f-b"]);
  });

  it("filters by project", () => {
    const result = filterItems(items, "", { project: "Dashboard" });
    expect(result.map((i) => i.id)).toEqual(["f-a"]);
  });
});

// ── sortItems ────────────────────────────────────────────────────────────────

describe("sortItems", () => {
  it("sorts by priority high → medium → low", () => {
    const sorted = sortItems(items, "priority");
    expect(sorted.map((i) => i.priority)).toEqual(["high", "medium", "low"]);
  });

  it("sorts by age oldest first", () => {
    const sorted = sortItems(items, "age");
    expect(sorted[0].id).toBe("f-c");
  });
});
