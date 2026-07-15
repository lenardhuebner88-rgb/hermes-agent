import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { getAutoresearchActionPlan } from "../lib/autoresearchActionPlan";
import { AUTORESEARCH_ADVANCED_GUIDE } from "../lib/autoresearchAdvanced";
import { getAutoresearchActivityCard } from "../lib/autoresearchActivity";
import { runLaneLabel, runLaneTone } from "../lib/autoresearch";
import { getAutoresearchRecommendation } from "../lib/autoresearchRecommendation";
import { getAutoresearchKeyboardAction } from "../lib/autoresearchKeyboard";
import { AUTORESEARCH_SECTION_NAV } from "../lib/autoresearchNavigation";
import { filterAutoresearchQueueByMode, getAutoresearchEmptyQueueModeGuidance, getAutoresearchQueueModeSummary } from "../lib/autoresearchQueueMode";
import { getAutoresearchResolvedSummary } from "../lib/autoresearchResolvedSummary";
import { getAutoresearchReviewFlow } from "../lib/autoresearchReviewFlow";
import { getAutoresearchReadiness } from "../lib/autoresearchReadiness";
import { canApplyAllOpenSkillProposals, canBatchConfirmAutoresearchSelection, describeTopCardMode, getAutoresearchDecisionGuide, getAutoresearchQueueActionSummary, getBatchSafeVisibleProposalIds } from "../lib/autoresearchDecisionGuide";
import { getAdvancedRunChecklist, getDeepAuditGuidance, getResearchLoopGuidance, getResearchLoopPreset, getResearchLoopStartChecklist, getResearchLoopStartControl, getResearchLoopStartSummary, getSelectedResearchLoopPresetId, RESEARCH_LOOP_PRESETS, getTestFoundryGuidance } from "../lib/autoresearchRunGuidance";
import { getAutoresearchLastRunBrief, getAutoresearchRunCard, getAutoresearchRunSummary } from "../lib/autoresearchRunSummary";
import { getTestFoundryResultSummary } from "../lib/autoresearchTestFoundrySummary";
import { getProposalOperatorBrief } from "../lib/autoresearchProposalBrief";
import { DeepAuditFindings, LatestActivityPanel } from "./AutoresearchView";
import { LastRun } from "./autoresearch/panels";
import { OutcomePanel } from "./autoresearch/OutcomePanel";
import { de } from "../i18n/de";
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

  it("renders structured findings as an operator-readable audit report", () => {
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
    expect(html).toContain("Audit-Ergebnis");
    expect(html).toContain("1 prüfbares Risiko gefunden.");
    expect(html).toContain("Hoch");
    expect(html).toContain("Bug-Risiko");
    expect(html).toContain("hermes_cli/autoresearch_runs.py:23");
    expect(html).toContain("_VALID_LANES");
    expect(html).toContain("1 Karte");
    expect(html).toContain("Run lane omitted");
    expect(html).toContain("Fix-Hinweis");
    expect(html).toContain("Beleg anzeigen");
  });

  it("summarizes the highest-risk finding as the most important point", () => {
    const lowFinding: DeepAuditFinding = {
      fileline: "skills/example/SKILL.md:10",
      severity: "low",
      category: "missing_section",
      title: "Minor copy gap",
      problem: "A hint is short.",
      evidence: "hint",
      fix_hint: "Add one sentence.",
    };
    const criticalFinding: DeepAuditFinding = {
      fileline: "gateway/run.py:88",
      severity: "critical",
      category: "security",
      title: "Token leak risk",
      problem: "A token can be exposed.",
      evidence: "token",
      fix_hint: "Redact before logging.",
    };

    const html = renderToStaticMarkup(<DeepAuditFindings findings={[lowFinding, criticalFinding]} proposals={[]} />);

    expect(html).toContain("Kritisch");
    expect(html).toContain("Wichtigster Punkt: Token leak risk");
  });

  it("keeps proposal-only audit responses from looking empty", () => {
    const html = renderToStaticMarkup(<DeepAuditFindings findings={[]} proposals={["deep-audit-x", "deep-audit-y"]} />);

    expect(html).toContain("Noch keine Deep-Audit-Findings.");
    expect(html).toContain("2 Karten");
    expect(html).toContain("Detail-Findings sind in dieser Antwort nicht enthalten");
  });

  it("renders the technical counter and audit branches without the legacy panel vocabulary", () => {
    const finding: DeepAuditFinding = {
      fileline: "gateway/run.py:88",
      severity: "critical",
      category: "security",
      title: "Token leak risk",
      problem: "A token can be exposed.",
      evidence: "token",
      fix_hint: "Redact before logging.",
    };
    const status = {
      state: "idle" as const,
      pid: null,
      request_id: "req-42",
      iteration: 0,
      max: 2,
      last_step: "eval",
      last_eval: "ok",
      route_status: "configured",
      heartbeat_age_s: 4,
      heartbeat_fresh: true,
      last_receipt: "reports/run-42.md",
      last_run: { proposed: 4, kept: 2, reverted: 1, skills_researched: 7, research_errors: 2, skills_with_findings: 3, research_tokens: 12345 },
      note: "fertig",
    };
    const html = [
      renderToStaticMarkup(<LastRun status={status} latestRun={null} />),
      renderToStaticMarkup(<DeepAuditFindings findings={[finding]} proposals={["deep-audit-x"]} />),
    ].join("");

    expect(html).toContain("font-data");
    expect(html).toContain("text-status-alert");
    for (const legacy of ["hc-eyebrow", "hc-mono", "white/10", "black/20", "red-500", "zinc-", "StatusPill", "ToneCallout"]) {
      expect(html).not.toContain(legacy);
    }
  });
});

