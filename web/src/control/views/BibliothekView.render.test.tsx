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

import { BibliothekView, LesesaalBody, type LibrarySavedSearch } from "./BibliothekView";
import { _resetPollingStore } from "../hooks/pollingStore";

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
  _resetPollingStore();
  window.localStorage.clear();
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
  categories: ["news", "briefings", "recherchen", "familie", "receipts", "wartung"],
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

const P9_KEYS = [
  "hc-bibliothek.lastVisit.briefings",
  "hc-bibliothek.lastVisit.wissen",
  "hc-bibliothek.lastVisit.lesesaal",
  "hc-bibliothek.lastVisit.ergebnisse",
  "hc-bibliothek.lastVisit.modelle",
] as const;

function mockP9Fetch(
  saved: { items: LibrarySavedSearch[]; count: number } = EMPTY_SAVED,
  extra?: (url: string) => unknown,
) {
  const knowledge = {
    collections: [{
      id: "canon", title: "Canon", description: "Kanonisches Wissen", accent: "cyan", icon: "book",
      doc_count: 1, updated_ts: 1_749_540_680,
      docs: [{ id: "canon::vision.md", collection: "canon", title: "Vision", summary: "North Star", source_ref: "vault:00-Canon/vision.md", tags: ["type:concept"], updated_ts: 1_749_540_680, heading_count: 3 }],
    }],
    count: 5, query: "", now: 1_749_540_700,
  };
  const result = {
    id: "task::t_real", title: "Bibliothek-Audit", kind: "implementation", profile: "coder",
    completed_at: "2025-06-10T07:31:09+00:00", result_summary: "P9 umgesetzt.", verdict: "APPROVED",
    outcome: "completed", cost_usd: 0.12, run_count: 1,
  };
  const models = {
    updated: "2025-06-10", pulse: [], guides: [],
    models: [{ id: "gpt-5", provider: "OpenAI", family: "gpt5-codex", context: "400k", price_in: 1.25, price_out: 10, created: "2025-06-10", scores: [], guide_family: "gpt5-codex" }],
  };
  fetchJSONMock.mockImplementation(async (url: string) => {
    if (extra) {
      const hit = extra(url);
      if (hit !== undefined) return hit;
    }
    if (url.includes("q=frontier+model+releases")) return { ...ITEMS_RESPONSE, count: 8 };
    if (url.includes("category=news")) return { ...ITEMS_RESPONSE, items: [{ ...ITEM, id: "news::real", category: "news", ts: 1_749_540_680 }], count: 1 };
    if (url.includes("category=briefings")) return { ...ITEMS_RESPONSE, count: 2 };
    if (url.startsWith("/api/library/items")) return { ...ITEMS_RESPONSE, count: 7 };
    if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
    if (url.startsWith("/api/library/saved-searches")) return saved;
    if (url.startsWith("/api/library/knowledge")) return knowledge;
    if (url.startsWith("/api/library/results")) return { items: [result], total: 4 };
    if (url.startsWith("/api/library/models")) return models;
    if (url.startsWith("/api/vault/provenance")) return VAULT_PROVENANCE_FIXTURE;
    throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
  });
}

