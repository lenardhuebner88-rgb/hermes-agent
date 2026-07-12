// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PendingItem } from "../../lib/fleetHub";
import type { CostBucket, RunsCostsResponse } from "../../lib/schemas";
import type { Worker } from "../../lib/types";
import { HeuteTab } from "./HeuteTab";
import type { PlanSpecRecord } from "./shared";

// Zählt echte (Re-)Mounts des Disclosure-Kindes. Ein Remount setzt den lokalen
// open-Zustand zurück und ersetzt den Button-DOM-Knoten — genau das darf ein
// Live-Poll-Rerender NICHT auslösen (siehe Disclosure-Stabilitäts-Test unten).
let laneSwitchMounts = 0;

// Zustandsbehaftetes Double für LaneQuickSwitch: ein Disclosure mit lokalem
// open-Zustand, aria-expanded und zwei Selects im offenen Panel. Steht
// stellvertretend für den realen Schnellschalter; sein Zustand überlebt genau
// dann, wenn HeuteTab den Knoten über Rerender hinweg wiederverwendet.
vi.mock("./LaneQuickSwitch", () => ({
  LaneQuickSwitch: () => {
    const [open, setOpen] = useState(false);
    useEffect(() => {
      laneSwitchMounts += 1;
    }, []);
    return (
      <section aria-label="Lane-Modell-Schnellschalter">
        <button
          data-testid="lane-disclosure-toggle"
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((prev) => !prev)}
          onKeyDown={(event) => {
            if (event.key === "Enter") setOpen((prev) => !prev);
          }}
        >
          Lane &amp; Modell
        </button>
        {open ? (
          <div data-testid="lane-disclosure-panel">
            <select aria-label="Profil" />
            <select aria-label="Modell" />
          </div>
        ) : null}
      </section>
    );
  },
}));

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
  blockedCount = 2,
  pendingItems = [],
  onNavigate = () => undefined,
}: {
  activeWorkers?: Worker[];
  costsData?: RunsCostsResponse | null;
  plans?: PlanSpecRecord[];
  blockedCount?: number;
  pendingItems?: PendingItem[];
  onNavigate?: (target: "worker" | "plan" | "risiko") => void;
} = {}) {
  return render(
    <HeuteTab
      allWorkers={activeWorkers}
      activeWorkers={activeWorkers}
      blockedCount={blockedCount}
      pendingApprovals={0}
      allPlanspecs={plans}
      costs={costsData}
      daily={null}
      now={100}
      pendingItems={pendingItems}
      onWorkerClick={() => undefined}
      onPlanSpecClick={() => undefined}
      onNavigate={onNavigate}
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

describe("HeuteTab action block + idle state", () => {
  it("shows a tappable action row for a waiting approval that navigates to Plan", () => {
    const onNavigate = vi.fn();
    renderHeute({
      blockedCount: 0,
      pendingItems: [{ kind: "approval", topic: "Cockpit-Umbau", targetSubtab: "plan" }],
      onNavigate,
    });

    const row = screen.getByRole("button", { name: "Freigabe wartet: Cockpit-Umbau" });
    // Primärer Handlungs-Callout wird höflich angekündigt (Ersatz der globalen PendingBar auf Heute).
    expect(row.getAttribute("aria-live")).toBe("polite");
    fireEvent.click(row);
    expect(onNavigate).toHaveBeenCalledWith("plan");
  });

  it("summarizes remaining blockers without double-counting operator holds", () => {
    renderHeute({
      blockedCount: 3,
      pendingItems: [{ kind: "blocked", topic: "Halt A", targetSubtab: "risiko" }],
    });

    expect(screen.getByRole("button", { name: "Operator-Halt: Halt A" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "2 Aufgaben blockiert — im Risiko-Tab lösen" })).toBeTruthy();
  });

  it("renders no action block and a compact idle state when nothing waits and no worker runs", () => {
    renderHeute({ blockedCount: 0, activeWorkers: [] });

    expect(screen.queryByLabelText("Handlungsbedarf")).toBeNull();
    expect(screen.getByText("Keine Worker aktiv — Board ruht.")).toBeTruthy();
  });
});

describe("HeuteTab PlanSpec relevance ordering", () => {
  function spec(topic: string, overrides: Partial<PlanSpecRecord>): PlanSpecRecord {
    return { ...planSpec("open"), path: `/${topic}.md`, topic, ...overrides };
  }

  function renderedPlanTopics(container: HTMLElement): string[] {
    return Array.from(container.querySelectorAll(".fleet-ps-name")).map((el) => el.textContent ?? "");
  }

  it("renders operator-waiting and running plans ahead of merely-open ones", () => {
    const waiting = spec("Waiting", { freigabe: "operator", kanban_state: "queued" });
    const running = spec("Running", { kanban_state: "running" });
    const open = spec("Open", { kanban_state: "not_ingested", open: true });

    const { container } = renderHeute({ plans: [open, running, waiting] });
    expect(renderedPlanTopics(container)).toEqual(["Waiting", "Running", "Open"]);
  });

  it("keeps input order for plans of equal relevance", () => {
    const a = spec("Alpha", { open: true });
    const b = spec("Beta", { open: true });

    const { container } = renderHeute({ plans: [a, b] });
    expect(renderedPlanTopics(container)).toEqual(["Alpha", "Beta"]);
  });
});

describe("HeuteTab Disclosure-Stabilität bei Live-Polling", () => {
  it("behält denselben Disclosure-Knoten und offenen Zustand, wenn Live-Daten davor einfügen", () => {
    laneSwitchMounts = 0;

    // Initial: keine PlanSpecs, kein aktiver Worker, keine Handlungszeile —
    // die konditionalen Geschwister vor dem Disclosure fehlen alle.
    const { rerender } = render(
      <HeuteTab
        allWorkers={[]}
        activeWorkers={[]}
        blockedCount={0}
        pendingApprovals={0}
        allPlanspecs={[]}
        costs={null}
        daily={null}
        now={100}
        pendingItems={[]}
        onWorkerClick={() => undefined}
        onPlanSpecClick={() => undefined}
        onNavigate={() => undefined}
      />,
    );

    const toggle = screen.getByTestId("lane-disclosure-toggle");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("lane-disclosure-panel")).toBeTruthy();
    expect(screen.getAllByRole("combobox")).toHaveLength(2);

    // Live-Poll-Rerender: aktive Worker, laufende PlanSpecs und eine
    // Handlungszeile erscheinen — alle als Geschwister VOR dem Disclosure.
    rerender(
      <HeuteTab
        allWorkers={[worker("1", "coder"), worker("2", "premium")]}
        activeWorkers={[worker("1", "coder"), worker("2", "premium")]}
        blockedCount={1}
        pendingApprovals={1}
        allPlanspecs={[planSpec("running")]}
        costs={costs()}
        daily={null}
        now={200}
        pendingItems={[{ kind: "approval", topic: "Cockpit-Umbau", targetSubtab: "plan" }]}
        onWorkerClick={() => undefined}
        onPlanSpecClick={() => undefined}
        onNavigate={() => undefined}
      />,
    );

    const toggleAfter = screen.getByTestId("lane-disclosure-toggle");
    // Kein Remount: derselbe DOM-Knoten, kein zusätzlicher Mount, Zustand hält.
    expect(laneSwitchMounts).toBe(1);
    expect(toggleAfter).toBe(toggle);
    expect(toggleAfter.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByTestId("lane-disclosure-panel")).toBeTruthy();
    expect(screen.getAllByRole("combobox")).toHaveLength(2);

    // Enter auf dem Toggle schließt den Disclosure wieder (false).
    fireEvent.keyDown(toggleAfter, { key: "Enter" });
    expect(toggleAfter.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByTestId("lane-disclosure-panel")).toBeNull();
  });
});
