// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ControlShell, type ControlTab } from "./ControlShell";
import type { DecisionInboxData } from "../hooks/useControlData";

const notificationBridgeSpy = vi.fn((_props: unknown) => null);
vi.mock("./NotificationBridge", () => ({ NotificationBridge: (props: unknown) => notificationBridgeSpy(props) }));
vi.mock("./Overlay", () => ({ Overlay: ({ children }: { children: ReactNode }) => <div>{children}</div> }));
vi.mock("../lib/clock", () => ({ useClientNowSeconds: () => 1783025500 }));

const baseProps = {
  density: "compact" as const,
  inbox: {
    items: [],
    summary: { total: 0, critical: 0, warnings: 0 },
    snapshot: { interventions: [] },
    worstTone: "emerald",
    loading: false,
    sourceErrors: [],
  } as unknown as DecisionInboxData,
  openProposals: 0,
  inboxTotal: 0,
  inboxTone: "emerald" as const,
  libraryUnread: 0,
  strategistCount: 0,
  health: { data: null, error: null, isStale: false, lastUpdated: null },
  onNavigate: vi.fn(),
  onOpenCommand: vi.fn(),
};

function renderShell(active: ControlTab) {
  return render(
    <MemoryRouter>
      <ControlShell {...baseProps} active={active}>
        <main>content</main>
      </ControlShell>
    </MemoryRouter>,
  );
}

describe("ControlShell unified responsive shell (W2-a)", () => {
  afterEach(() => {
    cleanup();
    notificationBridgeSpy.mockClear();
  });

  it("keeps the five PlanSpec primary tabs reachable and drops the retired ones", () => {
    renderShell("backlog");

    for (const label of ["Fleet", "Start", "Terminals", "Statistik", "Bibliothek"]) {
      expect(screen.getAllByRole("button", { name: label }).length).toBeGreaterThan(0);
    }
    for (const retired of ["Flow", "Ketten", "Hermes", "Puls", "Pressure", "Ops"]) {
      expect(screen.queryByRole("button", { name: retired })).toBeNull();
      expect(screen.queryByRole("link", { name: retired })).toBeNull();
    }
  });

  it("renders the rail as a Hauptnavigation landmark, hidden below the tab breakpoint", () => {
    renderShell("backlog");
    const rail = screen.getByRole("navigation", { name: "Hauptnavigation" });
    expect(rail.className).toContain("hidden");
    expect(rail.className).toContain("tab:flex");
  });

  it("keeps the rail 'Mehr' flyout panel scrollable and viewport-clamped", () => {
    renderShell("backlog");
    const panel = screen.getByTestId("rail-more-flyout");
    expect(panel.className).toContain("overflow-y-auto");
    expect(panel.className).toContain("max-h-");
  });

  it("renders the bottom bar hidden at/above the tab breakpoint", () => {
    renderShell("backlog");
    const bottomBar = screen.getByRole("navigation", { name: "Navigation" });
    expect(bottomBar.className).toContain("tab:hidden");
  });

  it("marks the active primary tab as aria-current=page everywhere it appears", () => {
    renderShell("statistik");
    const matches = screen.getAllByRole("button", { name: "Statistik" });
    expect(matches.length).toBeGreaterThan(0);
    for (const el of matches) expect(el.getAttribute("aria-current")).toBe("page");
  });

  it("pins the active secondary tab onto the rail with a short label and aria-current", () => {
    renderShell("loops");
    const pinned = screen.getAllByRole("link", { name: "Loops" }).find((el) => el.getAttribute("aria-current") === "page");
    expect(pinned).toBeDefined();
  });

  it("keeps the MoreSheet free of the retired Übersicht entry", () => {
    renderShell("backlog");
    const bottomBar = screen.getByRole("navigation", { name: "Navigation" });
    within(bottomBar).getByRole("button", { name: "Mehr" }).click();
    expect(screen.queryByText("Übersicht")).toBeNull();
  });

  it("shows the masthead route label for a view without its own masthead", () => {
    renderShell("crons");
    const masthead = screen.getByTestId("control-masthead");
    expect(masthead.textContent).toContain("Crons");
  });

  it("suppresses the masthead for a view with its own masthead (fleet)", () => {
    renderShell("fleet");
    expect(screen.queryByTestId("control-masthead")).toBeNull();
  });

  it("mounts NotificationBridge exactly once for a view with its own masthead", () => {
    renderShell("fleet");
    expect(notificationBridgeSpy).toHaveBeenCalledTimes(1);
  });

  it("mounts NotificationBridge exactly once for a view with the generic masthead", () => {
    renderShell("crons");
    expect(notificationBridgeSpy).toHaveBeenCalledTimes(1);
  });
});
