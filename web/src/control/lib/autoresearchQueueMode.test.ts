import { describe, expect, it } from "vitest";
import {
  filterAutoresearchQueueByMode,
  getAutoresearchEmptyQueueModeGuidance,
  getAutoresearchQueueModeSummary,
} from "./autoresearchQueueMode";
import type { Proposal } from "./types";

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id">): Proposal {
  return {
    target: "skill/foo",
    section: "Examples",
    title: "Tidy wording",
    rationale_plain: "small wording cleanup",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...overrides,
  };
}

// Mixed corpus reused across the mode-filter tests.
const safe1 = proposal({ id: "safe1", severity: "low" });
const safe2 = proposal({ id: "safe2", severity: "medium" });
const highSkill = proposal({ id: "high", severity: "high" });
const criticalSkill = proposal({ id: "crit", severity: "critical" });
const codeProposal = proposal({ id: "code", mode: "code", severity: "low" });
const safetySkill = proposal({ id: "safety", severity: "low", rationale_plain: "security risk" });

const corpus = [safe1, safe2, highSkill, criticalSkill, codeProposal, safetySkill];

describe("filterAutoresearchQueueByMode", () => {
  it("'all' returns every proposal untouched", () => {
    expect(filterAutoresearchQueueByMode(corpus, "all")).toEqual(corpus);
  });

  it("'high' keeps only severity high+ (high and critical)", () => {
    const ids = filterAutoresearchQueueByMode(corpus, "high").map((p) => p.id);
    expect(ids).toEqual(["high", "crit"]);
  });

  it("'safe' returns ONLY batch-safe proposals and excludes high/critical/code/safety", () => {
    const ids = filterAutoresearchQueueByMode(corpus, "safe").map((p) => p.id);
    expect(ids).toEqual(["safe1", "safe2"]);
    // safety invariant: nothing requiring manual review may surface as "safe"
    expect(ids).not.toContain("high");
    expect(ids).not.toContain("crit");
    expect(ids).not.toContain("code");
    expect(ids).not.toContain("safety");
  });

  it("'manual' is the exact complement of 'safe' (every non-safe proposal)", () => {
    const manualIds = filterAutoresearchQueueByMode(corpus, "manual").map((p) => p.id);
    const safeIds = filterAutoresearchQueueByMode(corpus, "safe").map((p) => p.id);
    expect(manualIds).toEqual(["high", "crit", "code", "safety"]);
    // safe + manual partition the corpus with no overlap
    expect([...safeIds, ...manualIds].sort()).toEqual(corpus.map((p) => p.id).sort());
    expect(safeIds.some((id) => manualIds.includes(id))).toBe(false);
  });
});

describe("getAutoresearchQueueModeSummary", () => {
  it("counts each mode bucket and marks the active option", () => {
    const summary = getAutoresearchQueueModeSummary(corpus, "safe");
    const byId = Object.fromEntries(summary.options.map((o) => [o.id, o.count]));
    expect(byId.all).toBe(6);
    expect(byId.high).toBe(2);
    expect(byId.manual).toBe(4);
    expect(byId.safe).toBe(2);
    expect(summary.active.id).toBe("safe");
  });

  it("falls back to the 'all' option when the active mode id is unknown-shaped", () => {
    const summary = getAutoresearchQueueModeSummary(corpus, "all");
    expect(summary.active.id).toBe("all");
  });

  it("uses zinc tone for empty buckets and a live tone when populated", () => {
    const summary = getAutoresearchQueueModeSummary([safe1, safe2], "all");
    const safe = summary.options.find((o) => o.id === "safe")!;
    const manual = summary.options.find((o) => o.id === "manual")!;
    expect(safe.count).toBe(2);
    expect(safe.tone).toBe("emerald");
    expect(manual.count).toBe(0);
    expect(manual.tone).toBe("zinc");
  });
});

describe("getAutoresearchEmptyQueueModeGuidance", () => {
  it("returns null when the active filter has results", () => {
    const summary = getAutoresearchQueueModeSummary(corpus, "safe");
    expect(getAutoresearchEmptyQueueModeGuidance(summary)).toBeNull();
  });

  it("returns null for the 'all' mode (never an empty-filter dead end)", () => {
    const summary = getAutoresearchQueueModeSummary([], "all");
    expect(getAutoresearchEmptyQueueModeGuidance(summary)).toBeNull();
  });

  it("guides from an empty 'high' filter toward manual review when manual cards remain", () => {
    // only manual-review (safety) cards present -> high filter is empty
    const summary = getAutoresearchQueueModeSummary([safetySkill], "high");
    const guidance = getAutoresearchEmptyQueueModeGuidance(summary);
    expect(guidance).not.toBeNull();
    expect(guidance!.primaryMode).toBe("manual");
  });

  it("guides from an empty 'safe' filter and refuses batch when only manual cards remain", () => {
    const summary = getAutoresearchQueueModeSummary([highSkill], "safe");
    const guidance = getAutoresearchEmptyQueueModeGuidance(summary);
    expect(guidance).not.toBeNull();
    expect(guidance!.primaryMode).toBe("manual");
    expect(guidance!.detail).toContain("Einzelreview");
  });
});
