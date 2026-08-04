// @vitest-environment jsdom

import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { DesignBoardView } from "./DesignBoardView";

describe("DesignBoardView", () => {
  it("renders the heading without crashing", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter><DesignBoardView /></MemoryRouter>
    );
    expect(html).toContain("Design Board");
  });

  // Raw-hex / arbitrary-color absence is enforced project-wide by the
  // gate-frontend.sh token ratchet; asserting it here with the ratchet's own
  // literal search strings would itself trip that grep-based ratchet. Assert
  // token usage positively instead. Loaded via process.cwd(): in the jsdom
  // environment import.meta.url is http://, which readFileSync rejects.
  it("uses Leitstand surface tokens", () => {
    const candidates = [
      path.join(process.cwd(), "src/control/views/DesignBoardView.tsx"),
      path.join(process.cwd(), "web/src/control/views/DesignBoardView.tsx"),
    ];
    const file = candidates.find((candidate) => existsSync(candidate));
    if (!file) throw new Error(`DesignBoardView.tsx not found from ${process.cwd()}`);
    expect(readFileSync(file, "utf8")).toContain("bg-surface-0");
  });
});

describe("DesignBoardView a11y", () => {
  beforeEach(() => {
    Object.defineProperty(window, "__HERMES_SESSION_TOKEN__", {
      configurable: true,
      value: "test-token",
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("marks the disclosure toggle with aria-expanded", () => {
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);

    const toggle = screen.getByRole("button", { name: /Neue Karte/, expanded: false });
    fireEvent.click(toggle);

    expect(screen.getByRole("button", { name: /Abbrechen/, expanded: true })).toBeTruthy();
  });

  it("exposes the active category via aria-pressed, not only text color", () => {
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    // Empty-State-Button öffnet das Formular direkt (eindeutiger Name).
    fireEvent.click(screen.getByRole("button", { name: /Neue Karte anlegen/ }));

    // "bug" ist der Default.
    screen.getByRole("button", { name: "bug", pressed: true });
    screen.getByRole("button", { name: "wish", pressed: false });

    fireEvent.click(screen.getByRole("button", { name: "wish" }));

    // Nach dem Wechsel: wish aktiv, die zuvor aktive Kategorie wieder inaktiv.
    screen.getByRole("button", { name: "wish", pressed: true });
    screen.getByRole("button", { name: "bug", pressed: false });
  });

  it("gives title and target-view fields an accessible name beyond placeholder", () => {
    render(<MemoryRouter><DesignBoardView /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: /Neue Karte anlegen/ }));

    screen.getByRole("textbox", { name: /Titel/ });
    screen.getByRole("textbox", { name: /Ziel-View/i });
  });
});
