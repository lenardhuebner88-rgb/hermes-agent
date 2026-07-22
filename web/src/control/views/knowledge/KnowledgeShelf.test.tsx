// @vitest-environment jsdom
//
// Der Rest der Datei rendert Subkomponenten via renderToStaticMarkup (node,
// Hauskonvention) — jsdom wird nur für die neuen Full-Component-Tests am
// Dateiende gebraucht (echter Fetch-Mock + Router-Kontext).
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Under full-suite CPU pressure the catalog fetch can resolve before the router
// commits the collection deep-link filter. Keep Testing Library's per-file async
// budget aligned with the other control-view integration suites.
configure({ asyncUtilTimeout: 5000 });

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { CollectionSection, DocCard, KnowledgeShelf } from "./KnowledgeShelf";
import { TocNav } from "./KnowledgeReader";
import { _resetPollingStore } from "../../hooks/pollingStore";
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

const originalMatchMedia = Object.getOwnPropertyDescriptor(window, "matchMedia");
const fixtureDir = path.dirname(fileURLToPath(import.meta.url));
const REAL_KNOWLEDGE_FIXTURE = JSON.parse(readFileSync(
  path.join(fixtureDir, "../briefings/__fixtures__/knowledge.json"),
  "utf-8",
)) as KnowledgeCatalog;
const REAL_VAULT_PLAN = JSON.parse(readFileSync(
  path.join(fixtureDir, "__fixtures__/vault-plan-item.json"),
  "utf-8",
)) as KnowledgeDoc;

