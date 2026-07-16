// @vitest-environment jsdom
//
// S6: Briefings-Regal — echte Zahlen statt Mock-Konstanten, klickbare/
// tastaturbedienbare Schnellauswahl-Kacheln, Polling-Parität mit dem Rest
// des Dashboards. Fixture = das REALE `/api/library/knowledge`-Payload,
// geerntet über `hermes_cli.library_knowledge.list_knowledge()` (Live-Repo,
// siehe Task-Report) — keine handgeschriebenen Mock-Zahlen.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { BriefingsShelf } from "./BriefingsShelf";

const fixturePath = path.join(path.dirname(fileURLToPath(import.meta.url)), "__fixtures__/knowledge.json");
const KNOWLEDGE_FIXTURE = JSON.parse(readFileSync(fixturePath, "utf-8")) as {
  collections: { id: string; title: string; doc_count: number }[];
  count: number;
};
const structuredFixturePath = path.join(
  path.dirname(fileURLToPath(import.meta.url)),
  "__fixtures__/structured-brief.json",
);
const STRUCTURED_BRIEF = JSON.parse(readFileSync(structuredFixturePath, "utf-8"));
const BRIEFINGS_SOURCE = readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), "BriefingsShelf.tsx"),
  "utf-8",
);

const REMOVED_LEGACY_MARKS = [
  "rgba(79,216,235",
  "bg-[radial-gradient",
  "bg-white/[.025]",
  "border-white/10",
  "text-[var(--hc-text)]",
  "text-[var(--hc-accent)]",
  "ToneCallout",
];

function expectRemovedLegacyMarksGone(html: string): void {
  for (const mark of REMOVED_LEGACY_MARKS) expect(html).not.toContain(mark);
}

interface ItemsResponseFixture {
  items: unknown[];
  count: number;
  truncated: boolean;
  has_more: boolean;
  categories: string[];
  now: number;
}

const EMPTY_ITEMS: ItemsResponseFixture = {
  items: [], count: 0, truncated: false, has_more: false, categories: [], now: 1_700_000_000,
};
const VAULT_PROVENANCE_FIXTURE = {
  schema: "vault-provenance/v1",
  error: null,
  stale_count: 0,
  open_sessions: [],
  recent_receipts: [],
};

function mockFetch(itemsResponse: ItemsResponseFixture = EMPTY_ITEMS) {
  fetchJSONMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/library/knowledge")) return KNOWLEDGE_FIXTURE;
    if (url.startsWith("/api/library/items?category=news")) return itemsResponse;
    if (url.startsWith("/api/library/items?category=briefings")) return EMPTY_ITEMS;
    if (url.startsWith("/api/library/items")) return EMPTY_ITEMS;
    if (url.startsWith("/api/vault/provenance")) return VAULT_PROVENANCE_FIXTURE;
    throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
  });
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="loc">{location.pathname}{location.search}</div>;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("BriefingsShelf: Nachschlagewerk-Schnellauswahl (S6)", () => {
  it("rendert eine Kachel pro echter Sammlung mit den echten Zählern — keine Mock-Zahlen", async () => {
    mockFetch();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <BriefingsShelf onOpenItem={() => {}} />
      </MemoryRouter>,
    );

    // Alle 6 echten Sammlungen — inkl. der beiden, die vorher fehlten.
    for (const collection of KNOWLEDGE_FIXTURE.collections) {
      expect(await screen.findByText(collection.title)).toBeTruthy();
      expect(screen.getByText(`${collection.doc_count} Dokumente`)).toBeTruthy();
    }
    expect(screen.getByText("LLM-Wiki")).toBeTruthy();
    expect(screen.getByText("Vault Plans")).toBeTruthy();

    // Die alten hartcodierten Mock-Zahlen (12 canon / 21 skills / 8 orchestrierung)
    // dürfen nirgends mehr auftauchen.
    expect(screen.queryByText("12 Dokumente")).toBeNull();
    expect(screen.queryByText("21 Dokumente")).toBeNull();
    expect(screen.queryByText("8 Dokumente")).toBeNull();
  });

  it("zeigt die echte Gesamtzahl im Hero-Badge statt der hartcodierten 12", async () => {
    mockFetch();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <BriefingsShelf onOpenItem={() => {}} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(`${KNOWLEDGE_FIXTURE.count} Dokumente im Nachschlagewerk`)).toBeTruthy();
  });

  it("Klick auf eine Kachel navigiert zu ?mode=wissen&collection=<id>", async () => {
    mockFetch();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <LocationProbe />
        <BriefingsShelf onOpenItem={() => {}} />
      </MemoryRouter>,
    );

    const tile = (await screen.findByText("LLM-Wiki")).closest('[role="button"]');
    expect(tile).toBeTruthy();
    fireEvent.click(tile as Element);

    await waitFor(() => {
      const loc = screen.getByTestId("loc").textContent ?? "";
      expect(loc).toContain("mode=wissen");
      expect(loc).toContain("collection=llm-wiki");
    });
  });

  it("Enter-Taste auf einer fokussierten Kachel navigiert ebenfalls", async () => {
    mockFetch();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <LocationProbe />
        <BriefingsShelf onOpenItem={() => {}} />
      </MemoryRouter>,
    );

    const tile = (await screen.findByText("Vault Plans")).closest('[role="button"]');
    expect(tile).toBeTruthy();
    fireEvent.keyDown(tile as Element, { key: "Enter" });

    await waitFor(() => {
      const loc = screen.getByTestId("loc").textContent ?? "";
      expect(loc).toContain("collection=vault-plans");
    });
  });
});

