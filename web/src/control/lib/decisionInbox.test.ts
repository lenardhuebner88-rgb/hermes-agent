import { describe, it, expect } from "vitest";
import { buildDecisionInbox, inboxSummary } from "./decisionInbox";
import type { Proposal } from "./types";
import type { BacklogItem } from "./schemas";
import type { AgentOpsIntervention } from "./agentOps";

const NOW = Math.floor(Date.parse("2026-06-05T00:00:00Z") / 1000);

function proposal(over: Partial<Proposal> & { id: string }): Proposal {
  return {
    target: "docs/x.md",
    section: null,
    rationale_plain: "because",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...over,
  };
}

function foItem(over: Partial<BacklogItem> & { id: string }): BacklogItem {
  return {
    title: `Task ${over.id}`,
    status: "later",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: undefined,
    source_path: `backlog/items/${over.id}-task.md`,
    ...over,
  };
}

function intervention(over: Partial<AgentOpsIntervention> & { id: string }): AgentOpsIntervention {
  return { tone: "amber", title: `IV ${over.id}`, detail: "needs you", target: "/control/orchestrator", ...over };
}

describe("buildDecisionInbox", () => {
  it("aggregates all three surfaces into one list", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "p1", severity: "high" })],
      foItems: [foItem({ id: "0001", status: "blocked" })],
      foNowSec: NOW,
      interventions: [intervention({ id: "iv1" })],
    });
    expect(items.map((i) => i.surface).sort()).toEqual(["autoresearch", "family", "orchestrator"]);
  });

  it("ranks higher-severity / blocked items to the top", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "low", severity: "low" })],
      foItems: [foItem({ id: "0001", status: "blocked" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items[0].surface).toBe("family"); // blocked (90) outranks a low proposal (40)
    expect(items[0].weight).toBeGreaterThan(items[1].weight);
  });

  it("skips non-actionable proposals and later/done FO items", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "applied", status: "applied", severity: "critical" })],
      foItems: [foItem({ id: "0001", status: "later" }), foItem({ id: "0002", status: "done" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items).toHaveLength(0);
  });

  it("treats an unowned active FO item as a decision even when status is later", () => {
    const items = buildDecisionInbox({
      proposals: [],
      foItems: [foItem({ id: "0001", status: "later", owner: "unassigned" })],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items).toHaveLength(1);
    expect(items[0].surface).toBe("family");
  });

  it("carries the navigation target per surface", () => {
    const items = buildDecisionInbox({
      proposals: [proposal({ id: "p1", severity: "high" })],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
    });
    expect(items[0].target).toBe("/control/autoresearch");
  });

  it("is deterministic for equal weights (stable key tiebreak)", () => {
    const input = {
      proposals: [proposal({ id: "b", severity: "high" }), proposal({ id: "a", severity: "high" })],
      foItems: [],
      foNowSec: NOW,
      interventions: [],
    };
    const first = buildDecisionInbox(input).map((i) => i.key);
    const second = buildDecisionInbox(input).map((i) => i.key);
    expect(first).toEqual(second);
    expect(first).toEqual(["ar:a", "ar:b"]);
  });
});

describe("inboxSummary", () => {
  it("counts per surface", () => {
    const summary = inboxSummary(
      buildDecisionInbox({
        proposals: [proposal({ id: "p1", severity: "high" }), proposal({ id: "p2", severity: "low" })],
        foItems: [foItem({ id: "0001", status: "blocked" })],
        foNowSec: NOW,
        interventions: [intervention({ id: "iv1" })],
      }),
    );
    expect(summary).toEqual({ total: 4, autoresearch: 2, family: 1, orchestrator: 1 });
  });
});
