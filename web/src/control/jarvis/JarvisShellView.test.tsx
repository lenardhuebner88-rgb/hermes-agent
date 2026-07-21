// @vitest-environment jsdom
/**
 * JarvisShellView — G2: Desktop-Grid (Graph | Säule), TopBar mit Klassik-
 * Link, Drawer öffnet weiter über das Periphery-Event. HUD/Mock-Floats
 * entfallen. Live-Kinder sind gestubbt: getestet wird das Shell-Markup.
 * G6-Sheet-Details: siehe JarvisSheet.test.tsx.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

const aktivitaetProps = vi.hoisted(() => ({ open: undefined as boolean | undefined }));

vi.mock("./useOfflineBannerHeight", () => ({ useOfflineBannerHeight: () => undefined }));
vi.mock("./JarvisGraph", () => ({
  JarvisGraph: () => <div data-testid="graph" />,
  JarvisGraphStatsTag: () => null,
  JarvisGraphTag: () => null,
}));
vi.mock("./JarvisChat", async () => {
  const actual = await vi.importActual<typeof import("./JarvisChat")>("./JarvisChat");
  return {
    ...actual,
    JarvisChat: (props: {
      aboveThread?: React.ReactNode;
      belowThread?: React.ReactNode;
      headerExtra?: React.ReactNode;
    }) => (
      <div data-testid="chat">
        {props.headerExtra}
        {props.aboveThread}
        {props.belowThread}
      </div>
    ),
  };
});
vi.mock("./ProjekteChip", () => ({
  ProjekteChip: () => <div data-testid="projekte-chip" />,
}));
vi.mock("./WartetPanel", () => ({
  WartetPanel: () => <div data-testid="wartet" />,
}));
vi.mock("./KiLageTicker", () => ({
  KiLageTicker: () => <div data-testid="ticker" />,
}));
vi.mock("./SystemVitals", () => ({
  SystemVitals: () => <div data-testid="vitals" />,
}));
vi.mock("./AktivitaetPanel", () => ({
  AktivitaetPanel: (props: { open: boolean }) => {
    aktivitaetProps.open = props.open;
    return null;
  },
}));
vi.mock("./SessionsPanel", () => ({ SessionsPanel: () => null }));

import { JarvisShellView } from "./JarvisShellView";
import { JARVIS_OPEN_AKTIVITAET_EVENT } from "./JarvisChat";

function renderShell() {
  return render(
    <MemoryRouter>
      <JarvisShellView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  aktivitaetProps.open = undefined;
  // Desktop-Default: keine Mobile-Sheet-Mechanik (matchMedia false / absent).
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: false,
      media: "(max-width: 759px)",
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("JarvisShellView — G2 Grid + TopBar + Drawer", () => {
  it("rendert Desktop-Grid-Struktur (Graph-Zone + Säule) und montiert Slot-Inhalte", () => {
    const { container } = renderShell();

    expect(container.querySelector(".jv-stage")).toBeTruthy();
    expect(container.querySelector(".jv-graphzone")).toBeTruthy();
    expect(container.querySelector(".jv-column")).toBeTruthy();
    expect(screen.getByTestId("graph")).toBeTruthy();
    expect(screen.getByTestId("projekte-chip")).toBeTruthy();
    expect(screen.getByTestId("vitals")).toBeTruthy();
    expect(screen.getByTestId("chat")).toBeTruthy();
    expect(screen.getByTestId("wartet")).toBeTruthy();
    expect(screen.getByTestId("ticker")).toBeTruthy();

    // Alte Floats/HUD sind weg.
    expect(container.querySelector(".jv-brainpanel")).toBeNull();
    expect(container.querySelector(".jv-filter")).toBeNull();
    expect(container.querySelector(".jv-news")).toBeNull();
    expect(container.querySelector(".jv-quiet")).toBeNull();
    expect(container.querySelector(".jv-hudtoggle")).toBeNull();
    expect(container.querySelector(".jv-strips")).toBeNull();
    expect(container.querySelector(".jv-emblem")).toBeNull();
  });

  it("TopBar trägt ← Dashboard und Klassik-Link", () => {
    renderShell();

    const dash = screen.getByRole("link", { name: "← Dashboard" });
    expect(dash.getAttribute("href")).toBe("/control");

    const klassik = screen.getByRole("link", { name: "Klassik" });
    expect(klassik.getAttribute("href")).toBe("/control/projekte-klassisch");
  });

  it("Periphery-Event des Chats öffnet den Aktivitaet-Drawer", () => {
    renderShell();
    expect(aktivitaetProps.open).toBe(false);

    fireEvent(window, new CustomEvent(JARVIS_OPEN_AKTIVITAET_EVENT));
    expect(aktivitaetProps.open).toBe(true);
  });
});
