import { describe, expect, it } from "vitest";
import { countActionableFailures, countOpenFunnelDrafts, summarizeFlowAttention } from "./flowAttention";

describe("countActionableFailures", () => {
  it("returns 0 for an empty failures array", () => {
    expect(countActionableFailures({ failures: [] })).toBe(0);
  });

  it("returns the exact length of the failures array (no client-side filter)", () => {
    const data = {
      failures: [
        { run_id: 1, task_id: "t_aaa" },
        { run_id: 2, task_id: "t_bbb" },
        { run_id: 3, task_id: "t_ccc" },
      ],
    };
    expect(countActionableFailures(data)).toBe(3);
  });

  it("counts a single failure correctly", () => {
    expect(countActionableFailures({ failures: [{ run_id: 42, task_id: "t_single" }] })).toBe(1);
  });
});

describe("countOpenFunnelDrafts", () => {
  it("returns 0 for an empty drafts array", () => {
    expect(countOpenFunnelDrafts({ drafts: [] })).toBe(0);
  });

  it("returns the exact length of the drafts array (no client-side filter)", () => {
    const data = {
      drafts: [
        { id: "draft-1" },
        { id: "draft-2" },
      ],
    };
    expect(countOpenFunnelDrafts(data)).toBe(2);
  });

  it("counts a single draft correctly", () => {
    expect(countOpenFunnelDrafts({ drafts: [{ id: "draft-x" }] })).toBe(1);
  });
});

describe("summarizeFlowAttention", () => {
  it("returns quiet=true and stable label when all queues are zero", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 0,
      triageCount: 0,
      funnelCount: 0,
      dispositionCount: 0,
      blockedCount: 0,
    });
    expect(result.quiet).toBe(true);
    expect(result.segments).toHaveLength(0);
    expect(result.line).toBe("Nichts wartet auf dich.");
  });

  it("returns only non-zero segments", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 3,
      triageCount: 0,
      funnelCount: 5,
      dispositionCount: 0,
      blockedCount: 0,
    });
    expect(result.quiet).toBe(false);
    expect(result.segments).toHaveLength(2);
    expect(result.segments[0]).toMatchObject({ label: "Recovery", count: 3 });
    expect(result.segments[1]).toMatchObject({ label: "Freigaben", count: 5 });
  });

  it("builds a readable dot-separated line from non-zero segments", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 3,
      triageCount: 0,
      funnelCount: 5,
      dispositionCount: 0,
      blockedCount: 1,
    });
    expect(result.line).toBe("3 Recovery · 5 Freigaben · 1 Blockiert");
  });

  it("includes all five queues when all are non-zero", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 2,
      triageCount: 4,
      funnelCount: 1,
      dispositionCount: 3,
      blockedCount: 7,
    });
    expect(result.quiet).toBe(false);
    expect(result.segments).toHaveLength(5);
    expect(result.line).toBe("2 Recovery · 4 Triage · 1 Freigaben · 3 Disposition · 7 Blockiert");
  });

  it("each segment carries an anchorId for scroll navigation", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 1,
      triageCount: 0,
      funnelCount: 2,
      dispositionCount: 0,
      blockedCount: 0,
    });
    const ids = result.segments.map((s) => s.anchorId);
    expect(ids).toContain("flow-section-recovery");
    expect(ids).toContain("flow-section-funnel");
    expect(ids).not.toContain("flow-section-triage");
  });

  it("single segment: triage only", () => {
    const result = summarizeFlowAttention({
      recoveryCount: 0,
      triageCount: 1,
      funnelCount: 0,
      dispositionCount: 0,
      blockedCount: 0,
    });
    expect(result.quiet).toBe(false);
    expect(result.line).toBe("1 Triage");
  });
});
