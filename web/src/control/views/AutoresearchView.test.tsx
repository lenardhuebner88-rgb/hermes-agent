import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { runLaneLabel, runLaneTone } from "../lib/autoresearch";
import { getAutoresearchRecommendation } from "../lib/autoresearchRecommendation";
import { getAutoresearchKeyboardAction } from "../lib/autoresearchKeyboard";
import { getAutoresearchReviewFlow } from "../lib/autoresearchReviewFlow";
import { canApplyAllOpenSkillProposals, canBatchConfirmAutoresearchSelection, describeTopCardMode, getAutoresearchDecisionGuide, getBatchSafeVisibleProposalIds } from "../lib/autoresearchDecisionGuide";
import { getDeepAuditGuidance, getResearchLoopGuidance, getResearchLoopPreset, getResearchLoopStartControl, getSelectedResearchLoopPresetId, RESEARCH_LOOP_PRESETS, getTestFoundryGuidance } from "../lib/autoresearchRunGuidance";
import { getAutoresearchRunSummary } from "../lib/autoresearchRunSummary";
import { DeepAuditFindings } from "./AutoresearchView";
import type { AutoresearchRun, Proposal } from "../lib/types";
import type { DeepAuditFinding } from "../hooks/useControlData";

function proposal(overrides: Partial<Proposal> = {}): Proposal {
  return {
    id: overrides.id ?? "p1",
    target: overrides.target ?? "skill.md",
    section: overrides.section ?? "examples",
    title: overrides.title ?? "Better example",
    category: overrides.category ?? "missing_section",
    severity: overrides.severity ?? "low",
    evidence: overrides.evidence ?? null,
    new_text: overrides.new_text ?? "New text",
    proposal_type: overrides.proposal_type ?? "skill_section",
    rationale_plain: overrides.rationale_plain ?? "Clearer operator guidance.",
    diff_before_after: overrides.diff_before_after ?? "",
    rank_score: overrides.rank_score ?? null,
    mode: overrides.mode ?? "skill",
    status: overrides.status ?? "proposed",
    last_outcome: overrides.last_outcome ?? null,
    result: overrides.result ?? null,
    created_at: overrides.created_at ?? "2026-06-04T20:00:00Z",
    applied_at: overrides.applied_at ?? null,
    gate: overrides.gate ?? null,
  };
}

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

describe("AutoresearchView review flow", () => {
  it("prioritizes high-risk decisions before batch flow", () => {
    const flow = getAutoresearchReviewFlow({
      openCount: 5,
      decidedCount: 2,
      selectedCount: 0,
      visibleCount: 5,
      highPriorityCount: 2,
      backlogCount: 0,
      revertedCount: 1,
      topTitle: "Secret leak in CLI",
    });

    expect(flow.tone).toBe("amber");
    expect(flow.primaryAction).toBe("select-top");
    expect(flow.primaryLabel).toBe("Top prüfen");
    expect(flow.title).toContain("Hoch+");
    expect(flow.detail).toContain("Secret leak in CLI");
  });

  it("switches to batch confirmation when proposals are selected", () => {
    const flow = getAutoresearchReviewFlow({
      openCount: 5,
      decidedCount: 2,
      selectedCount: 3,
      visibleCount: 5,
      highPriorityCount: 2,
      backlogCount: 1,
      revertedCount: 0,
      topTitle: "Any proposal",
    });

    expect(flow.tone).toBe("cyan");
    expect(flow.primaryAction).toBe("confirm-selection");
    expect(flow.primaryLabel).toBe("Auswahl übernehmen");
    expect(flow.progressLabel).toBe("2 von 7 entschieden");
  });

  it("does not offer batch confirmation for selected manual-review proposals", () => {
    const flow = getAutoresearchReviewFlow({
      openCount: 5,
      decidedCount: 2,
      selectedCount: 2,
      visibleCount: 5,
      highPriorityCount: 1,
      selectedManualReviewCount: 1,
      backlogCount: 1,
      revertedCount: 0,
      topTitle: "Risky proposal",
    });

    expect(flow.tone).toBe("amber");
    expect(flow.primaryAction).toBe("clear-selection");
    expect(flow.primaryLabel).toBe("Auswahl zurücksetzen");
    expect(flow.detail).toContain("entscheide sie direkt auf der Karte");
  });

  it("turns mixed visible cards into a safe batch-selection step", () => {
    const flow = getAutoresearchReviewFlow({
      openCount: 5,
      decidedCount: 2,
      selectedCount: 0,
      visibleCount: 4,
      batchSafeVisibleCount: 2,
      highPriorityCount: 0,
      backlogCount: 1,
      revertedCount: 0,
      topTitle: "Any proposal",
    });

    expect(flow.tone).toBe("emerald");
    expect(flow.primaryAction).toBe("select-visible");
    expect(flow.primaryLabel).toBe("Sichere markieren");
    expect(flow.detail).toContain("2 sichtbare Karten");
    expect(flow.detail).toContain("2 bleiben bewusst Einzelreview");
    expect(flow.steps.find((step) => step.label === "Sicher")?.value).toBe("2");
    expect(flow.steps.find((step) => step.label === "Einzeln")?.tone).toBe("amber");
  });

  it("turns an empty queue into an actionable next step", () => {
    const flow = getAutoresearchReviewFlow({
      openCount: 0,
      decidedCount: 4,
      selectedCount: 0,
      visibleCount: 0,
      highPriorityCount: 0,
      backlogCount: 0,
      revertedCount: 0,
    });

    expect(flow.tone).toBe("emerald");
    expect(flow.primaryAction).toBe("generate");
    expect(flow.primaryLabel).toBe("Neue Kandidaten holen");
    expect(flow.progressPercent).toBe(100);
  });
});

