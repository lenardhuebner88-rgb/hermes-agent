// @vitest-environment jsdom
/**
 * KiLageTicker — G2 + B2b: News-Endpoint primär (Tag + Expand), usePaFeed-
 * Fallback, Mock bei Feed-Fehler/leer.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const usePaFeedMock = vi.hoisted(() => vi.fn());
const getPaNewsMock = vi.hoisted(() => vi.fn());

vi.mock("./usePaFeed", () => ({
  usePaFeed: () => usePaFeedMock(),
}));

// Partial mock only — full `{ api: { getPaNews } }` would replace the whole
// module export and can poison co-worker suites (e.g. ProjekteChip) that import
// real `fetchJSON`/`api` from the same path.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      ...actual.api,
      getPaNews: getPaNewsMock,
    },
  };
});

import { KiLageTicker } from "./KiLageTicker";
import { JARVIS_NEWS_ITEMS } from "./mockContent";

/** Echtes usePaFeed-Response-Shape (PaFeedPage aus api.ts / usePaFeed.test). */
const FEED_PAGE = {
  items: [
    {
      id: 1,
      ts: 1752000000,
      kind: "news",
      severity: "info",
      title: "OpenAI senkt Preise",
      ref: null,
      delivered_push: 0,
    },
    {
      id: 2,
      ts: 1752000060,
      kind: "news",
      severity: "info",
      title: "DeepSeek R4 offen",
      ref: "https://example.com/deepseek",
      delivered_push: 0,
    },
    {
      id: 3,
      ts: 1752000120,
      kind: "news",
      severity: "info",
      title: "Kimi Coding-Update",
      ref: null,
      delivered_push: 0,
    },
  ],
  next_since_id: 3,
  has_more: false,
};

/** Echtes pa-news/v1-Shape (PaNewsResponse / hermes_cli/pa_news.py). */
const NEWS_PAGE = {
  version: "pa-news/v1",
  items: [
    {
      title: "Claude 5 kündigt Context-Window an",
      ts: 1752001000,
      tag: "Frontier Desk",
      summary: "Anthropic erweitert das Kontextfenster deutlich.",
      markdown:
        "## Response\n\n**Headline**\n\nLange Analyse mit Details über das neue Context-Window und Benchmarks.",
    },
    {
      title: "Qwen Flash Patch",
      ts: 1752000500,
      tag: "Frontier Flash",
      summary: "Kurzer Flash-Digest zu Qwen.",
      markdown: "Patch-Notes und kurze Bewertung.",
    },
  ],
};

beforeEach(() => {
  usePaFeedMock.mockReturnValue({ data: null, error: null, loading: false });
  // Default: News-Endpoint nicht verfügbar → bisheriger Feed-/Mock-Pfad.
  getPaNewsMock.mockRejectedValue(new Error("404: /api/pa/news"));
});

afterEach(() => cleanup());