describe("Bibliothek P9: per-tab Ungelesen und Live-Counts", () => {
  it("zeigt API-Gesamtzahlen + tabbezogene Neu-Zahlen und quittiert den gewählten Tab", async () => {
    for (const key of P9_KEYS) window.localStorage.setItem(key, "100");
    mockP9Fetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    const briefings = await screen.findByRole("tab", { name: /Briefings/ });
    await waitFor(() => expect(briefings.textContent).toContain("3"));
    expect(briefings.textContent).not.toContain("neu");
    expect(screen.getByRole("tab", { name: /Nachschlagewerk/ }).textContent).toContain("5");
    expect(screen.getByRole("tab", { name: /Lesesaal/ }).textContent).toContain("7");
    expect(screen.getByRole("tab", { name: /Ergebnisse/ }).textContent).toContain("4");
    expect(screen.getByRole("tab", { name: /Modelle/ }).textContent).toContain("1");

    const wissen = screen.getByRole("tab", { name: /Nachschlagewerk/ });
    expect(wissen.textContent).toContain("1 neu");
    fireEvent.click(wissen);
    await waitFor(() => expect(wissen.textContent).not.toContain("neu"));
    expect(Number(window.localStorage.getItem("hc-bibliothek.lastVisit.wissen"))).toBeGreaterThan(100);
    for (const key of P9_KEYS) expect(window.localStorage.getItem(key)).not.toBeNull();
  });

  it("zeigt pro Smart Shelf den Live-Count der bestehenden Items-Suche", async () => {
    mockP9Fetch({
      items: [{ id: "ss_1", name: "KI Modelle täglich", title: "KI Modelle täglich", query: "frontier model releases", topic_tags: ["KI-Modelle"], person_tags: ["Piet"], created_at: 1, updated_at: 2 }],
      count: 1,
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const title = await screen.findByText("KI Modelle täglich");
    expect(title.closest("li")?.textContent).toContain("8");
    expect(fetchJSONMock).toHaveBeenCalledWith(expect.stringContaining("q=frontier+model+releases"));
  });

  it("quittiert die Deep-Link-Ankunft, ohne die Neu-Schwelle der Lesesaal-Items zu verschieben", async () => {
    window.localStorage.setItem("hc-bibliothek.lastVisit.lesesaal", String(ITEM.ts - 1));
    mockP9Fetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const title = await lesesaalPanel().findByText(ITEM.title);
    expect(title.closest('[role="button"]')?.textContent).toContain("neu");

    const lesesaal = screen.getByRole("tab", { name: /Lesesaal/ });
    await waitFor(() => expect(lesesaal.textContent).not.toContain("neu"));
    expect(Number(window.localStorage.getItem("hc-bibliothek.lastVisit.lesesaal"))).toBeGreaterThan(ITEM.ts);
  });

  it("quittiert den Lesesaal auch beim Item-Open aus dem Briefings-Regal", async () => {
    window.localStorage.setItem("hc-bibliothek.lastVisit.lesesaal", String(ITEM.ts - 1));
    mockP9Fetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek"]}><BibliothekView /></MemoryRouter>);

    const titles = await briefingsPanel().findAllByText(ITEM.title);
    const trigger = titles.map((title) => title.closest('[role="button"]')).find(Boolean) as HTMLElement | null;
    expect(trigger).not.toBeNull();
    fireEvent.click(trigger as HTMLElement);

    const lesesaal = screen.getByRole("tab", { name: /Lesesaal/ });
    await waitFor(() => expect(lesesaal.textContent).not.toContain("neu"));
    expect(Number(window.localStorage.getItem("hc-bibliothek.lastVisit.lesesaal"))).toBeGreaterThan(ITEM.ts);
  });

  it("überspringt nur den fehlerhaften Saved-Search-Count ohne globalen Alert", async () => {
    const tooLongQuery = "x".repeat(201);
    const saved = {
      items: [
        { id: "ss_bad", name: "Zu lange Suche", title: "Zu lange Suche", query: tooLongQuery, topic_tags: [], person_tags: [], created_at: 1, updated_at: 2 },
        { id: "ss_ok", name: "Gültige Suche", title: "Gültige Suche", query: "healthy query", topic_tags: [], person_tags: [], created_at: 1, updated_at: 2 },
      ],
      count: 2,
    };
    mockP9Fetch(saved, (url) => {
      if (!url.startsWith("/api/library/items?")) return undefined;
      const query = new URL(url, "http://localhost").searchParams.get("q");
      if (query === tooLongQuery) throw new Error("400: q must have at most 200 characters");
      if (query === "healthy query") return { ...ITEMS_RESPONSE, count: 4 };
      return undefined;
    });
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const validCard = (await screen.findByText("Gültige Suche")).closest("li");
    const invalidCard = screen.getByText("Zu lange Suche").closest("li");
    expect(validCard).not.toBeNull();
    expect(invalidCard).not.toBeNull();
    expect(within(validCard as HTMLElement).getByText("4")).toBeTruthy();
    expect(within(invalidCard as HTMLElement).queryByText("7")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

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

describe("Lesesaal: zusammengeführte Receipt-Kategorie (P8a)", () => {
  it("zeigt genau einen Receipts-Filter und keinen alten Arbeit-Filter", async () => {
    mockLibraryFetch();
    render(<MemoryRouter initialEntries={["/control/bibliothek?mode=lesesaal"]}><BibliothekView /></MemoryRouter>);

    const panel = lesesaalPanel();
    expect(await panel.findAllByRole("button", { name: /Receipts$/ })).toHaveLength(1);
    expect(panel.queryByRole("button", { name: /Arbeit/ })).toBeNull();
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

// ---------------------------------------------------------------------------
// P6a — Provenienz: Badges, Facetten (URL), Herkunft-Disclosure (interaktiv)
// ---------------------------------------------------------------------------

const PROV_RECEIPT = {
  id: "receipt::Codex::2026-07-22-x.md",
  category: "receipts",
  series_id: "receipts/Codex",
  series: "Codex",
  title: "Receipt — Provenienz-Lauf",
  ts: 1_749_540_669,
  preview: "Inhalt.",
  source_ref: "receipt:Codex/2026-07-22-x.md",
  series_meta: "",
  provenance: {
    producer: "Codex", path: "Receipt", status: "partial",
    chain: { auftraggeber: "Unbekannt", delegation: "Unbekannt", autor: "Codex", review: "Unbekannt", ablage: "receipt:Codex/2026-07-22-x.md" },
    refs: ["receipt:Codex/2026-07-22-x.md"],
  },
};
const PROV_CRON = {
  ...ITEM,
  provenance: {
    producer: "Hermes-System", path: "Cron", status: "partial",
    chain: { auftraggeber: "Unbekannt", delegation: "Unbekannt", autor: "Hermes-System", review: "Unbekannt", ablage: "cron:5a2a54ac3dae" },
    refs: ["cron:5a2a54ac3dae", "2026-06-10_07-31-09.md"],
  },
};
const PROV_ITEMS_RESPONSE = {
  items: [PROV_RECEIPT, PROV_CRON],
  count: 2,
  truncated: false,
  has_more: false,
  categories: ["news", "briefings", "recherchen", "familie", "receipts", "wartung"],
  facets: {
    producer: [{ value: "Codex", count: 1 }, { value: "Hermes-System", count: 1 }],
    path: [{ value: "Cron", count: 1 }, { value: "Receipt", count: 1 }],
  },
  now: 1_749_540_700,
};

function mockProvenanceFetch() {
  fetchJSONMock.mockImplementation(async (url: string) => {
    if (url.startsWith("/api/library/item?id=")) {
      const id = decodeURIComponent(url.split("id=")[1] ?? "");
      const base = id === PROV_RECEIPT.id ? PROV_RECEIPT : PROV_CRON;
      return { ...base, body_md: "# Ausgabe\n\nLesetext ohne drei Überschriften." };
    }
    if (url.startsWith("/api/library/items")) return PROV_ITEMS_RESPONSE;
    if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
    if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
    throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
  });
}

describe("P6a Lesesaal: Erzeuger+Weg-Badges und Facetten-Filter", () => {
  it("zeigt an jeder Zeile kompakt Erzeuger + Weg", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    await screen.findByText("Receipt — Provenienz-Lauf");
    // Default-Ansicht ist die Frontpage-Karte; das Badge trägt das kombinierte Label.
    const badge = document.querySelector('[data-provenance-label="Codex · Receipt"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain("Codex");
    expect(badge?.textContent).toContain("Receipt");
  });

  it("rendert die Facetten-Filter mit kontextuellen Zahlen (unkomprimiert über den Bestand)", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    await screen.findByText("Receipt — Provenienz-Lauf");
    const codex = document.querySelector('[data-facet="Erzeuger"][data-facet-value="Codex"]');
    expect(codex?.textContent).toContain("Codex");
    expect(codex?.textContent).toContain("1");
    const receipt = document.querySelector('[data-facet="Weg"][data-facet-value="Receipt"]');
    expect(receipt?.textContent).toContain("Receipt");
    expect(receipt?.getAttribute("aria-pressed")).toBe("false");
    // Rollen-Gruppen sind für Screenreader benannt
    expect(document.querySelector('[role="group"][aria-label="Erzeuger"]')).not.toBeNull();
    expect(document.querySelector('[role="group"][aria-label="Weg"]')).not.toBeNull();
  });

  it("Facetten-Klick setzt den URL-Param (Mehrfachauswahl) und triggert den Refetch", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    await screen.findByText("Receipt — Provenienz-Lauf");
    const codex = document.querySelector('[data-facet="Erzeuger"][data-facet-value="Codex"]') as HTMLElement;
    fireEvent.click(codex);
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(expect.stringContaining("producer=Codex"));
    });
    await waitFor(() => {
      expect(document.querySelector('[data-facet-value="Codex"]')?.getAttribute("aria-pressed")).toBe("true");
    });
  });

  it("URL-Roundtrip: ?producer=Codex stellt die Auswahl über Reload wieder her", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter initialEntries={["/bibliothek?producer=Codex"]}><LesesaalBody /></MemoryRouter>);
    await screen.findByText("Receipt — Provenienz-Lauf");
    expect(fetchJSONMock).toHaveBeenCalledWith(expect.stringContaining("producer=Codex"));
    expect(document.querySelector('[data-facet-value="Codex"]')?.getAttribute("aria-pressed")).toBe("true");
  });

  it("Zurücksetzen entfernt die Facetten-Auswahl aus URL und Ansicht", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter initialEntries={["/bibliothek?producer=Codex"]}><LesesaalBody /></MemoryRouter>);
    await screen.findByText("Receipt — Provenienz-Lauf");
    fireEvent.click(screen.getByRole("button", { name: "Zurücksetzen" }));
    await waitFor(() => {
      expect(document.querySelector('[data-facet-value="Codex"]')?.getAttribute("aria-pressed")).toBe("false");
    });
    expect(screen.queryByRole("button", { name: "Zurücksetzen" })).toBeNull();
  });

  it("behält ausgewählte Nulltreffer-Facetten sichtbar und einzeln abschaltbar", async () => {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/items")) {
        return {
          ...PROV_ITEMS_RESPONSE,
          items: [],
          count: 0,
          facets: {
            producer: [{ value: "Hermes-System", count: 1 }],
            path: [{ value: "Receipt", count: 1 }],
          },
        };
      }
      if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
      if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
    render(
      <MemoryRouter initialEntries={["/bibliothek?producer=Codex&path=Cron"]}>
        <LesesaalBody />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(document.querySelector('[data-facet-value="Codex"]')?.getAttribute("aria-pressed")).toBe("true");
    });
    expect(document.querySelector('[data-facet-value="Codex"]')?.textContent).toContain("0");
    expect(document.querySelector('[data-facet-value="Cron"]')?.getAttribute("aria-pressed")).toBe("true");
    expect(document.querySelector('[data-facet-value="Cron"]')?.textContent).toContain("0");
    fireEvent.click(document.querySelector('[data-facet-value="Codex"]') as HTMLElement);
    await waitFor(() => {
      expect(document.querySelector('[data-facet-value="Codex"]')).toBeNull();
    });
  });

  it("bietet bei einem ungültigen Weg-Link trotz Ladefehler einen Reset an", async () => {
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/items") && url.includes("path=Manuel")) {
        throw new Error("unknown path facet");
      }
      if (url.startsWith("/api/library/items")) return PROV_ITEMS_RESPONSE;
      if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
      if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/bibliothek?path=Manuel"]}><LesesaalBody /></MemoryRouter>);

    expect((await screen.findByRole("alert")).textContent).toContain("unknown path facet");
    fireEvent.click(screen.getByRole("button", { name: "Zurücksetzen" }));
    expect(await screen.findByText("Receipt — Provenienz-Lauf")).toBeTruthy();
  });

  it("zeigt bei aktiver Provenienz-Facette die vollständige Trefferliste statt der Startseiten-Auswahl", async () => {
    const secondReceipt = {
      ...PROV_RECEIPT,
      id: "receipt::Codex::2026-07-22-y.md",
      title: "Receipt — Zweiter Lauf",
      ts: PROV_RECEIPT.ts - 1,
    };
    fetchJSONMock.mockImplementation(async (url: string) => {
      if (url.startsWith("/api/library/items")) {
        return { ...PROV_ITEMS_RESPONSE, items: [PROV_RECEIPT, secondReceipt], count: 2 };
      }
      if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
      if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });
    render(<MemoryRouter initialEntries={["/bibliothek?producer=Codex"]}><LesesaalBody /></MemoryRouter>);

    expect(await screen.findByText("Receipt — Provenienz-Lauf")).toBeTruthy();
    expect(screen.getByText("Receipt — Zweiter Lauf")).toBeTruthy();
  });

  it("refetcht die paginierte Liste beim Öffnen eines Dokuments nicht", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    const card = await screen.findByText("Receipt — Provenienz-Lauf");
    const itemCallsBefore = fetchJSONMock.mock.calls.filter(
      ([url]) => typeof url === "string" && url.startsWith("/api/library/items"),
    ).length;

    fireEvent.click(card);
    await waitFor(() => {
      expect(fetchJSONMock).toHaveBeenCalledWith(expect.stringContaining("/api/library/item?id="));
    });
    const itemCallsAfter = fetchJSONMock.mock.calls.filter(
      ([url]) => typeof url === "string" && url.startsWith("/api/library/items"),
    ).length;
    expect(itemCallsAfter).toBe(itemCallsBefore);
  });
});

