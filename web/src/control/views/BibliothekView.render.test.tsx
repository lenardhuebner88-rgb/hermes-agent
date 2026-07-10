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
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const { fetchJSONMock } = vi.hoisted(() => ({ fetchJSONMock: vi.fn() }));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return { ...actual, fetchJSON: fetchJSONMock };
});

import { BibliothekView } from "./BibliothekView";

const originalMatchMedia = Object.getOwnPropertyDescriptor(window, "matchMedia");

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
  if (originalMatchMedia) Object.defineProperty(window, "matchMedia", originalMatchMedia);
  else delete (window as { matchMedia?: unknown }).matchMedia;
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
  it("Lesesaal-Suchtext übersteht Briefings→Lesesaal→Briefings→Lesesaal", async () => {
    mockLibraryFetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    fireEvent.click(screen.getByRole("tab", { name: /Lesesaal/ }));
    const search = await screen.findByPlaceholderText("Suche in Titel + Text …");
    fireEvent.change(search, { target: { value: "quokkafund" } });
    expect((search as HTMLInputElement).value).toBe("quokkafund");

    fireEvent.click(screen.getByRole("tab", { name: /Briefings/ }));
    fireEvent.click(screen.getByRole("tab", { name: /Lesesaal/ }));

    const searchAfter = screen.getByPlaceholderText("Suche in Titel + Text …") as HTMLInputElement;
    expect(searchAfter.value).toBe("quokkafund");
  });
});

function lesesaalPanel() {
  const el = document.getElementById("bibliothek-panel-lesesaal");
  if (!el) throw new Error("Lesesaal-Panel nicht gefunden");
  return within(el);
}

describe("ReadingView: Inhaltsverzeichnis erst ab 3 Überschriften (S4)", () => {
  it("zeigt das Inhaltsverzeichnis bei 3 Überschriften", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) {
        return { ...ITEM, body_md: "# Eins\ntext\n\n## Zwei\ntext\n\n## Drei\ntext\n" };
      }
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const card = await lesesaalPanel().findByText(ITEM.title);
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

    const card = await lesesaalPanel().findByText(ITEM.title);
    fireEvent.click(card);

    await waitFor(() => screen.getByText(/Eins/));
    expect(screen.queryByText("Inhalt")).toBeNull();
  });
});

function briefingsPanel() {
  const el = document.getElementById("bibliothek-panel-briefings");
  if (!el) throw new Error("Briefings-Panel nicht gefunden");
  return within(el);
}

describe("BibliothekView: Regal-Provenienz im Briefings-Tab (S4)", () => {
  it("rendert echte Vault-Provenienz-Payloads in der aufgeklappten Provenienz-Sektion", async () => {
    mockLibraryFetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    // Provenienz-Vorschau (Mittelzeile) UND die eingeklappte Provenienz-Sektion
    // speisen sich aus denselben open_sessions → auf die Disclosure-Schaltfläche
    // (role=button, das Vorschau-Label ist ein span) zielen und über die
    // eindeutige Receipt-Datei prüfen, dass die volle VaultProvenanceShelf nach
    // dem Aufklappen rendert.
    fireEvent.click(await briefingsPanel().findByRole("button", { name: /Provenienz/ }));

    expect(await briefingsPanel().findByText("regal-receipt.md")).toBeTruthy();
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

describe("Lesesaal: TwoPane ab 1024 px", () => {
  it("behält den Shelf neben dem Reader und gibt Fokus an den Auslöser zurück", async () => {
    mockExpandedViewport(true);
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) return { ...ITEM, body_md: "# Ausgabe\n\nLesetext." };
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const title = await lesesaalPanel().findByText(ITEM.title);
    const trigger = title.closest('[role="button"]') as HTMLElement | null;
    expect(trigger).not.toBeNull();
    trigger?.focus();
    fireEvent.click(trigger as HTMLElement);

    expect(await screen.findByRole("region", { name: `Lesesaal: ${ITEM.title}` })).toBeTruthy();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger?.isConnected).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "Detail schließen" }));
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: `Lesesaal: ${ITEM.title}` })).toBeNull();
      expect(document.activeElement).toBe(trigger);
    });
  });

  it("fokussiert nach einem Briefings-Cross-Mode-Open den sichtbaren Lesesaal-Tab", async () => {
    mockExpandedViewport(true);
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) return { ...ITEM, body_md: "Kurzer Lesetext." };
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    const titles = await briefingsPanel().findAllByText(ITEM.title);
    const trigger = titles.map((title) => title.closest('[role="button"]')).find(Boolean) as HTMLElement | null;
    expect(trigger).not.toBeNull();
    trigger?.focus();
    fireEvent.click(trigger as HTMLElement);

    expect(await screen.findByRole("region", { name: `Lesesaal: ${ITEM.title}` })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Detail schließen" }));

    await waitFor(() => {
      expect(screen.queryByRole("region", { name: `Lesesaal: ${ITEM.title}` })).toBeNull();
      expect(document.activeElement).toBe(screen.getByRole("tab", { name: /Lesesaal/ }));
    });
  });
});

describe("BibliothekView: Fokus-Rückgabe nach Briefings→Lesesaal-Moduswechsel", () => {
  it("fokussiert beim Schließen den sichtbaren Lesesaal-Tab, wenn der ursprüngliche Briefing-Trigger hidden ist", async () => {
    mockLibraryFetch((url) => {
      if (url.startsWith("/api/library/item?id=")) return { ...ITEM, body_md: "Kurzer Lesetext." };
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    const titles = await briefingsPanel().findAllByText(ITEM.title);
    const trigger = titles.map((title) => title.closest('[role="button"]')).find(Boolean) as HTMLElement | null;
    expect(trigger).not.toBeNull();
    trigger?.focus();
    fireEvent.click(trigger as HTMLElement);

    const dialog = await screen.findByRole("dialog");
    const closeButtons = within(dialog).getAllByRole("button", { name: "← Übersicht" });
    fireEvent.click(closeButtons[0]);

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(document.activeElement).toBe(screen.getByRole("tab", { name: /Lesesaal/ }));
    });
  });
});
