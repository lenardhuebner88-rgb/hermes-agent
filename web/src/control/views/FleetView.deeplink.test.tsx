// @vitest-environment jsdom

/**
 * FleetView deep-link contract: ?board=<slug>&status=<TaskStatus>
 * One-shot consume (AgentTerminalsView idiom) after board catalog load.
 */
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FleetView } from "./FleetView";
import { FLEET_BOARD_STORAGE_KEY } from "../lib/multiBoard";
import type { BoardResponse, BoardTask } from "../lib/types";

const hooks = vi.hoisted(() => ({
  useHermesWorkers: vi.fn(),
  useAllBoardWorkers: vi.fn(),
  useBoardCatalog: vi.fn(),
  useBoard: vi.fn(),
  usePlanSpecs: vi.fn(),
  useHermesRunsCosts: vi.fn(),
  useHermesRunsDaily: vi.fn(),
  useHermesReliability: vi.fn(),
  useSystemHealth: vi.fn(),
  usePressureStatus: vi.fn(),
  useLanesCatalog: vi.fn(),
  useAccountUsage: vi.fn(),
  usePlanSpecDetail: vi.fn(),
  useKanbanDecisionQueue: vi.fn(),
  useReleaseStatus: vi.fn(),
  useReleaseMode: vi.fn(),
  useReleaseGateExecute: vi.fn(),
  useReleaseModeWrite: vi.fn(),
  useReleaseConcurrencyWrite: vi.fn(),
  useChainGraph: vi.fn(),
  useHermesChainCosts: vi.fn(),
  useHermesReviewVerdicts: vi.fn(),
  useRunLiveEvents: vi.fn(),
  useWorkerLifecycle: vi.fn(),
  useWorkerActivity: vi.fn(),
  useHermesRecentResults: vi.fn(),
  useCronObservability: vi.fn(),
}));

vi.mock("../hooks/chainFlow", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useChainGraph: hooks.useChainGraph,
}));
vi.mock("../hooks/costsUsage", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useHermesRunsCosts: hooks.useHermesRunsCosts,
  useAccountUsage: hooks.useAccountUsage,
  useHermesChainCosts: hooks.useHermesChainCosts,
}));
vi.mock("../hooks/cron", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useCronObservability: hooks.useCronObservability,
}));
vi.mock("../hooks/decisionInbox", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useKanbanDecisionQueue: hooks.useKanbanDecisionQueue,
}));
vi.mock("../hooks/planSpecsLanes", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  usePlanSpecs: hooks.usePlanSpecs,
  useLanesCatalog: hooks.useLanesCatalog,
  usePlanSpecDetail: hooks.usePlanSpecDetail,
}));
vi.mock("../hooks/reviewVerdicts", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useHermesReviewVerdicts: hooks.useHermesReviewVerdicts,
}));
vi.mock("../hooks/runsDigestRollup", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useHermesRunsDaily: hooks.useHermesRunsDaily,
  useHermesReliability: hooks.useHermesReliability,
  useHermesRecentResults: hooks.useHermesRecentResults,
}));
vi.mock("../hooks/systemReleaseHealth", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useSystemHealth: hooks.useSystemHealth,
  usePressureStatus: hooks.usePressureStatus,
  useReleaseStatus: hooks.useReleaseStatus,
  useReleaseMode: hooks.useReleaseMode,
  useReleaseModeWrite: hooks.useReleaseModeWrite,
  useReleaseConcurrencyWrite: hooks.useReleaseConcurrencyWrite,
}));
vi.mock("../hooks/taskActions", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useReleaseGateExecute: hooks.useReleaseGateExecute,
  useWorkerLifecycle: hooks.useWorkerLifecycle,
}));
vi.mock("../hooks/workersBoard", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useHermesWorkers: hooks.useHermesWorkers,
  useAllBoardWorkers: hooks.useAllBoardWorkers,
  useBoardCatalog: hooks.useBoardCatalog,
  useBoard: hooks.useBoard,
  useRunLiveEvents: hooks.useRunLiveEvents,
  useWorkerActivity: hooks.useWorkerActivity,
}));

const api = vi.hoisted(() => ({ fetchJSON: vi.fn() }));
vi.mock("@/lib/api", () => api);

const reload = vi.fn();

function setLgViewport(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
}

function boardTask(id: string, title: string, status: BoardTask["status"]): BoardTask {
  return {
    id,
    title,
    status,
    assignee: null,
    priority: 0,
    created_at: 1,
    started_at: status === "running" ? 2 : null,
    completed_at: status === "done" ? 3 : null,
    branch_name: null,
    latest_summary: null,
    link_counts: { parents: 0, children: 0 },
    comment_count: 0,
    progress: null,
    age: null,
    tenant: "orchestrator",
    root_id: null,
    epic_id: null,
  };
}

const BLOCKED_TASK = boardTask("t_blocked", "Blocked canary", "blocked");
const RUNNING_TASK = boardTask("t_running", "Running canary", "running");

