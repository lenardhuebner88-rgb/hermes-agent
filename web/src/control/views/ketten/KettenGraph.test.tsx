import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { KettenGraph } from "./KettenGraph";
import type { ChainGraphEdge, ChainGraphNode } from "../../lib/types";

function makeNode(
  id: string,
  status: ChainGraphNode["status"] = "todo",
  extra: Partial<ChainGraphNode> = {},
): ChainGraphNode {
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
    progress: null,
    latest_run: null,
    ...extra,
  };
}

const nodes: ChainGraphNode[] = [
  makeNode("a", "ready"),
  makeNode("b", "running", { parents: ["a"], runtime_seconds: 10 }),
  makeNode("c", "todo", { parents: ["a"] }),
];
const edges: ChainGraphEdge[] = [
  { from: "a", to: "b" },
  { from: "a", to: "c" },
];

describe("KettenGraph", () => {
  it("renders all nodes in a vertical flex-col stack (no CSS grid)", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={nodes} edges={edges} rootId="a" />);
    expect(html).toContain("A");
    expect(html).toContain("B");
    expect(html).toContain("C");
    expect(html).toContain("flex-col");
    // No CSS-grid column template like the old layout.
    expect(html).not.toMatch(/grid-template-columns|gridTemplateColumns|repeat\(/);
  });

  it("renders a progress bar with width and subtask counter", () => {
    const withProgress = [makeNode("p", "running", { progress: { done: 2, total: 4 } })];
    const html = renderToStaticMarkup(<KettenGraph nodes={withProgress} edges={[]} rootId="x" />);
    expect(html).toContain("width:50%");
    expect(html).toContain("50% — 2 von 4 Subtasks");
    expect(html).toContain('aria-label="Fortschritt"');
  });

  it("draws colored node dots: cyan=running, indigo=root, grey=open", () => {
    const html = renderToStaticMarkup(
      <KettenGraph
        nodes={[makeNode("run", "running"), makeNode("open", "todo"), makeNode("root", "todo")]}
        edges={[]}
        rootId="root"
             />,
    );
    expect(html).toContain('data-node-dot="running"');
    expect(html).toContain('data-node-dot="open"');
    expect(html).toContain('data-node-dot="root"');
    expect(html).toContain("bg-cyan-400");
    expect(html).toContain("bg-indigo-500");
  });

  it("renders a vertical pipeline line with a cyan→grey gradient", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={nodes} edges={edges} rootId="a" />);
    expect(html).toContain("data-pipeline-line");
    expect(html).toContain("linear-gradient");
    expect(html).toContain("#22d3ee");
  });

  it("contains no SVG/path elements (bézier edges removed)", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={nodes} edges={edges} rootId="a" />);
    expect(html).not.toContain("<svg");
    expect(html).not.toContain("<path");
  });

  it("shows 'waiting on predecessor' for an open node with deps and no progress", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={nodes} edges={edges} rootId="a" />);
    expect(html).toContain("Wartet auf Vorgänger");
  });

  it("shows empty-state when nodes are empty", () => {
    const html = renderToStaticMarkup(<KettenGraph nodes={[]} edges={[]} rootId="a" />);
    expect(html).toContain("Keine Knoten");
    expect(html).toContain("Die Kette enthält keine verarbeitbaren Tasks.");
  });
});
