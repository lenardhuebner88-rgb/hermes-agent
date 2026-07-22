// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import ControlPage from "./ControlPage";

vi.mock("./hooks/useDensity", () => ({
  useDensity: () => ({ density: "compact" }),
}));
vi.mock("./hooks/decisionInbox", () => ({
  useDecisionInbox: () => ({
    items: [],
    summary: { total: 0, critical: 0, warnings: 0 },
    snapshot: { interventions: [] },
    worstTone: "emerald",
    loading: false,
    sourceErrors: [],
  }),
}));
vi.mock("./hooks/costsUsage", () => ({
  useHermesRunsCosts: () => ({ data: null }),
}));
vi.mock("./hooks/workersBoard", () => ({
  useHermesWorkers: () => ({ data: { workers: [] } }),
}));
vi.mock("./hooks/libraryKnowledge", () => ({ useLibraryUnread: () => 0 }));
vi.mock("./hooks/proposalsDeepAudit", () => ({
  useProposals: () => ({
    openSkillProposals: [],
    proposals: [],
    lastUpdated: null,
    generate: vi.fn(),
    applyAll: vi.fn(),
  }),
}));
vi.mock("./hooks/strategist", () => ({ useStrategistCount: () => ({ data: null }) }));
vi.mock("./hooks/systemReleaseHealth", () => ({
  HEALTH_POLL_INTERVAL_MS: 15_000,
  useSystemHealth: () => ({
    data: null,
    error: null,
    isStale: false,
    lastUpdated: null,
  }),
}));
vi.mock("./hooks/useLiveEvents", () => ({
  useLiveEvents: vi.fn(),
  useLiveStatus: () => "off",
}));
vi.mock("./lib/clock", () => ({ useClientNowSeconds: () => 1_783_025_500 }));

vi.mock("./components/NotificationBridge", () => ({
  NotificationBridge: () => <div data-testid="notification-bridge" />,
}));
vi.mock("./components/CommandPalette", () => ({
  CommandPalette: () => <div data-testid="command-palette" />,
}));
vi.mock("./components/OfflineStaleBanner", () => ({
  OfflineStaleBanner: () => <div data-testid="offline-stale-banner" />,
}));
vi.mock("./components/primitives", () => ({
  RouteTransition: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock("./views/CommandHome", () => ({
  CommandHome: () => <div data-testid="command-home" />,
}));
vi.mock("./views/start/StartMissionControl", () => ({
  StartMissionControl: () => <div data-testid="start-mission-control" />,
}));
vi.mock("./views/CronView", () => ({
  CronView: () => <div data-testid="cron-view" />,
}));
vi.mock("./views/ProjekteView", () => ({
  ProjekteView: () => <div data-testid="projekte-klassisch-view" />,
}));
vi.mock("./jarvis/JarvisShellView", () => ({
  JarvisShellView: () => <div data-testid="jarvis-shell-view" />,
}));

function renderControl(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/control/*" element={<ControlPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function expectSharedOverlays() {
  expect(screen.getByTestId("offline-stale-banner")).toBeTruthy();
  expect(screen.getByTestId("command-palette")).toBeTruthy();
}

function expectNoControlShell() {
  expect(screen.queryByRole("navigation", { name: "Hauptnavigation" })).toBeNull();
  expect(screen.queryByTestId("control-masthead")).toBeNull();
  expect(screen.queryByRole("navigation", { name: "Navigation" })).toBeNull();
}

function expectControlShell() {
  expect(screen.getByRole("navigation", { name: "Hauptnavigation" })).toBeTruthy();
  expect(screen.getByTestId("control-masthead")).toBeTruthy();
  expect(screen.getByRole("navigation", { name: "Navigation" })).toBeTruthy();
}

describe("ControlPage chromeless Jarvis route", () => {
  afterEach(cleanup);

  it("uses A4.2 for Start and keeps the decision inbox at /control/inbox", async () => {
    const start = renderControl("/control");
    expect(await screen.findByTestId("start-mission-control")).toBeTruthy();
    expect(screen.queryByTestId("command-home")).toBeNull();
    start.unmount();

    renderControl("/control/inbox");
    expect(await screen.findByTestId("command-home")).toBeTruthy();
  });

  for (const path of ["/control/projekte", "/control/projekte/"]) {
    it(`renders ${path} without rail, masthead, or bottom bar`, async () => {
      renderControl(path);

      expect(await screen.findByTestId("jarvis-shell-view")).toBeTruthy();
      expectNoControlShell();
      expectSharedOverlays();
    });
  }

  it("keeps the full shell on /control/projekte-klassisch", async () => {
    renderControl("/control/projekte-klassisch");

    expect(await screen.findByTestId("projekte-klassisch-view")).toBeTruthy();
    expectControlShell();
    expectSharedOverlays();
  });

  it("keeps the full shell on another Control tab", async () => {
    renderControl("/control/crons");

    expect(await screen.findByTestId("cron-view")).toBeTruthy();
    expectControlShell();
    expectSharedOverlays();
  });
});
