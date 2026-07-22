// Ausgelagert aus BibliothekView.tsx (react-refresh/only-export-components):
// Komponentendateien exportieren nur Komponenten; Test-/Nachbar-Helper leben
// in Sibling-Modulen.
import type { LibraryItem } from "./BibliothekView";

// ---------------------------------------------------------------------------
// P6a — Provenienz (Herkunft): Vertragstypen + verständliche Labels.
// Spiegelt den Backend-Vertrag aus hermes_cli/library_view.py (_build_provenance).
// ---------------------------------------------------------------------------

export type ProvenanceStatus = "evidenced" | "partial" | "unknown";

export interface LibraryProvenanceChain {
  auftraggeber: string;
  delegation: string;
  autor: string;
  review: string;
  ablage: string;
}

export interface LibraryProvenance {
  producer: string;
  path: string;
  status: ProvenanceStatus | string;
  chain: LibraryProvenanceChain;
  refs: string[];
}

// ---------------------------------------------------------------------------
// P6b — Korrektur-Overlay: Original + aktive Felder + Audit. Spiegelt den
// Backend-Vertrag aus hermes_cli/library_corrections.py (apply → correction-Block).
// Der effektive Wert steht in `provenance` selbst; dieser Block hält den
// Ursprung und die Historie additiv sichtbar.
// ---------------------------------------------------------------------------

/** Unveränderlicher Originalsnapshot des abgeleiteten Vertrags (vor Korrektur). */
export interface CorrectionOriginal {
  producer: string;
  path: string;
  status: ProvenanceStatus | string;
  chain: LibraryProvenanceChain;
}

export interface CorrectionHistoryEntry {
  at: number;
  action: "set" | "revert";
  fields: Record<string, string>;
  reason: string;
  actor: string;
}

export interface LibraryCorrection {
  item_id: string;
  active: boolean;
  /** Aktive Overrides (kanonische Felder: path + die fünf Rollen). */
  fields: Record<string, string>;
  original: CorrectionOriginal;
  reason: string;
  actor: string;
  created_at: number;
  updated_at: number;
  history: CorrectionHistoryEntry[];
}

/** Feste Weg-Werte für die Korrektur-Auswahl (keine freien Wegtypen). */
export const PATH_OPTIONS = ["Cron", "Task", "Receipt", "Manuell", "Unbekannt"] as const;

export interface FacetCount {
  value: string;
  count: number;
}

/** Weg → verständliches Label (technische Untertypen leben nur in den Refs). */
export const PATH_LABEL: Record<string, string> = {
  Cron: "Cron",
  Task: "Task",
  Receipt: "Receipt",
  Manuell: "Manuell",
  Unbekannt: "Unbekannt",
};

export const PROVENANCE_STATUS_LABEL: Record<string, string> = {
  evidenced: "vollständig belegt",
  partial: "teilweise belegt",
  unknown: "unbekannt",
};

export const CHAIN_ROLE_LABEL: Record<string, string> = {
  auftraggeber: "Auftraggeber",
  delegation: "Delegation",
  autor: "Autor",
  review: "Review",
  ablage: "Ablage",
};

export const CHAIN_ROLE_ORDER = ["auftraggeber", "delegation", "autor", "review", "ablage"] as const;

export function pathLabel(path: string | undefined): string {
  return PATH_LABEL[path ?? ""] ?? path ?? "Unbekannt";
}

export function provenanceStatusLabel(status: string | undefined): string {
  return PROVENANCE_STATUS_LABEL[status ?? ""] ?? "unbekannt";
}

/** Kompaktes Zeilen-Label "Erzeuger · Weg" (null ohne Provenienz-Vertrag). */
export function provenanceRowLabel(provenance: LibraryProvenance | undefined): string | null {
  if (!provenance) return null;
  return `${provenance.producer || "Unbekannt"} · ${pathLabel(provenance.path)}`;
}

export const CATEGORY_LABEL: Record<string, string> = {
  news: "News",
  briefings: "Briefings",
  recherchen: "Recherchen",
  familie: "Familie",
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

/** Briefings-Filter: alle Items der Kategorie `briefings`, neueste zuerst. */
export function filterBriefings(items: LibraryItem[]): LibraryItem[] {
  return items.filter((i) => i.category === "briefings");
}

/** Neuestes Briefing (für Featured-Kachel). */
export function newestBriefing(items: LibraryItem[]): LibraryItem | null {
  return filterBriefings(items)[0] ?? null;
}

/** Bereinigt einen Markdown-Vorschautext für Karten/Zeilenvorschauen:
 *  Heading-Marker, Inline-Emphasis, Code-Backticks und Links werden entfernt,
 *  der sichtbare Text bleibt erhalten. Kein globales Zeichen-Strippen, damit
 *  Identifiers wie `t_7ab7e21a` oder `GPT-5.6` intakt bleiben. */
export function plainMarkdownPreview(value: string): string {
  return (
    value
      // Heading-Marker am Zeilenanfang
      .replace(/^#{1,6}\s+/gm, "")
      // Heading-Marker als eigenständiges Token (Anfang oder nach Whitespace)
      .replace(/(^|\s)#{1,6}\s+/g, "$1")
      // Fett **…**
      .replace(/\*\*(.+?)\*\*/g, "$1")
      // Kursiv *…* (nicht Teil eines **-Paars; Wortgrenzen vermeiden Strippen in IDs)
      .replace(/(?<![A-Za-z0-9_])\*(?!\*)(.+?)(?<!\*)\*(?![A-Za-z0-9_])/g, "$1")
      // Kursiv _…_ (wortumschließend, damit Unterstriche in IDs erhalten bleiben)
      .replace(/(?<![A-Za-z0-9_])_(?=\S)(.+?)(?<=\S)_(?![A-Za-z0-9_])/g, "$1")
      // Inline-Code `…`
      .replace(/`([^`]+)`/g, "$1")
      // Markdown-Links [Label](url) → Label
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      // Mehrfach-Whitespace flachziehen (Vorschau bleibt einzeilig)
      .replace(/\s+/g, " ")
      .trim()
  );
}
