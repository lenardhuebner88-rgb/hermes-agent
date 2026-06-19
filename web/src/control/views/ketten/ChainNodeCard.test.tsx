import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ChainNodeCard } from "./ChainNodeCard";
import type { ChainGraphNode } from "../../lib/types";

const node: ChainGraphNode = {
  id: "t_abc",
  title: "Build feature",
  status: "running",
  assignee: "coder",
  level: 0,
  parents: [],
  children: [],
  created_at: 0,
  started_at: null,
  completed_at: null,
  last_heartbeat_at: 1_700_000_000,
  runtime_seconds: 125,
  progress: null,
  latest_run: null,
  cost_usd: 0,
  input_tokens: 0,
  output_tokens: 0,
};

describe("ChainNodeCard", () => {
  it("renders title, task id, assignee and status pill", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    expect(html).toContain("Build feature");
    expect(html).toContain("t_abc");
    expect(html).toContain("coder");
    expect(html).toContain("Läuft");
  });

  it("marks the root node with focus badge", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot />);
    expect(html).toContain("Fokus");
  });

  it("shows runtime in the progress meta row", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    // 125s → "2m"
    expect(html).toContain("2m");
  });

  it("no longer renders heartbeat text on the card (liveness moved to the pipeline dot)", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} />);
    expect(html).not.toContain("Heartbeat");
    expect(html).not.toContain("jetzt");
  });

  it("renders a progress bar with percent and subtask counter", () => {
    const withProgress: ChainGraphNode = { ...node, progress: { done: 1, total: 2 } };
    const html = renderToStaticMarkup(<ChainNodeCard node={withProgress} isRoot={false} />);
    expect(html).toContain("width:50%");
    expect(html).toContain("50% — 1 von 2 Subtasks");
  });

  it("shows 'waiting on predecessor' for an open node with deps and no progress", () => {
    const waiting: ChainGraphNode = { ...node, status: "todo", parents: ["t_dep"], runtime_seconds: null };
    const html = renderToStaticMarkup(<ChainNodeCard node={waiting} isRoot={false} />);
    expect(html).toContain("Wartet auf Vorgänger");
  });

  it("prefers latest_run runtime_seconds over task-level runtime_seconds", () => {
    const withRun: ChainGraphNode = {
      ...node,
      runtime_seconds: 60,
      latest_run: {
        id: 1,
        profile: "coder",
        status: "running",
        outcome: null,
        started_at: 1_700_000_000,
        ended_at: null,
        last_heartbeat_at: 1_700_000_000,
        runtime_seconds: 200,
        heartbeat_age_seconds: 10,
      },
    };
    const html = renderToStaticMarkup(<ChainNodeCard node={withRun} isRoot={false} />);
    // 200s = 3m, not 60s = 1m
    expect(html).toContain("3m");
  });
});