function mockExpandedViewport(expanded: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: expanded,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  _resetPollingStore();
  if (originalMatchMedia) Object.defineProperty(window, "matchMedia", originalMatchMedia);
  else delete (window as { matchMedia?: unknown }).matchMedia;
});

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
  const validEpoch = 1_780_041_720;
  const collection: KnowledgeCollection = {
    id: "kanon",
    title: "Kanon — Die geteilte Wahrheit",
    description: "Dauerhafte, agent-übergreifende Fakten.",
    accent: "cyan",
    icon: "Landmark",
    doc_count: 2,
    updated_ts: validEpoch,
    docs: [doc({ id: "a", title: "Doc A" }), doc({ id: "b", title: "Doc B" })],
  };

  it("rendert Titel, Beschreibung, Doc-Zahl und beide Karten", () => {
    const html = renderToStaticMarkup(<CollectionSection collection={collection} now={validEpoch} onOpen={() => {}} />);
    expect(html).toContain("Kanon — Die geteilte Wahrheit");
    expect(html).toContain("Dauerhafte, agent-übergreifende Fakten.");
    expect(html).toContain("2 Dokumente");
    expect(html).toContain("Doc A");
    expect(html).toContain("Doc B");
  });

  it("zeigt einen „aktualisiert vor X“-Chip aus updated_ts/now", () => {
    const html = renderToStaticMarkup(<CollectionSection collection={collection} now={validEpoch + 3600} onOpen={() => {}} />);
    expect(html).toContain("aktualisiert vor 1h");
  });

  it("blendet den Chip aus, wenn updated_ts 0 ist (leere Sammlung)", () => {
    const html = renderToStaticMarkup(
      <CollectionSection collection={{ ...collection, updated_ts: 0 }} now={validEpoch} onOpen={() => {}} />,
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

  it("knowledgeTypeLabel übersetzt Reports und Guides", () => {
    expect(knowledgeTypeLabel("report")).toBe("Reports");
    expect(knowledgeTypeLabel("guide")).toBe("Guides");
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

// S6: Baseline-Polling (useKnowledgeCatalog) + Deep-Link-Preselect aus dem
// `collection`-Suchparameter (von der BriefingsShelf-Schnellauswahl gesetzt).
// Fixture = dieselbe echte /api/library/knowledge-Ernte wie in
// briefings/BriefingsShelf.test.tsx (geteiltes Fixture wäre ein zusätzlicher
// Datei-Import über View-Grenzen — hier reicht ein kleiner, aber echter
// Zwei-Sammlungs-Ausschnitt aus demselben Live-Payload).
describe("KnowledgeShelf: Baseline-Poll + Collection-Deep-Link (S6)", () => {
  const KNOWLEDGE_CATALOG = {
    collections: [
      {
        id: "kanon",
        title: "Kanon — Die geteilte Wahrheit",
        description: "Dauerhafte, agent-übergreifende Fakten.",
        accent: "cyan",
        icon: "Landmark",
        doc_count: 6,
        updated_ts: 1783575727,
        docs: [doc({ id: "kb::doc::canon-index", collection: "kanon", title: "Canon-Index" })],
      },
      {
        id: "llm-wiki",
        title: "LLM-Wiki",
        description: "Agentisch gepflegtes Wissen aus ~/llm-wiki/wiki.",
        accent: "indigo",
        icon: "Brain",
        doc_count: 39,
        updated_ts: 1783623276,
        docs: [doc({ id: "kb::llm::overview.md", collection: "llm-wiki", title: "Overview" })],
      },
    ],
    count: 45,
    query: "",
    now: 1783624432,
  };

  function mockKnowledgeFetch() {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/knowledge")) return KNOWLEDGE_CATALOG;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
  }

  it("lädt und rendert den Katalog ohne `collection`-Param (Baseline-Poll)", async () => {
    mockKnowledgeFetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=wissen"]}><KnowledgeShelf /></MemoryRouter>);

    expect(await screen.findByText("Canon-Index")).toBeTruthy();
    expect(screen.getByText("Overview")).toBeTruthy();
  });

  it("preselektiert die Sammlung aus `?collection=<id>` (Deep-Link von der Briefings-Kachel)", async () => {
    mockKnowledgeFetch();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek?mode=wissen&collection=llm-wiki"]}>
        <KnowledgeShelf />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Overview")).toBeTruthy();
    // Kanon ist gefiltert weg — nur die vorselektierte Sammlung zeigt Docs.
    await waitFor(() => expect(screen.queryByText("Canon-Index")).toBeNull());
    expect(screen.getByRole("button", { name: /Kanon/ }).getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByRole("button", { name: /LLM-Wiki/ }).getAttribute("aria-pressed")).toBe("true");
  });

  it("behält das Regal ab 1024 px neben dem Reader und gibt Fokus an die DocCard zurück", async () => {
    mockExpandedViewport(true);
    const overview = KNOWLEDGE_CATALOG.collections[1].docs[0];
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/knowledge/doc")) {
        return { ...overview, body_md: "# Overview\n\n## Modelle\n\nInhalt." };
      }
      if (url.startsWith("/api/library/knowledge")) return KNOWLEDGE_CATALOG;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=wissen"]}><KnowledgeShelf /></MemoryRouter>);

    const trigger = await screen.findByRole("button", { name: /Overview/ });
    trigger.focus();
    fireEvent.click(trigger);

    expect(await screen.findByRole("region", { name: /LLM-Wiki: Overview/ })).toBeTruthy();
    expect(screen.getByText("Canon-Index")).toBeTruthy();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: "Alle Regale" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: /LLM-Wiki: Overview/ })).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });

  it("behält unter 1024 px das Full-Replace-Muster", async () => {
    mockExpandedViewport(false);
    const overview = KNOWLEDGE_CATALOG.collections[1].docs[0];
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/knowledge/doc")) return { ...overview, body_md: "# Overview\n\nInhalt." };
      if (url.startsWith("/api/library/knowledge")) return KNOWLEDGE_CATALOG;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=wissen"]}><KnowledgeShelf /></MemoryRouter>);

    fireEvent.click(await screen.findByRole("button", { name: /Overview/ }));

    expect(await screen.findByRole("button", { name: "Alle Regale" })).toBeTruthy();
    expect(screen.queryByLabelText("Im Nachschlagewerk suchen")).toBeNull();
    expect(screen.queryByRole("region", { name: /LLM-Wiki: Overview/ })).toBeNull();
  });
});

describe("KnowledgeShelf: Vault-Plans Disclosure + Virtualisierung-light (P7)", () => {
  const planCount = 345;
  const firstPageSize = 24;

  function catalogWithCurrentPlanCount(): KnowledgeCatalog {
    const plans = Array.from({ length: planCount }, (_, index) => ({
      ...REAL_VAULT_PLAN,
      id: `${REAL_VAULT_PLAN.id}#${index + 1}`,
      title: index === 0 ? REAL_VAULT_PLAN.title : `${REAL_VAULT_PLAN.title} · ${index + 1}`,
    }));
    return {
      ...REAL_KNOWLEDGE_FIXTURE,
      count: 433,
      collections: REAL_KNOWLEDGE_FIXTURE.collections.map((collection) => (
        collection.id === "vault-plans"
          ? { ...collection, doc_count: planCount, docs: plans }
          : collection
      )),
    };
  }

  it("zeigt bei aktiven Filtern die ungekürzte gefilterte Plan-Zahl", () => {
    const filteredPlans = Array.from({ length: 30 }, (_, index) => vaultPlan({ id: `filtered-plan-${index}` }));
    const collection: KnowledgeCollection = {
      id: "vault-plans",
      title: "Vault Plans",
      description: "Gefilterte Pläne",
      accent: "rose",
      icon: "Newspaper",
      doc_count: planCount,
      updated_ts: 1,
      docs: filteredPlans,
    };

    const html = renderToStaticMarkup(
      <CollectionSection collection={collection} now={1} onOpen={() => {}} filtersActive />,
    );
    expect(html).toContain("Vault Plans (30)");
    expect(html).not.toContain(`Vault Plans (${planCount})`);
  });

  it("hält 345 reale Plan-Items initial aus dem DOM und rendert nach Öffnung nur die erste 24er-Seite", async () => {
    mockExpandedViewport(false);
    const catalog = catalogWithCurrentPlanCount();
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/knowledge")) return catalog;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/control/bibliothek?mode=wissen"]}>
        <KnowledgeShelf />
      </MemoryRouter>,
    );

    // Die kuratierten Regale bleiben sichtbar, Plan-Karten hingegen werden
    // vor der expliziten Öffnung gar nicht gemountet.
    expect(await screen.findByText("Canon-Index")).toBeTruthy();
    expect(screen.getByText("Overview")).toBeTruthy();
    const disclosure = screen.getByRole("button", { name: /Vault Plans \(345\)/ });
    expect(disclosure.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("list", { name: "Vault Plans Dokumente" })).toBeNull();
    expect(screen.queryByText(REAL_VAULT_PLAN.title)).toBeNull();

    fireEvent.click(disclosure);

    expect(disclosure.getAttribute("aria-expanded")).toBe("true");
    const firstPage = screen.getByRole("list", { name: "Vault Plans Dokumente" });
    expect(within(firstPage).getAllByRole("listitem")).toHaveLength(firstPageSize);
    expect(screen.getByText(`${firstPageSize} von ${planCount} Plänen sichtbar`)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Weitere 24 Pläne laden" })).toBeTruthy();
  });
});
