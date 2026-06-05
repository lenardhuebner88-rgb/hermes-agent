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
      detail: "Zeigt die komplette offene Queue in Relevanz-Reihenfolge.",
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
