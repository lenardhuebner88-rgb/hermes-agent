import type { ToneName } from "./types";

export type AutoresearchAdvancedGuideKind = "models" | "deep-audit" | "test-foundry";

export interface AutoresearchAdvancedGuideItem {
  kind: AutoresearchAdvancedGuideKind;
  label: string;
  title: string;
  when: string;
  cost: string;
  safety: string;
  tone: ToneName;
}

export const AUTORESEARCH_ADVANCED_GUIDE = [
  {
    kind: "models",
    label: "Routing",
    title: "Modelle nur zuweisen, wenn Auto nicht reicht.",
    when: "Für Spezialfälle, in denen eine Lane bewusst ein anderes Modell braucht.",
    cost: "Keine direkte Laufkosten-Aktion; ändert nur die nächste Ausführung.",
    safety: "Speichert nur die Modellwahl. Offene Karten und Vorschläge bleiben unverändert.",
    tone: "zinc",
  },
  {
    kind: "deep-audit",
    label: "Teuer",
    title: "Deep-Audit nur für gezielte Subsystem-Fragen.",
    when: "Wenn du ein konkretes Risiko in einem Bereich vermutest.",
    cost: "Sehr hoch: typischerweise 1-2 Mio Token pro Lauf.",
    safety: "Schreibt keinen Code. Findings landen später als Review-Karten.",
    tone: "amber",
  },
  {
    kind: "test-foundry",
    label: "Tests",
    title: "Test-Foundry härtet Tests, nicht Produktfunktionen.",
    when: "Wenn vorhandene Tests wahrscheinlich Lücken haben.",
    cost: "Mittel: kostet vor allem Laufzeit.",
    safety: "Ohne Auto-Apply nur Review-Karten; Auto-Apply bleibt auf separatem Branch.",
    tone: "cyan",
  },
] as const satisfies readonly AutoresearchAdvancedGuideItem[];
