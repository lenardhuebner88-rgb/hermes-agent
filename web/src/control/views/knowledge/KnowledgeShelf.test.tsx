import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CollectionSection, DocCard } from "./KnowledgeShelf";
import { TocNav } from "./KnowledgeReader";
import {
  filterCatalog,
  knowledgeType,
  knowledgeTypeLabel,
  sectionsLabel,
  totalDocs,
  typeCounts,
  type KnowledgeCatalog,
  type KnowledgeCollection,
  type KnowledgeDoc,
} from "./knowledge.helpers";

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

const vaultPlan = (over: Partial<KnowledgeDoc> = {}): KnowledgeDoc => doc({
  id: "kb::plan::Hermes/plans/dashboard-refresh.md",
  collection: "vault-plans",
  title: "Dashboard Refresh",
  summary: "Widgets härten.",
  source_ref: "Hermes/plans/dashboard-refresh.md",
  tags: ["vault-plans", "owner:Hermes", "status:active"],
  created: "2026-07-01",
  owner: "Hermes",
  type: "implementation",
  status: "active",
  ...over,
});

describe("DocCard", () => {
  it("zeigt Typ, Titel, Kurzbeschreibung, Quelle, Abschnittszahl und Tags", () => {
    const html = renderToStaticMarkup(<DocCard doc={doc({})} onOpen={() => {}} />);
    expect(html).toContain("Dokumente");
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
    doc_count: 2,
    updated_ts: 1000,
    docs: [doc({ id: "a", title: "Doc A" }), doc({ id: "b", title: "Doc B" })],
  };

  it("rendert Titel, Beschreibung, Doc-Zahl und beide Karten", () => {
    const html = renderToStaticMarkup(<CollectionSection collection={collection} now={1000} onOpen={() => {}} />);
    expect(html).toContain("Kanon — Die geteilte Wahrheit");
    expect(html).toContain("Dauerhafte, agent-übergreifende Fakten.");
    expect(html).toContain("2 Dokumente");
    expect(html).toContain("Doc A");
    expect(html).toContain("Doc B");
  });

  it("zeigt einen „aktualisiert vor X“-Chip aus updated_ts/now", () => {
    const html = renderToStaticMarkup(<CollectionSection collection={collection} now={1000 + 3600} onOpen={() => {}} />);
    expect(html).toContain("aktualisiert vor 1h");
  });

  it("blendet den Chip aus, wenn updated_ts 0 ist (leere Sammlung)", () => {
    const html = renderToStaticMarkup(
      <CollectionSection collection={{ ...collection, updated_ts: 0 }} now={1000} onOpen={() => {}} />,
    );
    expect(html).not.toContain("aktualisiert vor");
  });

  it("rendert den Wissens-Puls-Strip nur, wenn `pulse` gesetzt ist", () => {
    const withoutPulse = renderToStaticMarkup(<CollectionSection collection={collection} now={1000} onOpen={() => {}} />);
    expect(withoutPulse).not.toContain("Neu entdeckt:");

    const withPulse = renderToStaticMarkup(
      <CollectionSection
        collection={{
          ...collection,
          pulse: [
            { date: "2026-07-02", model: "x-ai/grok-build-0.1", detail: "context 256k, $1.00/$2.00 per 1M" },
            { date: "2026-07-01", model: "google/gemini-3.5-flash", detail: "context 1M, $1.50/$9.00 per 1M" },
          ],
        }}
        now={1000}
        onOpen={() => {}}
      />,
    );
    expect(withPulse).toContain("Neu entdeckt:");
    expect(withPulse).toContain("x-ai/grok-build-0.1");
    expect(withPulse).toContain("2026-07-02");
    expect(withPulse).toContain("google/gemini-3.5-flash");
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
      { id: "a", title: "", description: "", accent: "cyan", icon: "", doc_count: 2, updated_ts: 0, docs: [doc({}), doc({})] },
      { id: "b", title: "", description: "", accent: "amber", icon: "", doc_count: 1, updated_ts: 0, docs: [doc({})] },
    ])).toBe(3);
  });

  it("knowledgeType liest type:-Tags und hat Collection-Fallbacks", () => {
    expect(knowledgeType(doc({ tags: ["llm-wiki", "type:concept"] }))).toBe("concept");
    expect(knowledgeType(vaultPlan())).toBe("plan");
    expect(knowledgeType(doc({ collection: "skills", tags: [] }))).toBe("skill");
    expect(knowledgeTypeLabel("concept")).toBe("Konzepte");
    expect(knowledgeTypeLabel("implementation")).toBe("Implementierung");
  });

  it("knowledgeTypeLabel übersetzt den neuen models-Typ", () => {
    expect(knowledgeType(doc({ tags: ["llm-wiki", "type:model"] }))).toBe("model");
    expect(knowledgeTypeLabel("model")).toBe("Modelle");
  });

  it("akzeptiert das Vault-Plans-Backend-Schema inklusive Metadaten, Icon und Akzent", () => {
    const plan = vaultPlan({ tags: ["vault-plans", "type:planspec", "status:done"] });
    const catalog: KnowledgeCatalog = {
      collections: [
        {
          id: "vault-plans",
          title: "Vault Plans",
          description: "Vault-Plan-Dokumente aus 03-Agents.",
          accent: "rose",
          icon: "Newspaper",
          doc_count: 1,
          updated_ts: plan.updated_ts,
          docs: [plan],
        },
      ],
      count: 1,
      query: "",
      now: 1,
    };

    expect(plan.created).toBe("2026-07-01");
    expect(plan.owner).toBe("Hermes");
    expect(plan.type).toBe("implementation");
    expect(plan.status).toBe("active");
    expect(catalog.collections[0].accent).toBe("rose");
    expect(catalog.collections[0].icon).toBe("Newspaper");
    expect(typeCounts(catalog.collections)).toEqual([{ id: "planspec", label: "PlanSpec", count: 1 }]);
  });

  it("typeCounts sortiert bekannte Typen stabil", () => {
    const collections: KnowledgeCollection[] = [
      {
        id: "llm-wiki",
        title: "LLM-Wiki",
        description: "",
        accent: "indigo",
        icon: "Brain",
        doc_count: 4,
        updated_ts: 0,
        docs: [
          doc({ id: "query", tags: ["type:query"] }),
          doc({ id: "concept-a", tags: ["type:concept"] }),
          doc({ id: "concept-b", tags: ["type:concept"] }),
          doc({ id: "model-a", tags: ["type:model"] }),
        ],
      },
    ];
    expect(typeCounts(collections)).toEqual([
      { id: "concept", label: "Konzepte", count: 2 },
      { id: "model", label: "Modelle", count: 1 },
      { id: "query", label: "Antworten", count: 1 },
    ]);
  });

  it("filterCatalog filtert nach Regal und Typ und aktualisiert count", () => {
    const catalog: KnowledgeCatalog = {
      collections: [
        {
          id: "kanon",
          title: "Kanon",
          description: "",
          accent: "cyan",
          icon: "Landmark",
          doc_count: 1,
          updated_ts: 0,
          docs: [doc({ id: "canon", collection: "kanon", tags: [] })],
        },
        {
          id: "llm-wiki",
          title: "LLM-Wiki",
          description: "",
          accent: "indigo",
          icon: "Brain",
          doc_count: 2,
          updated_ts: 0,
          docs: [
            doc({ id: "concept", collection: "llm-wiki", tags: ["type:concept"] }),
            doc({ id: "query", collection: "llm-wiki", tags: ["type:query"] }),
          ],
        },
      ],
      count: 3,
      query: "",
      now: 1,
    };

    const filtered = filterCatalog(catalog, "llm-wiki", "concept");
    expect(filtered.count).toBe(1);
    expect(filtered.collections).toHaveLength(1);
    expect(filtered.collections[0].docs.map((item) => item.id)).toEqual(["concept"]);
  });
});
