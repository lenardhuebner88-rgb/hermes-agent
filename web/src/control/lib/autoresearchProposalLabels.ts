export interface ProposalCategoryLabel {
  label: string;
  help: string | null;
}

const CATEGORY_LABELS: Record<string, ProposalCategoryLabel> = {
  bug_risk: {
    label: "Bug-Risiko",
    help: "Kann zu falschem Verhalten oder schwer nachvollziehbaren Fehlern führen.",
  },
  dead_logic: {
    label: "Tote Logik",
    help: "Code oder Regeln wirken nicht mehr so, wie sie gemeint waren.",
  },
  error_handling: {
    label: "Fehlerbehandlung",
    help: "Macht Fehlfälle sichtbarer oder robuster.",
  },
  info_leak: {
    label: "Geheimnis sichtbar",
    help: "Token, Zugangsdaten oder interne Details könnten nach außen sichtbar werden.",
  },
  mutation_survivor: {
    label: "Test-Lücke",
    help: "Ein simulierter Fehler wurde von den Tests nicht erkannt.",
  },
  contradiction: {
    label: "Widerspruch",
    help: "Die bestehende Anleitung widerspricht sich oder führt in die falsche Richtung.",
  },
  stale: {
    label: "Veraltet",
    help: "Die Anleitung passt nicht mehr zum aktuellen Systemzustand.",
  },
  missing_trigger: {
    label: "Auslöser fehlt",
    help: "Es fehlt eine klare Regel, wann diese Fähigkeit überhaupt starten soll.",
  },
  unclear_trigger: {
    label: "Auslöser unklar",
    help: "Ein Startpunkt ist vorhanden, aber für Nutzer nicht eindeutig genug.",
  },
  incomplete_steps: {
    label: "Schritte fehlen",
    help: "Die Anleitung lässt wichtige Zwischenschritte aus.",
  },
  missing_section: {
    label: "Abschnitt fehlt",
    help: "Eine nützliche Struktur oder Erklärung fehlt noch.",
  },
};

function titleCaseToken(value: string): string {
  return value ? `${value.charAt(0).toUpperCase()}${value.slice(1)}` : value;
}

export function formatProposalCategory(raw: string | null | undefined): ProposalCategoryLabel | null {
  const category = raw?.trim();
  if (!category) return null;
  const known = CATEGORY_LABELS[category];
  if (known) return known;
  return {
    label: category.split(/[_\s-]+/).filter(Boolean).map(titleCaseToken).join(" "),
    help: `Backend-Kategorie: ${category}.`,
  };
}
