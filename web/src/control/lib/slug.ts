// Heading-Slugs + Inhaltsverzeichnis-Extraktion fürs Bibliothek-Nachschlagewerk.
// EINE Quelle der Wahrheit: `slugifyHeading` wird sowohl von `extractToc` (baut
// die TOC-Liste) als auch von `ProseMarkdown` (vergibt die Heading-`id`s)
// genutzt — so zeigen die TOC-Anker garantiert auf die gerenderten Überschriften.
// Bewusst OHNE Dedup: identische Überschriften teilen sich denselben Slug
// (Klick scrollt zur ersten) — beide Seiten bleiben dadurch deckungsgleich.

export interface TocEntry {
  level: number; // 1–3
  text: string;
  slug: string;
}

/** Markdown-Heading-Text → URL-/id-tauglicher Slug (Unicode-Buchstaben/Ziffern
 *  bleiben erhalten, inkl. Umlaute). */
export function slugifyHeading(text: string): string {
  return text
    .toLowerCase()
    .replace(/`/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1") // [Label](url) → Label
    .replace(/[*_~]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, "-")
    .replace(/-{2,}/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** ATX-Überschriften (#, ##, ###) eines Markdown-Dokuments als TOC. Code-Fences
 *  (```) werden übersprungen, damit `# kommentar` in Code nicht zählt. */
export function extractToc(markdown: string): TocEntry[] {
  const out: TocEntry[] = [];
  let fenced = false;
  for (const raw of markdown.split("\n")) {
    const line = raw.trimEnd();
    if (line.trimStart().startsWith("```")) {
      fenced = !fenced;
      continue;
    }
    if (fenced) continue;
    const m = /^(#{1,3})\s+(.+?)\s*#*$/.exec(line);
    if (!m) continue;
    const text = m[2].trim();
    // Fallback identisch zu ProseMarkdown (id=slugifyHeading||"abschnitt"),
    // damit der TOC-Anker auch bei leer-slugbaren Headings deckungsgleich ist.
    const slug = slugifyHeading(text) || "abschnitt";
    out.push({ level: m[1].length, text, slug });
  }
  return out;
}
