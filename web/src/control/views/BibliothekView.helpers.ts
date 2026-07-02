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

/** Frontpage-Auswahl: das neueste Item je Kategorie. Erwartet `items`
 *  ts-absteigend (so liefert der Server sie) — pro Kategorie gewinnt das erste. */
export function newestPerCategory(items: LibraryItem[]): LibraryItem[] {
  const seen = new Set<string>();
  const top: LibraryItem[] = [];
  for (const item of items) {
    if (!seen.has(item.category)) {
      seen.add(item.category);
      top.push(item);
    }
  }
  return top;
}

/** Anzahl Einträge je Kategorie — Zähler an den Filter-Chips. */
export function countByCategory(items: LibraryItem[]): Record<string, number> {
  const counts: Record<string, number> = {};
  for (const item of items) counts[item.category] = (counts[item.category] ?? 0) + 1;
  return counts;
}

/** Vor-/Zurück-Navigation innerhalb derselben Serie. Die Liste ist
 *  neueste-zuerst: "ältere" (`prev`) = idx+1, "neuere" (`next`) = idx-1. */
export function seriesNeighbors(
  items: LibraryItem[],
  current: LibraryItem | null,
): { prev: LibraryItem | null; next: LibraryItem | null } {
  if (!current) return { prev: null, next: null };
  const series = items.filter((i) => i.series_id === current.series_id);
  const idx = series.findIndex((i) => i.id === current.id);
  return {
    prev: idx >= 0 && idx + 1 < series.length ? series[idx + 1] : null,
    next: idx > 0 ? series[idx - 1] : null,
  };
}

/** Sortier-Kontrolle Lesesaal (S5): "Neueste" ist bereits die Server-
 *  Reihenfolge (ts-absteigend) — die anderen Modi sortieren clientseitig über
 *  die bislang geladenen Items. */
export type LesesaalSort = "newest" | "oldest" | "az";

export function sortItems(items: LibraryItem[], sort: LesesaalSort): LibraryItem[] {
  if (sort === "newest") return items;
  const copy = [...items];
  if (sort === "oldest") copy.sort((a, b) => a.ts - b.ts);
  else copy.sort((a, b) => a.title.localeCompare(b.title, "de"));
  return copy;
}

/** "Mehr laden" (S6): angehängte Seiten nach `id` deduplizieren — der Server
 *  liefert bei Rand-Overlaps (neue Items zwischen zwei Requests) sonst
 *  doppelte Zeilen in der Liste. */
export function dedupeById<T extends { id: string }>(items: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}
