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
  operatorTitle: string;
  operatorFit: string;
  operatorResult: string;
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
    operatorTitle: "Guter Standardlauf",
    operatorFit: "Nimm das, wenn du einfach frische, harmlose Vorschläge willst.",
    operatorResult: "Neue Kandidaten landen als Queue-Karten; nichts wird automatisch geändert.",
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
    operatorTitle: "Weniger Rauschen",
    operatorFit: "Nimm das, wenn nur viel genutzte Skills verbessert werden sollen.",
    operatorResult: "Seltene Skills werden ausgelassen, damit die Queue kürzer und relevanter bleibt.",
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
    operatorTitle: "Dashboard prüfen",
    operatorFit: "Nimm das, wenn das Control-Dashboard selbst untersucht werden soll.",
    operatorResult: "Code-nahe Vorschläge landen in der Queue und brauchen später Review.",
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

export interface ResearchLoopStartSummary {
  title: string;
  scope: string;
  detail: string;
  cost: string;
  safety: string;
  technicalLabel: string;
}

export interface ResearchLoopStartChecklistItem {
  label: string;
  value: string;
  detail: string;
  tone: ToneName;
}

export interface ResearchLoopStartChecklist {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  items: ResearchLoopStartChecklistItem[];
}

export function getResearchLoopStartSummary(input: {
  selectedPresetId: ResearchLoopPresetId | null;
  areaLabel: string;
  focus: string;
  maxIterations: number;
  minUseCount: number | null;
}): ResearchLoopStartSummary {
  const preset = input.selectedPresetId ? getResearchLoopPreset(input.selectedPresetId) : null;
  if (preset) {
    return {
      title: preset.operatorTitle,
      scope: preset.operatorFit,
      detail: preset.operatorResult,
      cost: preset.cost,
      safety: "Dry-Run: Änderungen entstehen erst nach Review in der Queue.",
      technicalLabel: `${preset.area} · ${preset.focus}`,
    };
  }
  return {
    title: "Eigene Feinsteuerung",
    scope: `Sucht in ${input.areaLabel}.`,
    detail: `Fokus ${input.focus.trim() || "recommended_sections"}; ${input.minUseCount !== null ? `nur Nutzung >= ${input.minUseCount}` : "ohne Nutzungsfilter"}.`,
    cost: `${input.maxIterations} Iterationen maximal.`,
    safety: "Dry-Run: Änderungen entstehen erst nach Review in der Queue.",
    technicalLabel: "Manuelle Werte",
  };
}

export function getResearchLoopStartChecklist(input: {
  routeOk: boolean;
  running: boolean;
  busy: boolean;
  selectedPresetId: ResearchLoopPresetId | null;
  maxIterations: number;
  openCount: number;
  highPriorityCount: number;
}): ResearchLoopStartChecklist {
  const preset = input.selectedPresetId ? getResearchLoopPreset(input.selectedPresetId) : null;
  const startItem = startReadinessItem(input);
  const queueItem = queueImpactItem(input.openCount, input.highPriorityCount);
  const safetyItem = safetyEffortItem({
    presetLabel: preset?.label ?? null,
    maxIterations: input.maxIterations,
    customValues: !preset,
  });
  const items = [startItem, queueItem, safetyItem];
  const tone = !input.routeOk
    ? "amber"
    : input.running
      ? "cyan"
      : input.busy || input.highPriorityCount > 0
        ? input.busy ? "violet" : "amber"
        : "emerald";

  return {
    tone,
    label: !input.routeOk ? "Nicht starten" : input.running ? "Beobachten" : input.busy ? "Startet" : input.highPriorityCount > 0 ? "Erst Review" : "Startklar",
    title: "Start-Check vor dem Probelauf",
    detail: !input.routeOk
      ? "Die Modellroute ist noch nicht bereit; der Start bleibt gesperrt, bis die Route bestätigt ist."
      : input.running
        ? "Ein Lauf ist aktiv. Beobachte den Fortschritt, statt parallel einen zweiten Lauf zu starten."
        : input.busy
          ? "Das Startsignal ist unterwegs. Warte auf Rückmeldung, bevor du erneut klickst."
          : input.highPriorityCount > 0
      ? "Der Lauf ist möglich, aber die offene Queue hat Vorrang, damit der Review-Stau nicht wächst."
      : "Diese Punkte zeigen, ob der Start gerade sinnvoll und sicher begrenzt ist.",
    items,
  };
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

function startReadinessItem(input: { routeOk: boolean; running: boolean; busy: boolean }): ResearchLoopStartChecklistItem {
  if (!input.routeOk) {
    return {
      label: "Startsignal",
      value: "Route fehlt",
      detail: "Erst Modellroute prüfen; sonst kann der Lauf direkt scheitern.",
      tone: "amber",
    };
  }
  if (input.running) {
    return {
      label: "Startsignal",
      value: "läuft bereits",
      detail: "Keinen zweiten Lauf starten; beobachte Heartbeat und letzten Schritt.",
      tone: "cyan",
    };
  }
  if (input.busy) {
    return {
      label: "Startsignal",
      value: "unterwegs",
      detail: "Das Startsignal wurde gesendet und wartet auf Rückmeldung.",
      tone: "violet",
    };
  }
  return {
    label: "Startsignal",
    value: "bereit",
    detail: "Die Route steht; der Button startet einen begrenzten Dry-Run.",
    tone: "emerald",
  };
}

function queueImpactItem(openCount: number, highPriorityCount: number): ResearchLoopStartChecklistItem {
  if (highPriorityCount > 0) {
    return {
      label: "Queue-Wirkung",
      value: `${highPriorityCount} Hoch+ offen`,
      detail: "Neue Kandidaten würden den Review-Stau erhöhen; erst kritische Karten entscheiden.",
      tone: "amber",
    };
  }
  if (openCount > 0) {
    return {
      label: "Queue-Wirkung",
      value: `${openCount} offen`,
      detail: "Start ist möglich, aber neue Treffer landen zusätzlich in der Queue.",
      tone: "cyan",
    };
  }
  return {
    label: "Queue-Wirkung",
    value: "leer",
    detail: "Guter Zeitpunkt für neue Kandidaten; es liegt nichts Unerledigtes davor.",
    tone: "emerald",
  };
}

function safetyEffortItem(input: { presetLabel: string | null; maxIterations: number; customValues: boolean }): ResearchLoopStartChecklistItem {
  if (input.customValues) {
    return {
      label: "Sicherheit",
      value: "eigene Werte",
      detail: `Dry-Run mit maximal ${input.maxIterations} Iterationen; prüfe Scope und Fokus bewusst.`,
      tone: input.maxIterations > 5 ? "amber" : "cyan",
    };
  }
  return {
    label: "Sicherheit",
    value: input.presetLabel ?? "Preset",
    detail: `Dry-Run mit maximal ${input.maxIterations} Iterationen; Stop bleibt jederzeit möglich.`,
    tone: input.maxIterations > 3 ? "cyan" : "emerald",
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
