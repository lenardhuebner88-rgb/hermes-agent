import type { AutoresearchState, ToneName } from "./types";

export type AutoresearchRecommendationKind = "review" | "monitor" | "recover" | "inspect" | "generate";

export interface AutoresearchRecommendation {
  kind: AutoresearchRecommendationKind;
  tone: ToneName;
  eyebrow: string;
  title: string;
  detail: string;
  primaryLabel: string;
}

export function getAutoresearchRecommendation(input: {
  state?: AutoresearchState | null;
  openCount: number;
  revertedCount: number;
  loopRunning: boolean;
  routeStatus?: string | null;
}): AutoresearchRecommendation {
  const routeNeedsRecovery = !!input.routeStatus && input.routeStatus !== "configured";
  const statusUnknown = !input.state && !input.routeStatus;

  if (input.state === "crashed" || routeNeedsRecovery) {
    return {
      kind: "recover",
      tone: "red",
      eyebrow: "Achtung",
      title: "Erst stabilisieren, dann weiterforschen.",
      detail: "Der Loop oder die Modellroute meldet einen Fehler. Prüfe Status und Route, bevor neue Läufe gestartet werden.",
      primaryLabel: "Status ansehen",
    };
  }
  if (input.loopRunning) {
    return {
      kind: "monitor",
      tone: "cyan",
      eyebrow: "Läuft gerade",
      title: "Beobachten, nicht neu starten.",
      detail: "Der Research-Loop arbeitet. Warte auf neue Vorschläge oder stoppe ihn bewusst, wenn der Lauf nicht mehr passt.",
      primaryLabel: "Lauf ansehen",
    };
  }
  if (input.openCount > 0) {
    return {
      kind: "review",
      tone: input.openCount > 3 ? "amber" : "emerald",
      eyebrow: "Nächster sinnvoller Schritt",
      title: `${input.openCount} geprüfte ${input.openCount === 1 ? "Verbesserung" : "Verbesserungen"} entscheiden.`,
      detail: input.revertedCount > 0
        ? `${input.revertedCount} zurückgerollte Kandidaten sind bereits sicher aussortiert. Entscheide zuerst die offenen Karten.`
        : "Lies zuerst Nutzen und Risiko der obersten Karte. Annehmen startet die geschützte Prüfung, Ablehnen räumt auf.",
      primaryLabel: "Entscheidungen prüfen",
    };
  }
  if (statusUnknown) {
    return {
      kind: "inspect",
      tone: "amber",
      eyebrow: "Status offen",
      title: "Erst Status prüfen.",
      detail: "Der Autoresearch-Status ist noch nicht geladen. Warte kurz oder prüfe die Loop-Steuerung, bevor du neue Kandidaten startest.",
      primaryLabel: "Status ansehen",
    };
  }
  return {
    kind: "generate",
    tone: "violet",
    eyebrow: "Alles sauber",
    title: "Neue Kandidaten holen.",
    detail: "Es liegen keine offenen Entscheidungen an. Starte einen gezielten Scan oder einen kleinen Dry-Run.",
    primaryLabel: "Vorschläge holen",
  };
}
