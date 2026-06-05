import type { Proposal, ToneName } from "./types";

export interface AutoresearchResolvedSummary {
  tone: ToneName;
  label: string;
  title: string;
  detail: string;
  next: string;
  archiveLabel: string | null;
  facts: { label: string; value: string; tone: ToneName }[];
}

export function getAutoresearchResolvedSummary(input: {
  reverted: readonly Proposal[];
  applied: readonly Proposal[];
  skipped: readonly Proposal[];
}): AutoresearchResolvedSummary | null {
  const reverted = input.reverted.length;
  const applied = input.applied.length;
  const skipped = input.skipped.length;
  const total = reverted + applied + skipped;
  if (total === 0) return null;

  const facts = [
    { label: "Zurückgerollt", value: String(reverted), tone: reverted > 0 ? "amber" : "zinc" },
    { label: "Übernommen", value: String(applied), tone: applied > 0 ? "emerald" : "zinc" },
    { label: "Übersprungen", value: String(skipped), tone: skipped > 0 ? "zinc" : "zinc" },
  ] satisfies AutoresearchResolvedSummary["facts"];

  if (reverted > 0) {
    return {
      tone: "amber",
      label: "Aufräumen",
      title: `${reverted} ${reverted === 1 ? "Karte ist" : "Karten sind"} sicher aussortiert.`,
      detail: "Zurückgerollte Kandidaten haben keinen Nutzen gezeigt. Sie blockieren keine Entscheidung mehr, können aber die Ansicht füllen.",
      next: "Zurückgerollte archivieren, damit offene und erledigte Arbeit klar getrennt bleibt.",
      archiveLabel: `${reverted === 1 ? "Karte" : "Karten"} archivieren`,
      facts,
    };
  }

  if (applied > 0) {
    return {
      tone: "emerald",
      label: "Erledigt",
      title: `${applied} ${applied === 1 ? "Vorschlag wurde" : "Vorschläge wurden"} übernommen.`,
      detail: skipped > 0 ? "Übernommene und übersprungene Karten sind abgeschlossen." : "Die erledigten Karten brauchen keine Aktion mehr.",
      next: "Bei Bedarf Details aufklappen; sonst mit Entscheidungen oder Probelauf weiterarbeiten.",
      archiveLabel: null,
      facts,
    };
  }

  return {
    tone: "zinc",
    label: "Aussortiert",
    title: `${skipped} ${skipped === 1 ? "Vorschlag wurde" : "Vorschläge wurden"} übersprungen.`,
    detail: "Diese Karten wurden bewusst nicht übernommen und brauchen keine weitere Aktion.",
    next: "Bei Bedarf Details aufklappen; sonst mit Entscheidungen oder Probelauf weiterarbeiten.",
    archiveLabel: null,
    facts,
  };
}
