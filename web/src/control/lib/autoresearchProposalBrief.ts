import { getProposalSeverity } from "./autoresearch";
import { formatProposalCategory } from "./autoresearchProposalLabels";
import type { Proposal, ToneName } from "./types";

export interface ProposalBriefFact {
  label: string;
  value: string;
  tone: ToneName;
}

export interface ProposalOperatorBrief {
  tone: ToneName;
  label: string;
  title: string;
  summary: string;
  facts: ProposalBriefFact[];
}

export function getProposalOperatorBrief(proposal: Proposal): ProposalOperatorBrief {
  const severity = getProposalSeverity(proposal);
  const category = formatProposalCategory(proposal.category);
  const affected = affectedLabel(proposal);
  const why = category?.help ?? firstSentence(proposal.rationale_plain) ?? "Der Vorschlag hat ein Signal geliefert, aber keine kurze Begründung.";

  if (proposal.status === "applied" || proposal.status === "skipped") {
    const applied = proposal.status === "applied";
    return {
      tone: applied ? "emerald" : "zinc",
      label: applied ? "Erledigt" : "Übersprungen",
      title: applied ? "Diese Karte ist schon übernommen." : "Diese Karte ist schon aussortiert.",
      summary: proposal.result || (applied ? "Die Änderung wurde bereits angewendet." : "Der Vorschlag wurde bewusst übersprungen."),
      facts: [
        { label: "Betroffen", value: affected, tone: applied ? "emerald" : "zinc" },
        { label: "Warum", value: why, tone: "zinc" },
        { label: "Stand", value: applied ? "Keine Aktion offen." : "Keine Übernahme offen.", tone: applied ? "emerald" : "zinc" },
      ],
    };
  }

  if (proposal.status === "testing") {
    return {
      tone: "violet",
      label: "Gate läuft",
      title: "Diese Karte wird gerade geprüft.",
      summary: proposal.result || "Autoresearch wartet auf das Test-Gate. Währenddessen ist keine neue Entscheidung nötig.",
      facts: [
        { label: "Betroffen", value: affected, tone: "violet" },
        { label: "Warum", value: why, tone: "cyan" },
        { label: "Stand", value: "Auf Gate-Ergebnis warten.", tone: "violet" },
      ],
    };
  }

  if (proposal.status === "proposed" && proposal.last_outcome === "reverted_no_improvement") {
    return {
      tone: "zinc",
      label: "Schon getestet",
      title: "Wahrscheinlich nur archivieren.",
      summary: "Dieser Kandidat wurde automatisch zurückgerollt, weil er keine Verbesserung gebracht hat.",
      facts: [
        { label: "Betroffen", value: affected, tone: "zinc" },
        { label: "Warum", value: "Kein messbarer Nutzen im letzten Lauf.", tone: "zinc" },
        { label: "Klick", value: "Archivieren räumt ihn aus den offenen Karten.", tone: "zinc" },
      ],
    };
  }

  if (proposal.mode === "code") {
    return {
      tone: severity === "critical" || severity === "high" ? "amber" : "violet",
      label: "Code mit Gate",
      title: severity === "critical" || severity === "high" ? "Erst lesen, dann übernehmen." : "Technische Änderung mit Sicherheitsnetz.",
      summary: "Der Vorschlag schreibt Code, startet danach aber ein Gate und kann bei rotem Lauf zurückgerollt werden.",
      facts: [
        { label: "Betroffen", value: affected, tone: "violet" },
        { label: "Warum", value: why, tone: severity === "critical" || severity === "high" ? "amber" : "cyan" },
        { label: "Klick", value: "Übernehmen startet Code-Änderung plus Test-Gate.", tone: "amber" },
      ],
    };
  }

  if (proposal.mode === "test" || proposal.proposal_type === "mutation_test") {
    return {
      tone: "cyan",
      label: "Test stärken",
      title: "Macht die Absicherung besser.",
      summary: "Der Vorschlag verändert vor allem Testabdeckung, damit Fehler künftig früher auffallen.",
      facts: [
        { label: "Betroffen", value: affected, tone: "cyan" },
        { label: "Warum", value: why, tone: "cyan" },
        { label: "Klick", value: "Übernehmen legt die Test-Härtung an.", tone: "emerald" },
      ],
    };
  }

  return {
    tone: severity === "critical" || severity === "high" ? "emerald" : "cyan",
    label: severity === "critical" || severity === "high" ? "Guter Skill-Fix" : "Skill-Polish",
    title: "Verbessert eine Anleitung ohne Code-Lauf.",
    summary: "Der Vorschlag ändert Skill-Text. Das ist der leichteste Autoresearch-Pfad und lässt sich bei Bedarf überspringen.",
    facts: [
      { label: "Betroffen", value: affected, tone: "cyan" },
      { label: "Warum", value: why, tone: severity === "critical" || severity === "high" ? "amber" : "zinc" },
      { label: "Klick", value: "Übernehmen schreibt den Skill-Text direkt.", tone: "emerald" },
    ],
  };
}

function affectedLabel(proposal: Proposal): string {
  const target = proposal.target?.trim() || "unbekanntes Ziel";
  const section = proposal.section?.trim();
  const prefix = proposal.mode === "code"
    ? "Code"
    : proposal.mode === "test" || proposal.proposal_type === "mutation_test"
      ? "Test"
      : "Skill";
  return section ? `${prefix}: ${target} · ${section}` : `${prefix}: ${target}`;
}

function firstSentence(value: string | null | undefined): string | null {
  const text = value?.trim().replace(/\s+/g, " ");
  if (!text) return null;
  const sentence = text.match(/^(.{1,180}?[.!?])(?:\s|$)/)?.[1];
  if (sentence) return sentence;
  return text.length > 180 ? `${text.slice(0, 177)}...` : text;
}
