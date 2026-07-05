// @vitest-environment jsdom
//
// Interaktive Ergänzung zu BibliothekView.test.tsx (das bleibt renderToStatic-
// Markup/node — Hauskonvention): die beiden Verhaltensweisen, die sich nicht
// über statisches Rendern oder Quelltext-Regex beweisen lassen, weil sie
// entweder einen echten Klick (Zustands-Erhalt über den Moduswechsel, S3)
// oder einen aufgelösten Fetch (Inhaltsverzeichnis-Gating, S4) brauchen.
//
// Nicht Teil des im Auftrag genannten Gate-Kommandos (das nennt nur
// BibliothekView.test.tsx) — zusätzlich selbst gebaut und gelaufen, siehe
// Rückgabe-Notiz.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { BibliothekView } from "./BibliothekView";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Felder exakt wie `_Item.as_dict(with_body=False)` in hermes_cli/library_view.py.
const ITEM = {
  id: "cron::main::5a2a54ac3dae::2026-06-10_07-31-09.md",
  category: "briefings",
  series_id: "main/5a2a54ac3dae",
  series: "Morning Digest",
  title: "Morning Digest — Ausgabe 10.06. 07:31",
  ts: 1_749_540_669,
  preview: "Heute: Spezialwort Quokkafund im Digest.",
  source_ref: "cron:5a2a54ac3dae",
  series_meta: "30 7 * * *",
};

const ITEMS_RESPONSE = {
  items: [ITEM],
  count: 1,
  truncated: false,
  has_more: false,
  categories: ["news", "briefings", "recherchen", "familie", "arbeit", "receipts", "wartung"],
  now: 1_749_540_700,
};

const EMPTY_TOPICS = { items: [], count: 0, demo_topics: [] };
const EMPTY_SAVED = { items: [], count: 0 };
const EMPTY_KNOWLEDGE = { collections: [] };
const VAULT_PROVENANCE_FIXTURE = {
  schema: "vault-provenance/v1",
  error: null,
  stale_count: 1,
  open_sessions: [
    {
      agent: "Hermes",
      started: "2026-07-05T17:00:00+02:00",
      task: "Regal-Testfixture",
      path: "/home/piet/vault/_agents/_coordination/regal.md",
      stale: true,
    },
  ],
  recent_receipts: [
    {
      when: "2026-07-05 17:05",
      agent: "Hermes",
      file: "regal-receipt.md",
      path: "/home/piet/vault/03-Agents/Hermes/receipts/regal-receipt.md",
    },
  ],
};

function mockLibraryFetch(extra?: (url: string) => unknown) {
  fetchJSONMock.mockImplementation(async (url: string) => {
    if (extra) {
      const hit = extra(url);
      if (hit !== undefined) return hit;
    }
    if (url.startsWith("/api/library/items")) return ITEMS_RESPONSE;
    if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
    if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
    if (url.startsWith("/api/library/knowledge")) return EMPTY_KNOWLEDGE;
    if (url.startsWith("/api/vault/provenance")) return VAULT_PROVENANCE_FIXTURE;
    throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
  });
}

describe("BibliothekView: Zustand bleibt beim Moduswechsel erhalten (S3)", () => {
  it("Lesesaal-Suchtext übersteht Nachschlagewerk→Lesesaal→Nachschlagewerk→Lesesaal", async () => {
    mockLibraryFetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    fireEvent.click(screen.getByRole("tab", { name: "Lesesaal" }));
    const search = await screen.findByPlaceholderText("Suche in Titel + Text …");
    fireEvent.change(search, { target: { value: "quokkafund" } });
    expect((search as HTMLInputElement).value).toBe("quokkafund");

    fireEvent.click(screen.getByRole("tab", { name: "Nachschlagewerk" }));
    fireEvent.click(screen.getByRole("tab", { name: "Lesesaal" }));

    const searchAfter = screen.getByPlaceholderText("Suche in Titel + Text …") as HTMLInputElement;
    expect(searchAfter.value).toBe("quokkafund");
  });
});

describe("ReadingView: Inhaltsverzeichnis erst ab 3 Überschriften (S4)", () => {
  it("zeigt das Inhaltsverzeichnis bei 3 Überschriften", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) {
        return { ...ITEM, body_md: "# Eins\ntext\n\n## Zwei\ntext\n\n## Drei\ntext\n" };
      }
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const card = await screen.findByText(ITEM.title);
    fireEvent.click(card);

    await waitFor(() => screen.getByText("Inhalt"));
    expect(screen.getByRole("button", { name: "Eins" })).toBeTruthy();
  });

  it("versteckt das Inhaltsverzeichnis bei nur 2 Überschriften", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) {
        return { ...ITEM, body_md: "# Eins\ntext\n\n## Zwei\ntext\n" };
      }
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const card = await screen.findByText(ITEM.title);
    fireEvent.click(card);

    await waitFor(() => screen.getByText(/Eins/));
    expect(screen.queryByText("Inhalt")).toBeNull();
  });
});

describe("BibliothekView: Regal-Provenienz und Reader-Drawer (S4)", () => {
  it("rendert echte Knowledge-/Provenienz-Payloads und öffnet den Reader im Drawer", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) {
        return { ...ITEM, body_md: "# Eins\ntext\n\n## Zwei\ntext\n\n## Drei\ntext\n" };
      }
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    expect(await screen.findByText("Regal-Testfixture")).toBeTruthy();
    expect(screen.getByText("regal-receipt.md")).toBeTruthy();

    fireEvent.click(await screen.findByText(ITEM.title));

    await waitFor(() => screen.getByRole("dialog", { name: new RegExp(ITEM.title) }));
    expect(screen.getByText("Inhalt")).toBeTruthy();
  });
});

describe("BibliothekView: Deep-Link stellt Modus + Dokument aus der URL wieder her (S2)", () => {
  it("?mode=lesesaal&item=… öffnet den Lesesaal direkt mit dem referenzierten Dokument", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) return { ...ITEM, body_md: "Kurzer Text ohne Überschriften." };
      return undefined;
    });
    render(
      <MemoryRouter initialEntries={[`/control/bibliothek?mode=lesesaal&item=${encodeURIComponent(ITEM.id)}`]}>
        <BibliothekView />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getAllByText(ITEM.title).length).toBeGreaterThan(0));
    // Direkter Detail-Fetch, nicht (nur) über die geladene Liste — der Deep-Link
    // muss auch funktionieren, bevor/ohne dass die Liste selbst das Item führt.
    expect(fetchJSONMock).toHaveBeenCalledWith(
      expect.stringContaining(`/api/library/item?id=${encodeURIComponent(ITEM.id)}`),
    );
  });
});