const HEALTH_BOARD: BoardResponse = {
  columns: [
    { name: "blocked", tasks: [BLOCKED_TASK] },
    { name: "running", tasks: [RUNNING_TASK] },
  ],
  tenants: ["orchestrator"],
  assignees: [],
  latest_event_id: 2,
  source_errors: [],
  now: 10,
};

const DEFAULT_BOARD: BoardResponse = {
  columns: [{ name: "running", tasks: [boardTask("t_default", "Default running", "running")] }],
  tenants: ["orchestrator"],
  assignees: [],
  latest_event_id: 1,
  source_errors: [],
  now: 10,
};

const CATALOG = {
  current: "default",
  boards: [
    { slug: "default", name: "Hermes Agent", archived: false, is_current: true, project_bound: true },
    { slug: "health-track", name: "Health Track", archived: false, is_current: false, project_bound: true },
  ],
};

function setHookDefaults() {
  hooks.useHermesWorkers.mockReturnValue({ data: { workers: [] }, loading: false, error: null, reload });
  hooks.useAllBoardWorkers.mockReturnValue({ data: { workers: [] }, loading: false, error: null, reload });
  hooks.useBoardCatalog.mockReturnValue({ data: CATALOG, loading: false, error: null, reload });
  hooks.useBoard.mockImplementation((slug?: string | null) => ({
    data: slug === "health-track" ? HEALTH_BOARD : DEFAULT_BOARD,
    loading: false,
    error: null,
    reload,
  }));
  hooks.usePlanSpecs.mockReturnValue({ data: { planspecs: [], count: 0 }, loading: false, error: null, reload });
  hooks.useHermesRunsCosts.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useHermesRunsDaily.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useHermesReliability.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useSystemHealth.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.usePressureStatus.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useLanesCatalog.mockReturnValue({ data: { lanes: [] }, loading: false, error: null, reload });
  hooks.useAccountUsage.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useKanbanDecisionQueue.mockReturnValue({ data: { decisions: [], count: 0, checked_at: 0 }, loading: false, error: null, reload });
  hooks.useReleaseStatus.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useReleaseMode.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useReleaseGateExecute.mockReturnValue({ busyId: null, activatingIds: {}, doneIds: {}, errorById: {}, run: vi.fn() });
  hooks.useReleaseModeWrite.mockReturnValue({ run: vi.fn(), busy: false, error: null });
  hooks.useReleaseConcurrencyWrite.mockReturnValue({ run: vi.fn(), busy: false, error: null });
  hooks.useChainGraph.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useHermesChainCosts.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useHermesReviewVerdicts.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useRunLiveEvents.mockReturnValue({ events: [], loading: false, error: null });
  hooks.useWorkerLifecycle.mockReturnValue({
    busyId: null,
    errorById: {},
    run: vi.fn(),
    terminate: vi.fn(),
    clearError: vi.fn(),
  });
  hooks.useWorkerActivity.mockReturnValue({ data: { events: [] }, loading: false, error: null, reload });
  hooks.useHermesRecentResults.mockReturnValue({ data: { results: [] }, loading: false, error: null, lastUpdated: null, reload });
  hooks.useCronObservability.mockReturnValue({ data: { jobs: [] }, loading: false, error: null, lastUpdated: null, reload });
  hooks.usePlanSpecDetail.mockReturnValue({ data: null, loading: false, error: null });
}

function renderFleet(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <FleetView />
    </MemoryRouter>,
  );
}

