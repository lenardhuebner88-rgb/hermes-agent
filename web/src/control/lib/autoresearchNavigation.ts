export interface AutoresearchSectionNavItem {
  id: "autoresearch-queue" | "autoresearch-loop" | "autoresearch-history" | "autoresearch-advanced";
  label: string;
  detail: string;
  kind: "review" | "run" | "history" | "advanced";
}

export const AUTORESEARCH_SECTION_NAV = [
  {
    id: "autoresearch-queue",
    label: "Queue",
    detail: "Entscheidungen prüfen",
    kind: "review",
  },
  {
    id: "autoresearch-loop",
    label: "Probelauf",
    detail: "neue Kandidaten suchen",
    kind: "run",
  },
  {
    id: "autoresearch-history",
    label: "Verlauf",
    detail: "letzte Wirkung lesen",
    kind: "history",
  },
  {
    id: "autoresearch-advanced",
    label: "Erweitert",
    detail: "Spezialläufe und Modelle",
    kind: "advanced",
  },
] as const satisfies readonly AutoresearchSectionNavItem[];
