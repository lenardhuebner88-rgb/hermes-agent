import { describe, expect, it } from "vitest";
import { clampLoopIterations, clearProposalSelection, describeLoopStatus, getProposalPriorityGroup, isActionable, pruneProposalSelection, rankAutoresearchProposals, rankAutoresearchReviewQueue, selectVisibleProposals, splitAutoresearchProposals, toggleProposalSelection } from "./autoresearch";
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
    expect(clampLoopIterations(99)).toBe(50);
    expect(clampLoopIterations(50)).toBe(50);
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


describe("autoresearch proposal actionability", () => {
  it("treats reverted no-improvement proposals as non-actionable while retryable crashes stay actionable", () => {
    expect(isActionable(proposal({ id: "fresh", target: "s" }))).toBe(true);
    expect(isActionable(proposal({ id: "reverted", target: "s", last_outcome: "reverted_no_improvement" }))).toBe(false);
    expect(isActionable(proposal({ id: "crash", target: "s", last_outcome: null }))).toBe(true);
  });

  it("excludes reverted proposals from the relevance queue and buckets them separately", () => {
    const items = [
      proposal({ id: "fresh", target: "a", section: "Output" }),
      proposal({ id: "reverted", target: "b", section: "Safety", last_outcome: "reverted_no_improvement" }),
      proposal({ id: "testing", target: "c", status: "testing" }),
      proposal({ id: "done", target: "d", status: "applied", last_outcome: "applied" }),
    ];

    const split = splitAutoresearchProposals(items);
    expect(split.actionable.map((p) => p.id)).toEqual(["fresh"]);
    expect(split.reverted.map((p) => p.id)).toEqual(["reverted"]);
    expect(split.testing.map((p) => p.id)).toEqual(["testing"]);
    expect(split.done.map((p) => p.id)).toEqual(["done"]);

    const ranked = rankAutoresearchProposals(items, 10);
    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["fresh"]);
    expect(ranked.summary.total).toBe(1);
  });
});

describe("rankAutoresearchReviewQueue (rank_score ordering)", () => {
  it("orders actionable proposals by rank_score descending, highest first", () => {
    const ranked = rankAutoresearchReviewQueue([
      proposal({ id: "low", target: "a", rank_score: 0.2 }),
      proposal({ id: "high", target: "b", rank_score: 0.9 }),
      proposal({ id: "mid", target: "c", rank_score: 0.5 }),
    ], 10);

    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["high", "mid", "low"]);
    expect(ranked.summary).toEqual({ total: 3, shown: 3, remaining: 0 });
  });

  it("treats proposals without a rank_score as lowest (after every scored proposal)", () => {
    const ranked = rankAutoresearchReviewQueue([
      proposal({ id: "unscored", target: "a" }),
      proposal({ id: "scored-low", target: "b", rank_score: 0.1 }),
      proposal({ id: "scored-high", target: "c", rank_score: 0.8 }),
    ], 10);

    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["scored-high", "scored-low", "unscored"]);
  });

  it("falls back to the priority-group order when no proposal carries a rank_score", () => {
    const ranked = rankAutoresearchReviewQueue([
      proposal({ id: "generic", target: "misc", section: "Notes" }),
      proposal({ id: "safety", target: "blogwatcher", section: "Safety" }),
      proposal({ id: "quick", target: "openhue", section: "Output" }),
    ], 10);

    // Safety (score 0) < Quick Win (1) < Other (3) — unchanged group ranking.
    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["safety", "quick", "generic"]);
  });

  it("ignores rank_score on non-actionable (reverted) proposals", () => {
    const ranked = rankAutoresearchReviewQueue([
      proposal({ id: "reverted", target: "a", rank_score: 0.99, last_outcome: "reverted_no_improvement" }),
      proposal({ id: "open", target: "b", rank_score: 0.3 }),
    ], 10);

    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["open"]);
    expect(ranked.summary.total).toBe(1);
  });

  it("splits the ranked list into shortlist and backlog at the limit", () => {
    const ranked = rankAutoresearchReviewQueue([
      proposal({ id: "a", target: "t", rank_score: 0.9 }),
      proposal({ id: "b", target: "t", rank_score: 0.7 }),
      proposal({ id: "c", target: "t", rank_score: 0.5 }),
    ], 2);

    expect(ranked.shortlist.map((item) => item.proposal.id)).toEqual(["a", "b"]);
    expect(ranked.backlog.map((item) => item.proposal.id)).toEqual(["c"]);
    expect(ranked.summary).toEqual({ total: 3, shown: 2, remaining: 1 });
  });
});

describe("batch selection reducer helpers", () => {
  it("toggleProposalSelection adds and removes an id immutably", () => {
    const start = new Set<string>(["a"]);
    const added = toggleProposalSelection(start, "b", true);
    expect([...added].sort()).toEqual(["a", "b"]);
    // original untouched
    expect([...start]).toEqual(["a"]);

    const removed = toggleProposalSelection(added, "a", false);
    expect([...removed]).toEqual(["b"]);
  });

  it("toggleProposalSelection is idempotent when the desired state already holds", () => {
    const start = new Set<string>(["a"]);
    expect([...toggleProposalSelection(start, "a", true)]).toEqual(["a"]);
    expect([...toggleProposalSelection(start, "z", false)]).toEqual(["a"]);
  });

  it("selectVisibleProposals selects ONLY the visible ids — never hidden backlog (BLOCKER fix)", () => {
    // The shortlist (visible) ids; the backlog ids must NOT appear here.
    const visibleShortlist = ["v1", "v2"];
    const selection = selectVisibleProposals(visibleShortlist);

    expect([...selection].sort()).toEqual(["v1", "v2"]);
    // Backlog proposals hidden behind the collapsed <details> stay unselected.
    expect(selection.has("backlog-hidden")).toBe(false);
  });

  it("selectVisibleProposals on an empty visible set yields an empty selection", () => {
    expect(selectVisibleProposals([]).size).toBe(0);
  });

  it("clearProposalSelection returns an empty set", () => {
    expect(clearProposalSelection().size).toBe(0);
  });

  it("pruneProposalSelection drops ids that left the queue and preserves queue order", () => {
    const current = new Set<string>(["c", "a", "gone"]);
    const validIds = ["a", "b", "c"];
    expect(pruneProposalSelection(current, validIds)).toEqual(["a", "c"]);
  });

  it("pruneProposalSelection returns empty when nothing is still valid", () => {
    expect(pruneProposalSelection(new Set(["x"]), ["a", "b"])).toEqual([]);
  });
});
