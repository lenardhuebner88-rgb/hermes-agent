// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FleetView } from "./FleetView";
import type { BoardResponse, BoardTask, PlanSpecRecord, Worker } from "../lib/types";
import { de } from "../i18n/de";

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
  useChainGraph: vi.fn(),
  useHermesChainCosts: vi.fn(),
  useHermesReviewVerdicts: vi.fn(),
  useRunLiveEvents: vi.fn(),
  useWorkerLifecycle: vi.fn(),
  useWorkerActivity: vi.fn(),
}));

vi.mock("../hooks/useControlData", () => hooks);

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

const planSpec = {
  path: "/home/piet/vault/03-Agents/Hermes/plans/alpha.md",
  agent: "Hermes",
  filename: "alpha.md",
  topic: "Alpha Volltext Plan",
  status: "pending",
  freigabe: "operator",
  live_test_depth: "smoke",
  binding: false,
  subtask_count: 3,
  valid: true,
  open: true,
  closed_reason: null,
  kanban_root_task_id: "t_alpha",
  kanban_root_status: "running",
  kanban_state: "queued",
  kanban_child_done: 1,
  kanban_child_total: 3,
  kanban_child_blocked: 0,
  kanban_child_running: 1,
  kanban_ingested_at: null,
  ingest_disposition: "ok",
  ingest_would_block: false,
  ingest_findings: [],
  errors: [],
} satisfies PlanSpecRecord;

const signedCompletePlanSpec = {
  ...planSpec,
  path: "/home/piet/vault/03-Agents/Hermes/plans/signed-complete.md",
  filename: "signed-complete.md",
  topic: "Signed Complete Plan",
  freigabe: "complete",
  live_test_depth: "ui-real",
  kanban_root_task_id: "t_signed_root",
  kanban_root_status: "scheduled",
  kanban_state: "queued",
  kanban_child_done: 0,
  kanban_child_total: 2,
  kanban_child_blocked: 0,
  kanban_child_running: 0,
} satisfies PlanSpecRecord;

function boardTask(id: string, title: string, status: BoardTask["status"], rootId: string | null = null): BoardTask {
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
    link_counts: { parents: rootId ? 1 : 0, children: 0 },
    comment_count: 0,
    progress: null,
    age: null,
    tenant: "orchestrator",
    root_id: rootId,
    epic_id: null,
  };
}

const DEFAULT_ACTIVE_ROOT = boardTask("t_default_active", "Default aktive Kette", "scheduled");
const DEFAULT_ACTIVE_CHILD = boardTask("t_default_active_child", "Default aktiver Task", "running", DEFAULT_ACTIVE_ROOT.id);
const DEFAULT_OLD_ROOT = boardTask("t_default_old", "Default alte Kette", "scheduled");
const DEFAULT_OLD_CHILD = boardTask("t_default_old_child", "Default alter Worker-Task", "ready", DEFAULT_OLD_ROOT.id);
const HEALTH_ROOT = boardTask("t_health_root", "Health aktive Kette", "scheduled");
const HEALTH_CHILD = boardTask("t_health_child", "Health aktiver Task", "running", HEALTH_ROOT.id);

const DEFAULT_BOARD: BoardResponse = {
  columns: [{ name: "running", tasks: [DEFAULT_ACTIVE_CHILD] }, { name: "scheduled", tasks: [DEFAULT_ACTIVE_ROOT, DEFAULT_OLD_ROOT, DEFAULT_OLD_CHILD] }],
  tenants: ["orchestrator"],
  assignees: [],
  latest_event_id: 1,
  source_errors: [],
  now: 10,
};

const HEALTH_BOARD: BoardResponse = {
  columns: [{ name: "running", tasks: [HEALTH_CHILD] }, { name: "scheduled", tasks: [HEALTH_ROOT] }],
  tenants: ["orchestrator"],
  assignees: [],
  latest_event_id: 2,
  source_errors: [],
  now: 10,
};

const DEFAULT_WORKER: Worker = {
  run_id: "42",
  task_id: DEFAULT_OLD_CHILD.id,
  task_title: DEFAULT_OLD_CHILD.title,
  task_status: "running",
  task_assignee: "coder",
  profile: "coder",
  worker_pid: 4242,
  started_at: 1,
  claim_lock: "lock-42",
  claim_expires: 100,
  last_heartbeat_at: 9,
  max_runtime_seconds: 1800,
  run_status: "running",
  run_outcome: null,
  block_reason: null,
  inspect: null,
  last_heartbeat_note: null,
  last_heartbeat_note_at: null,
  eta_p50_seconds: null,
  eta_p90_seconds: null,
  step_key: null,
  model_override: null,
  effective_model: null,
  input_tokens: null,
  output_tokens: null,
  token_status: "no_live_sample",
  token_status_reason: null,
  run_progress: null,
  heartbeat_ticks: [],
};