describe("AutoresearchView cockpit recommendation", () => {
  it("keeps cockpit section navigation anchored to the main operator areas", () => {
    expect(AUTORESEARCH_SECTION_NAV.map((item) => item.id)).toEqual([
      "autoresearch-queue",
      "autoresearch-loop",
      "autoresearch-history",
      "autoresearch-advanced",
    ]);
    expect(AUTORESEARCH_SECTION_NAV.map((item) => item.label)).toEqual(["Entscheidungen", "Probelauf", "Verlauf", "Erweitert"]);
  });

  it("sends the operator to review when actionable proposals exist", () => {
    const recommendation = getAutoresearchRecommendation({
      state: "idle",
      openCount: 4,
      revertedCount: 1,
      loopRunning: false,
      routeStatus: "configured",
    });

    expect(recommendation.kind).toBe("review");
    expect(recommendation.primaryLabel).toBe("Entscheidungen prüfen");
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

  it("keeps the cockpit top-card brief readable for technical code proposals", () => {
    const brief = getProposalOperatorBrief(proposal({
      id: "top-code",
      mode: "code",
      severity: "high",
      category: "bug_risk",
      target: "hermes_cli/web_server.py:6420",
      title: "Deep-Audit in hermes_cli/web_server.py:6420: subprocess.Popen with shell-built start and osascript",
      rationale_plain: "subprocess.Popen is called with shell-built commands.",
    }));

    expect(brief).toMatchObject({
      tone: "amber",
      label: "Code mit Gate",
      title: "Erst lesen, dann übernehmen.",
    });
    expect(brief.summary).toContain("Gate");
    expect(brief.facts.find((fact) => fact.label === "Betroffen")?.value).toContain("hermes_cli/web_server.py");
    expect(brief.facts.find((fact) => fact.label === "Klick")?.value).toContain("Code-Änderung plus Test-Gate");
  });
});

describe("AutoresearchView cockpit readiness", () => {
  it("keeps the cockpit in route-check mode until the model route is confirmed", () => {
    const readiness = getAutoresearchReadiness({
      state: "idle",
      routeStatus: "unavailable",
      heartbeatFresh: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      busy: false,
    });

    expect(readiness.tone).toBe("amber");
    expect(readiness.label).toBe("Route prüfen");
    expect(readiness.next).toContain("Modellroute prüfen");
    expect(readiness.facts.find((fact) => fact.label === "Route")).toMatchObject({
      value: "unavailable",
      tone: "amber",
    });
  });

  it("prioritizes crashed loop recovery over queue or start guidance", () => {
    const readiness = getAutoresearchReadiness({
      state: "crashed",
      routeStatus: "configured",
      heartbeatFresh: false,
      loopRunning: false,
      openCount: 2,
      highPriorityCount: 1,
      busy: false,
    });

    expect(readiness.tone).toBe("red");
    expect(readiness.label).toBe("Fehler prüfen");
    expect(readiness.next).toContain("Status");
  });

  it("shows monitor mode while the loop is running", () => {
    const readiness = getAutoresearchReadiness({
      state: "running",
      routeStatus: "configured",
      heartbeatFresh: true,
      loopRunning: true,
      openCount: 0,
      highPriorityCount: 0,
      busy: false,
    });

    expect(readiness.tone).toBe("cyan");
    expect(readiness.label).toBe("Lauf aktiv");
    expect(readiness.next).toContain("Loop beobachten");
  });

  it("turns an open high-priority queue into explicit review readiness", () => {
    const readiness = getAutoresearchReadiness({
      state: "idle",
      routeStatus: "configured",
      heartbeatFresh: true,
      loopRunning: false,
      openCount: 5,
      highPriorityCount: 2,
      busy: false,
    });

    expect(readiness.tone).toBe("amber");
    expect(readiness.label).toBe("Review bereit");
    expect(readiness.title).toContain("wichtigen Karten");
    expect(readiness.facts.find((fact) => fact.label === "Hoch+")).toMatchObject({
      value: "2",
      tone: "amber",
    });
  });

  it("offers a small dry-run only when route, loop, queue, and busy state are calm", () => {
    const readiness = getAutoresearchReadiness({
      state: "idle",
      routeStatus: "configured",
      heartbeatFresh: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      busy: false,
    });

    expect(readiness.tone).toBe("emerald");
    expect(readiness.label).toBe("Betriebsbereit");
    expect(readiness.next).toContain("Dry-Run starten");
  });

  it("keeps running cockpit actions visible before suggesting the next operation", () => {
    const readiness = getAutoresearchReadiness({
      state: "idle",
      routeStatus: "configured",
      heartbeatFresh: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      busy: true,
    });

    expect(readiness.tone).toBe("violet");
    expect(readiness.label).toBe("Aktion läuft");
    expect(readiness.facts.find((fact) => fact.label === "Loop")).toMatchObject({
      value: "aktualisiert",
      tone: "violet",
    });
  });
});

describe("AutoresearchView action plan", () => {
  it("discourages new starts while high-priority queue work is open", () => {
    const plan = getAutoresearchActionPlan({
      routeOk: true,
      loopRunning: false,
      openCount: 5,
      highPriorityCount: 2,
      openSkillCount: 1,
      openSkillManualReviewCount: 1,
      revertedCount: 0,
      storeBusy: false,
      pruneBusy: false,
    });

    expect(plan.generate).toMatchObject({ label: "Erst Hoch+", tone: "amber" });
    expect(plan.scan.reason).toContain("Review-Stau");
    expect(plan.applySkills).toMatchObject({ label: "Einzelreview", tone: "amber" });
  });

  it("recommends a small skill generation when route, loop, and queue are calm", () => {
    const plan = getAutoresearchActionPlan({
      routeOk: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      openSkillCount: 0,
      openSkillManualReviewCount: 0,
      revertedCount: 0,
      storeBusy: false,
      pruneBusy: false,
    });

    expect(plan.generate).toMatchObject({ label: "Empfohlen", tone: "emerald" });
    expect(plan.scan).toMatchObject({ label: "Optional", tone: "cyan" });
    expect(plan.applySkills.label).toBe("Nichts offen");
  });

  it("puts start actions behind route recovery", () => {
    const plan = getAutoresearchActionPlan({
      routeOk: false,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      openSkillCount: 0,
      openSkillManualReviewCount: 0,
      revertedCount: 2,
      storeBusy: false,
      pruneBusy: false,
    });

    expect(plan.generate).toMatchObject({ label: "Route prüfen", tone: "amber" });
    expect(plan.scan.after).toContain("Route stabilisieren");
    expect(plan.prune).toMatchObject({ label: "Aufräumen", tone: "emerald" });
  });

  it("locks all action explanations while any cockpit action is busy", () => {
    const plan = getAutoresearchActionPlan({
      routeOk: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      openSkillCount: 0,
      openSkillManualReviewCount: 0,
      revertedCount: 0,
      storeBusy: true,
      pruneBusy: false,
    });

    expect(Object.values(plan).map((item) => item.label)).toEqual(["Warten", "Warten", "Warten", "Warten"]);
    expect(plan.generate.reason).toContain("Aktion läuft");
  });

  it("keeps ordinary actions available in the plan while only cleanup is busy", () => {
    const plan = getAutoresearchActionPlan({
      routeOk: true,
      loopRunning: false,
      openCount: 0,
      highPriorityCount: 0,
      openSkillCount: 0,
      openSkillManualReviewCount: 0,
      revertedCount: 3,
      storeBusy: false,
      pruneBusy: true,
    });

    expect(plan.generate.label).toBe("Empfohlen");
    expect(plan.scan.label).toBe("Optional");
    expect(plan.prune.label).toBe("Warten");
  });
});

describe("AutoresearchView queue modes", () => {
  const safe = proposal({ id: "safe", severity: "low", mode: "skill", title: "Safe skill" });
  const high = proposal({ id: "high", severity: "high", mode: "skill", title: "High skill" });
  const code = proposal({ id: "code", severity: "medium", mode: "code", title: "Code patch" });
  const safety = proposal({ id: "safety", severity: "low", mode: "skill", title: "Token safety note" });
  const proposals = [safe, high, code, safety];

  it("builds operator review modes with counts and plain labels", () => {
    const summary = getAutoresearchQueueModeSummary(proposals, "manual");

    expect(summary.active.label).toBe("Einzelreview");
    expect(summary.options.map((option) => [option.id, option.count])).toEqual([
      ["all", 4],
      ["high", 1],
      ["manual", 3],
      ["safe", 1],
    ]);
    expect(summary.options.find((option) => option.id === "all")?.detail).toBe("Zeigt alle offenen Karten in sinnvoller Reihenfolge.");
  });

  it("filters the queue by risk and review style without dropping the source queue", () => {
    expect(filterAutoresearchQueueByMode(proposals, "all").map((item) => item.id)).toEqual(["safe", "high", "code", "safety"]);
    expect(filterAutoresearchQueueByMode(proposals, "high").map((item) => item.id)).toEqual(["high"]);
    expect(filterAutoresearchQueueByMode(proposals, "manual").map((item) => item.id)).toEqual(["high", "code", "safety"]);
    expect(filterAutoresearchQueueByMode(proposals, "safe").map((item) => item.id)).toEqual(["safe"]);
  });

  it("guides operators out of empty queue filter modes", () => {
    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary([safe], "high"))).toMatchObject({
      tone: "cyan",
      label: "Kein Hoch+",
      primaryMode: "safe",
      primaryLabel: "Sammel-sicher zeigen",
    });

    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary([safe], "manual"))).toMatchObject({
      tone: "emerald",
      label: "Sammel-sicher",
      primaryMode: "safe",
    });

    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary([high, code], "safe"))).toMatchObject({
      tone: "amber",
      label: "Erst lesen",
      primaryMode: "manual",
      detail: expect.stringContaining("Einzelreview"),
    });
    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary([high, code], "safe"))?.detail).not.toContain("Queue");
  });

  it("does not show empty-filter guidance for non-empty or fully empty queues", () => {
    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary(proposals, "all"))).toBeNull();
    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary(proposals, "manual"))).toBeNull();
    expect(getAutoresearchEmptyQueueModeGuidance(getAutoresearchQueueModeSummary([], "safe"))).toBeNull();
  });
});

