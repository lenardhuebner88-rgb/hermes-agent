import { getProposalPriorityGroup, getProposalSeverity, SEVERITY_ORDER } from "./autoresearch";
import type { Proposal, ToneName } from "./types";

export interface AutoresearchDecisionGuide {
  tone: ToneName;
  headline: string;
  summary: string;
  next: string;
  primaryLabel: string;
  facts: { label: string; value: string; tone: ToneName }[];
}

export function canBatchConfirmAutoresearchSelection(input: {
  selectedCount: number;
  selectedManualReviewCount: number;
  busy: boolean;
}): boolean {
  return input.selectedCount > 0 && input.selectedManualReviewCount === 0 && !input.busy;
}

export function proposalNeedsManualReview(proposal: Proposal): boolean {
  if (proposal.mode !== "skill") return true;
  if (SEVERITY_ORDER[getProposalSeverity(proposal)] >= SEVERITY_ORDER.high) return true;
  return getProposalPriorityGroup(proposal).key === "safety";
}

export function getAutoresearchDecisionGuide(input: {
  visibleProposals: readonly Proposal[];
  selectedProposals: readonly Proposal[];
  openCount: number;
  selectedCount: number;
  backlogCount: number;
  revertedCount: number;
  topTitle?: string | null;
}): AutoresearchDecisionGuide {
  const manualReviewCount = input.visibleProposals.filter(proposalNeedsManualReview).length;
  const selectedManualReviewCount = input.selectedProposals.filter(proposalNeedsManualReview).length;
  const batchCandidateCount = input.visibleProposals.length - manualReviewCount;
  const top = input.topTitle?.trim() || "die oberste Karte";

  const visibleFacts = [
    { label: "Sammeln ok", value: String(batchCandidateCount), tone: batchCandidateCount > 0 ? "emerald" : "zinc" },
    { label: "Einzeln lesen", value: String(manualReviewCount), tone: manualReviewCount > 0 ? "amber" : "emerald" },
    { label: "Versteckt", value: String(input.backlogCount), tone: input.backlogCount > 0 ? "zinc" : "emerald" },
  ] satisfies AutoresearchDecisionGuide["facts"];
  const selectedFacts = [
    { label: "Markiert", value: String(input.selectedCount), tone: "cyan" },
    { label: "Einzeln lesen", value: String(selectedManualReviewCount), tone: selectedManualReviewCount > 0 ? "amber" : "emerald" },
    { label: "Versteckt", value: String(input.backlogCount), tone: input.backlogCount > 0 ? "zinc" : "emerald" },
  ] satisfies AutoresearchDecisionGuide["facts"];

  if (input.openCount === 0) {
    return {
      tone: input.revertedCount > 0 ? "amber" : "emerald",
      headline: input.revertedCount > 0 ? "Keine Entscheidung offen, nur Aufräumen." : "Nichts offen. Du musst gerade nichts entscheiden.",
      summary: input.revertedCount > 0
        ? "Zurückgerollte Vorschläge sind keine aktive Verbesserung mehr. Archivieren macht die Ansicht ruhiger."
        : "Die Queue ist leer. Starte erst wieder einen Lauf, wenn du neue Kandidaten brauchst.",
      next: input.revertedCount > 0 ? "Zurückgerollte archivieren oder einen neuen gezielten Lauf starten." : "Bei Bedarf neue Kandidaten holen.",
      primaryLabel: input.revertedCount > 0 ? "Aufräumen" : "Bereit",
      facts: visibleFacts,
    };
  }

  if (input.selectedCount > 0) {
    if (selectedManualReviewCount > 0) {
      return {
        tone: "amber",
        headline: `${input.selectedCount} markiert, davon ${selectedManualReviewCount} mit Einzelreview-Pflicht.`,
        summary: "Die Auswahl enthält Code, Hoch+-Risiko oder Safety-Bezug. Sammel-Übernehmen wäre zu pauschal.",
        next: "Riskante Karten einzeln öffnen und bewusst übernehmen oder aus der Auswahl nehmen.",
        primaryLabel: "Auswahl prüfen",
        facts: selectedFacts,
      };
    }
    return {
      tone: "cyan",
      headline: `${input.selectedCount} markiert. Jetzt geht es um diese Auswahl.`,
      summary: "Der Sammelknopf übernimmt nur markierte Karten. Nicht markierte und versteckte Vorschläge bleiben liegen.",
      next: "Kurz prüfen, ob alle markierten Karten wirklich zusammen gehören, dann Auswahl übernehmen.",
      primaryLabel: "Auswahl prüfen",
      facts: selectedFacts,
    };
  }

  if (manualReviewCount > 0) {
    return {
      tone: "amber",
      headline: "Erst einzeln lesen, dann entscheiden.",
      summary: "In den sichtbaren Karten steckt Code, Hoch+-Risiko oder Safety-Bezug. Das ist nicht für blindes Sammel-Übernehmen gedacht.",
      next: `${top} zuerst öffnen: Nutzen, Risiko und Diff lesen; danach bewusst übernehmen oder überspringen.`,
      primaryLabel: "Einzelreview",
      facts: visibleFacts,
    };
  }

  return {
    tone: "emerald",
    headline: "Sichtbare Skill-Karten sind für Sammelreview geeignet.",
    summary: "Keine sichtbare Karte ist Code, Hoch+-Risiko oder Safety-kritisch. Du kannst sie gesammelt markieren und danach einmal bestätigen.",
    next: batchCandidateCount > 1 ? "Sichtbare markieren, kurz gegenlesen, dann Auswahl übernehmen." : `${top} auswählen und entscheiden.`,
    primaryLabel: batchCandidateCount > 1 ? "Sammelreview" : "Einzelkarte",
    facts: visibleFacts,
  };
}
