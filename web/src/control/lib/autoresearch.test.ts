import { describe, expect, it } from "vitest";
import { clampLoopIterations, describeLoopStatus, getProposalPriorityGroup, rankAutoresearchProposals } from "./autoresearch";
import type { AutoresearchStatus, Proposal } from "./types";

const base: AutoresearchStatus = {
  state: "idle", pid: null, request_id: null, iteration: 0, max: 0,
  last_step: null, last_eval: null, route_status: "configured",
  heartbeat_age_s: null, heartbeat_fresh: false, last_receipt: null, last_run: null, note: null,
};

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id" | "target">): Proposal {
  return {
    section: null,
    title: null,
    rationale_plain: "",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...overrides,
  };
}

describe("autoresearch loop display", () => {
  it("clamps loop iterations to the bounded runner range", () => {
    expect(clampLoopIterations(0)).toBe(1);
    expect(clampLoopIterations(3)).toBe(3);
    expect(clampLoopIterations(99)).toBe(5);
  });

  it("does not show 0/0 when idle", () => {
    const view = describeLoopStatus(base);
    expect(view.iterationLabel).toBe("kein Lauf aktiv");
    expect(view.progressPercent).toBe(0);
  });

  it("shows live progress and heartbeat freshness while running", () => {
    const view = describeLoopStatus({ ...base, state: "running", iteration: 2, max: 5, heartbeat_age_s: 7, heartbeat_fresh: true, last_step: "eval" });
    expect(view.iterationLabel).toBe("2 / 5");
    expect(view.progressPercent).toBe(40);
    expect(view.heartbeatLabel).toBe("7s frisch");
    expect(view.stepLabel).toBe("eval");
  });

  it("marks unconfirmed model routes as amber", () => {
    const view = describeLoopStatus({ ...base, route_status: "unavailable" });
    expect(view.routeTone).toBe("amber");
    expect(view.routeHint).toBe("Modell-Route nicht bestätigt");
  });
});

describe("autoresearch proposal relevance queue", () => {
  it("classifies open proposals into operator-friendly priority groups", () => {
    expect(getProposalPriorityGroup(proposal({ id: "p1", target: "blogwatcher", section: "Safety" })).label).toBe("Safety-Lücke");
    expect(getProposalPriorityGroup(proposal({ id: "p2", target: "openhue", section: "Output" })).label).toBe("Quick Win");
    expect(getProposalPriorityGroup(proposal({ id: "p3", target: "gateway", mode: "code", title: "Fix crash" })).label).toBe("Code-Gate");
  });

  it("ranks Top-N proposals by value and leaves lower-priority proposals visible only behind the shortlist", () => {
    const ranked = rankAutoresearchProposals([
      proposal({ id: "generic", target: "misc", section: "Notes", created_at: 100 }),
      proposal({ id: "quick", target: "openhue", section: "Output", created_at: 101 }),
      proposal({ id: "safety", target: "blogwatcher", section: "Safety", created_at: 99 }),
      proposal({ id: "code", target: "api", mode: "code", title: "Fix route", created_at: 102 }),
    ], 3);

    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["safety", "quick", "code"]);
    expect(ranked.backlog.map((item) => item.proposal.id)).toEqual(["generic"]);
    expect(ranked.summary).toEqual({ total: 4, shown: 3, remaining: 1 });
  });
});
