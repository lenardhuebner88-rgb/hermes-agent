import { describe, expect, it } from "vitest";
import { AUTORESEARCH_AREAS, clampLoopIterations, clearProposalSelection, codeWeaknessBusyKey, describeArea, describeLoopStatus, filterBySeverityThreshold, formatResearchTokens, formatRunTime, getProposalPriorityGroup, getProposalSeverity, hasResearchCounters, isActionable, parseMinUseCount, partitionBySeverity, proposalAgeDays, pruneProposalSelection, rankAutoresearchProposals, rankAutoresearchReviewQueue, readLastRunCounters, runLaneLabel, runLaneTone, runModelLabel, runVetoedCount, selectVisibleProposals, severityDistribution, severityRank, shouldShowResearchErrorBadge, splitAutoresearchProposals, summarizeProposalRoi, summarizeRecentRuns, sumRunTokens, toggleProposalSelection } from "./autoresearch";
import type { AutoresearchStatus, Proposal } from "./types";

const base: AutoresearchStatus = {
  state: "idle", pid: null, request_id: null, iteration: 0, max: 0,
  last_step: null, last_eval: null, route_status: "configured",
  heartbeat_age_s: null, heartbeat_fresh: false, last_receipt: null, last_run: null, note: null,
};

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id">): Proposal {
  return {
    target: "skill/foo",
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

describe("autoresearch on-demand driver helpers", () => {
  it("readLastRunCounters pulls numeric counters and distinguishes 0 from missing", () => {
    const c = readLastRunCounters({ skills_researched: 3, research_errors: 0, skills_with_findings: 0, research_tokens: 1234 });
    expect(c).toEqual({ skillsResearched: 3, researchErrors: 0, skillsWithFindings: 0, researchTokens: 1234 });
    const missing = readLastRunCounters({ mode: "dry-run" });
    expect(missing).toEqual({ skillsResearched: null, researchErrors: null, skillsWithFindings: null, researchTokens: null });
  });

  it("readLastRunCounters is null-safe for non-object / non-numeric last_run", () => {
    expect(readLastRunCounters(null).skillsResearched).toBeNull();
    expect(readLastRunCounters("done").researchErrors).toBeNull();
    expect(readLastRunCounters({ research_errors: "2" }).researchErrors).toBeNull();
  });

  it("hasResearchCounters is true iff any of the three observability counters is present", () => {
    expect(hasResearchCounters(readLastRunCounters({ skills_researched: 0 }))).toBe(true);
    expect(hasResearchCounters(readLastRunCounters({ research_tokens: 5 }))).toBe(false); // tokens alone is not an observability counter
    expect(hasResearchCounters(readLastRunCounters(null))).toBe(false);
  });

  it("shouldShowResearchErrorBadge only fires on a positive error count", () => {
    expect(shouldShowResearchErrorBadge(0)).toBe(false);
    expect(shouldShowResearchErrorBadge(null)).toBe(false);
    expect(shouldShowResearchErrorBadge(undefined)).toBe(false);
    expect(shouldShowResearchErrorBadge(2)).toBe(true);
  });

  it("formatResearchTokens shows a real count or 'n/v' for 0/missing — never a guess", () => {
    expect(formatResearchTokens(0)).toBe("n/v");
    expect(formatResearchTokens(null)).toBe("n/v");
    expect(formatResearchTokens(undefined)).toBe("n/v");
    expect(formatResearchTokens(1234)).toBe((1234).toLocaleString("de-DE"));
  });

  it("parseMinUseCount sends only a finite positive value, else null (keep backend default)", () => {
    expect(parseMinUseCount("")).toBeNull();
    expect(parseMinUseCount("   ")).toBeNull();
    expect(parseMinUseCount("abc")).toBeNull();
    expect(parseMinUseCount("0")).toBeNull();
    expect(parseMinUseCount("-3")).toBeNull();
    expect(parseMinUseCount("2")).toBe(2);
    expect(parseMinUseCount(" 1.5 ")).toBe(1.5);
  });
});

describe("autoresearch run-history (ROI panel) helpers", () => {
  const run = (over: Partial<import("./types").AutoresearchRun> = {}): import("./types").AutoresearchRun => ({
    at: "2026-05-31T19:31:21Z", lane: "skill", request_id: "r", tokens: 0, proposed: 0, errors: 0, scanned: 0, ...over,
  });

  it("sumRunTokens totals tokens and ignores non-finite", () => {
    expect(sumRunTokens([run({ tokens: 100 }), run({ tokens: 50 }), run({ tokens: NaN })])).toBe(150);
    expect(sumRunTokens([])).toBe(0);
  });

  it("runLaneLabel / runLaneTone map lane to label + tone", () => {
    expect(runLaneLabel("code")).toBe("Code");
    expect(runLaneLabel("skill")).toBe("Skills");
    expect(runLaneLabel("deep-audit")).toBe("Deep-Audit");
    expect(runLaneLabel("test")).toBe("Test-Foundry");
    expect(runLaneTone("code")).toBe("violet");
    expect(runLaneTone("skill")).toBe("cyan");
    expect(runLaneTone("deep-audit")).toBe("amber");
    expect(runLaneTone("test")).toBe("emerald");
  });

  it("runVetoedCount and runModelLabel keep missing fields backward-compatible", () => {
    expect(runVetoedCount(run())).toBe(0);
    expect(runVetoedCount(run({ vetoed: 2 }))).toBe(2);
    expect(runVetoedCount(run({ vetoed: NaN }))).toBe(0);
    expect(runModelLabel(run())).toBeNull();
    expect(runModelLabel(run({ model: "  minimax/m1  " }))).toBe("minimax/m1");
  });

  it("formatRunTime returns '—' for empty/invalid and a string for valid ISO", () => {
    expect(formatRunTime("")).toBe("—");
    expect(formatRunTime(null)).toBe("—");
    expect(formatRunTime("not-a-date")).toBe("—");
    expect(formatRunTime("2026-05-31T19:31:21Z")).not.toBe("—");
  });

  it("summarizeRecentRuns sums within the window and excludes older/invalid runs", () => {
    const now = Date.parse("2026-06-01T12:00:00Z");
    const inside1 = run({ at: "2026-06-01T00:00:00Z", tokens: 100, proposed: 2, scanned: 30 });
    const inside2 = run({ at: "2026-05-28T00:00:00Z", tokens: 50, proposed: 0, scanned: 10 });
    const older = run({ at: "2026-05-20T00:00:00Z", tokens: 999, proposed: 9, scanned: 99 }); // >7d ago
    const summary = summarizeRecentRuns([inside1, inside2, older], 7, now);
    expect(summary).toEqual({ runs: 2, tokens: 150, proposed: 2, scanned: 40 });
    expect(summarizeRecentRuns([], 7, now)).toEqual({ runs: 0, tokens: 0, proposed: 0, scanned: 0 });
  });

  it("summarizeRecentRuns excludes unparseable `at` and counts non-finite fields as 0", () => {
    const now = Date.parse("2026-06-01T12:00:00Z");
    const bad = run({ at: "", tokens: 5 });
    const finiteGuard = run({ at: "2026-06-01T00:00:00Z", tokens: NaN, proposed: 3, scanned: 7 });
    const summary = summarizeRecentRuns([bad, finiteGuard], 7, now);
    expect(summary).toEqual({ runs: 1, tokens: 0, proposed: 3, scanned: 7 });
  });

  it("summarizeRecentRuns boundary: now inclusive, now-8d excluded, custom days narrows", () => {
    const now = Date.parse("2026-06-01T12:00:00Z");
    const day = 24 * 60 * 60 * 1000;
    const atNow = run({ at: new Date(now).toISOString(), tokens: 1 });
    const eightDaysAgo = run({ at: new Date(now - 8 * day).toISOString(), tokens: 1 });
    expect(summarizeRecentRuns([atNow, eightDaysAgo], 7, now).runs).toBe(1);
    const twoDaysAgo = run({ at: new Date(now - 2 * day).toISOString(), tokens: 1 });
    expect(summarizeRecentRuns([atNow, twoDaysAgo], 1, now).runs).toBe(1); // days=1 drops the 2-day-old run
  });

  it("summarizeProposalRoi calculates acceptance rate and tokens per applied proposal", () => {
    const proposals = [
      proposal({ id: "a", status: "applied" }),
      proposal({ id: "s", status: "skipped" }),
      proposal({ id: "r", status: "proposed", last_outcome: "reverted_no_improvement" }),
      proposal({ id: "o", status: "proposed" }),
    ];
    expect(summarizeProposalRoi(proposals, 900)).toEqual({
      applied: 1,
      skipped: 1,
      reverted: 1,
      decided: 3,
      acceptanceRate: 1 / 3,
      tokensPerApplied: 900,
    });
    expect(summarizeProposalRoi([], 900).acceptanceRate).toBeNull();
    expect(summarizeProposalRoi([proposal({ id: "s2", status: "skipped" })], 900).tokensPerApplied).toBeNull();
  });

  it("proposalAgeDays handles ISO strings, epoch seconds, and missing values", () => {
    const now = Date.parse("2026-06-03T12:00:00Z");
    expect(proposalAgeDays(proposal({ id: "age-iso", created_at: "2026-06-01T12:00:00Z" }), now)).toBe(2);
    expect(proposalAgeDays(proposal({ id: "age-seconds", created_at: Date.parse("2026-06-02T12:00:00Z") / 1000 }), now)).toBe(1);
    expect(proposalAgeDays(proposal({ id: "age-missing", created_at: null }), now)).toBeNull();
  });

  it("codeWeaknessBusyKey matches the generateCodeWeaknesses handler contract", () => {
    expect(codeWeaknessBusyKey("incremental")).toBe("generate-code");
    expect(codeWeaknessBusyKey("full")).toBe("generate-code-full");
    expect(codeWeaknessBusyKey("deep")).toBe("generate-code-deep");
  });

  it("describeArea returns a plain-German scope for known areas, raw value otherwise", () => {
    expect(describeArea("all")).toBe("alle Skills");
    expect(describeArea("dashboard")).toBe("Dashboard-Code (scripts + tests)");
    expect(describeArea("hermes-kanban")).toBe("alle Kanban-Skills");
    expect(describeArea("does-not-exist")).toBe("does-not-exist");
  });

  it("AUTORESEARCH_AREAS starts with 'all' and every value is a valid backend slug", () => {
    expect(AUTORESEARCH_AREAS[0].value).toBe("all");
    const slug = /^[a-z0-9][a-z0-9_-]*$/;
    for (const a of AUTORESEARCH_AREAS) expect(a.value).toMatch(slug);
  });
});

describe("severity grouping + distribution", () => {
  it("honours a valid model-assigned severity", () => {
    expect(getProposalSeverity(proposal({ id: "a", target: "x", severity: "critical" }))).toBe("critical");
  });

  it("falls back from category when severity is missing or invalid", () => {
    expect(getProposalSeverity(proposal({ id: "a", target: "x", category: "bug_risk" }))).toBe("high");
    expect(getProposalSeverity(proposal({ id: "b", target: "x", category: "contradiction" }))).toBe("critical");
    expect(getProposalSeverity(proposal({ id: "c", target: "x", category: "missing_section" }))).toBe("low");
    // unknown category + no severity → medium default
    expect(getProposalSeverity(proposal({ id: "d", target: "x", category: "mystery" }))).toBe("medium");
  });

  it("ranks severities critical > high > medium > low", () => {
    expect(severityRank(proposal({ id: "a", target: "x", severity: "critical" }))).toBeGreaterThan(
      severityRank(proposal({ id: "b", target: "x", severity: "high" })),
    );
    expect(severityRank(proposal({ id: "b", target: "x", severity: "high" }))).toBeGreaterThan(
      severityRank(proposal({ id: "c", target: "x", severity: "low" })),
    );
  });

  it("partitions critical+high open and medium+low collapsed", () => {
    const items = [
      proposal({ id: "crit", target: "x", severity: "critical" }),
      proposal({ id: "med", target: "x", severity: "medium" }),
      proposal({ id: "high", target: "x", severity: "high" }),
      proposal({ id: "low", target: "x", severity: "low" }),
    ];
    const { open, collapsed } = partitionBySeverity(items);
    expect(open.map((p) => p.id)).toEqual(["crit", "high"]);
    expect(collapsed.map((p) => p.id)).toEqual(["med", "low"]);
  });

  it("filters to a severity threshold for the 'nur hoch+' chip", () => {
    const items = [
      proposal({ id: "crit", target: "x", severity: "critical" }),
      proposal({ id: "med", target: "x", severity: "medium" }),
      proposal({ id: "high", target: "x", severity: "high" }),
      proposal({ id: "low", target: "x", severity: "low" }),
    ];
    expect(filterBySeverityThreshold(items, "high").map((p) => p.id)).toEqual(["crit", "high"]);
  });

  it("counts proposals per severity and per category", () => {
    const items = [
      proposal({ id: "a", target: "x", severity: "critical", category: "bug_risk" }),
      proposal({ id: "b", target: "x", severity: "high", category: "bug_risk" }),
      proposal({ id: "c", target: "x", category: "missing_section" }), // → low
    ];
    const dist = severityDistribution(items);
    expect(dist.total).toBe(3);
    expect(dist.bySeverity).toEqual({ critical: 1, high: 1, medium: 0, low: 1 });
    expect(dist.byCategory).toEqual({ bug_risk: 2, missing_section: 1 });
  });
});
