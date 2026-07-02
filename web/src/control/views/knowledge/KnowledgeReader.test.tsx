// @vitest-environment jsdom
// Wissens-Regal S2: Obsidian-Wikilinks im llm-wiki-Reader (Preprocessing +
// interne Navigation), tote Links, Tabellen-Scroll-Wrapper. Fixtures unten
// sind wörtliche Ausschnitte aus ~/llm-wiki/wiki/synthesis.md /
// ~/llm-wiki/wiki/overview.md (echtes Wikilink-Format), keine Erfindung.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { resolveWikiLinks, type KnowledgeDoc, type KnowledgeDocDetail } from "./knowledge.helpers";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { KnowledgeReader } from "./KnowledgeReader";

// Wörtlich aus ~/llm-wiki/wiki/synthesis.md (Absatz "Karpathy source provides…").
const SYNTHESIS_EXCERPT =
  "The Karpathy source provides the structural pattern:\n" +
  "[[wiki/concepts/raw-wiki-schema|raw sources, wiki pages, and schema]] plus the\n" +
  "[[wiki/concepts/ingest-query-lint|ingest/query/lint]] operating loop. The Hermes\n" +
  "Bible test source provides a concrete adjacent domain where the same compounding\n" +
  "principle appears as an operational [[wiki/concepts/self-improvement-loop]].";

// Wörtlich aus ~/llm-wiki/wiki/overview.md (letzte Zeile).
const OVERVIEW_TAIL = "Start with [[index]] for navigation and [[log]] for chronology.";

describe("resolveWikiLinks (echtes llm-wiki-Format)", () => {
  it("wandelt `[[wiki/x|Label]]` in einen internen Link-Href um", () => {
    const out = resolveWikiLinks(SYNTHESIS_EXCERPT);
    const id = encodeURIComponent("kb::llm::concepts/raw-wiki-schema.md");
    expect(out).toContain(`[raw sources, wiki pages, and schema](internal-link:${id})`);
  });

  it("wandelt `[[wiki/x]]` ohne Alias per Slug-Titel-Case in einen internen Link um", () => {
    const out = resolveWikiLinks(SYNTHESIS_EXCERPT);
    const id = encodeURIComponent("kb::llm::concepts/self-improvement-loop.md");
    expect(out).toContain(`[Self Improvement Loop](internal-link:${id})`);
  });

  it("markiert `[[index]]`/`[[log]]` (kein wiki/-Präfix) als toten Link", () => {
    const out = resolveWikiLinks(OVERVIEW_TAIL);
    expect(out).toContain(`[index](dead-link:${encodeURIComponent("index")})`);
    expect(out).toContain(`[log](dead-link:${encodeURIComponent("log")})`);
  });

  it("lässt Wikilinks in Fenced-Code-Blöcken unangetastet", () => {
    const md = "```\n[[wiki/concepts/foo]]\n```";
    expect(resolveWikiLinks(md)).toBe(md);
  });
});

function llmDoc(over: Partial<KnowledgeDoc> = {}): KnowledgeDoc {
  return {
    id: "kb::llm::overview.md",
    collection: "llm-wiki",
    title: "Overview",
    summary: "Entry point.",
    source_ref: "llm-wiki/overview.md",
    tags: ["llm-wiki", "type:overview"],
    updated_ts: 1000,
    heading_count: 1,
    ...over,
  };
}

function detailFor(doc: KnowledgeDoc, body_md: string): KnowledgeDocDetail {
  return { ...doc, body_md };
}

describe("KnowledgeReader (llm-wiki Wikilinks)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    cleanup();
  });

  it("navigiert bei Klick auf einen internen Wikilink zum referenzierten Doc (Preprocessing → Klick → Reload)", async () => {
    const overview = detailFor(
      llmDoc(),
      `# Overview\n\n${SYNTHESIS_EXCERPT}\n`,
    );
    const target = detailFor(
      llmDoc({
        id: "kb::llm::concepts/raw-wiki-schema.md",
        title: "Raw Wiki Schema",
        source_ref: "llm-wiki/concepts/raw-wiki-schema.md",
      }),
      "# Raw Wiki Schema\n\nThree layers: raw, wiki, schema.\n",
    );
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.includes(encodeURIComponent("kb::llm::concepts/raw-wiki-schema.md"))) return target;
      return overview;
    });

    render(<KnowledgeReader doc={llmDoc()} collectionTitle="LLM-Wiki" onBack={() => {}} />);

    const link = await screen.findByText("raw sources, wiki pages, and schema");
    fireEvent.click(link);

    await waitFor(() => {
      expect(screen.getByRole("heading", { level: 2 }).textContent).toBe("Raw Wiki Schema");
    });
    expect(fetchJSONMock).toHaveBeenCalledWith(
      expect.stringContaining(encodeURIComponent("kb::llm::concepts/raw-wiki-schema.md")),
    );
  });

  it("kennzeichnet einen nicht auflösbaren Wikilink als toten Link mit title-Attribut, nicht klickbar", async () => {
    const overview = detailFor(llmDoc(), `# Overview\n\n${OVERVIEW_TAIL}\n`);
    fetchJSONMock.mockResolvedValue(overview);

    render(<KnowledgeReader doc={llmDoc()} collectionTitle="LLM-Wiki" onBack={() => {}} />);

    const dead = await screen.findByTitle("Ziel nicht gefunden: index");
    expect(dead.tagName).toBe("SPAN");
    expect(screen.queryByRole("link", { name: "index" })).toBeNull();
  });

  it("lässt Wikilinks außerhalb der llm-wiki-Sammlung unverarbeitet (Scope-Gate an der Doc-Id)", async () => {
    const canonDoc: KnowledgeDoc = {
      id: "kb::doc::canon-index",
      collection: "kanon",
      title: "Canon-Index",
      summary: "Einstieg.",
      source_ref: "vault/00-Canon/_index.md",
      tags: ["kanon"],
      updated_ts: 1000,
      heading_count: 1,
    };
    fetchJSONMock.mockResolvedValue(
      detailFor(canonDoc, "# Canon-Index\n\nSiehe [[wiki/concepts/foo]] (kein llm-wiki-Doc, bleibt roh).\n"),
    );

    render(<KnowledgeReader doc={canonDoc} collectionTitle="Kanon" onBack={() => {}} />);

    await screen.findByText(/kein llm-wiki-Doc, bleibt roh/);
    expect(screen.queryByRole("link", { name: "foo" })).toBeNull();
    expect(document.body.textContent).toContain("[[wiki/concepts/foo]]");
  });

  it("wrapt breite GFM-Tabellen im Reader horizontal scrollbar", async () => {
    const md =
      "# LLM Model Landscape\n\n" +
      "| Modell-ID | Erstellt | Kontext | Prompt/Completion pro 1M |\n" +
      "|---|---|---|---|\n" +
      "| `anthropic/claude-sonnet-5` | 2026-06-30 | 1M | $2.00 / $10.00 |\n";
    fetchJSONMock.mockResolvedValue(
      detailFor(llmDoc({ id: "kb::llm::models/model-landscape.md", title: "LLM Model Landscape" }), md),
    );

    const { container } = render(
      <KnowledgeReader doc={llmDoc({ id: "kb::llm::models/model-landscape.md" })} collectionTitle="LLM-Wiki" onBack={() => {}} />,
    );

    await screen.findByText("anthropic/claude-sonnet-5");
    const table = container.querySelector("table");
    expect(table).not.toBeNull();
    expect(table?.closest(".overflow-x-auto")).not.toBeNull();
  });
});
