// Bibliothek → Wissen/Kanon (Nachschlagewerk): Typen + kleine reine Helfer.
// Ausgelagert aus den Komponentendateien (react-refresh/only-export-components)
// und für Unit-Tests importierbar.
import { PROSE_DEAD_LINK_SCHEME, PROSE_INTERNAL_LINK_SCHEME } from "../../components/ProseMarkdown";
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

/** Ein Eintrag im "Wissens-Puls" — jüngste Zeile aus dem cron-gepflegten
 *  llm-wiki model-log (siehe hermes_cli/library_knowledge.py `_model_log_pulse`). */
export interface KnowledgePulseItem {
  date: string;
  model: string;
  detail: string;
}

export interface KnowledgeCollection {
  id: string;
  title: string;
  description: string;
  accent: ToneName;
  icon: string;
  /** Gesamtzahl Docs der Sammlung (Backend-Feld, unabhängig von Client-Filtern). */
  doc_count: number;
  /** jüngste mtime aller Docs der Sammlung, 0 = unbekannt/leer. */
  updated_ts: number;
  /** nur llm-wiki: die letzten Modell-Discovery-Einträge, neuestes zuerst. */
  pulse?: KnowledgePulseItem[];
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
    case "model":
      return "Modelle";
    case "report":
      return "Reports";
    case "guide":
      return "Guides";
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
    ["model", 3],
    ["query", 4],
    ["overview", 5],
    ["synthesis", 6],
    ["implementation", 7],
    ["planspec", 8],
    ["plan", 9],
    ["skill", 10],
    ["rolle", 11],
    ["doc", 12],
    ["lint", 13],
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

// --- Obsidian-Wikilinks (llm-wiki) → interne Navigation ---------------------
// Seiten unter ~/llm-wiki/wiki verlinken sich per Obsidian-Wikilink:
// `[[wiki/concepts/foo|Label]]` oder `[[wiki/concepts/foo]]`. rel-Format
// spiegelt hermes_cli/library_knowledge.py `_LLM_WIKI_REL_RE`: pro Segment
// Kleinbuchstaben/Ziffern/„-"/„_", endet auf `.md`. Nur `wiki/`-präfigierte
// Ziele in diesem Format bilden eine gültige Doc-Id (`kb::llm::<rel>`) —
// alles andere (`[[index]]`, `[[log]]`, kaputte Slugs) ist ein toter Link.
const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;
const LLM_WIKI_REL_RE = /^(?:[a-z0-9][a-z0-9_-]*\/)*[a-z0-9][a-z0-9_-]*\.md$/;

/** "concepts/raw-wiki-schema.md" → "Raw Wiki Schema" (letztes Segment,
 *  Bindestriche zu Leerzeichen, Titel-Case) — Fallback-Label ohne Alias,
 *  spiegelt den Titel-Fallback im Backend (`Path(rel).stem…title()`). */
function humanizeWikiSlug(relMd: string): string {
  const stem = relMd.slice(relMd.lastIndexOf("/") + 1).replace(/\.md$/, "");
  return stem
    .split("-")
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function resolveWikiLinkMatch(_full: string, rawTarget: string, rawLabel?: string): string {
  const target = rawTarget.trim().replace(/\s+/g, " ");
  const rel = target.startsWith("wiki/") ? target.slice("wiki/".length) : target;
  const relMd = rel.endsWith(".md") ? rel : `${rel}.md`;
  if (target.startsWith("wiki/") && LLM_WIKI_REL_RE.test(relMd)) {
    const label = (rawLabel ?? humanizeWikiSlug(relMd)).trim().replace(/\s+/g, " ");
    const id = `kb::llm::${relMd}`;
    return `[${label}](${PROSE_INTERNAL_LINK_SCHEME}${encodeURIComponent(id)})`;
  }
  const label = (rawLabel ?? target).trim().replace(/\s+/g, " ");
  return `[${label}](${PROSE_DEAD_LINK_SCHEME}${encodeURIComponent(target)})`;
}

/** Wikilinks im Markdown-Rohtext einer llm-wiki-Seite durch normale
 *  Markdown-Links mit `internal-link:`/`dead-link:`-Schema ersetzen
 *  (Preprocessing vor `ProseMarkdown`, siehe deren `onInternalLink`).
 *  Fenced-Code-Blöcke (```) bleiben unangetastet. Innerhalb eines Absatzes
 *  (durch Leerzeilen begrenzt) wird über Zeilenumbrüche hinweg gematcht,
 *  damit ein hart umgebrochenes Alias (`[[ziel|label\nfortsetzung]]`) — wie
 *  in llm-wiki-Quelltext, der bei ~80 Spalten umbricht — noch aufgelöst
 *  wird, statt als rohes `[[...]]` stehen zu bleiben. */
export function resolveWikiLinks(bodyMd: string): string {
  const lines = bodyMd.split("\n");
  const out: string[] = [];
  let fenced = false;
  let paragraph: string[] = [];

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    const resolved = paragraph.join("\n").replace(WIKILINK_RE, resolveWikiLinkMatch);
    out.push(...resolved.split("\n"));
    paragraph = [];
  };

  for (const line of lines) {
    if (line.trimStart().startsWith("```")) {
      flushParagraph();
      out.push(line);
      fenced = !fenced;
      continue;
    }
    if (fenced) {
      out.push(line);
      continue;
    }
    if (line.trim() === "") {
      flushParagraph();
      out.push(line);
      continue;
    }
    paragraph.push(line);
  }
  flushParagraph();

  return out.join("\n");
}
