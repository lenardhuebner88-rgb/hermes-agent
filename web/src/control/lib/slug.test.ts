import { describe, expect, it } from "vitest";
import { extractToc, slugifyHeading } from "./slug";

describe("slugifyHeading", () => {
  it("normalisiert Text, behält Umlaute, kappt Ränder", () => {
    expect(slugifyHeading("Konventionen & Gates")).toBe("konventionen-gates");
    expect(slugifyHeading("Ports / Pfade")).toBe("ports-pfade");
    expect(slugifyHeading("Übersicht")).toBe("übersicht");
  });

  it("entfernt Markdown-Auszeichnung (Code, Fett, Links)", () => {
    expect(slugifyHeading("Heading `code` **fett**")).toBe("heading-code-fett");
    expect(slugifyHeading("[Label](https://x.y)")).toBe("label");
  });
});

describe("extractToc", () => {
  it("liest #/##/### in Reihenfolge mit Level + Slug", () => {
    const toc = extractToc("# A\n\nText\n\n## B C\n\n### D\n");
    expect(toc).toEqual([
      { level: 1, text: "A", slug: "a" },
      { level: 2, text: "B C", slug: "b-c" },
      { level: 3, text: "D", slug: "d" },
    ]);
  });

  it("überspringt Überschriften in Code-Fences und tiefere Ebenen", () => {
    const toc = extractToc("# Echt\n\n```\n# nicht\n## auch nicht\n```\n\n#### zu tief\n\n## Zweiter\n");
    expect(toc.map((t) => t.text)).toEqual(["Echt", "Zweiter"]);
  });

  it("dedupt NICHT — gleiche Überschrift, gleicher Slug (deckungsgleich mit Renderer)", () => {
    const toc = extractToc("## Wartung\n\n## Wartung\n");
    expect(toc.map((t) => t.slug)).toEqual(["wartung", "wartung"]);
  });
});
