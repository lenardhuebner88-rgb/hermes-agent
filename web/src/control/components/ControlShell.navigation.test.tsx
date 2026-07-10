// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import type { ComponentProps, ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ControlShell, type ControlTab } from "./ControlShell";
import type { DecisionInboxData } from "../hooks/useControlData";
import type { SystemHealthResponse } from "../lib/types";

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

// Mirrors ControlPage's `tabPath` for the tabs these tests use — the
// masthead's `hasOwnMasthead` fork is pathname-driven (P3 fix), not
// active-tab-driven, so `path` must default to the real route ControlPage
// would put that tab at for the existing per-tab assertions below to keep
// exercising the exact same fork the live app hits. Tests that need to prove
// the fork independently of the active tab (e.g. the /control/issues P3 case)
// override `path` explicitly.
const TEST_TAB_PATH: Partial<Record<ControlTab, string>> = {
  fleet: "/control/fleet",
  inbox: "/control",
  statistik: "/control/statistik",
  backlog: "/control/backlog",
  crons: "/control/crons",
  loops: "/control/loops",
};

function renderShell(active: ControlTab, options: { path?: string; pulse?: ComponentProps<typeof ControlShell>["pulse"]; health?: ComponentProps<typeof ControlShell>["health"] } = {}) {
  return render(
    <MemoryRouter initialEntries={[options.path ?? TEST_TAB_PATH[active] ?? "/control"]}>
      <ControlShell {...baseProps} active={active} pulse={options.pulse} health={options.health ?? baseProps.health}>
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

  it("pulls the rail up by the residual App-wrapper top padding so its sticky h-dvh bottom cluster isn't clipped pre-scroll (W2-c)", () => {
    renderShell("backlog");
    const rail = screen.getByRole("navigation", { name: "Hauptnavigation" });
    // App.tsx wraps [data-control] in pt-2 sm:pt-4 lg:pt-6; .hc-page only
    // cancels a flat -0.5rem — the rail cancels the rest itself so its
    // h-dvh box starts flush at the true viewport top instead of 0.5-1rem
    // below it (residual = sm:pt-4(1rem) - 0.5rem = 0.5rem, lg:pt-6(1.5rem)
    // - 0.5rem = 1rem).
    expect(rail.className).toContain("sm:-mt-2");
    expect(rail.className).toContain("lg:-mt-4");
    expect(rail.className).toContain("h-dvh");
  });

  it("gives the sticky rail its own z-40 stacking layer so content-rich pages can't occlude the flyout", () => {
    renderShell("backlog");
    const rail = screen.getByRole("navigation", { name: "Hauptnavigation" });
    expect(rail.className).toContain("z-40");
  });

  it("keeps the rail 'Mehr' flyout panel scrollable and viewport-clamped", () => {
    renderShell("backlog");
    const panel = screen.getByTestId("rail-more-flyout");
    expect(panel.className).toContain("overflow-y-auto");
    expect(panel.className).toContain("max-h-");
  });

  it("drives the rail 'Mehr' flyout visibility only from the open state, not the legacy focus-within hover fallback", () => {
    renderShell("backlog");
    const panel = screen.getByTestId("rail-more-flyout");
    expect(panel.className).not.toContain("group-focus-within");
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

  it("feeds the generic masthead's Puls-Leiste instruments when `pulse` is given (W2-b)", () => {
    const healthyHealth = {
      data: {
        schema: "1",
        checked_at: 1783025490,
        overall: "healthy",
        subsystems: {
          gateway: { status: "healthy", detail: "", error: null },
          autoresearch: { status: "healthy", detail: "", error: null },
          kanban_db: { status: "healthy", detail: "", error: null },
          kanban_dispatcher: { status: "healthy", detail: "", error: null },
        },
      } as unknown as SystemHealthResponse,
      error: null,
      isStale: false,
      lastUpdated: 1783025490,
    };
    renderShell("crons", { pulse: { workers: 3, fragen: 2, fragenTone: "amber", kostenUsd: 4.1 }, health: healthyHealth });
    const masthead = screen.getByTestId("control-masthead");
    expect(within(masthead).getByText("3")).toBeTruthy();
    expect(within(masthead).getByText("2")).toBeTruthy();
    expect(within(masthead).getByText("$4,10")).toBeTruthy();
    // "gesund" also appears in the legacy StatusDots (Hermes/Dashboard) that
    // shares the masthead's right-side slot — scope to the Gateway instrument
    // specifically via its label's sibling value.
    const gatewayValue = within(masthead).getByText("Gateway").nextElementSibling;
    expect(gatewayValue?.textContent).toBe("gesund");
  });

  it("demotes the legacy StatusDots pill to lg: once the Puls-Leiste carries real instruments, since its Gateway LED already covers it (W2-c)", () => {
    renderShell("crons", { pulse: { workers: 3, fragen: 0, kostenUsd: 4.1 } });
    const statusDots = screen.getByTestId("status-dots");
    expect(statusDots.className).toContain("lg:flex");
    expect(statusDots.className).not.toContain("md:flex");
  });

  it("keeps the legacy StatusDots pill at md: when no `pulse` is given", () => {
    renderShell("crons");
    const statusDots = screen.getByTestId("status-dots");
    expect(statusDots.className).toContain("md:flex");
    expect(statusDots.className).not.toContain("lg:flex");
  });

  it("shows the masthead on /control/issues even though the tab economy pins it to statistik (P3 fix)", () => {
    // ControlPage.activeFromPath maps /control/issues -> active="statistik" (same
    // tab), but IssuesView has no own masthead — the old active-only check
    // suppressed it there too. The fork must key off the real pathname.
    renderShell("statistik", { path: "/control/issues" });
    expect(screen.getByTestId("control-masthead")).toBeTruthy();
  });

  it("still suppresses the masthead for the real Statistik route (P3 regression guard)", () => {
    renderShell("statistik", { path: "/control/statistik" });
    expect(screen.queryByTestId("control-masthead")).toBeNull();
  });

  it("suppresses the masthead for the legacy /control/inbox route (B1 fix)", () => {
    renderShell("inbox", { path: "/control/inbox" });
    expect(screen.queryByTestId("control-masthead")).toBeNull();
  });

  it("suppresses the masthead for /control/statistik/ with a trailing slash (B1 fix)", () => {
    renderShell("statistik", { path: "/control/statistik/" });
    expect(screen.queryByTestId("control-masthead")).toBeNull();
  });
});
