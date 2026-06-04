import type { ToneName } from "./types";

export interface AutoresearchRunGuidance {
  tone: ToneName;
  label: string;
  outcome: string;
  cost: string;
  safety: string;
}

export interface ResearchLoopStartControl {
  disabled: boolean;
  label: string;
  title: string;
}

export type ResearchLoopPresetId = "recommended" | "popular" | "dashboard";

export interface ResearchLoopPreset {
  id: ResearchLoopPresetId;
  label: string;
  badge: string;
  area: string;
  focus: string;
  maxIterations: string;
  minUseCount: string;
  summary: string;
  cost: string;
  title: string;
}

export const RESEARCH_LOOP_PRESETS = [
  {
    id: "recommended",
    label: "Empfohlen",
    badge: "klein",
    area: "all",
    focus: "recommended_sections",
    maxIterations: "2",
    minUseCount: "",
    summary: "Breit genug für frische Vorschläge, klein genug für einen kontrollierten Probelauf.",
    cost: "2 Iterationen",
    title: "Setzt den günstigen Standardlauf für neue Skill-Kandidaten.",
  },
  {
    id: "popular",
    label: "Häufig genutzt",
    badge: "enger",
    area: "all",
    focus: "recommended_sections",
    maxIterations: "3",
    minUseCount: "10",
    summary: "Reduziert Rauschen, indem selten genutzte Skills ausgelassen werden.",
    cost: "3 Iterationen · Nutzung >= 10",
    title: "Sucht bevorzugt dort, wo Verbesserungen wahrscheinlich tatsächlich helfen.",
  },
  {
    id: "dashboard",
    label: "Dashboard",
    badge: "Code",
    area: "dashboard",
    focus: "code_review",
    maxIterations: "2",
    minUseCount: "",
    summary: "Prüft Dashboard-Skripte und Tests statt die Skill-Sammlung zu durchsuchen.",
    cost: "2 Iterationen",
    title: "Startet einen kleinen Dashboard-Code-Probelauf.",
  },
] as const satisfies readonly ResearchLoopPreset[];

export function getResearchLoopPreset(id: ResearchLoopPresetId): ResearchLoopPreset {
  return RESEARCH_LOOP_PRESETS.find((preset) => preset.id === id) ?? RESEARCH_LOOP_PRESETS[0];
}

export function getSelectedResearchLoopPresetId(input: {
  area: string;
  focus: string;
  maxIterations: string;
  minUseCount: string;
}): ResearchLoopPresetId | null {
  const focus = input.focus.trim() || "recommended_sections";
  const maxIterations = String(input.maxIterations).trim();
  const minUseCount = String(input.minUseCount).trim();
  return RESEARCH_LOOP_PRESETS.find((preset) => (
    preset.area === input.area
    && preset.focus === focus
    && preset.maxIterations === maxIterations
    && preset.minUseCount === minUseCount
  ))?.id ?? null;
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

export function getResearchLoopStartControl(input: { running: boolean; busy: boolean; routeOk: boolean }): ResearchLoopStartControl {
  if (!input.routeOk) {
    return {
      disabled: true,
      label: "Route prüfen",
      title: "Der Research-Loop startet erst, wenn die Modellroute bestätigt ist.",
    };
  }
  if (input.running) {
    return {
      disabled: true,
      label: "Loop läuft",
      title: "Es läuft bereits ein Research-Loop.",
    };
  }
  if (input.busy) {
    return {
      disabled: true,
      label: "Startet...",
      title: "Startsignal wird gerade gesendet.",
    };
  }
  return {
    disabled: false,
    label: "Research-Loop starten",
    title: "Startet einen Dry-Run. Änderungen landen erst als Vorschläge in der Queue.",
  };
}
