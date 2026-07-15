import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { de } from "./de";

// Abriss S5: FlowView + OverviewView wurden entfernt. Die „Kette starten"-CTA
// (früher an FlowView.tsx gepinnt) lebt jetzt im Fleet-Cockpit (PlanTab,
// de.fleet.planKetteStarten*). Wir pinnen daher die überlebenden Fleet-Quellen
// statt der gelöschten Views — die negative Verify-Copy-Prüfung deckte vorher
// FlowView ab und liegt jetzt auf FleetPipeline/lib.fleet (unten).
const planTabSrc = readFileSync(fileURLToPath(new URL("../views/fleet/PlanTab.tsx", import.meta.url)), "utf8");
const fleetPipelineSrc = readFileSync(fileURLToPath(new URL("../components/fleet/FleetPipeline.tsx", import.meta.url)), "utf8");
const fleetSrc = readFileSync(fileURLToPath(new URL("../lib/fleet.ts", import.meta.url)), "utf8");

describe("German gated Flow copy", () => {
  it("explains that the gated release frees held subtasks without a force-run promise", () => {
    expect(de.flow.capture.gateGateHint).toBe("Subtasks werden gehalten, bis du „Kette starten“ klickst.");
    expect(de.flow.plan.release).toBe("Kette starten");
    expect(de.flow.plan.heldGate).toBe("Gibt gehaltene Subtasks frei; Start bleibt von Dependencies, Queue/Assignee und Worker-Kapazität abhängig.");
  });

  it("uses the chain-start CTA for the release confirmation button", () => {
    expect(de.flow.plan.releaseConfirmButton).toBe("Kette starten");
    // Chain-start zog beim Abriss (S5) in die Fleet-PlanTab um; sie verdrahtet
    // die i18n-Keys statt Hardcode — hier gepinnt (Coverage-Umzug, kein -Verlust).
    expect(de.fleet.planKetteStarten).toBe("Kette starten");
    expect(planTabSrc).toContain("de.fleet.planKetteStarten");
  });

  it("offers chain-first choices when a held subtask is dispatched directly", () => {
    expect(de.flow.singleDispatch.startChain).toBe("Ganze Kette starten");
    expect(de.flow.singleDispatch.startSingle).toBe("Nur diesen Task starten");
    expect(de.flow.singleDispatch.cancel).toBe("Abbrechen");
  });

  it("uses consistent German Worker-panel error and status copy", () => {
    expect(de.systemHealth.metricsError).toBe("Metriken konnten nicht geladen werden.");
    expect(de.worker.runawayWarn).toBe("Entgleisungsrisiko");
    expect(de.worker.runawayCritical).toBe("Entgleist");
    expect(de.worker.actionFailed).toBe("Worker-Aktion fehlgeschlagen");
    expect(de.fleet.actionFailed).toBe("Kanban-Aktion fehlgeschlagen");
    expect(de.crons.actionFailed).toBe("Cron-Aktion fehlgeschlagen");

    expect(fleetPipelineSrc).not.toMatch(/Rework|Verify:/);
    expect(fleetSrc).not.toMatch(/ship: \{ key: "ship", label: "Ship"|rework: \{ key: "rework", label: "Rework"|Review abnehmen/);
  });
});

describe("German Autoresearch decision copy", () => {
  it("uses the agreed plain-language triage labels", () => {
    expect(de.autoresearch.accept).toBe("Annehmen");
    expect(de.autoresearch.reject).toBe("Ablehnen");
    expect(de.autoresearch.decisionWhat).toBe("Was es ist");
    expect(de.autoresearch.decisionBenefit).toBe("Warum es dir etwas bringt");
    expect(de.autoresearch.decisionRecommendation).toBe("Empfehlung und Grund");
    expect(de.autoresearch.decisionEffortRisk).toBe("Aufwand, Kosten und Risiko grob");
    expect(de.autoresearch.technicalExpand).toBe("Für Technik ausklappen");
  });
});
