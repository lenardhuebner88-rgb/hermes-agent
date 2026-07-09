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

const EMPTY_ITEMS = { items: [], count: 0, truncated: false, has_more: false, categories: [], now: 1_700_000_000 };
const VAULT_PROVENANCE_FIXTURE = {
  schema: "vault-provenance/v1",
  error: null,
  stale_count: 0,
  open_sessions: [],
  recent_receipts: [],
};

function mockFetch() {
  fetchJSONMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/library/knowledge")) return KNOWLEDGE_FIXTURE;
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
