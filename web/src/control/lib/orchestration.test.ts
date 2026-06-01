import { describe, expect, it } from "vitest";

import { depState, readiness } from "./orchestration";

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