function setHookDefaults() {
  hooks.useHermesWorkers.mockReturnValue({ data: { workers: [] }, loading: false, error: null, reload });
  hooks.useAllBoardWorkers.mockReturnValue({ data: { workers: [] }, loading: false, error: null, reload });
  hooks.useBoardCatalog.mockReturnValue({ data: null, loading: false, error: null, reload });
  hooks.useBoard.mockReturnValue({ data: { columns: [] }, loading: false, error: null, reload });
  hooks.usePlanSpecs.mockReturnValue({ data: { planspecs: [planSpec], count: 1 }, loading: false, error: null, reload });
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
  hooks.usePlanSpecDetail.mockImplementation((path: string | null) => {
    return {
      data: path
        ? {
            path,
            filename: "alpha.md",
            topic: "Alpha Volltext Plan",
            goal: "PlanSpec-Detail aus GET /planspecs/detail?path=.",
            freigabe: "operator",
            live_test_depth: "smoke",
            acceptance_criteria: [],
            anti_scope: [],
            subtasks: [],
          }
        : null,
      loading: false,
      error: null,
    };
  });
}

describe("FleetView PlanSpec detail drawer", () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
    api.fetchJSON.mockResolvedValue({ ok: true });
    setLgViewport(false);
    setHookDefaults();
  });

  function renderFleetView() {
    return render(
      <MemoryRouter>
        <FleetView />
      </MemoryRouter>,
    );
  }

  it("opens the PlanSpec full-text drawer from Heute cards", () => {
    const { container } = renderFleetView();

    const heutePlanCard = container.querySelector<HTMLButtonElement>("button.fleet-ps");
    expect(heutePlanCard).toBeTruthy();
    fireEvent.click(heutePlanCard!);

    expect(hooks.usePlanSpecDetail).toHaveBeenLastCalledWith(planSpec.path);
    const drawer = screen.getByRole("dialog", { name: "PlanSpec Details" });
    expect(drawer).toBeTruthy();
    expect(within(drawer).getByText("PlanSpec-Detail aus GET /planspecs/detail?path=."));
  });

  it("surfaces retained board data as stale after a failed refresh", () => {
    hooks.useBoard.mockReturnValue({
      data: DEFAULT_BOARD,
      loading: false,
      error: "500: audit injected board failure",
      errorObj: { code: "500", message: "500: audit injected board failure" },
      isStale: true,
      lastUpdated: Math.floor(Date.now() / 1000) - 10,
      reload,
    });

    renderFleetView();
    fireEvent.click(screen.getByRole("button", { name: "Subtab Board" }));

    expect(screen.getByText(/Daten von vor/)).toBeTruthy();
    expect(screen.getByText("Default aktive Kette")).toBeTruthy();
  });

  it("mounts PlanSpec detail beside the list without an overlay at lg", () => {
    setLgViewport(true);
    const { container } = renderFleetView();

    const heutePlanCard = container.querySelector<HTMLButtonElement>("button.fleet-ps");
    expect(heutePlanCard).toBeTruthy();
    fireEvent.click(heutePlanCard!);

    expect(screen.queryByRole("dialog", { name: "PlanSpec Details" })).toBeNull();
    const detail = screen.getByRole("region", { name: "PlanSpec-Details" });
    expect(within(detail).getByText("PlanSpec-Detail aus GET /planspecs/detail?path=.")).toBeTruthy();
  });

  it("collapses the idle detail pane at lg when no chain exists", () => {
    setLgViewport(true);
    const { container } = renderFleetView();

    expect(container.querySelector('[data-layout="single"]')).toBeTruthy();
    expect(screen.queryByRole("region", { name: "Aktive Kette" })).toBeNull();
    expect(screen.queryByText("Keine aktiven Ketten")).toBeNull();
  });

  it("announces the pending operator callout politely", () => {
    const { container } = renderFleetView();

    const pendingCallout = container.querySelector<HTMLButtonElement>('button[aria-live="polite"]');
    expect(pendingCallout).toBeTruthy();
    expect(pendingCallout?.getAttribute("aria-label")).toBeTruthy();
  });

  it("opens the PlanSpec full-text drawer from Plan tab approval cards", () => {
    renderFleetView();

    fireEvent.click(screen.getByRole("button", { name: "Subtab Plan" }));
    fireEvent.click(screen.getByRole("button", { name: "PlanSpec-Volltext öffnen" }));

    expect(hooks.usePlanSpecDetail).toHaveBeenLastCalledWith(planSpec.path);
    const drawer = screen.getByRole("dialog", { name: "PlanSpec Details" });
    expect(drawer).toBeTruthy();
    expect(within(drawer).getByText("PlanSpec-Detail aus GET /planspecs/detail?path=."));
  });

  it("shows signed complete parked PlanSpecs and starts the chain via flow-release", async () => {
    hooks.usePlanSpecs.mockReturnValue({ data: { planspecs: [signedCompletePlanSpec], count: 1 }, loading: false, error: null, reload });
    hooks.usePlanSpecDetail.mockImplementation((path: string | null) => ({
      data: path
        ? {
            path,
            filename: "signed-complete.md",
            topic: "Signed Complete Plan",
            goal: "Echte PlanSpec-Payload: freigabe complete, Root scheduled.",
            freigabe: "complete",
            live_test_depth: "ui-real",
            acceptance_criteria: [],
            anti_scope: [],
            subtasks: [],
          }
        : null,
      loading: false,
      error: null,
    }));

    renderFleetView();
    fireEvent.click(screen.getByRole("button", { name: "Subtab Plan" }));

    expect(screen.getByText("Signed Complete Plan")).toBeTruthy();
    expect(screen.getByText("signiert · geparkt")).toBeTruthy();
    expect(screen.getByTestId("signed-chain-start-card")).toBeTruthy();

    const startButton = screen.getByRole("button", { name: "Kette starten" });
    fireEvent.click(startButton);
    fireEvent.click(screen.getByRole("button", { name: "Start bestätigen" }));

    await waitFor(() => expect(api.fetchJSON).toHaveBeenCalledWith(
      "/api/plugins/kanban/tasks/t_signed_root/flow-release",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ release_level: "live" }),
      }),
    ));
  });

  it("opens the cost drawer from the Heute cost KPI", () => {
    hooks.useHermesRunsCosts.mockReturnValue({
      data: {
        days: 7,
        now: 1783200000,
        today: {
          runs: 3,
          cost_usd: 0,
          cost_usd_equivalent: 4.2,
          api_equivalent_usd: 4.2,
          actual_cost_usd: 0,
          billing_neuralwatt_kwh: null,
          billing_neuralwatt_charged_kwh: null,
          billing_neuralwatt_usd_per_kwh: null,
          billing_neuralwatt_cost_usd: null,
          input_tokens: 1200,
          output_tokens: 800,
        },
        window: {
          runs: 9,
          cost_usd: 1.5,
          cost_usd_equivalent: 12.75,
          api_equivalent_usd: 12.75,
          actual_cost_usd: 1.5,
          billing_neuralwatt_kwh: null,
          billing_neuralwatt_charged_kwh: null,
          billing_neuralwatt_usd_per_kwh: null,
          billing_neuralwatt_cost_usd: null,
          input_tokens: 5000,
          output_tokens: 3000,
        },
        profiles: [
          {
            profile: "coder",
            subscription: "chatgpt",
            runs: 6,
            cost_usd: 0,
            cost_usd_equivalent: 7.25,
            api_equivalent_usd: 7.25,
            actual_cost_usd: 0,
            billing_neuralwatt_kwh: null,
            billing_neuralwatt_charged_kwh: null,
            billing_neuralwatt_usd_per_kwh: null,
            billing_neuralwatt_cost_usd: null,
            input_tokens: 3000,
            output_tokens: 2000,
          },
        ],
        review_value: [],
      },
      loading: false,
      error: null,
      reload,
    });
    hooks.useHermesRunsDaily.mockReturnValue({
      data: {
        days: 7,
        now: 1783200000,
        series: [
          { date: "2026-07-01", done_roots: 1, done_roots_by_class: { nutzer: 1, haertung: 0, meta: 0 }, done_tasks: 2, cost_usd: 0, input_tokens: 100, output_tokens: 50, runs_completed: 1, runs_failed: 0, cycle_time_p50_seconds: null },
          { date: "2026-07-02", done_roots: 2, done_roots_by_class: { nutzer: 1, haertung: 1, meta: 0 }, done_tasks: 4, cost_usd: 1.5, input_tokens: 400, output_tokens: 200, runs_completed: 2, runs_failed: 0, cycle_time_p50_seconds: null },
        ],
      },
      loading: false,
      error: null,
      reload,
    });

    renderFleetView();
    fireEvent.click(screen.getByRole("button", { name: "Kosten-Details öffnen" }));

    const costDrawer = screen.getByRole("dialog", { name: "Kosten-Details" });
    expect(within(costDrawer).getByText("Kosten heute"));
    expect(within(costDrawer).getByText("Ist: $0.00"));
    expect(within(costDrawer).getByText("≈ API: $4.20"));
    expect(within(costDrawer).getByText("coder"));
    expect(within(costDrawer).getByText("ChatGPT/Codex"));
    expect(within(costDrawer).getByText("$0 ist bei Abo-Lanes Grenzpreis, nicht kostenlos — Tokenverbrauch und API-Äquivalent zeigen den Verbrauch."));
  });

  it("resets the active chain selection on every board switch instead of reusing the old root", async () => {
    hooks.useHermesWorkers.mockReturnValue({ data: { workers: [DEFAULT_WORKER] }, loading: false, error: null, reload });
    hooks.useAllBoardWorkers.mockReturnValue({ data: { workers: [DEFAULT_WORKER] }, loading: false, error: null, reload });
    hooks.useBoardCatalog.mockReturnValue({
      data: {
        current: "default",
        boards: [
          { slug: "default", name: "Default", archived: false },
          { slug: "health-track", name: "Health Track", archived: false },
        ],
      },
      loading: false,
      error: null,
      reload,
    });
    hooks.useBoard.mockImplementation((slug?: string | null) => ({
      data: slug === "health-track" ? HEALTH_BOARD : DEFAULT_BOARD,
      loading: false,
      error: null,
      reload,
    }));

    renderFleetView();
    fireEvent.click(screen.getByRole("button", { name: "Subtab Worker" }));
    fireEvent.click(screen.getByRole("button", { name: "Worker coder öffnen" }));
    fireEvent.click(screen.getByRole("button", { name: de.fleet.drawerKetteOeffnen }));
    await waitFor(() => expect(hooks.useChainGraph).toHaveBeenCalledWith(DEFAULT_OLD_ROOT.id, null));

    fireEvent.change(screen.getByRole("combobox", { name: "Board auswählen" }), { target: { value: "health-track" } });
    await waitFor(() => expect(hooks.useChainGraph).toHaveBeenCalledWith(HEALTH_ROOT.id, "health-track"));
    expect(hooks.useChainGraph.mock.calls.some(([rootId, board]) => rootId === DEFAULT_OLD_ROOT.id && board === "health-track")).toBe(false);

    hooks.useChainGraph.mockClear();
    fireEvent.change(screen.getByRole("combobox", { name: "Board auswählen" }), { target: { value: "" } });
    await waitFor(() => expect(hooks.useChainGraph).toHaveBeenCalledWith(DEFAULT_ACTIVE_ROOT.id, null));
    expect(hooks.useChainGraph.mock.calls.some(([rootId]) => rootId === DEFAULT_OLD_ROOT.id)).toBe(false);
  });

  it("uses the selected board and its root for the Plan tab active-chain fetch", async () => {
    setLgViewport(true);
    hooks.useBoardCatalog.mockReturnValue({
      data: {
        current: "default",
        boards: [
          { slug: "default", name: "Default", archived: false },
          { slug: "health-track", name: "Health Track", archived: false },
        ],
      },
      loading: false,
      error: null,
      reload,
    });
    hooks.useBoard.mockImplementation((slug?: string | null) => ({
      data: slug === "health-track" ? HEALTH_BOARD : DEFAULT_BOARD,
      loading: false,
      error: null,
      reload,
    }));

    renderFleetView();
    fireEvent.click(screen.getByRole("button", { name: "Subtab Plan" }));
    hooks.useChainGraph.mockClear();
    fireEvent.change(screen.getByRole("combobox", { name: "Board auswählen" }), { target: { value: "health-track" } });

    await waitFor(() => expect(hooks.useChainGraph).toHaveBeenCalledWith(HEALTH_ROOT.id, "health-track"));
    expect(hooks.useChainGraph.mock.calls.some(([rootId, board]) => rootId === DEFAULT_ACTIVE_ROOT.id && board === "health-track")).toBe(false);
  });
});
