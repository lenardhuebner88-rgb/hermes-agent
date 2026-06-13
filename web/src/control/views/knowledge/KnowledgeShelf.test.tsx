import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CollectionSection, DocCard } from "./KnowledgeShelf";
import { TocNav } from "./KnowledgeReader";
import { sectionsLabel, totalDocs, type KnowledgeCollection, type KnowledgeDoc } from "./knowledge.helpers";

const doc = (over: Partial<KnowledgeDoc>): KnowledgeDoc => ({
  id: "kb::doc::canon-infra-topology",
  collection: "kanon",
  title: "Infrastruktur & Topologie",
  summary: "Ports, Pfade, Services.",
  source_ref: "vault/00-Canon/infra-topology.md",
  tags: ["topologie", "ports"],
  updated_ts: 1000,
  heading_count: 4,
  ...over,
});

describe("DocCard", () => {
  it("zeigt Titel, Kurzbeschreibung, Quelle, Abschnittszahl und Tags", () => {
    const html = renderToStaticMarkup(<DocCard doc={doc({})} onOpen={() => {}} />);
    expect(html).toContain("Infrastruktur &amp; Topologie");
    expect(html).toContain("Ports, Pfade, Services.");
    expect(html).toContain("vault/00-Canon/infra-topology.md");
    expect(html).toContain("4 Abschnitte");
    expect(html).toContain("topologie");
    expect(html).toContain("ports");
  });

  it("blendet Abschnittszahl bei 0 Headings aus", () => {
    const html = renderToStaticMarkup(<DocCard doc={doc({ heading_count: 0 })} onOpen={() => {}} />);
    expect(html).not.toContain("Abschnitt");
  });
});

describe("CollectionSection", () => {
  const collection: KnowledgeCollection = {
    id: "kanon",
    title: "Kanon — Die geteilte Wahrheit",
    description: "Dauerhafte, agent-übergreifende Fakten.",
    accent: "cyan",
    icon: "Landmark",
    docs: [doc({ id: "a", title: "Doc A" }), doc({ id: "b", title: "Doc B" })],
  };

  it("rendert Titel, Beschreibung, Doc-Zahl und beide Karten", () => {
    const html = renderToStaticMarkup(<CollectionSection collection={collection} onOpen={() => {}} />);
    expect(html).toContain("Kanon — Die geteilte Wahrheit");
    expect(html).toContain("Dauerhafte, agent-übergreifende Fakten.");
    expect(html).toContain("2 Dokumente");
    expect(html).toContain("Doc A");
    expect(html).toContain("Doc B");
  });
});

describe("TocNav", () => {
  it("rendert Einträge in Reihenfolge", () => {
    const html = renderToStaticMarkup(
      <TocNav
        entries={[
          { level: 1, text: "Topologie", slug: "topologie" },
          { level: 2, text: "Ports", slug: "ports" },
        ]}
        onJump={() => {}}
      />,
    );
    expect(html).toContain("Topologie");
    expect(html).toContain("Ports");
  });

  it("zeigt einen Hinweis, wenn es keine Abschnitte gibt", () => {
    const html = renderToStaticMarkup(<TocNav entries={[]} onJump={() => {}} />);
    expect(html).toContain("Keine Abschnitte");
  });
});

describe("helpers", () => {
  it("sectionsLabel ist grammatisch korrekt", () => {
    expect(sectionsLabel(1)).toBe("1 Abschnitt");
    expect(sectionsLabel(3)).toBe("3 Abschnitte");
  });

  it("totalDocs summiert über Sammlungen", () => {
    expect(totalDocs([
      { id: "a", title: "", description: "", accent: "cyan", icon: "", docs: [doc({}), doc({})] },
      { id: "b", title: "", description: "", accent: "amber", icon: "", docs: [doc({})] },
    ])).toBe(3);
  });
});
