import { filterBySeverityThreshold } from "./autoresearch";
import { proposalNeedsManualReview } from "./autoresearchDecisionGuide";
import type { Proposal, ToneName } from "./types";

export type AutoresearchQueueMode = "all" | "high" | "manual" | "safe";

export interface AutoresearchQueueModeOption {
  id: AutoresearchQueueMode;
  label: string;
  count: number;
  tone: ToneName;
  detail: string;
}

export interface AutoresearchQueueModeSummary {
  active: AutoresearchQueueModeOption;
  options: AutoresearchQueueModeOption[];
}

export interface AutoresearchEmptyQueueModeGuidance {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  primaryMode: AutoresearchQueueMode;
  primaryLabel: string;
  facts: { label: string; value: string; tone: ToneName }[];
}

export function filterAutoresearchQueueByMode(proposals: Proposal[], mode: AutoresearchQueueMode): Proposal[] {
  if (mode === "high") return filterBySeverityThreshold(proposals, "high");
  if (mode === "manual") return proposals.filter(proposalNeedsManualReview);
  if (mode === "safe") return proposals.filter((proposal) => !proposalNeedsManualReview(proposal));
  return proposals;
}

export function getAutoresearchQueueModeSummary(proposals: Proposal[], activeMode: AutoresearchQueueMode): AutoresearchQueueModeSummary {
  const high = filterAutoresearchQueueByMode(proposals, "high").length;
  const manual = filterAutoresearchQueueByMode(proposals, "manual").length;
  const safe = filterAutoresearchQueueByMode(proposals, "safe").length;
  const options: AutoresearchQueueModeOption[] = [
    {
      id: "all",
      label: "Alle",
      count: proposals.length,
      tone: proposals.length > 0 ? "cyan" : "zinc",
      detail: "Zeigt alle offenen Karten in sinnvoller Reihenfolge.",
    },
    {
      id: "high",
      label: "Hoch+",
      count: high,
      tone: high > 0 ? "amber" : "zinc",
      detail: "Nur kritische und hohe Risiken. Gut für den ersten Review-Durchlauf.",
    },
    {
      id: "manual",
      label: "Einzelreview",
      count: manual,
      tone: manual > 0 ? "amber" : "zinc",
      detail: "Karten, die du einzeln lesen und direkt entscheiden solltest.",
    },
    {
      id: "safe",
      label: "Sammel-sicher",
      count: safe,
      tone: safe > 0 ? "emerald" : "zinc",
      detail: "Niedrigeres Risiko: diese Karten können gesammelt markiert werden.",
    },
  ];
  return {
    active: options.find((option) => option.id === activeMode) ?? options[0],
    options,
  };
}

export function getAutoresearchEmptyQueueModeGuidance(summary: AutoresearchQueueModeSummary): AutoresearchEmptyQueueModeGuidance | null {
  if (summary.active.count > 0 || summary.active.id === "all") return null;

  const all = optionFor(summary, "all");
  const high = optionFor(summary, "high");
  const manual = optionFor(summary, "manual");
  const safe = optionFor(summary, "safe");
  if (!all || all.count === 0 || !high || !manual || !safe) return null;

  const primaryMode = manual.count > 0
    ? "manual"
    : safe.count > 0
      ? "safe"
      : "all";
  const primary = optionFor(summary, primaryMode) ?? all;
  const facts = [
    { label: "Alle", value: String(all.count), tone: all.tone },
    { label: "Hoch+", value: String(high.count), tone: high.tone },
    { label: "Einzeln", value: String(manual.count), tone: manual.tone },
    { label: "Sicher", value: String(safe.count), tone: safe.tone },
  ] satisfies AutoresearchEmptyQueueModeGuidance["facts"];

  if (summary.active.id === "high") {
    return {
      tone: "cyan",
      label: "Kein Hoch+",
      title: "Keine kritischen Karten in diesem Filter.",
      detail: manual.count > 0
        ? "Gut: nichts ganz Dringendes. Bearbeite jetzt die Einzelreview-Karten."
        : "Gut: nichts ganz Dringendes. Wechsle zu den übrigen offenen Karten.",
      primaryMode,
      primaryLabel: `${primary.label} zeigen`,
      facts,
    };
  }

  if (summary.active.id === "manual") {
    return {
      tone: safe.count > 0 ? "emerald" : "cyan",
      label: safe.count > 0 ? "Sammel-sicher" : "Keine Einzelreview",
      title: "Keine Karten für Einzelreview in diesem Filter.",
      detail: safe.count > 0
        ? "Es liegen nur noch sammelsichere Karten an. Du kannst sie gezielt markieren."
        : "Der Filter ist leer. Zeige alle Karten, um die nächste Entscheidung zu wählen.",
      primaryMode,
      primaryLabel: `${primary.label} zeigen`,
      facts,
    };
  }

  return {
    tone: manual.count > 0 ? "amber" : "cyan",
    label: manual.count > 0 ? "Erst lesen" : "Filter leer",
    title: "Keine sammelsicheren Karten in diesem Filter.",
    detail: manual.count > 0
      ? "Die übrigen Karten brauchen bewusstes Einzelreview. Sammelübernahme bleibt deshalb aus."
      : "Der Filter ist leer. Zeige alle Karten, um die nächste Entscheidung zu wählen.",
    primaryMode,
    primaryLabel: `${primary.label} zeigen`,
    facts,
  };
}

function optionFor(summary: AutoresearchQueueModeSummary, id: AutoresearchQueueMode): AutoresearchQueueModeOption | null {
  return summary.options.find((option) => option.id === id) ?? null;
}