describe("BriefingsShelf: strukturierte KI-Frontpage (S5)", () => {
  it("rendert Top-Story, verlinkte Modell-News, Watchlist und Frische-Stempel", async () => {
    mockFetch({ ...EMPTY_ITEMS, items: [STRUCTURED_BRIEF], count: 1 });
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <BriefingsShelf onOpenItem={() => {}} />
      </MemoryRouter>,
    );

    expect(await screen.findByText("Top-Story")).toBeTruthy();
    expect(screen.getByText(/OpenAI hat GPT-5\.6 heute aus der Preview/)).toBeTruthy();
    expect(screen.getByText("OpenAI - GPT-5.6 (Sol/Terra/Luna)")).toBeTruthy();
    expect(screen.getByText("Meta - Muse Spark 1.1")).toBeTruthy();
    expect(screen.getByText("Watchlist-Update")).toBeTruthy();
    expect(screen.getByText(/Stand 20:03 · nächster Lauf 08:00/)).toBeTruthy();

    const openAI = screen.getByText("OpenAI - GPT-5.6 (Sol/Terra/Luna)").closest("a");
    expect(openAI?.getAttribute("href")).toBe("https://openai.com/index/gpt-5-6");
  });

  it("öffnet über den CTA weiterhin das vollständige kanonische Briefing", async () => {
    mockFetch({ ...EMPTY_ITEMS, items: [STRUCTURED_BRIEF], count: 1 });
    const onOpen = vi.fn();
    render(
      <MemoryRouter initialEntries={["/control/bibliothek"]}>
        <BriefingsShelf onOpenItem={onOpen} />
      </MemoryRouter>,
    );

    fireEvent.click(await screen.findByText("Ganzes Briefing lesen →"));
    expect(onOpen).toHaveBeenCalledOnce();
    expect(onOpen.mock.calls[0][0].id).toBe(STRUCTURED_BRIEF.id);
  });
});

describe("BriefingsShelf: Markdown-Preview wird in Karten bereinigt", () => {
  const MARKDOWN_BRIEFING = {
    id: "brief-md-1",
    category: "briefings",
    series_id: "main/ki",
    series: "KI Modell-Brief (Abend)",
    title: "KI Modell-Brief (Abend) — Ausgabe 16.07. 20:03",
    ts: 1_752_670_980,
    preview:
      "OpenAI hat GPT-5.6 heute aus der Preview in den echten Rollout geschoben: ChatGPT, Codex und API starten gleichzeitig. Anthropic zieht mit einer Fable-5-Preisstaffel nach, die Batch-Workloads deutlich günstiger macht. ## Modell-News - **OpenAI — GPT-5.6 (Sol/Terra/Luna)**: GA-Rol",
    source_ref: "cron:ki",
    series_meta: "0 20 * * *",
    structured: false,
    structured_brief: undefined,
  };

  it("zeigt Featured-Karten-Text ohne Roh-Markdown-Tokens", async () => {
    mockFetch({ ...EMPTY_ITEMS, items: [MARKDOWN_BRIEFING], count: 1 });
    const { container } = render(
      <MemoryRouter><BriefingsShelf onOpenItem={() => {}} /></MemoryRouter>,
    );

    expect(await screen.findByText(/Modell-News/)).toBeTruthy();
    expect(screen.getByText(/OpenAI/)).toBeTruthy();

    const cardText = container.textContent ?? "";
    expect(cardText).not.toContain("##");
    expect(cardText).not.toContain("**");
  });
});

describe("BriefingsShelf: Sheet-A negative guard über alle Renderzweige", () => {
  it("enthält weder die beiden alten Cyan-Glows noch die migrierten Legacy-Klassen im Quelltext", () => {
    expectRemovedLegacyMarksGone(BRIEFINGS_SOURCE);
  });

  it("hält den leeren Zweig frei von den entfernten Briefings-Legacy-Marken", async () => {
    mockFetch();
    const { container } = render(
      <MemoryRouter><BriefingsShelf onOpenItem={() => {}} /></MemoryRouter>,
    );
    await screen.findByText("Noch keine Briefings");
    expectRemovedLegacyMarksGone(container.innerHTML);
  });

  it("hält den strukturierten und den klassischen Karten-Zweig frei von den entfernten Marken", async () => {
    const plain = {
      ...STRUCTURED_BRIEF,
      id: "plain-brief",
      title: "Klassisches Briefing",
      structured: false,
      structured_brief: undefined,
    };
    mockFetch({ ...EMPTY_ITEMS, items: [STRUCTURED_BRIEF, plain], count: 2 });
    const { container } = render(
      <MemoryRouter><BriefingsShelf onOpenItem={() => {}} /></MemoryRouter>,
    );
    await screen.findByText("Top-Story");
    expect(screen.getByText("Klassisches Briefing")).toBeTruthy();
    expectRemovedLegacyMarksGone(container.innerHTML);
  });

  it("hält den Fehlerzweig frei von den entfernten Briefings-Legacy-Marken", async () => {
    fetchJSONMock.mockRejectedValue(new Error("offline"));
    const { container } = render(
      <MemoryRouter><BriefingsShelf onOpenItem={() => {}} /></MemoryRouter>,
    );
    await screen.findByText("Briefings konnten nicht geladen werden.");
    expectRemovedLegacyMarksGone(container.innerHTML);
  });
});
