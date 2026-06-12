// Ausgelagert aus BibliothekView.tsx (react-refresh/only-export-components):
// Komponentendateien exportieren nur Komponenten; Test-/Nachbar-Helper leben
// in Sibling-Modulen.
import type { LibraryItem } from "./BibliothekView";

export const CATEGORY_LABEL: Record<string, string> = {
  news: "News",
  briefings: "Briefings",
  recherchen: "Recherchen",
  familie: "Familie",
  arbeit: "Arbeit & Receipts",
  receipts: "Receipts",
  wartung: "Wartung",
};

/** Serien-Gruppierung fürs Regal (exportiert für den Test). */
export function groupBySeries(items: LibraryItem[]): { seriesId: string; series: string; meta: string; items: LibraryItem[] }[] {
  const groups = new Map<string, { seriesId: string; series: string; meta: string; items: LibraryItem[] }>();
  for (const item of items) {
    let g = groups.get(item.series_id);
    if (!g) {
      g = { seriesId: item.series_id, series: item.series, meta: item.series_meta, items: [] };
      groups.set(item.series_id, g);
    }
    g.items.push(item);
  }
  return [...groups.values()].sort((a, b) => (b.items[0]?.ts ?? 0) - (a.items[0]?.ts ?? 0));
}
