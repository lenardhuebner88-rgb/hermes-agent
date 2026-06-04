import type { ToneName } from "./types";

export interface AutoresearchReviewStep {
  label: string;
  value: string;
  tone: ToneName;
}

export interface AutoresearchReviewFlow {
  tone: ToneName;
  title: string;
  detail: string;
  progressLabel: string;
  progressPercent: number;
  primaryAction: "select-top" | "select-visible" | "confirm-selection" | "archive-reverted" | "generate";
  primaryLabel: string;
  steps: AutoresearchReviewStep[];
}

export function getAutoresearchReviewFlow(input: {
  openCount: number;
  decidedCount: number;
  selectedCount: number;
  visibleCount: number;
  highPriorityCount: number;
  backlogCount: number;
  revertedCount: number;
  topTitle?: string | null;
}): AutoresearchReviewFlow {
  const total = input.openCount + input.decidedCount;
  const progressPercent = total > 0 ? Math.round((input.decidedCount / total) * 100) : 100;
  const progressLabel = total > 0 ? `${input.decidedCount} von ${total} entschieden` : "Keine offenen Entscheidungen";
  const top = input.topTitle?.trim() || "Top-Karte";

  if (input.openCount === 0) {
    return {
      tone: input.revertedCount > 0 ? "amber" : "emerald",
      title: input.revertedCount > 0 ? "Nur Aufräumen offen." : "Review ist leer.",
      detail: input.revertedCount > 0
        ? "Es gibt keine aktiven Entscheidungen mehr; zurückgerollte Kandidaten können archiviert werden."
        : "Aktuell liegt nichts zur Entscheidung an. Der nächste sinnvolle Schritt ist ein gezielter neuer Lauf.",
      progressLabel,
      progressPercent,
      primaryAction: input.revertedCount > 0 ? "archive-reverted" : "generate",
      primaryLabel: input.revertedCount > 0 ? "Zurückgerollte archivieren" : "Neue Kandidaten holen",
      steps: [
        { label: "Offen", value: "0", tone: "emerald" },
        { label: "Zurückgerollt", value: String(input.revertedCount), tone: input.revertedCount > 0 ? "amber" : "zinc" },
        { label: "Bereit", value: input.revertedCount > 0 ? "Pflege" : "Scan", tone: input.revertedCount > 0 ? "amber" : "violet" },
      ],
    };
  }

  if (input.selectedCount > 0) {
    return {
      tone: "cyan",
      title: `${input.selectedCount} zur Entscheidung markiert.`,
      detail: "Die Auswahl ist bereit. Bestätige sie gesammelt oder räume einzelne Karten wieder aus der Auswahl.",
      progressLabel,
      progressPercent,
      primaryAction: "confirm-selection",
      primaryLabel: "Auswahl übernehmen",
      steps: [
        { label: "Auswahl", value: String(input.selectedCount), tone: "cyan" },
        { label: "Sichtbar", value: String(input.visibleCount), tone: "zinc" },
        { label: "Rest", value: String(input.openCount), tone: "amber" },
      ],
    };
  }

  if (input.highPriorityCount > 0) {
    return {
      tone: "amber",
      title: `${input.highPriorityCount} Hoch+-Entscheidungen zuerst.`,
      detail: `${top} ist die nächste Karte. Prüfe Nutzen, Risiko und Diff, dann entscheide sie einzeln.`,
      progressLabel,
      progressPercent,
      primaryAction: "select-top",
      primaryLabel: "Top auswählen",
      steps: [
        { label: "Hoch+", value: String(input.highPriorityCount), tone: "amber" },
        { label: "Sichtbar", value: String(input.visibleCount), tone: "cyan" },
        { label: "Backlog", value: String(input.backlogCount), tone: input.backlogCount > 0 ? "zinc" : "emerald" },
      ],
    };
  }

  return {
    tone: "emerald",
    title: `${input.openCount} normale Entscheidungen offen.`,
    detail: `${top} ist ein guter Start. Ohne Hoch+-Risiko kannst du sichtbare Skill-Karten gesammelt markieren.`,
    progressLabel,
    progressPercent,
    primaryAction: input.visibleCount > 1 ? "select-visible" : "select-top",
    primaryLabel: input.visibleCount > 1 ? "Sichtbare markieren" : "Top auswählen",
    steps: [
      { label: "Offen", value: String(input.openCount), tone: "emerald" },
      { label: "Sichtbar", value: String(input.visibleCount), tone: "cyan" },
      { label: "Backlog", value: String(input.backlogCount), tone: input.backlogCount > 0 ? "zinc" : "emerald" },
    ],
  };
}
