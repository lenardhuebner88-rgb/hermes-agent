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

export interface AutoresearchTopCardMode {
  tone: ToneName;
  label: string;
  detail: string;
}

export interface AutoresearchQueueActionSummary {
  tone: ToneName;
  title: string;
  batchLine: string;
  manualLine: string;
  confirmLine: string;
  facts: { label: string; value: string; tone: ToneName }[];
}

export function canBatchConfirmAutoresearchSelection(input: {
  selectedCount: number;
  selectedManualReviewCount: number;
  busy: boolean;
}): boolean {
  return input.selectedCount > 0 && input.selectedManualReviewCount === 0 && !input.busy;
}

export function canApplyAllOpenSkillProposals(input: {
  openSkillProposals: readonly Proposal[];
  busy: boolean;
}): boolean {
  return input.openSkillProposals.length > 0 && !input.busy && input.openSkillProposals.every((proposal) => !proposalNeedsManualReview(proposal));
}

export function getBatchSafeVisibleProposalIds(visibleProposals: readonly Proposal[]): string[] {
  return visibleProposals.filter((proposal) => !proposalNeedsManualReview(proposal)).map((proposal) => proposal.id);
}

export function proposalNeedsManualReview(proposal: Proposal): boolean {
  if (proposal.mode !== "skill") return true;
  if (SEVERITY_ORDER[getProposalSeverity(proposal)] >= SEVERITY_ORDER.high) return true;
  return getProposalPriorityGroup(proposal).key === "safety";
}

export function describeTopCardMode(proposal: Proposal): AutoresearchTopCardMode {
  if (proposalNeedsManualReview(proposal)) {
    return {
      tone: "amber",
      label: "Einzelreview",
      detail: "Diese Karte bitte einzeln lesen; Sammelübernahme bleibt aus.",
    };
  }
  return {
    tone: "emerald",
    label: "Sammel-sicher",
    detail: "Diese Karte darf in die sichere Sammelauswahl.",
  };
}

export function getAutoresearchQueueActionSummary(input: {
  visibleCount: number;
  batchSafeVisibleCount: number;
  manualReviewVisibleCount: number;
  selectedCount: number;
  selectedManualReviewCount: number;
}): AutoresearchQueueActionSummary {
  const facts = [
    { label: "Sicher sammelbar", value: String(input.batchSafeVisibleCount), tone: input.batchSafeVisibleCount > 0 ? "emerald" : "zinc" },
    { label: "Einzelreview", value: String(input.manualReviewVisibleCount), tone: input.manualReviewVisibleCount > 0 ? "amber" : "emerald" },
    { label: "Markiert", value: String(input.selectedCount), tone: input.selectedCount > 0 ? "cyan" : "zinc" },
  ] satisfies AutoresearchQueueActionSummary["facts"];

  if (input.visibleCount === 0) {
    return {
      tone: "zinc",
      title: "Keine sichtbare Karte in dieser Ansicht.",
      batchLine: "Es gibt gerade nichts zu markieren.",
      manualLine: "Einzelreview erscheint, sobald riskantere Karten sichtbar sind.",
      confirmLine: "Sammel-Übernehmen bleibt aus, bis eine sichere Auswahl markiert ist.",
      facts,
    };
  }

  if (input.selectedCount > 0) {
    if (input.selectedManualReviewCount > 0) {
      return {
        tone: "amber",
        title: "Die Auswahl ist nicht sammelsicher.",
        batchLine: `${input.selectedCount} markiert, aber ${input.selectedManualReviewCount} davon brauchen Einzelreview.`,
        manualLine: "Riskante Karten bitte öffnen, die Klartextfelder lesen und direkt auf der Karte entscheiden.",
        confirmLine: "Sammel-Übernehmen bleibt gesperrt, bis nur sichere Karten markiert sind.",
        facts,
      };
    }
    return {
      tone: "cyan",
      title: "Diese Auswahl ist für Sammel-Übernehmen bereit.",
      batchLine: `${input.selectedCount} sichere ${input.selectedCount === 1 ? "Karte ist" : "Karten sind"} markiert.`,
      manualLine: input.manualReviewVisibleCount > 0
        ? `${input.manualReviewVisibleCount} sichtbare ${input.manualReviewVisibleCount === 1 ? "Karte bleibt" : "Karten bleiben"} bewusst Einzelreview.`
        : "Keine sichtbare Karte braucht Einzelreview.",
      confirmLine: "Sammel-Übernehmen wirkt nur auf die markierten Karten; alles andere bleibt liegen.",
      facts,
    };
  }

  if (input.batchSafeVisibleCount === 0) {
    return {
      tone: "amber",
      title: "Heute ist Einzelreview dran.",
      batchLine: "Keine sichtbare Karte ist für Sammel-Übernehmen freigegeben.",
      manualLine: `${input.manualReviewVisibleCount} sichtbare ${input.manualReviewVisibleCount === 1 ? "Karte braucht" : "Karten brauchen"} bewusstes Lesen.`,
      confirmLine: "Öffne die Top-Karte und entscheide direkt mit Annehmen oder Ablehnen.",
      facts,
    };
  }

  return {
    tone: input.manualReviewVisibleCount > 0 ? "emerald" : "cyan",
    title: input.manualReviewVisibleCount > 0 ? "Zwei Wege: sichere sammeln, riskante einzeln." : "Sammelweg ist frei.",
    batchLine: `${input.batchSafeVisibleCount} sichtbare ${input.batchSafeVisibleCount === 1 ? "Karte darf" : "Karten dürfen"} gesammelt markiert werden.`,
    manualLine: input.manualReviewVisibleCount > 0
      ? `${input.manualReviewVisibleCount} sichtbare ${input.manualReviewVisibleCount === 1 ? "Karte bleibt" : "Karten bleiben"} ohne Checkbox im Einzelreview.`
      : "Keine sichtbare Karte ist als Einzelreview markiert.",
    confirmLine: "Erst sichere Karten markieren, dann die Auswahl gesammelt übernehmen.",
    facts,
  };
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
        : "Es gibt keine offene Entscheidung. Starte erst wieder einen Lauf, wenn du neue Kandidaten brauchst.",
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
      next: `${top} zuerst öffnen: Nutzen, Aufwand und Risiko lesen; danach bewusst annehmen oder ablehnen.`,
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