describe("AutoresearchView activity timeline", () => {
  it("turns raw activity entries into operator-readable cards", () => {
    expect(getAutoresearchActivityCard({ at: 1, text: "Batch übernommen", tone: "emerald" })).toMatchObject({
      label: "Erledigt",
      title: "Die Aktion ist abgeschlossen.",
      detail: "Batch übernommen",
    });
    expect(getAutoresearchActivityCard({ at: 2, text: "Loop fehlgeschlagen", tone: "red" })).toMatchObject({
      label: "Fehler",
      next: expect.stringContaining("erneut"),
    });
    expect(getAutoresearchActivityCard({ at: 3, text: "Auswahl enthält Risiko", tone: "amber" })).toMatchObject({
      label: "Achtung",
      next: expect.stringContaining("prüfen"),
    });
    expect(getAutoresearchActivityCard({ at: 4, text: "Quelle geprüft", tone: "violet" })).toMatchObject({
      label: "Info",
      tone: "cyan",
    });
  });

  it("renders the latest activity as an operator status card", () => {
    const card = getAutoresearchActivityCard({ at: 1_780_653_600, text: "Batch übernommen", tone: "emerald" });
    const html = renderToStaticMarkup(<LatestActivityPanel at={1_780_653_600} card={card} />);

    expect(html).toContain("Letzte Autoresearch Aktion");
    expect(html).toContain("Letzte Aktion");
    expect(html).toContain("Erledigt");
    expect(html).toContain("Die Aktion ist abgeschlossen.");
    expect(html).toContain("Batch übernommen");
    expect(html).toContain("Jetzt sinnvoll:");
    expect(html).toContain("Weiter mit dem nächsten sicheren Schritt");
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

  it("frames an empty decision area without queue jargon", () => {
    const guide = getAutoresearchDecisionGuide({
      visibleProposals: [],
      selectedProposals: [],
      openCount: 0,
      selectedCount: 0,
      backlogCount: 0,
      revertedCount: 0,
    });

    expect(guide.headline).toContain("Nichts offen");
    expect(guide.summary).toContain("keine offene Entscheidung");
    expect(guide.summary).not.toContain("Queue ist leer");
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

  it("explains mixed queue actions as safe batch plus manual review", () => {
    const summary = getAutoresearchQueueActionSummary({
      visibleCount: 5,
      batchSafeVisibleCount: 3,
      manualReviewVisibleCount: 2,
      selectedCount: 0,
      selectedManualReviewCount: 0,
    });

    expect(summary.title).toBe("Zwei Wege: sichere sammeln, riskante einzeln.");
    expect(summary.batchLine).toContain("3 sichtbare Karten");
    expect(summary.manualLine).toContain("2 sichtbare Karten bleiben");
    expect(summary.confirmLine).toContain("Erst sichere Karten markieren");
    expect(summary.facts.find((fact) => fact.label === "Einzelreview")?.tone).toBe("amber");
  });

  it("explains that a safe selected batch affects only marked cards", () => {
    const summary = getAutoresearchQueueActionSummary({
      visibleCount: 4,
      batchSafeVisibleCount: 4,
      manualReviewVisibleCount: 0,
      selectedCount: 2,
      selectedManualReviewCount: 0,
    });

    expect(summary.tone).toBe("cyan");
    expect(summary.title).toContain("Sammel-Übernehmen bereit");
    expect(summary.batchLine).toContain("2 sichere Karten");
    expect(summary.confirmLine).toContain("nur auf die markierten Karten");
  });

  it("keeps an all-manual queue out of the batch path", () => {
    const summary = getAutoresearchQueueActionSummary({
      visibleCount: 2,
      batchSafeVisibleCount: 0,
      manualReviewVisibleCount: 2,
      selectedCount: 0,
      selectedManualReviewCount: 0,
    });

    expect(summary.tone).toBe("amber");
    expect(summary.title).toBe("Heute ist Einzelreview dran.");
    expect(summary.batchLine).toContain("Keine sichtbare Karte");
    expect(summary.confirmLine).toContain("Top-Karte");
  });

  it("explains an empty visible queue without enabling batch action", () => {
    const summary = getAutoresearchQueueActionSummary({
      visibleCount: 0,
      batchSafeVisibleCount: 0,
      manualReviewVisibleCount: 0,
      selectedCount: 0,
      selectedManualReviewCount: 0,
    });

    expect(summary.tone).toBe("zinc");
    expect(summary.title).toContain("Keine sichtbare Karte");
    expect(summary.batchLine).toContain("nichts zu markieren");
    expect(summary.confirmLine).toContain("bleibt aus");
  });

  it("explains selected manual-review cards as blocked for batch confirmation", () => {
    const summary = getAutoresearchQueueActionSummary({
      visibleCount: 3,
      batchSafeVisibleCount: 2,
      manualReviewVisibleCount: 1,
      selectedCount: 2,
      selectedManualReviewCount: 1,
    });

    expect(summary.tone).toBe("amber");
    expect(summary.title).toContain("nicht sammelsicher");
    expect(summary.batchLine).toContain("1 davon brauchen Einzelreview");
    expect(summary.confirmLine).toContain("bleibt gesperrt");
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
  it("keeps advanced controls framed as optional specialist actions", () => {
    expect(AUTORESEARCH_ADVANCED_GUIDE.map((item) => item.kind)).toEqual(["models", "deep-audit", "test-foundry"]);
    expect(AUTORESEARCH_ADVANCED_GUIDE.find((item) => item.kind === "deep-audit")?.cost).toContain("1-2 Mio Token");
    expect(AUTORESEARCH_ADVANCED_GUIDE.find((item) => item.kind === "test-foundry")?.safety).toContain("separatem Branch");
    expect(AUTORESEARCH_ADVANCED_GUIDE.find((item) => item.kind === "models")?.safety).toContain("Offene Karten");
  });

  it("keeps plain-language research-loop presets mapped to backend payload values", () => {
    expect(RESEARCH_LOOP_PRESETS).toHaveLength(3);
    expect(getResearchLoopPreset("recommended")).toMatchObject({
      label: "Empfohlen",
      operatorTitle: "Guter Standardlauf",
      area: "all",
      focus: "recommended_sections",
      maxIterations: "2",
      minUseCount: "",
    });
    expect(getResearchLoopPreset("popular")).toMatchObject({
      operatorTitle: "Weniger Rauschen",
      area: "all",
      focus: "recommended_sections",
      maxIterations: "3",
      minUseCount: "10",
    });
    expect(getResearchLoopPreset("dashboard")).toMatchObject({
      operatorTitle: "Dashboard prüfen",
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

  it("summarizes selected research-loop presets without exposing raw controls first", () => {
    const summary = getResearchLoopStartSummary({
      selectedPresetId: "popular",
      areaLabel: "alle Skills",
      focus: "recommended_sections",
      maxIterations: 3,
      minUseCount: 10,
    });

    expect(summary.title).toBe("Weniger Rauschen");
    expect(summary.scope).toContain("viel genutzte Skills");
    expect(summary.detail).toContain("relevantere Karten");
    expect(summary.cost).toContain("Nutzung >= 10");
    expect(summary.safety).toContain("Dry-Run");
    expect(summary.technicalLabel).toBe("all · recommended_sections");
  });

  it("keeps custom research-loop summaries explicit when values no longer match a preset", () => {
    const summary = getResearchLoopStartSummary({
      selectedPresetId: null,
      areaLabel: "Dashboard",
      focus: "code_review",
      maxIterations: 5,
      minUseCount: null,
    });

    expect(summary.title).toBe("Eigene Feinsteuerung");
    expect(summary.scope).toContain("Dashboard");
    expect(summary.detail).toContain("code_review");
    expect(summary.cost).toBe("5 Iterationen maximal.");
    expect(summary.technicalLabel).toBe("Manuelle Werte");
  });

  it("warns in the start checklist when high-priority decisions should happen first", () => {
    const checklist = getResearchLoopStartChecklist({
      routeOk: true,
      running: false,
      busy: false,
      selectedPresetId: "recommended",
      maxIterations: 2,
      openCount: 20,
      highPriorityCount: 7,
    });

    expect(checklist).toMatchObject({
      tone: "amber",
      label: "Erst Review",
    });
    expect(checklist.detail).toContain("Hoch+-Karten haben Vorrang");
    expect(checklist.items.find((item) => item.label === "Entscheidungswirkung")).toMatchObject({
      value: "7 Hoch+ offen",
      tone: "amber",
    });
  });

  it("keeps start checklist route, safety, and custom values explicit", () => {
    expect(getResearchLoopStartChecklist({
      routeOk: false,
      running: false,
      busy: false,
      selectedPresetId: "recommended",
      maxIterations: 2,
      openCount: 0,
      highPriorityCount: 0,
    })).toMatchObject({
      tone: "amber",
      label: "Nicht starten",
      items: [
        expect.objectContaining({ label: "Startsignal", value: "Route fehlt" }),
        expect.objectContaining({ label: "Entscheidungswirkung", value: "leer" }),
        expect.objectContaining({ label: "Sicherheit", value: "Empfohlen" }),
      ],
    });

    expect(getResearchLoopStartChecklist({
      routeOk: true,
      running: false,
      busy: false,
      selectedPresetId: null,
      maxIterations: 8,
      openCount: 0,
      highPriorityCount: 0,
    }).items.find((item) => item.label === "Sicherheit")).toMatchObject({
      value: "eigene Werte",
      tone: "amber",
    });
  });

  it("keeps start checklist headline aligned with disabled or in-flight start states", () => {
    expect(getResearchLoopStartChecklist({
      routeOk: true,
      running: false,
      busy: true,
      selectedPresetId: "recommended",
      maxIterations: 2,
      openCount: 0,
      highPriorityCount: 0,
    })).toMatchObject({
      tone: "violet",
      label: "Startet",
      detail: expect.stringContaining("Startsignal ist unterwegs"),
    });

    expect(getResearchLoopStartChecklist({
      routeOk: false,
      running: false,
      busy: false,
      selectedPresetId: "recommended",
      maxIterations: 2,
      openCount: 20,
      highPriorityCount: 7,
    })).toMatchObject({
      tone: "amber",
      label: "Nicht starten",
      detail: expect.stringContaining("Route"),
    });
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

  it("explains deep-audit cost and review-card safety", () => {
    const guidance = getDeepAuditGuidance({ subsystem: "autoresearch", running: false });

    expect(guidance.label).toBe("Teurer Audit");
    expect(guidance.cost).toContain("gezielter Audit");
    expect(guidance.safety).toContain("Karte");
  });

  it("adds explicit start checks for expensive deep-audit runs", () => {
    const checklist = getAdvancedRunChecklist({
      kind: "deep-audit",
      target: "autoresearch",
      running: false,
      busy: false,
    });

    expect(checklist).toMatchObject({
      tone: "amber",
      label: "Teuer",
      title: "Check vor Deep-Audit",
    });
    expect(checklist.items.find((item) => item.label === "Wirkung")).toMatchObject({
      value: "nur Karten",
      tone: "emerald",
    });
    expect(checklist.items.find((item) => item.label === "Aufwand")).toMatchObject({
      value: "sehr hoch",
      tone: "amber",
    });
  });

  it("makes test-foundry auto-apply branch safety explicit", () => {
    const guidance = getTestFoundryGuidance({ target: "hermes_state.py", running: false, autoApply: true });

    expect(guidance.tone).toBe("amber");
    expect(guidance.label).toBe("Auto-Apply aktiv");
    expect(guidance.safety).toContain("f-test-foundry");
  });

  it("keeps test-foundry start checks distinct for review-card and branch-gated modes", () => {
    expect(getAdvancedRunChecklist({
      kind: "test-foundry",
      target: "hermes_state.py",
      running: false,
      busy: false,
      autoApply: false,
    })).toMatchObject({
      tone: "emerald",
      label: "Vorschlags-sicher",
      detail: expect.stringContaining("Vorschlagsmodus"),
    });

    expect(getAdvancedRunChecklist({
      kind: "test-foundry",
      target: "hermes_state.py",
      running: false,
      busy: false,
      autoApply: true,
    })).toMatchObject({
      tone: "amber",
      label: "Branch-Gate",
      detail: expect.stringContaining("separaten Branch"),
    });
  });

  it("summarizes successful test-foundry queue proposals without claiming a branch write", () => {
    const summary = getTestFoundryResultSummary({
      ok: true,
      target: "hermes_state.py",
      tests_kept: 2,
      proposals: ["p1", "p2"],
      mutants_run: 8,
      survivors: [{ lineno: 10 }],
      tokens: 3200,
      model: "test-model",
    });

    expect(summary).toMatchObject({
      tone: "emerald",
      label: "Karten bereit",
      title: "2 Tests wurden validiert.",
    });
    expect(summary?.detail).toContain("2 Karten");
    expect(summary?.next).toContain("Karten prüfen");
    expect(summary?.facts.find((fact) => fact.label === "Target")?.value).toBe("hermes_state.py");
  });

  it("summarizes branch-gated test-foundry apply results explicitly", () => {
    const summary = getTestFoundryResultSummary({
      ok: true,
      target: "hermes_state.py",
      tests_kept: 1,
      proposals: ["p1"],
      mutants_run: 5,
      tokens: 1800,
      apply_branch: "f-test-foundry",
      apply_commit: "abcdef123456",
    });

    expect(summary).toMatchObject({
      tone: "emerald",
      label: "Branch bereit",
      title: "1 Test wurde validiert.",
    });
    expect(summary?.detail).toContain("f-test-foundry");
    expect(summary?.next).toContain("Branch prüfen");
  });

  it("surfaces failed branch-gate apply results before queue success", () => {
    const summary = getTestFoundryResultSummary({
      ok: true,
      target: "hermes_state.py",
      tests_kept: 2,
      proposals: ["p1", "p2"],
      apply_result: {
        ok: false,
        detail: "regression gate failed",
      },
    });

    expect(summary).toMatchObject({
      tone: "amber",
      label: "Branch-Gate fehlgeschlagen",
      title: "2 Tests wurden validiert.",
      detail: "regression gate failed",
    });
    expect(summary?.next).toContain("Gate-Ausgabe prüfen");
  });

  it("does not invent a kept-test count when test-foundry omits it", () => {
    const summary = getTestFoundryResultSummary({
      ok: true,
      target: "hermes_state.py",
      proposals: ["p1"],
    });

    expect(summary).toMatchObject({
      tone: "emerald",
      label: "Karten bereit",
      title: "Test-Foundry-Lauf erfolgreich.",
    });
    expect(summary?.facts.find((fact) => fact.label === "Tests")?.value).toBe("n/v");
  });

  it("keeps no-kept-test-foundry runs actionable instead of looking successful", () => {
    const summary = getTestFoundryResultSummary({
      ok: false,
      target: "hermes_state.py",
      tests_kept: 0,
      proposals: [],
      mutants_run: 4,
      reason: "affected baseline tests failed",
    });

    expect(summary).toMatchObject({
      tone: "amber",
      label: "Nichts behalten",
      title: "Lauf hat keinen sicheren Test behalten.",
    });
    expect(summary?.detail).toBe("affected baseline tests failed");
    expect(summary?.next).toContain("Baseline prüfen");
  });

  it("blocks advanced run checklist readiness until a target is selected", () => {
    const checklist = getAdvancedRunChecklist({
      kind: "test-foundry",
      target: "",
      running: false,
      busy: false,
      autoApply: false,
    });

    expect(checklist).toMatchObject({
      tone: "amber",
      label: "Ziel fehlt",
    });
    expect(checklist.items.find((item) => item.label === "Startsignal")).toMatchObject({
      value: "Target fehlt",
      tone: "amber",
    });
  });

  it("keeps advanced run checklist headline aligned with running and busy states", () => {
    expect(getAdvancedRunChecklist({
      kind: "deep-audit",
      target: "autoresearch",
      running: true,
      busy: false,
    })).toMatchObject({
      tone: "cyan",
      label: "Läuft",
      detail: expect.stringContaining("aktiv"),
    });

    expect(getAdvancedRunChecklist({
      kind: "test-foundry",
      target: "hermes_state.py",
      running: false,
      busy: true,
      autoApply: false,
    })).toMatchObject({
      tone: "violet",
      label: "Startet",
      detail: expect.stringContaining("Startsignal ist unterwegs"),
    });
  });
});

describe("AutoresearchView resolved work summary", () => {
  it("stays hidden while no resolved work exists", () => {
    expect(getAutoresearchResolvedSummary({
      reverted: [],
      applied: [],
      skipped: [],
    })).toBeNull();
  });

  it("prioritizes reverted cards and exposes a cleanup action", () => {
    const summary = getAutoresearchResolvedSummary({
      reverted: [proposal({ id: "r1", last_outcome: "reverted_no_improvement" })],
      applied: [proposal({ id: "a1", status: "applied" })],
      skipped: [proposal({ id: "s1", status: "skipped" })],
    });

    expect(summary).toMatchObject({
      tone: "amber",
      label: "Aufräumen",
      archiveLabel: "Karte archivieren",
    });
    expect(summary?.next).toContain("archivieren");
    expect(summary?.facts.map((fact) => [fact.label, fact.value])).toEqual([
      ["Zurückgerollt", "1"],
      ["Übernommen", "1"],
      ["Übersprungen", "1"],
    ]);
  });

  it("summarizes applied-only work without archive action", () => {
    expect(getAutoresearchResolvedSummary({
      reverted: [],
      applied: [proposal({ id: "a1", status: "applied" }), proposal({ id: "a2", status: "applied" })],
      skipped: [],
    })).toMatchObject({
      tone: "emerald",
      label: "Erledigt",
      archiveLabel: null,
      title: "2 Vorschläge wurden übernommen.",
    });
  });

  it("summarizes skipped-only work as intentionally sorted out", () => {
    expect(getAutoresearchResolvedSummary({
      reverted: [],
      applied: [],
      skipped: [proposal({ id: "s1", status: "skipped" })],
    })).toMatchObject({
      tone: "zinc",
      label: "Aussortiert",
      archiveLabel: null,
      title: "1 Vorschlag wurde übersprungen.",
    });
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
    expect(summary.next).toContain("Offene Karten zuerst entscheiden");
    expect(summary.facts.map((fact) => fact.label)).toEqual(["Aufwand", "Treffer", "Fehler", "Quote", "Aufwand/OK"]);
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

  it("turns individual runs into readable operator cards", () => {
    expect(getAutoresearchRunCard({ ...baseRun, proposed: 2 })).toMatchObject({
      tone: "emerald",
      label: "Geliefert",
      title: "2 neue Karten zur Prüfung.",
    });
    expect(getAutoresearchRunCard({ ...baseRun, proposed: 2 }).facts.find((fact) => fact.label === "Aufwand")).toBeTruthy();
    expect(getAutoresearchRunCard({ ...baseRun, errors: 1 })).toMatchObject({
      tone: "red",
      label: "Fehler",
    });
    expect(getAutoresearchRunCard({ ...baseRun, tokens: 180_000, proposed: 0 })).toMatchObject({
      tone: "amber",
      label: "Teuer ruhig",
    });
  });

  it("prioritizes run-card errors over delivered proposals", () => {
    expect(getAutoresearchRunCard({ ...baseRun, proposed: 3, errors: 1 })).toMatchObject({
      tone: "red",
      label: "Fehler",
      title: "1 Fehler im Lauf.",
    });
  });

  it("turns a raw last-run error into an operator recovery brief", () => {
    const brief = getAutoresearchLastRunBrief({
      lastRun: {
        mode: "dry-run",
        research_errors: 2,
        research_tokens: 2400,
      },
      latestRun: null,
      receipt: "receipt-123",
    });

    expect(brief).toMatchObject({
      tone: "red",
      label: "Fehler prüfen",
    });
    expect(brief.next).toContain("Receipt");
    expect(brief.rawLine).toContain("dry-run");
    expect(brief.rawLine).toContain("receipt-123");
  });

  it("summarizes delivered last-run proposals without exposing counters first", () => {
    const brief = getAutoresearchLastRunBrief({
      lastRun: {
        mode: "deep-audit",
        finished_at: "2026-06-04T20:15:00Z",
        proposed: 2,
        kept: 1,
        reverted: 1,
      },
      latestRun: null,
    });

    expect(brief).toMatchObject({
      tone: "emerald",
      label: "Hat geliefert",
      title: "2 neue Karten zur Prüfung.",
    });
    expect(brief.detail).toContain("1 übernommen");
    expect(brief.rawLine).toContain("deep-audit");
  });

  it("keeps refused and stopped last-run outcomes distinct from errors", () => {
    expect(getAutoresearchLastRunBrief({
      lastRun: { refused: "Route ist nicht konfiguriert." },
      latestRun: { ...baseRun, errors: 1 },
    })).toMatchObject({
      tone: "amber",
      label: "Abgelehnt",
      detail: "Route ist nicht konfiguriert.",
    });

    expect(getAutoresearchLastRunBrief({
      lastRun: { stopped: true },
      latestRun: null,
    })).toMatchObject({
      tone: "cyan",
      label: "Gestoppt",
    });
  });

  it("does not let stale run history override a structured last-run payload", () => {
    expect(getAutoresearchLastRunBrief({
      lastRun: { research_errors: 2 },
      latestRun: { ...baseRun, errors: 0, tokens: 180_000 },
    })).toMatchObject({
      tone: "red",
      label: "Fehler prüfen",
    });

    expect(getAutoresearchLastRunBrief({
      lastRun: { refused: "Route fehlt." },
      latestRun: { ...baseRun, errors: 3 },
    })).toMatchObject({
      tone: "amber",
      label: "Abgelehnt",
      detail: "Route fehlt.",
    });
  });

  it("uses the latest run as fallback when the backend has no structured last-run payload", () => {
    expect(getAutoresearchLastRunBrief({
      lastRun: null,
      latestRun: { ...baseRun, tokens: 180_000, proposed: 0 },
    })).toMatchObject({
      tone: "amber",
      label: "Teuer ruhig",
      title: "Viel Aufwand ohne neue Karten.",
    });

    const calmBrief = getAutoresearchLastRunBrief({
      lastRun: undefined,
      latestRun: { ...baseRun, scanned: 4, tokens: 1200, proposed: 0 },
    });

    expect(calmBrief).toMatchObject({
      tone: "cyan",
      label: "Ruhig",
    });
    expect(calmBrief.detail).toContain("4 Ziele geprüft");
  });

  it("frames run history value in plain cost-benefit wording", () => {
    expect(de.autoresearch.roi7dHeading).toBe("Kosten-Nutzen");
    expect(de.autoresearch.roi7dLine(2, 12345, 3, 8)).toBe("2 Läufe · 3 Treffer · 8 geprüft · Aufwand 12.345");
    expect(de.autoresearch.roi7dAcceptedLine(0.5, 1, 2, 45678)).toBe("Übernommen 50% (1/2) · Aufwand pro OK 45.678");
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

describe("AutoresearchView measured outcomes", () => {
  it("separates integration from verified benefit and renders evidence", () => {
    const measured: Proposal = {
      ...proposal({ id: "measured-1", title: "Silent exception removed", status: "routed_to_kanban" }),
      delivery_state: "integrated",
      outcome_applicability: "applicable",
      measurement_status: "measured",
      outcome_verdict: "improved",
      evidence_grade: "contract_verified",
      probe_contract: {
        contract_id: "outcome:source_pattern.v1:abc",
        contract_hash: "abc",
        claim: "Silent exceptions decrease",
        success_rule: { metric: "occurrences", operator: "lower_is_better" },
        counter_rules: [],
        observation_window: { kind: "immediate" },
        environment_requirements: { pytest_version: "9.0.2" },
        measurement_budget: { max_attempts: 3 },
      },
      outcome_baseline: { metric: "occurrences", value: 1, evidence_ref: "outcome-evidence:sha256:before" },
      outcome_observation: { metric: "occurrences", value: 0, evidence_ref: "outcome-evidence:sha256:after" },
      outcome_integration_sha: "a".repeat(40),
      outcome_cost_usd: 0.25,
      outcome_cost_status: "complete",
    };
    const integratedOnly: Proposal = {
      ...proposal({ id: "integrated-only", title: "Integrated without measurement", status: "routed_to_kanban" }),
      delivery_state: "integrated",
      outcome_applicability: "applicable",
      measurement_status: "exhausted",
      outcome_verdict: "unmeasurable",
      evidence_grade: "legacy_observational",
    };

    const html = renderToStaticMarkup(<OutcomePanel metrics={null} proposals={[measured, integratedOnly]} />);

    expect(html).toContain("Integriert ist nicht gleich verbessert.");
    expect(html).toContain("Nutzen bestätigt");
    expect(html).toContain("Silent exception removed");
    expect(html).toContain("outcome:source_pattern.v1:abc");
    expect(html).toContain("aaaaaaaaaaaa");
    expect(html).toContain("occurrences: 1");
    expect(html).toContain("occurrences: 0");
    expect(html).toContain("Silent exceptions decrease");
    expect(html).toContain("pytest_version");
    expect(html).toContain("max_attempts");
    expect(html).toContain("n=1");
    expect(html).toContain("Integrated without measurement");
    expect(html).toContain("nicht messbar");
  });

  it("shows an honest empty state when no contract has measured benefit", () => {
    const html = renderToStaticMarkup(<OutcomePanel metrics={null} proposals={[]} />);
    expect(html).toContain("Noch kein vertragsgeprüfter Nutzenbeleg");
    expect(html).toContain("0 von 0 anwendbaren Änderungen gemessen");
  });

  it("labels incomplete subscription costs instead of rendering a false zero", () => {
    const measured: Proposal = {
      ...proposal({ id: "unknown-cost", title: "Subscription delivery", status: "routed_to_kanban" }),
      delivery_state: "integrated",
      outcome_applicability: "applicable",
      measurement_status: "measured",
      outcome_verdict: "improved",
      evidence_grade: "contract_verified",
      outcome_cost_usd: 0,
      outcome_cost_status: "partial",
    };

    const html = renderToStaticMarkup(<OutcomePanel metrics={null} proposals={[measured]} />);

    expect(html).toContain("Kosten unvollständig");
    expect(html).toContain("Kostenabdeckung 0/1");
    expect(html).not.toContain("Gesamt 0,00");
  });

  it("labels legacy improved as historical and excludes it from verified benefit", () => {
    const legacy: Proposal = {
      ...proposal({ id: "legacy-improved", title: "Old observation", status: "routed_to_kanban" }),
      delivery_state: "integrated",
      outcome_applicability: "applicable",
      measurement_status: "measured",
      outcome_verdict: "improved",
      evidence_grade: "legacy_observational",
    };
    const html = renderToStaticMarkup(<OutcomePanel metrics={null} proposals={[legacy]} />);
    expect(html).toContain("Historisch verbessert");
    expect(html).toContain("Old observation");
    expect(html).toContain("0");
    expect(html).not.toContain("Nutzen bestätigt</article>");
  });
});
