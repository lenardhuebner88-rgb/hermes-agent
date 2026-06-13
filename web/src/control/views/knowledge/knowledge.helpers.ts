// Bibliothek → Wissen/Kanon (Nachschlagewerk): Typen + kleine reine Helfer.
// Ausgelagert aus den Komponentendateien (react-refresh/only-export-components)
// und für Unit-Tests importierbar.
import type { ToneName } from "../../lib/types";

export interface KnowledgeDoc {
  id: string;
  collection: string;
  title: string;
  summary: string;
  source_ref: string;
  tags: string[];
  updated_ts: number;
  heading_count: number;
}

export interface KnowledgeCollection {
  id: string;
  title: string;
  description: string;
  accent: ToneName;
  icon: string;
  docs: KnowledgeDoc[];
}

export interface KnowledgeCatalog {
  collections: KnowledgeCollection[];
  count: number;
  query: string;
  now: number;
}

export type KnowledgeDocDetail = KnowledgeDoc & { body_md: string };

/** "3 Abschnitte" / "1 Abschnitt" — grammatisch korrekt. */
export function sectionsLabel(n: number): string {
  return `${n} Abschnitt${n === 1 ? "" : "e"}`;
}

/** Gesamtzahl Dokumente über alle Sammlungen. */
export function totalDocs(collections: KnowledgeCollection[]): number {
  return collections.reduce((sum, c) => sum + c.docs.length, 0);
}