describe("AutoresearchView decision guide", () => {
  it("allows batch review only for low-risk visible skill proposals", () => {
    const guide = getAutoresearchDecisionGuide({
      visibleProposals: [proposal({ id: "p1", severity: "low" }), proposal({ id: "p2", severity: "medium" })],
      selectedProposals: [],
      openCount: 2,
      selectedCount: 0,
      backlogCount: 0,
      revertedCount: 0,
      topTitle: "Better example",
    });

    expect(guide.tone).toBe("emerald");
    expect(guide.primaryLabel).toBe("Sammelreview");
    expect(guide.summary).toContain("gesammelt markieren");
    expect(guide.facts.find((fact) => fact.label === "Sammeln ok")?.value).toBe("2");
  });

  it("pushes code, high-risk, or safety proposals into manual review", () => {
    const guide = getAutoresearchDecisionGuide({
      visibleProposals: [
        proposal({ id: "code", mode: "code", severity: "medium", title: "Patch retry loop" }),
        proposal({ id: "high", severity: "high", title: "Credential warning" }),
        proposal({ id: "safety", severity: "low", title: "Token safety note", rationale_plain: "Mentions token risk." }),
      ],
      selectedProposals: [],
      openCount: 3,
      selectedCount: 0,
      backlogCount: 1,
      revertedCount: 0,
      topTitle: "Patch retry loop",
    });

    expect(guide.tone).toBe("amber");
    expect(guide.primaryLabel).toBe("Einzelreview");
    expect(guide.next).toContain("Patch retry loop");
    expect(guide.facts.find((fact) => fact.label === "Einzeln lesen")?.value).toBe("3");
  });

  it("explains that selected batch actions only touch the current selection", () => {
    const guide = getAutoresearchDecisionGuide({
      visibleProposals: [proposal({ id: "p1" }), proposal({ id: "p2" })],
      selectedProposals: [proposal({ id: "p1" }), proposal({ id: "p2" })],
      openCount: 4,
      selectedCount: 2,
      backlogCount: 2,
      revertedCount: 0,
    });

    expect(guide.tone).toBe("cyan");
    expect(guide.headline).toContain("2 markiert");
    expect(guide.summary).toContain("nur markierte Karten");
  });

  it("keeps selected risky proposals in manual review guidance", () => {
    const guide = getAutoresearchDecisionGuide({
      visibleProposals: [proposal({ id: "p1", severity: "low" })],
      selectedProposals: [proposal({ id: "backlog-code", mode: "code", severity: "medium", title: "Hidden code patch" })],
      openCount: 3,
      selectedCount: 1,
      backlogCount: 2,
      revertedCount: 0,
    });

    expect(guide.tone).toBe("amber");
    expect(guide.headline).toContain("Einzelreview-Pflicht");
    expect(guide.summary).toContain("Sammel-Übernehmen wäre zu pauschal");
    expect(guide.facts.find((fact) => fact.label === "Einzeln lesen")?.value).toBe("1");
  });

  it("blocks the toolbar batch confirm action for risky selections", () => {
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 2, selectedManualReviewCount: 0, busy: false })).toBe(true);
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 2, selectedManualReviewCount: 1, busy: false })).toBe(false);
    expect(canBatchConfirmAutoresearchSelection({ selectedCount: 0, selectedManualReviewCount: 0, busy: false })).toBe(false);
  });

  it("selects only visible proposals that can be batch-confirmed safely", () => {
    const ids = getBatchSafeVisibleProposalIds([
      proposal({ id: "safe-low", severity: "low" }),
      proposal({ id: "safe-medium", severity: "medium" }),
      proposal({ id: "code", mode: "code", severity: "medium" }),
      proposal({ id: "high-risk", severity: "high" }),
      proposal({ id: "safety", severity: "low", title: "Token safety note" }),
    ]);

    expect(ids).toEqual(["safe-low", "safe-medium"]);
  });

  it("labels the cockpit top card review mode with the same manual-review rule", () => {
    expect(describeTopCardMode(proposal({ id: "safe", severity: "low" }))).toMatchObject({
      label: "Sammel-sicher",
      tone: "emerald",
    });
    expect(describeTopCardMode(proposal({ id: "code", mode: "code", severity: "medium" }))).toMatchObject({
      label: "Einzelreview",
      tone: "amber",
    });
  });

  it("blocks global apply-all when open skill proposals need manual review", () => {
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "safe", severity: "low" })], busy: false })).toBe(true);
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "risky", severity: "high" })], busy: false })).toBe(false);
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "safety", severity: "low", title: "Token safety note" })], busy: false })).toBe(false);
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [proposal({ id: "safe", severity: "low" })], busy: true })).toBe(false);
    expect(canApplyAllOpenSkillProposals({ openSkillProposals: [], busy: false })).toBe(false);
  });
});

