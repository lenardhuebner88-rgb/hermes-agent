import { describe, expect, it } from "vitest";
import { groupAutoresearchProposals, rankAutoresearchProposalGroups } from "./proposalGroups";
import { liveAutoresearchMutationFixture } from "./proposalGroups.live.fixture";
import type { Proposal } from "./types";

function proposal(overrides: Partial<Proposal> & Pick<Proposal, "id" | "target">): Proposal {
  return {
    section: null,
    title: null,
    rationale_plain: "because",
    diff_before_after: "",
    mode: "skill",
    status: "proposed",
    ...overrides,
  };
}

describe("groupAutoresearchProposals", () => {
  it("folds the live Test-Foundry fixture by mode, category, and target", () => {
    const groups = groupAutoresearchProposals(liveAutoresearchMutationFixture);

    expect(liveAutoresearchMutationFixture).toHaveLength(30);
    expect(groups).toHaveLength(2);
    expect(groups.length).toBeLessThanOrEqual(10);
    expect(groups[0].title).toContain("Mutation survivor in kanban_decompose.py");
    expect(groups[0].title).toContain("28 Vorschläge");
    expect(groups[0].ids).toHaveLength(28);
    expect(groups[1].target).toBe("hermes_cli/kanban_db.py");
    expect(groups[1].ids).toHaveLength(2);
  });

  it("keeps distinct mode/category/target combinations separate", () => {
    const groups = groupAutoresearchProposals([
      proposal({ id: "a", target: "x.py", mode: "test", category: "mutation_survivor" }),
      proposal({ id: "b", target: "x.py", mode: "code", category: "mutation_survivor" }),
      proposal({ id: "c", target: "x.py", mode: "test", category: "bug_risk" }),
      proposal({ id: "d", target: "y.py", mode: "test", category: "mutation_survivor" }),
    ]);

    expect(groups.map((group) => group.ids)).toEqual([["a"], ["b"], ["c"], ["d"]]);
  });

  it("uses the highest severity in the group", () => {
    const groups = groupAutoresearchProposals([
      proposal({ id: "low", target: "x.py", severity: "low" }),
      proposal({ id: "critical", target: "x.py", severity: "critical" }),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].severity).toBe("critical");
    expect(groups[0].tone).toBe("red");
  });
});

describe("rankAutoresearchProposalGroups", () => {
  it("returns the live fixture as grouped operator cards", () => {
    const queue = rankAutoresearchProposalGroups(liveAutoresearchMutationFixture, 10);

    expect(queue.summary.proposalCount).toBe(30);
    expect(queue.summary.total).toBe(2);
    expect(queue.summary.shown).toBe(2);
    expect(queue.summary.remaining).toBe(0);
    expect(queue.shortlist.map((group) => group.count)).toEqual([28, 2]);
  });
});
