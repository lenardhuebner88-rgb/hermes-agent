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
  latest_run: null,
};

describe("ChainNodeCard", () => {
  it("renders title, task id, assignee and status pill", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} now={1_700_000_010} />);
    expect(html).toContain("Build feature");
    expect(html).toContain("t_abc");
    expect(html).toContain("coder");
    expect(html).toContain("Läuft");
  });

  it("marks the root node with focus badge", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot now={1_700_000_010} />);
    expect(html).toContain("Fokus");
  });

  it("shows runtime and heartbeat age", () => {
    const html = renderToStaticMarkup(<ChainNodeCard node={node} isRoot={false} now={1_700_000_012} />);
    expect(html).toContain("2m");
    expect(html).toContain("Heartbeat");
    expect(html).toContain("12s");
  });

  it("shows 'now' for very fresh heartbeats", () => {
    const fresh: ChainGraphNode = { ...node, last_heartbeat_at: 1_700_000_000, runtime_seconds: 0 };
    const html = renderToStaticMarkup(<ChainNodeCard node={fresh} isRoot={false} now={1_700_000_001} />);
    expect(html).toContain("jetzt");
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
    const html = renderToStaticMarkup(<ChainNodeCard node={withRun} isRoot={false} now={1_700_000_010} />);
    // 200s = 3m, not 60s = 1m
    expect(html).toContain("3m");
  });
});
