import { describe, expect, it } from "vitest";
import { getAutoresearchResolvedSummary } from "./autoresearchResolvedSummary";
import type { Proposal } from "./types";

function proposal(id: string): Proposal {
  return {
    id,
    target: "skill/foo",
    section: null,
    title: null,
    rationale_plain: "",
    diff_before_after: "",
    mode: "skill",
    status: "applied",
  };
}

describe("getAutoresearchResolvedSummary", () => {
  it("returns null when nothing is resolved", () => {
    expect(getAutoresearchResolvedSummary({ reverted: [], applied: [], skipped: [] })).toBeNull();
  });

  it("prioritizes cleanup of reverted cards (amber) over applied/skipped", () => {
    const s = getAutoresearchResolvedSummary({
      reverted: [proposal("r1")],
      applied: [proposal("a1")],
      skipped: [proposal("s1")],
    });
    expect(s).not.toBeNull();
    expect(s!.tone).toBe("amber");
    expect(s!.label).toBe("Aufräumen");
    expect(s!.archiveLabel).toBe("Karte archivieren");
  });

  it("reports applied (emerald) when there are no reverted cards", () => {
    const s = getAutoresearchResolvedSummary({ reverted: [], applied: [proposal("a1"), proposal("a2")], skipped: [] });
    expect(s!.tone).toBe("emerald");
    expect(s!.label).toBe("Erledigt");
    expect(s!.archiveLabel).toBeNull();
    expect(s!.title).toContain("2");
  });

  it("reports skipped-only (zinc) when nothing was reverted or applied", () => {
    const s = getAutoresearchResolvedSummary({ reverted: [], applied: [], skipped: [proposal("s1")] });
    expect(s!.tone).toBe("zinc");
    expect(s!.label).toBe("Aussortiert");
    expect(s!.archiveLabel).toBeNull();
  });

  it("counts each bucket in the facts", () => {
    const s = getAutoresearchResolvedSummary({
      reverted: [proposal("r1"), proposal("r2")],
      applied: [proposal("a1")],
      skipped: [proposal("s1"), proposal("s2"), proposal("s3")],
    });
    const byLabel = Object.fromEntries(s!.facts.map((f) => [f.label, f.value]));
    expect(byLabel.Zurückgerollt).toBe("2");
    expect(byLabel.Übernommen).toBe("1");
    expect(byLabel.Übersprungen).toBe("3");
  });
});