describe("FleetView deep-link ?board=&status=", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.fetchJSON.mockResolvedValue({
      columns: [{ name: "done", tasks: [] }],
      tenants: [],
      assignees: [],
      latest_event_id: 0,
      source_errors: [],
      now: 10,
      done_page: { next_cursor: null, total_count: 0, limit: 0 },
    });
    setLgViewport(false);
    setHookDefaults();
  });

  function selectValue(name: string): string {
    return (screen.getByRole("combobox", { name }) as HTMLSelectElement).value;
  }

  it("selects board + status filter after catalog load and strips params (one-shot)", async () => {
    const { rerender } = render(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(selectValue("Board auswählen")).toBe("health-track");
    });
    expect(window.localStorage.getItem(FLEET_BOARD_STORAGE_KEY)).toBe("health-track");

    expect(selectValue("Nach Status filtern")).toBe("blocked");
    expect(screen.getByText("Blocked canary")).toBeTruthy();
    expect(screen.getByLabelText("Aktive Board-Filter")).toBeTruthy();
    expect(screen.getByText("Status: Blockiert")).toBeTruthy();

    // One-shot: re-render same tree after catalog identity change — no re-consume / no reset.
    hooks.useBoardCatalog.mockReturnValue({
      data: { ...CATALOG },
      loading: false,
      error: null,
      reload,
    });
    rerender(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );
    expect(selectValue("Board auswählen")).toBe("health-track");
    expect(selectValue("Nach Status filtern")).toBe("blocked");
    // Params were consumed on first pass; a second effect pass must not wipe selection
    // even if the URL still carried them in initialEntries (ref guard).
  });

  it("ignores invalid board and invalid status; defaults remain", async () => {
    renderFleet("/control/fleet?board=does-not-exist&status=not-a-status");

    // Board subtab still activates (params were present), but selection stays default.
    await waitFor(() => {
      expect(screen.getByRole("combobox", { name: "Board auswählen" })).toBeTruthy();
    });
    expect(selectValue("Board auswählen")).toBe("");
    expect(window.localStorage.getItem(FLEET_BOARD_STORAGE_KEY)).toBeNull();
    expect(selectValue("Nach Status filtern")).toBe("all");
    // Default board content, not health-track blocked canary.
    expect(screen.getByText("Default running")).toBeTruthy();
    expect(screen.queryByText("Blocked canary")).toBeNull();
  });

  it("survives a transient catalog failure: params stay unconsumed until data arrives", async () => {
    // Erster Poll scheitert: usePolling liefert loading=false + data=null + error.
    // Der Deep-Link darf dann NICHT konsumiert werden (sonst still verloren).
    const catalogBox = { data: null as typeof CATALOG | null, error: "boom" as string | null };
    hooks.useBoardCatalog.mockImplementation(() => ({
      data: catalogBox.data,
      loading: false,
      error: catalogBox.error,
      reload,
    }));

    const { rerender } = render(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );

    // Nichts konsumiert: keine Board-Auswahl persistiert, kein Subtab-Zwang.
    expect(window.localStorage.getItem(FLEET_BOARD_STORAGE_KEY)).toBeNull();
    expect(screen.queryByRole("combobox", { name: "Board auswählen" })).toBeNull();

    // Nächster Poll liefert den Katalog → Deep-Link wird jetzt angewendet.
    catalogBox.data = CATALOG;
    catalogBox.error = null;
    rerender(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(selectValue("Board auswählen")).toBe("health-track");
    });
    expect(selectValue("Nach Status filtern")).toBe("blocked");
  });

  it("does not re-apply the deep-link status after leaving and re-entering the Board subtab", async () => {
    const { getByRole } = renderFleet("/control/fleet?board=health-track&status=blocked");

    await waitFor(() => {
      expect(selectValue("Nach Status filtern")).toBe("blocked");
    });

    // Operator verlässt den Board-Subtab und kommt zurück → der Deep-Link-Status
    // ist verbraucht; der Filter startet wie immer auf "all" statt still auf
    // "blocked" zurückzuspringen.
    getByRole("button", { name: "Subtab Heute" }).click();
    await waitFor(() => {
      expect(screen.queryByRole("combobox", { name: "Nach Status filtern" })).toBeNull();
    });
    getByRole("button", { name: "Subtab Board" }).click();
    await waitFor(() => {
      expect(selectValue("Nach Status filtern")).toBe("all");
    });
    // Board-Auswahl bleibt erhalten (localStorage-Semantik des Switchers).
    expect(selectValue("Board auswählen")).toBe("health-track");
  });

  it("waits for catalog load before consuming board param", async () => {
    const catalogBox = { data: null as typeof CATALOG | null, loading: true };
    hooks.useBoardCatalog.mockImplementation(() => ({
      data: catalogBox.data,
      loading: catalogBox.loading,
      error: null,
      reload,
    }));

    const { rerender } = render(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );

    // Still on default Heute while catalog loads — Board switcher not present yet.
    expect(screen.queryByRole("combobox", { name: "Board auswählen" })).toBeNull();
    expect(window.localStorage.getItem(FLEET_BOARD_STORAGE_KEY)).toBeNull();

    catalogBox.data = CATALOG;
    catalogBox.loading = false;
    rerender(
      <MemoryRouter initialEntries={["/control/fleet?board=health-track&status=blocked"]}>
        <FleetView />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(selectValue("Board auswählen")).toBe("health-track");
    });
    expect(selectValue("Nach Status filtern")).toBe("blocked");
  });

  it("opens the task detail drawer for ?task=<id> (kanban risk deep-link → Fleet focus)", async () => {
    // Kanban-Attention aus dem Postfach linkt auf /control/fleet?task=<id>. Der
    // Param darf NICHT inert bleiben: FleetView öffnet den Node-Detail-Drawer für
    // den referenzierten Task (mobil, setLgViewport(false) im beforeEach).
    renderFleet("/control/fleet?task=t_blocked");

    await waitFor(() => {
      expect(screen.getByText("Task t_blocked")).toBeTruthy();
    });
    // Der Task-Fokus überlebt den Mount-Reset (Board-Wechsel-Cleanup darf den
    // Deep-Link-Drawer nicht sofort wieder schließen).
    await waitFor(() => {}, { timeout: 50 });
    expect(screen.getByText("Task t_blocked")).toBeTruthy();
  });

  it("does not open any drawer without a ?task= param", async () => {
    renderFleet("/control/fleet");
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Subtab Heute" })).toBeTruthy();
    });
    expect(screen.queryByText("Task t_blocked")).toBeNull();
  });
});

