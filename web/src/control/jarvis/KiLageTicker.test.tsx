// @vitest-environment jsdom
/**
 * KiLageTicker — G2: 1-Zeilen-Ticker + Akkordeon, usePaFeed-Realshape +
 * Mock-Fallback bei Fehler/leer.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const usePaFeedMock = vi.hoisted(() => vi.fn());

vi.mock("./usePaFeed", () => ({
  usePaFeed: () => usePaFeedMock(),
}));

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

beforeEach(() => {
  usePaFeedMock.mockReturnValue({ data: null, error: null, loading: false });
});

afterEach(() => cleanup());

describe("KiLageTicker", () => {
  it("rendert Feed-Items im echten usePaFeed-Shape und klappt per aria-expanded", () => {
    usePaFeedMock.mockReturnValue({ data: FEED_PAGE, error: null, loading: false });
    render(<KiLageTicker />);

    // Neuester Titel in der 1-Zeilen-Leiste (letzte Feed-Row).
    const toggle = screen.getByRole("button", { name: /KI-LAGE · Kimi Coding-Update/ });
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

  it("fällt bei Feed-Fehler auf JARVIS_NEWS_ITEMS zurück", () => {
    usePaFeedMock.mockReturnValue({ data: null, error: "netz", loading: false });
    render(<KiLageTicker />);

    const newestMock = JARVIS_NEWS_ITEMS[0].text;
    const toggle = screen.getByRole("button", {
      name: new RegExp(`KI-LAGE · ${newestMock.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`),
    });
    fireEvent.click(toggle);

    const list = screen.getByRole("list");
    const titles = Array.from(list.querySelectorAll(".jv-ticker-title")).map((el) => el.textContent);
    expect(titles).toEqual(JARVIS_NEWS_ITEMS.slice(0, 5).map((item) => item.text));
  });

  it("fällt bei leerem Feed auf Mock-Fallback zurück", () => {
    usePaFeedMock.mockReturnValue({
      data: { items: [], next_since_id: 0, has_more: false },
      error: null,
      loading: false,
    });
    render(<KiLageTicker />);

    expect(
      screen.getByRole("button", {
        name: new RegExp(
          `KI-LAGE · ${JARVIS_NEWS_ITEMS[0].text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`,
        ),
      }),
    ).toBeTruthy();
  });
});