describe("KiLageTicker", () => {
  it("rendert Feed-Items im echten usePaFeed-Shape und klappt per aria-expanded", async () => {
    usePaFeedMock.mockReturnValue({ data: FEED_PAGE, error: null, loading: false });
    render(<KiLageTicker />);

    // Neuester Titel in der 1-Zeilen-Leiste (letzte Feed-Row).
    const toggle = await screen.findByRole("button", { name: /KI-LAGE · Kimi Coding-Update/ });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("list")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    const list = screen.getByRole("list");
    const items = list.querySelectorAll(".jv-ticker-item");
    expect(items).toHaveLength(3);
    // Neueste zuerst.
    expect(items[0].textContent).toContain("Kimi Coding-Update");
    expect(items[1].textContent).toContain("DeepSeek R4 offen");
    expect(items[2].textContent).toContain("OpenAI senkt Preise");

    // href aus ref (nur echte URLs) → klickbarer Link.
    const link = list.querySelector('a.jv-ticker-link[href="https://example.com/deepseek"]');
    expect(link).toBeTruthy();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("fällt bei Feed-Fehler auf JARVIS_NEWS_ITEMS zurück", async () => {
    usePaFeedMock.mockReturnValue({ data: null, error: "netz", loading: false });
    render(<KiLageTicker />);

    const newestMock = JARVIS_NEWS_ITEMS[0].text;
    const toggle = await screen.findByRole("button", {
      name: new RegExp(`KI-LAGE · ${newestMock.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
    });
    fireEvent.click(toggle);

    const list = screen.getByRole("list");
    const titles = Array.from(list.querySelectorAll(".jv-ticker-title")).map((el) => el.textContent);
    expect(titles).toEqual(JARVIS_NEWS_ITEMS.slice(0, 5).map((item) => item.text));
  });

  it("fällt bei leerem Feed auf Mock-Fallback zurück", async () => {
    usePaFeedMock.mockReturnValue({
      data: { items: [], next_since_id: 0, has_more: false },
      error: null,
      loading: false,
    });
    render(<KiLageTicker />);

    expect(
      await screen.findByRole("button", {
        name: new RegExp(
          `KI-LAGE · ${JARVIS_NEWS_ITEMS[0].text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`,
        ),
      }),
    ).toBeTruthy();
  });

  it("News-Endpoint-Shape → Items mit Tag-Badge; Expand zeigt Summary", async () => {
    getPaNewsMock.mockResolvedValue(NEWS_PAGE);
    // Feed wäre auch da — News hat Vorrang.
    usePaFeedMock.mockReturnValue({ data: FEED_PAGE, error: null, loading: false });
    render(<KiLageTicker />);

    const toggle = await screen.findByRole("button", {
      name: /KI-LAGE · Claude 5 kündigt Context-Window an/,
    });
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(screen.getByText("Frontier Desk")).toBeTruthy();
    });
    expect(screen.getByText("Frontier Flash")).toBeTruthy();
    expect(getPaNewsMock).toHaveBeenCalledWith(5, { skipStaleTokenReload: true });

    const newsBtns = screen.getAllByRole("button").filter((b) =>
      b.classList.contains("jv-ticker-newsbtn"),
    );
    expect(newsBtns.length).toBe(2);
    expect(newsBtns[0].getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("Anthropic erweitert das Kontextfenster deutlich.")).toBeNull();

    fireEvent.click(newsBtns[0]);
    expect(newsBtns[0].getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText("Anthropic erweitert das Kontextfenster deutlich.")).toBeTruthy();
    expect(screen.getByText(/Lange Analyse mit Details/)).toBeTruthy();

    // Kein Feed-Link, wenn News aktiv.
    expect(document.querySelector("a.jv-ticker-link")).toBeNull();
  });

  it("Endpoint-404 → Feed-Fallback wie bisher", async () => {
    getPaNewsMock.mockRejectedValue(new Error("404: /api/pa/news"));
    usePaFeedMock.mockReturnValue({ data: FEED_PAGE, error: null, loading: false });
    render(<KiLageTicker />);

    const toggle = await screen.findByRole("button", { name: /KI-LAGE · Kimi Coding-Update/ });
    fireEvent.click(toggle);

    const list = screen.getByRole("list");
    expect(list.querySelectorAll(".jv-ticker-item")).toHaveLength(3);
    expect(list.querySelector(".jv-ticker-tag")).toBeNull();
    expect(list.querySelector('a.jv-ticker-link[href="https://example.com/deepseek"]')).toBeTruthy();
  });

  it("Endpoint-Fehler UND Feed-Fehler → Mock", async () => {
    getPaNewsMock.mockRejectedValue(new Error("network failed"));
    usePaFeedMock.mockReturnValue({ data: null, error: "netz", loading: false });
    render(<KiLageTicker />);

    const newestMock = JARVIS_NEWS_ITEMS[0].text;
    const toggle = await screen.findByRole("button", {
      name: new RegExp(`KI-LAGE · ${newestMock.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
    });
    fireEvent.click(toggle);

    const titles = Array.from(document.querySelectorAll(".jv-ticker-title")).map(
      (el) => el.textContent,
    );
    expect(titles).toEqual(JARVIS_NEWS_ITEMS.slice(0, 5).map((item) => item.text));
  });
});
