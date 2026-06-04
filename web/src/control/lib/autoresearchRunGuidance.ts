import type { ToneName } from "./types";

export interface AutoresearchRunGuidance {
  tone: ToneName;
  label: string;
  outcome: string;
  cost: string;
  safety: string;
}

export function getDeepAuditGuidance(input: { subsystem?: string | null; running: boolean }): AutoresearchRunGuidance {
  if (input.running) {
    return {
      tone: "cyan",
      label: "Läuft gerade",
      outcome: "Sucht systematisch nach Code-Risiken im gewählten Subsystem.",
      cost: "Sehr teuer: typischerweise 1-2 Mio Token.",
      safety: "Schreibt keinen Code; Findings landen als geprüfte Vorschläge in der Queue.",
    };
  }
  return {
    tone: input.subsystem ? "amber" : "zinc",
    label: input.subsystem ? "Teurer Audit" : "Subsystem fehlt",
    outcome: input.subsystem ? `Prüft ${input.subsystem} auf konkrete Risiken.` : "Wähle zuerst ein Subsystem.",
    cost: "Nur starten, wenn ein gezielter Audit sinnvoll ist.",
    safety: "Keine direkte Code-Änderung; du entscheidest später in der Queue.",
  };
}

export function getTestFoundryGuidance(input: { target?: string | null; running: boolean; autoApply: boolean }): AutoresearchRunGuidance {
  if (input.running) {
    return {
      tone: "cyan",
      label: "Härtung läuft",
      outcome: "Mutation-Tests suchen Lücken in vorhandenen Tests.",
      cost: "Kann einige Minuten dauern.",
      safety: input.autoApply ? "Validierte Tests landen auf f-test-foundry, nicht auf main." : "Erzeugt nur Vorschläge; du entscheidest später.",
    };
  }
  return {
    tone: input.autoApply ? "amber" : "emerald",
    label: input.autoApply ? "Auto-Apply aktiv" : "Nur Vorschläge",
    outcome: input.target ? `Härtet Tests rund um ${input.target}.` : "Wähle zuerst ein Ziel.",
    cost: "Mittel: Laufzeit statt hoher Token-Kosten.",
    safety: input.autoApply ? "Branch f-test-foundry bleibt getrennt von main." : "Keine direkte Änderung; Vorschläge erscheinen in der Queue.",
  };
}

export function getResearchLoopGuidance(input: { running: boolean; routeOk: boolean; maxIterations: number; area: string }): AutoresearchRunGuidance {
  if (!input.routeOk) {
    return {
      tone: "amber",
      label: "Route prüfen",
      outcome: "Neue Läufe können ohne bestätigte Modellroute scheitern.",
      cost: "Kosten unklar, bis die Route steht.",
      safety: "Erst Route prüfen, dann starten.",
    };
  }
  if (input.running) {
    return {
      tone: "cyan",
      label: "Loop aktiv",
      outcome: "Der Loop sucht bereits nach neuen Kandidaten.",
      cost: `${input.maxIterations} Iterationen maximal in diesem Lauf.`,
      safety: "Dry-Run erzeugt Vorschläge; Änderungen passieren erst nach Review.",
    };
  }
  return {
    tone: "violet",
    label: "Dry-Run",
    outcome: `Sucht Kandidaten in ${input.area}.`,
    cost: `${input.maxIterations} Iterationen, klein genug für einen kontrollierten Lauf.`,
    safety: "Schreibt keine Änderungen; neue Kandidaten landen zuerst in der Queue.",
  };
}
