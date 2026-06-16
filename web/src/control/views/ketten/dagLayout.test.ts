import { describe, expect, it } from "vitest";
import { computeLevels, statusDot, statusTone } from "./dagLayout";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

function makeNode(id: string, status: ChainGraphNode["status"] = "todo"): ChainGraphNode {
  return {
    id,
    title: id.toUpperCase(),
    status,
    assignee: null,
    level: 0,
    parents: [],
    children: [],
    created_at: 0,
    started_at: null,
    completed_at: null,
    last_heartbeat_at: null,
    runtime_seconds: null,
    latest_run: null,
  };
}

describe("dagLayout", () => {
  it("computes levels for a diamond DAG", () => {
    const nodes: ChainGraphNode[] = [
      makeNode("a", "ready"),
      makeNode("b", "running"),
      makeNode("c", "todo"),
      makeNode("d", "done"),
    ];
    const edges: ChainGraphEdge[] = [
      { from: "a", to: "b" },
      { from: "a", to: "c" },
      { from: "b", to: "d" },
      { from: "c", to: "d" },
    ];
    const levels = computeLevels(nodes, edges);
    expect(levels.map((l) => ({ level: l.level, ids: l.nodes.map((n) => n.id) }))).toEqual([
      { level: 0, ids: ["a"] },
      { level: 1, ids: ["b", "c"] },
      { level: 2, ids: ["d"] },
    ]);
  });

  it("handles disconnected nodes in one level each", () => {
    const nodes: ChainGraphNode[] = [makeNode("x", "ready"), makeNode("y", "ready")];
    const levels = computeLevels(nodes, []);
    expect(levels).toHaveLength(1);
    expect(levels[0].nodes.map((n) => n.id)).toEqual(["x", "y"]);
  });

  it("maps status to tone and dot", () => {
    expect(statusTone("done")).toBe("emerald");
    expect(statusTone("running")).toBe("cyan");
    expect(statusTone("review")).toBe("amber");
    expect(statusTone("blocked")).toBe("red");
    expect(statusTone("todo")).toBe("zinc");

    expect(statusDot("running")).toBe("live");
    expect(statusDot("blocked")).toBe("error");
    expect(statusDot("review")).toBe("warn");
    expect(statusDot("done")).toBe("ready");
    expect(statusDot("todo")).toBe("idle");
  });
});