describe("AutoresearchView run guidance", () => {
  it("keeps plain-language research-loop presets mapped to backend payload values", () => {
    expect(RESEARCH_LOOP_PRESETS).toHaveLength(3);
    expect(getResearchLoopPreset("recommended")).toMatchObject({
      label: "Empfohlen",
      area: "all",
      focus: "recommended_sections",
      maxIterations: "2",
      minUseCount: "",
    });
    expect(getResearchLoopPreset("popular")).toMatchObject({
      area: "all",
      focus: "recommended_sections",
      maxIterations: "3",
      minUseCount: "10",
    });
    expect(getResearchLoopPreset("dashboard")).toMatchObject({
      area: "dashboard",
      focus: "code_review",
      maxIterations: "2",
    });
  });

  it("detects whether current research-loop values still match a preset", () => {
    expect(getSelectedResearchLoopPresetId({
      area: "all",
      focus: "recommended_sections",
      maxIterations: "2",
      minUseCount: "",
    })).toBe("recommended");
    expect(getSelectedResearchLoopPresetId({
      area: "all",
      focus: "",
      maxIterations: "2",
      minUseCount: "",
    })).toBe("recommended");
    expect(getSelectedResearchLoopPresetId({
      area: "all",
      focus: "recommended_sections",
      maxIterations: "7",
      minUseCount: "",
    })).toBeNull();
  });

  it("warns before starting the research loop when the model route is not ready", () => {
    const guidance = getResearchLoopGuidance({
      running: false,
      routeOk: false,
      maxIterations: 2,
      area: "alle Skills",
    });

    expect(guidance.tone).toBe("amber");
    expect(guidance.label).toBe("Route prüfen");
    expect(guidance.safety).toContain("Erst Route prüfen");
  });

  it("disables research-loop start until the model route is ready", () => {
    expect(getResearchLoopStartControl({ running: false, busy: false, routeOk: false })).toEqual({
      disabled: true,
      label: "Route prüfen",
      title: "Der Research-Loop startet erst, wenn die Modellroute bestätigt ist.",
    });
    expect(getResearchLoopStartControl({ running: true, busy: false, routeOk: true }).label).toBe("Loop läuft");
    expect(getResearchLoopStartControl({ running: false, busy: true, routeOk: true }).label).toBe("Startet...");
    expect(getResearchLoopStartControl({ running: false, busy: false, routeOk: true }).disabled).toBe(false);
  });

  it("explains deep-audit cost and queue-only safety", () => {
    const guidance = getDeepAuditGuidance({ subsystem: "autoresearch", running: false });

    expect(guidance.label).toBe("Teurer Audit");
    expect(guidance.cost).toContain("gezielter Audit");
    expect(guidance.safety).toContain("Queue");
  });

  it("makes test-foundry auto-apply branch safety explicit", () => {
    const guidance = getTestFoundryGuidance({ target: "hermes_state.py", running: false, autoApply: true });

    expect(guidance.tone).toBe("amber");
    expect(guidance.label).toBe("Auto-Apply aktiv");
    expect(guidance.safety).toContain("f-test-foundry");
  });
});

