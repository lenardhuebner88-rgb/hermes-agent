// @vitest-environment jsdom
/**
 * G6 — Mobile-Sheet-Zustandsautomat (closed | half | full).
 * Desktop-Pfad bleibt ohne Sheet-Mechanik; Drawer liegt im Markup über dem Sheet.
 */
import { cleanup, fireEvent, render, screen, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

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
    }) => (
      <div className="jv-chatcol" data-testid="chat">
        {props.aboveThread}
        <div className="jv-chat" data-testid="thread">
          thread
        </div>
        {props.belowThread}
        <form className="jv-ask" aria-label="Composer">
          <input type="text" aria-label="Frage" />
        </form>
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
  AktivitaetPanel: () => <div data-testid="aktiv-panel" />,
}));
vi.mock("./SessionsPanel", () => ({ SessionsPanel: () => null }));

import { JarvisShellView } from "./JarvisShellView";

function mockMatchMedia(matches: boolean) {
  const listeners = new Set<(e: MediaQueryListEvent) => void>();
  const mql = {
    matches,
    media: "(max-width: 759px)",
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => {
      listeners.add(cb);
    },
    removeEventListener: (_: string, cb: (e: MediaQueryListEvent) => void) => {
      listeners.delete(cb);
    },
    dispatchEvent: vi.fn(),
  };
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => mql),
  );
  return {
    setMatches(next: boolean) {
      mql.matches = next;
      const event = { matches: next } as MediaQueryListEvent;
      listeners.forEach((cb) => cb(event));
    },
  };
}

function renderShell() {
  return render(
    <MemoryRouter>
      <JarvisShellView />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("JarvisSheet G6 — Desktop-Pfad", () => {
  it("rendert keine Sheet-Mechanik (kein Handle, kein data-sheet)", () => {
    mockMatchMedia(false);
    const { container } = renderShell();

    expect(screen.queryByTestId("jv-sheet-handle")).toBeNull();
    expect(container.querySelector(".jv-chatsheet")).toBeNull();
    expect(container.querySelector("[data-sheet]")).toBeNull();
    expect(container.querySelector("[data-mobile-sheet]")).toBeNull();
    // Grid-Struktur bleibt.
    expect(container.querySelector(".jv-stage")).toBeTruthy();
    expect(container.querySelector(".jv-graphzone")).toBeTruthy();
    expect(container.querySelector(".jv-column")).toBeTruthy();
    expect(screen.getByTestId("chat")).toBeTruthy();
  });
});

describe("JarvisSheet G6 — Mobile-Zustandsautomat", () => {
  beforeEach(() => {
    mockMatchMedia(true);
  });

  it("startet closed und wechselt closed → half → closed über den Griff", () => {
    const { container } = renderShell();

    const sheet = container.querySelector(".jv-chatsheet");
    expect(sheet).toBeTruthy();
    expect(sheet?.getAttribute("data-sheet")).toBe("closed");
    expect(container.querySelector(".jv")?.getAttribute("data-mobile-sheet")).toBe(
      "closed",
    );

    const handle = screen.getByTestId("jv-sheet-handle");
    fireEvent.click(handle);
    expect(sheet?.getAttribute("data-sheet")).toBe("half");
    expect(screen.getByTestId("jv-sheet-expand")).toBeTruthy();

    fireEvent.click(handle);
    expect(sheet?.getAttribute("data-sheet")).toBe("closed");
    expect(screen.queryByTestId("jv-sheet-expand")).toBeNull();
  });

  it("Expand steuert half ↔ full; Griff aus full → half", () => {
    const { container } = renderShell();
    const sheet = container.querySelector(".jv-chatsheet");
    const handle = screen.getByTestId("jv-sheet-handle");

    fireEvent.click(handle); // closed → half
    expect(sheet?.getAttribute("data-sheet")).toBe("half");

    fireEvent.click(screen.getByTestId("jv-sheet-expand")); // half → full
    expect(sheet?.getAttribute("data-sheet")).toBe("full");

    fireEvent.click(screen.getByTestId("jv-sheet-expand")); // full → half
    expect(sheet?.getAttribute("data-sheet")).toBe("half");

    fireEvent.click(screen.getByTestId("jv-sheet-expand")); // half → full
    expect(sheet?.getAttribute("data-sheet")).toBe("full");

    fireEvent.click(handle); // full → half
    expect(sheet?.getAttribute("data-sheet")).toBe("half");
  });

  it("Fokus auf Composer öffnet closed → half und hält open", () => {
    const { container } = renderShell();
    const sheet = container.querySelector(".jv-chatsheet");
    expect(sheet?.getAttribute("data-sheet")).toBe("closed");

    const input = screen.getByLabelText("Frage");
    fireEvent.focusIn(input);
    expect(sheet?.getAttribute("data-sheet")).toBe("half");

    // erneuter Fokus klappt nicht zu
    fireEvent.focusIn(input);
    expect(sheet?.getAttribute("data-sheet")).toBe("half");
  });

  it("Drawer-Host liegt im Markup nach dem Sheet (Stacking über Sheet)", () => {
    const { container } = renderShell();
    const column = container.querySelector(".jv-column");
    const drawers = container.querySelector(".jv-drawers");
    expect(column).toBeTruthy();
    expect(drawers).toBeTruthy();

    const stage = container.querySelector(".jv-stage");
    const children = Array.from(stage?.children ?? []);
    const colIdx = children.indexOf(column as Element);
    const drawerIdx = children.indexOf(drawers as Element);
    expect(drawerIdx).toBeGreaterThan(colIdx);

    // CSS-Vertrag: drawers z-index 12, sheet z-index 5 (String-Guard im CSS-File
    // ist optional — hier Markup-Reihenfolge + data-testid).
    expect(screen.getByTestId("jv-drawers")).toBeTruthy();
  });

  it("Chip + Vitals bleiben im Graph gemountet (closed erreichbar)", () => {
    renderShell();
    expect(screen.getByTestId("projekte-chip")).toBeTruthy();
    expect(screen.getByTestId("vitals")).toBeTruthy();
    expect(screen.getByTestId("graph")).toBeTruthy();
  });
});

describe("JarvisSheet G6 — MatchMedia-Umschaltung", () => {
  it("entfernt Sheet-Chrome wenn Viewport Desktop wird", () => {
    const mq = mockMatchMedia(true);
    const { container } = renderShell();
    expect(screen.getByTestId("jv-sheet-handle")).toBeTruthy();

    act(() => {
      mq.setMatches(false);
    });
    expect(screen.queryByTestId("jv-sheet-handle")).toBeNull();
    expect(container.querySelector(".jv-chatsheet")).toBeNull();
  });
});
