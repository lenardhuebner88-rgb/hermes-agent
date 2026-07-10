// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CostBucket, RunsCostsResponse } from "../../lib/schemas";
import type { Worker } from "../../lib/types";
import { HeuteTab } from "./HeuteTab";
import type { PlanSpecRecord } from "./shared";

vi.mock("./LaneQuickSwitch", () => ({ LaneQuickSwitch: () => null }));

afterEach(cleanup);

function worker(runId: string, profile: Worker["profile"]): Worker {
  return {
    run_id: runId,
    task_id: `task-${runId}`,
    task_title: `Task ${runId}`,
    task_status: "running",
    task_assignee: profile,
    profile,
    worker_pid: null,
    started_at: 1,
    claim_lock: "claim",
    claim_expires: 2,
    last_heartbeat_at: 1,
    max_runtime_seconds: 3600,
    run_status: "running",
    run_outcome: null,
  };
}

function bucket(overrides: Partial<CostBucket> = {}): CostBucket {
  return {
    runs: 0,
    cost_usd: null,
    cost_usd_equivalent: null,
    api_equivalent_usd: null,
    actual_cost_usd: null,
    billing_neuralwatt_kwh: null,
    billing_neuralwatt_charged_kwh: null,
    billing_neuralwatt_usd_per_kwh: null,
    billing_neuralwatt_cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    ...overrides,
  };
}

function costs(): RunsCostsResponse {
  return {
    days: 7,
    now: 1,
    today: bucket({ runs: 4, actual_cost_usd: 4.2 }),
    window: bucket({ runs: 20, actual_cost_usd: 14 }),
    profiles: [],
    review_value: [],
  };
}

function planSpec(status: string): PlanSpecRecord {
  return {
    path: `/plans/${status}.md`,
    agent: "Hermes",
    filename: `${status}.md`,
    topic: `Plan ${status}`,
    status,
    freigabe: "sofort",
    live_test_depth: null,
    binding: false,
    subtask_count: 0,
    valid: true,
    open: true,
    closed_reason: null,
    kanban_root_task_id: null,
    kanban_root_status: null,
    kanban_state: "not_ingested",
    kanban_child_total: 0,
    kanban_child_done: 0,
    kanban_child_blocked: 0,
    kanban_child_running: 0,
    kanban_ingested_at: null,
    ingest_disposition: "not_ingestable",
    ingest_would_block: true,
    ingest_findings: [],
    errors: [],
  };
}

function renderHeute({
  activeWorkers = [],
  costsData = null,
  plans = [],
}: {
  activeWorkers?: Worker[];
  costsData?: RunsCostsResponse | null;
  plans?: PlanSpecRecord[];
} = {}) {
  return render(
    <HeuteTab
      allWorkers={activeWorkers}
      activeWorkers={activeWorkers}
      blockedCount={2}
      pendingApprovals={0}
      allPlanspecs={plans}
      costs={costsData}
      daily={null}
      now={100}
      onWorkerClick={() => undefined}
      onPlanSpecClick={() => undefined}
    />,
  );
}

describe("HeuteTab dimension rule", () => {
  it("keeps duplicate Worker/Cost KPIs only with a visible profile breakdown or 7-day comparison", () => {
    renderHeute({
      activeWorkers: [worker("1", "coder"), worker("2", "coder"), worker("3", "premium")],
      costsData: costs(),
    });

    expect(screen.getByText("Aktiv")).toBeTruthy();
    expect(screen.getByText("Coder 2 · Premium 1")).toBeTruthy();
    expect(screen.getByText("Kosten 24h")).toBeTruthy();
    expect(screen.getByText("Ø 7T 2,0$")).toBeTruthy();
  });

  it("removes duplicate Worker/Cost tiles when their supporting dimension is unavailable", () => {
    renderHeute();

    expect(screen.queryByText("Aktiv")).toBeNull();
    expect(screen.queryByText("Kosten 24h")).toBeNull();
    expect(screen.getByText("Blockiert")).toBeTruthy();
    expect(screen.getByText("Fertig 24h")).toBeTruthy();
  });
});

describe("HeuteTab PlanSpec status chips", () => {
  it("renders deferred, superseded and archived as neutral lifecycle states", () => {
    renderHeute({ plans: [planSpec("deferred"), planSpec("superseded"), planSpec("archived")] });

    for (const status of ["deferred", "superseded", "archived"]) {
      const chip = screen.getByText(status).parentElement;
      expect(chip?.className).toContain("border-line");
      expect(chip?.className).not.toMatch(/status-(ok|warn|alert)/);
    }
  });

  it("clips a long status chip with ellipsis while retaining the full title", () => {
    const longStatus = "deferred — wartet auf die nächste belastbare Produktentscheidung aus dem vollständigen PlanSpec-Drawer";
    renderHeute({ plans: [planSpec(longStatus)] });

    const label = screen.getByText(longStatus);
    expect(label.className).toContain("truncate");
    expect(label.getAttribute("title")).toBe(longStatus);
    expect(label.parentElement?.className).toContain("max-w-[min(52%,28rem)]");
  });
});
