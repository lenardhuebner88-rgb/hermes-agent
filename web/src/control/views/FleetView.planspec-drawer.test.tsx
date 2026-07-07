// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FleetView } from "./FleetView";
import type { PlanSpecRecord } from "../lib/types";

const hooks = vi.hoisted(() => ({
  useHermesWorkers: vi.fn(),
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
}));

vi.mock("../hooks/useControlData", () => hooks);

const api = vi.hoisted(() => ({ fetchJSON: vi.fn() }));
vi.mock("@/lib/api", () => api);

const reload = vi.fn();

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

function setHookDefaults() {
  hooks.useHermesWorkers.mockReturnValue({ data: { workers: [] }, loading: false, error: null, reload });
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
    api.fetchJSON.mockResolvedValue({ ok: true });
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    });
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
});
