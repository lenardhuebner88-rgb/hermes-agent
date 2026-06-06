import { describe, expect, it } from "vitest";
import {
  canApplyAllOpenSkillProposals,
  canBatchConfirmAutoresearchSelection,
  describeTopCardMode,
  getBatchSafeVisibleProposalIds,
  proposalNeedsManualReview,
} from "./autoresearchDecisionGuide";
import type { Proposal } from "./types";

// Minimal valid Proposal fixture. Defaults are deliberately the "safe" case:
// a skill-mode proposal with no severity, no category, and neutral text -> its
// severity falls back to "medium" (below high) and its priority group is "other",
// so by default proposalNeedsManualReview(...) is FALSE.
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

describe("proposalNeedsManualReview", () => {
  it("treats a plain medium-severity skill proposal as batch-safe (no manual review)", () => {
    // baseline: skill mode, fallback severity medium, "other" group -> safe
    expect(proposalNeedsManualReview(proposal({ id: "safe" }))).toBe(false);
  });

  it("FORCES manual review for any non-skill mode (code)", () => {
    expect(proposalNeedsManualReview(proposal({ id: "code", mode: "code" }))).toBe(true);
  });

  it("FORCES manual review for any non-skill mode (test)", () => {
    expect(proposalNeedsManualReview(proposal({ id: "test", mode: "test" }))).toBe(true);
  });

  it("FORCES manual review when explicit severity is high", () => {
    expect(proposalNeedsManualReview(proposal({ id: "high", severity: "high" }))).toBe(true);
  });

  it("FORCES manual review when explicit severity is critical", () => {
    expect(proposalNeedsManualReview(proposal({ id: "crit", severity: "critical" }))).toBe(true);
  });

  it("allows batch for explicit low/medium severity skill proposals (boundary just below high)", () => {
    expect(proposalNeedsManualReview(proposal({ id: "low", severity: "low" }))).toBe(false);
    expect(proposalNeedsManualReview(proposal({ id: "med", severity: "medium" }))).toBe(false);
  });

  it("FORCES manual review via category->severity fallback that lands at high (no explicit severity)", () => {
    // category "stale" falls back to "high" -> manual review
    expect(proposalNeedsManualReview(proposal({ id: "stale", severity: null, category: "stale" }))).toBe(true);
    // category "contradiction" falls back to "critical" -> manual review
    expect(proposalNeedsManualReview(proposal({ id: "contra", severity: null, category: "contradiction" }))).toBe(true);
  });

  it("keeps batch-safe for category->severity fallback below high", () => {
    // category "unclear_trigger" -> medium (below high), text neutral -> safe
    expect(
      proposalNeedsManualReview(
        proposal({ id: "unclear", severity: null, category: "unclear_trigger", target: "skill/x", section: "Examples", title: "x", rationale_plain: "x", new_text: "x" }),
      ),
    ).toBe(false);
  });

  it("FORCES manual review when the priority group is 'safety' even at low severity", () => {
    // a safety-term ("security") anywhere in the haystack -> safety group -> manual
    const p = proposal({
      id: "safety",
      severity: "low",
      target: "skill/security-checks",
      section: "Examples",
      title: "Tidy wording",
      rationale_plain: "neutral",
    });
    expect(proposalNeedsManualReview(p)).toBe(true);
  });

  it("FORCES manual review when safety terms appear only in free-text (rationale)", () => {
    const p = proposal({
      id: "safety-text",
      severity: "low",
      target: "skill/foo",
      section: "Examples",
      title: "Tidy wording",
      rationale_plain: "mentions a secret token credential risk",
    });
    expect(proposalNeedsManualReview(p)).toBe(true);
  });
});

describe("getBatchSafeVisibleProposalIds", () => {
  it("returns only the ids of batch-safe proposals and EXCLUDES every manual-review one", () => {
    const safeA = proposal({ id: "safe-a" });
    const safeB = proposal({ id: "safe-b", severity: "medium" });
    const codeManual = proposal({ id: "code-manual", mode: "code" });
    const highManual = proposal({ id: "high-manual", severity: "high" });
    const safetyManual = proposal({ id: "safety-manual", severity: "low", rationale_plain: "security risk" });

    const ids = getBatchSafeVisibleProposalIds([safeA, codeManual, safeB, highManual, safetyManual]);

    expect(ids).toEqual(["safe-a", "safe-b"]);
    // safety-critical invariant: a manual-review proposal must never be returned as batch-safe
    expect(ids).not.toContain("code-manual");
    expect(ids).not.toContain("high-manual");
    expect(ids).not.toContain("safety-manual");
  });

  it("never returns a manual-review proposal as batch-safe, even when it is the only proposal", () => {
    const ids = getBatchSafeVisibleProposalIds([proposal({ id: "only-high", severity: "critical" })]);
    expect(ids).toEqual([]);
  });

  it("returns an empty array for an empty input", () => {
    expect(getBatchSafeVisibleProposalIds([])).toEqual([]);
  });

  it("preserves input order of the surviving safe proposals", () => {
    const ids = getBatchSafeVisibleProposalIds([
      proposal({ id: "z" }),
      proposal({ id: "a" }),
      proposal({ id: "m", mode: "code" }),
    ]);
    expect(ids).toEqual(["z", "a"]);
  });
});

describe("canBatchConfirmAutoresearchSelection", () => {
  it("allows batch confirm only when something is selected, none need manual review, and not busy", () => {
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 3, selectedManualReviewCount: 0, busy: false })).toBe(true);
  });

  it("blocks batch confirm when any selected proposal needs manual review", () => {
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 3, selectedManualReviewCount: 1, busy: false })).toBe(false);
  });

  it("blocks batch confirm when nothing is selected", () => {
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 0, selectedManualReviewCount: 0, busy: false })).toBe(false);
  });

  it("blocks batch confirm while busy", () => {
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 2, selectedManualReviewCount: 0, busy: true })).toBe(false);
  });
});

describe("canApplyAllOpenSkillProposals", () => {
  it("allows apply-all only when there are proposals, none need manual review, and not busy", () => {
    expect(
      canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "a" }), proposal({ id: "b" })], busy: false }),
    ).toBe(true);
  });

  it("blocks apply-all when ANY proposal needs manual review", () => {
    expect(
      canApplyAllOpenSkillProposals({
        openSkillProposals: [proposal({ id: "a" }), proposal({ id: "high", severity: "high" })],
        busy: false,
      }),
    ).toBe(false);
  });

  it("blocks apply-all on an empty list", () => {
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [], busy: false })).toBe(false);
  });

  it("blocks apply-all while busy", () => {
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "a" })], busy: true })).toBe(false);
  });
});

describe("describeTopCardMode", () => {
  it("labels a manual-review card as Einzelreview (amber)", () => {
    const mode = describeTopCardMode(proposal({ id: "high", severity: "high" }));
    expect(mode.label).toBe("Einzelreview");
    expect(mode.tone).toBe("amber");
  });

  it("labels a batch-safe card as Sammel-sicher (emerald)", () => {
    const mode = describeTopCardMode(proposal({ id: "safe" }));
    expect(mode.label).toBe("Sammel-sicher");
    expect(mode.tone).toBe("emerald");
  });
});
