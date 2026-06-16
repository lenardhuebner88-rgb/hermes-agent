import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { KettenGraph } from "./KettenGraph";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

function makeNode(id: string, status: ChainGraphNode["status"] = "todo"): ChainGraphNode {
  return {
    id,
    title: id.toUpperCase(),
    status,
    assignee: "coder",
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

const nodes: ChainGraphNode[] = [
  makeNode("a", "ready"),
  { ...makeNode("b", "running"), last_heartbeat_at: 1_700_000_000, runtime_seconds: 10 },
  makeNode("c", "todo"),
];
const edges: ChainGraphEdge[] = [
  { from: "a", to: "b" },
  { from: "a", to: "c" },
];

describe("KettenGraph", () => {
  it("renders all nodes and an SVG edge layer", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={nodes} edges={edges} rootId="a" now={1_700_000_010} />);
    expect(html).toContain("A");
    expect(html).toContain("B");
    expect(html).toContain("C");
    expect(html).toContain("Fokus");
    expect(html).toContain("<svg");
  });

  it("renders nothing when nodes are empty", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={[]} edges={[]} rootId="a" now={1_700_000_010} />);
    expect(html).toBe("");
  });
});