describe("AutoresearchView run summary", () => {
  const baseRun: AutoresearchRun = {
    at: "2026-06-04T20:00:00Z",
    lane: "skill",
    request_id: "r1",
    tokens: 1200,
    proposed: 0,
    scanned: 4,
    errors: 0,
  };

  it("tells the operator to review when the latest run produced proposals", () => {
    const summary = getAutoresearchRunSummary({
      runs: [{ ...baseRun, proposed: 3 }],
      acceptanceRate: 0.6,
      tokensPerApplied: 20_000,
    });

    expect(summary.tone).toBe("emerald");
    expect(summary.label).toBe("Hat geliefert");
    expect(summary.next).toContain("Queue zuerst leeren");
  });

  it("prioritizes error investigation before more runs", () => {
    const summary = getAutoresearchRunSummary({
      runs: [{ ...baseRun, errors: 2 }],
      acceptanceRate: null,
      tokensPerApplied: null,
    });

    expect(summary.tone).toBe("red");
    expect(summary.title).toContain("Fehler prüfen");
    expect(summary.next).toContain("Receipt prüfen");
  });

  it("warns when a run spent many tokens without producing proposals", () => {
    const summary = getAutoresearchRunSummary({
      runs: [{ ...baseRun, tokens: 220_000, proposed: 0 }],
      acceptanceRate: null,
      tokensPerApplied: null,
    });

    expect(summary.tone).toBe("amber");
    expect(summary.label).toBe("Teuer ohne Treffer");
    expect(summary.next).toContain("Scope enger");
  });
});

describe("AutoresearchView keyboard safety", () => {
  it("does not map single-letter apply or skip keys to destructive actions", () => {
    expect(getAutoresearchKeyboardAction({ key: "a", hasTopProposal: true, hasVisibleProposals: true, hasSelection: false })).toBeNull();
    expect(getAutoresearchKeyboardAction({ key: "s", hasTopProposal: true, hasVisibleProposals: true, hasSelection: false })).toBeNull();
  });

  it("keeps keyboard shortcuts to selection-only actions", () => {
    expect(getAutoresearchKeyboardAction({ key: "t", hasTopProposal: true, hasVisibleProposals: true, hasSelection: false })).toBe("select-top");
    expect(getAutoresearchKeyboardAction({ key: "v", hasTopProposal: true, hasVisibleProposals: true, hasSelection: false })).toBe("select-visible");
    expect(getAutoresearchKeyboardAction({ key: "Escape", hasTopProposal: true, hasVisibleProposals: true, hasSelection: true })).toBe("clear-selection");
  });
});
