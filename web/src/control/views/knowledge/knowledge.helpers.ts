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
  created?: string;
  owner?: string;
  type?: string;
  status?: string;
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

export interface KnowledgeTypeCount {
  id: string;
  label: string;
  count: number;
}

/** "3 Abschnitte" / "1 Abschnitt" — grammatisch korrekt. */
export function sectionsLabel(n: number): string {
  return `${n} Abschnitt${n === 1 ? "" : "e"}`;
}

/** Gesamtzahl Dokumente über alle Sammlungen. */
export function totalDocs(collections: KnowledgeCollection[]): number {
  return collections.reduce((sum, c) => sum + c.docs.length, 0);
}

export function allDocs(collections: KnowledgeCollection[]): KnowledgeDoc[] {
  return collections.flatMap((collection) => collection.docs);
}

export function knowledgeType(doc: KnowledgeDoc): string {
  const typeTag = doc.tags.find((tag) => tag.startsWith("type:"));
  if (typeTag) return typeTag.slice("type:".length);
  if (doc.collection === "vault-plans") return "plan";
  if (doc.collection === "skills") return "skill";
  if (doc.collection === "rollen") return "rolle";
  if (doc.collection === "kanon" || doc.collection === "orchestrierung") return "doc";
  return "page";
}

export function knowledgeTypeLabel(type: string): string {
  switch (type) {
    case "concept":
      return "Konzepte";
    case "entity":
      return "Entitäten";
    case "source":
      return "Quellen";
    case "query":
      return "Antworten";
    case "lint":
      return "Checks";
    case "overview":
      return "Überblick";
    case "synthesis":
      return "Synthesen";
    case "implementation":
      return "Implementierung";
    case "planspec":
      return "PlanSpec";
    case "plan":
      return "Pläne";
    case "skill":
      return "Skills";
    case "rolle":
      return "Rollen";
    case "doc":
      return "Dokumente";
    default:
      return type;
  }
}

export function typeCounts(collections: KnowledgeCollection[]): KnowledgeTypeCount[] {
  const counts = new Map<string, number>();
  for (const doc of allDocs(collections)) {
    const type = knowledgeType(doc);
    counts.set(type, (counts.get(type) ?? 0) + 1);
  }
  const rank = new Map([
    ["concept", 0],
    ["entity", 1],
    ["source", 2],
    ["query", 3],
    ["overview", 4],
    ["synthesis", 5],
    ["implementation", 6],
    ["planspec", 7],
    ["plan", 8],
    ["skill", 9],
    ["rolle", 10],
    ["doc", 11],
    ["lint", 12],
  ]);
  return Array.from(counts.entries())
    .map(([id, count]) => ({ id, label: knowledgeTypeLabel(id), count }))
    .sort((a, b) => (rank.get(a.id) ?? 99) - (rank.get(b.id) ?? 99) || a.label.localeCompare(b.label));
}

export function filterCatalog(
  catalog: KnowledgeCatalog,
  collectionId: string | null,
  typeId: string | null,
): KnowledgeCatalog {
  const collections = catalog.collections
    .filter((collection) => !collectionId || collection.id === collectionId)
    .map((collection) => ({
      ...collection,
      docs: collection.docs.filter((doc) => !typeId || knowledgeType(doc) === typeId),
    }))
    .filter((collection) => collection.docs.length > 0);
  return {
    ...catalog,
    collections,
    count: totalDocs(collections),
  };
}
