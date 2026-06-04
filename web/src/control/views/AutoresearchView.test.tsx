import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { runLaneLabel, runLaneTone } from "../lib/autoresearch";
import { getAutoresearchRecommendation } from "../lib/autoresearchRecommendation";
import { DeepAuditFindings } from "./AutoresearchView";
import type { DeepAuditFinding } from "../hooks/useControlData";

describe("AutoresearchView Deep-Audit", () => {
  it("keeps the deep-audit run label and tone", () => {
    expect(runLaneLabel("deep-audit")).toBe("Deep-Audit");
    expect(runLaneTone("deep-audit")).toBe("amber");
  });

  it("keeps the test-foundry run label and tone", () => {
    expect(runLaneLabel("test")).toBe("Test-Foundry");
    expect(runLaneTone("test")).toBe("emerald");
  });

  it("renders structured findings with fileline, evidence, and proposal count", () => {
    const finding: DeepAuditFinding = {
      fileline: "hermes_cli/autoresearch_runs.py:23",
      severity: "high",
      category: "bug_risk",
      title: "Run lane omitted",
      problem: "The run lane allowlist can drop audit runs.",
      evidence: "_VALID_LANES",
      fix_hint: "Keep the deep-audit lane in the run history allowlist.",
    };
    const html = renderToStaticMarkup(<DeepAuditFindings findings={[finding]} proposals={["deep-audit-x"]} />);
    expect(html).toContain("hermes_cli/autoresearch_runs.py:23");
    expect(html).toContain("_VALID_LANES");
    expect(html).toContain("1 in Queue");
    expect(html).toContain("Run lane omitted");
  });
});

describe("AutoresearchView cockpit recommendation", () => {
  it("sends the operator to review when actionable proposals exist", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "idle",
      openCount: 4,
      revertedCount: 1,
      loopRunning: false,
      routeStatus: "configured",
    });

    expect(recommendation.kind).toBe("review");
    expect(recommendation.primaryLabel).toBe("Queue öffnen");
    expect(recommendation.title).toContain("4 geprüfte Verbesserungen");
  });

  it("keeps the operator in monitor mode while the loop is running", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "running",
      openCount: 0,
      revertedCount: 0,
      loopRunning: true,
      routeStatus: "configured",
    });

    expect(recommendation.kind).toBe("monitor");
    expect(recommendation.primaryLabel).toBe("Lauf ansehen");
  });

  it("prioritizes recovery before new runs when the loop crashed", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "crashed",
      openCount: 0,
      revertedCount: 0,
      loopRunning: false,
      routeStatus: "configured",
    });

    expect(recommendation.kind).toBe("recover");
    expect(recommendation.tone).toBe("red");
  });

  it("prioritizes recovery when the model route is not configured", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "idle",
      openCount: 0,
      revertedCount: 0,
      loopRunning: false,
      routeStatus: "unavailable",
    });

    expect(recommendation.kind).toBe("recover");
    expect(recommendation.primaryLabel).toBe("Status ansehen");
  });

  it("offers generation when the queue is empty and the loop is idle", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "idle",
      openCount: 0,
      revertedCount: 0,
      loopRunning: false,
      routeStatus: "configured",
    });

    expect(recommendation.kind).toBe("generate");
    expect(recommendation.primaryLabel).toBe("Vorschläge holen");
  });

  it("does not claim everything is clean while status is still unknown", () => {
    const recommendation = getAutoresearchRecommendation({
      state: undefined,
      openCount: 0,
      revertedCount: 0,
      loopRunning: false,
      routeStatus: undefined,
    });

    expect(recommendation.kind).toBe("inspect");
    expect(recommendation.title).toContain("Status prüfen");
    expect(recommendation.primaryLabel).toBe("Status ansehen");
  });
});
