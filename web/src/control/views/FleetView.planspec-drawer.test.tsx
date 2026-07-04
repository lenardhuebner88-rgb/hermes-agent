// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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
}));

vi.mock("../hooks/useControlData", () => hooks);

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
});