describe("P6a Lesesaal: Herkunft-Disclosure im geöffneten Dokument", () => {
  it("ist zugeklappt und zeigt Status, fünf Rollen, Belege und benennt Unbekanntes", async () => {
    mockProvenanceFetch();
    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    const card = await screen.findByText("Receipt — Provenienz-Lauf");
    fireEvent.click(card);

    const summary = await screen.findByText("Herkunft");
    expect(summary.closest("details")?.hasAttribute("open")).toBe(false);
    expect(screen.getAllByText("teilweise belegt").length).toBeGreaterThanOrEqual(1);
    for (const role of ["Auftraggeber", "Delegation", "Autor", "Review", "Ablage"]) {
      expect(screen.getByText(role)).toBeTruthy();
    }
    expect(screen.getAllByText("Unbekannt").length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText("Technische Belege")).toBeTruthy();
    expect(screen.getAllByText("receipt:Codex/2026-07-22-x.md").length).toBeGreaterThan(0);
  });
});

describe("P6b Lesesaal: Korrektur aktualisiert Detail, Liste und Facetten", () => {
  it("refetcht nach bestätigtem Speichern beide Lesewege und zeigt das Korrigiert-Badge", async () => {
    let corrected = false;
    const correction = {
      item_id: PROV_RECEIPT.id,
      active: true,
      fields: { auftraggeber: "Piet" },
      original: PROV_RECEIPT.provenance,
      reason: "Auftraggeber belegt",
      actor: "operator",
      created_at: 1_753_200_000,
      updated_at: 1_753_200_000,
      history: [{
        at: 1_753_200_000,
        action: "set",
        fields: { auftraggeber: "Piet" },
        reason: "Auftraggeber belegt",
        actor: "operator",
      }],
    };
    const effective = {
      ...PROV_RECEIPT.provenance,
      chain: { ...PROV_RECEIPT.provenance.chain, auftraggeber: "Piet" },
    };
    const correctedItem = { ...PROV_RECEIPT, provenance: effective, correction };

    fetchJSONMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/library/correction/preview" && init?.method === "POST") {
        return { provenance: effective, fields: { auftraggeber: "Piet" } };
      }
      if (url === "/api/library/correction" && init?.method === "PUT") {
        corrected = true;
        return { correction, provenance: effective };
      }
      if (url.startsWith("/api/library/correction?id=")) {
        return { correction: null };
      }
      if (url.startsWith("/api/library/item?id=")) {
        const base = corrected ? correctedItem : PROV_RECEIPT;
        return { ...base, body_md: "# Ausgabe\n\nLesetext." };
      }
      if (url.startsWith("/api/library/items")) {
        return {
          ...PROV_ITEMS_RESPONSE,
          items: corrected ? [correctedItem, PROV_CRON] : [PROV_RECEIPT, PROV_CRON],
        };
      }
      if (url.startsWith("/api/library/topics")) return EMPTY_TOPICS;
      if (url.startsWith("/api/library/saved-searches")) return EMPTY_SAVED;
      throw new Error(`unerwarteter fetchJSON-Aufruf: ${url}`);
    });

    render(<MemoryRouter><LesesaalBody /></MemoryRouter>);
    fireEvent.click(await screen.findByText("Receipt — Provenienz-Lauf"));
    fireEvent.click(await screen.findByRole("button", { name: "Herkunft korrigieren" }));
    await waitFor(() => expect((screen.getByLabelText(/^Auftraggeber/) as HTMLInputElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText(/^Auftraggeber/), { target: { value: "Piet" } });
    fireEvent.change(screen.getByLabelText("Begründung (Pflicht)"), {
      target: { value: "Auftraggeber belegt" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Korrektur prüfen" }));
    fireEvent.click(await screen.findByRole("button", { name: "Jetzt verbindlich speichern" }));

    await screen.findByRole("status");
    await waitFor(() => {
      const listCalls = fetchJSONMock.mock.calls.filter(
        ([url]) => typeof url === "string" && url.startsWith("/api/library/items"),
      );
      const detailCalls = fetchJSONMock.mock.calls.filter(
        ([url]) => typeof url === "string" && url.startsWith("/api/library/item?id="),
      );
      expect(listCalls.length).toBeGreaterThanOrEqual(2);
      expect(detailCalls.length).toBeGreaterThanOrEqual(2);
      expect(screen.getAllByText("Korrigiert").length).toBeGreaterThanOrEqual(1);
    });
  });
});
