import { describe, expect, it } from "vitest";

import {
  buildAgentOpsDispatchPrompt,
  buildAgentOpsSnapshot,
  buildFourAgentLaunchBrief,
  buildMorningBrief,
  buildProjectLanes,
  selectDispatchCandidates,
} from "./agentOps";
import type { OrchestrationBacklogResponse, OrchestrationItem } from "./schemas";
import type { KanbanResult, MetricsLiteResponse, Proposal, SystemHealthResponse, Worker } from "./types";

const NOW = 1_780_614_000;

function item(overrides: Partial<OrchestrationItem> & { id: string }): OrchestrationItem {
  const { id, ...rest } = overrides;
  return {
    id,
    title: `Task ${id}`,
    status: "todo",
    priority: "medium",
    dependsOn: [],
    planGate: false,
    created: "2026-06-01",
    root: "/home/piet/.hermes/hermes-agent",
    owner: "piet",
    source: "spec",
    lastProof: "",
    excerpt: "",
    ...rest,
  };
}

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    run_id: "run-1",
    task_id: "doing",
    task_title: "Active worker",
    task_status: "running",
    task_assignee: "hermes",
    profile: "coder",
    worker_pid: 123,
    started_at: NOW - 120,
    claim_lock: "lock",
    claim_expires: NOW + 600,
    last_heartbeat_at: NOW - 5,
    max_runtime_seconds: 3600,
    run_status: "running",
    run_outcome: null,
    inspect: { cpu_percent: 3, rss: 128, num_threads: 1, num_fds: 2, status: "running", alive: true },
    ...overrides,
  };
}

const healthy: SystemHealthResponse = {
  schema: "hermes-health-v1",
  checked_at: NOW,
  overall: "healthy",
  subsystems: {
    gateway: { status: "healthy", detail: "", error: null },
    autoresearch: { status: "healthy", detail: "", error: null },
    kanban_db: { status: "healthy", detail: "", error: null },
    kanban_dispatcher: { status: "healthy", detail: "", error: null },
  },
};

const metrics: MetricsLiteResponse = {
  schema: "hermes-metrics-lite-v1",
  checked_at: NOW,
  uptime_seconds: 100,
  groups: {
    api: { count: 100, error_count: 1, error_rate: 0.01, p50_ms: 20, p95_ms: 140 },
    kanban: { count: 20, error_count: 0, error_rate: 0, p50_ms: 30, p95_ms: 260 },
  },
};

const contractHealth: OrchestrationBacklogResponse["contract_health"] = {
  source_count: 6,
  counted_sum: 5,
  unknown_statuses: [{ status: "planning", count: 1, ids: ["drift"] }],
  invalid_priority_count: 1,
  missing_dep_count: 0,
};

describe("selectDispatchCandidates", () => {
  it("picks dispatchable work first, then plan gates and review", () => {
    const rows = [
      item({ id: "done-dep", status: "done", priority: "low" }),
      item({ id: "blocked", priority: "high", dependsOn: ["missing"] }),
      item({ id: "plan", priority: "high", planGate: true }),
      item({ id: "review", status: "review", priority: "low" }),
      item({ id: "ready-med", priority: "medium", created: "2026-05-01" }),
      item({ id: "ready-high", priority: "high", created: "2026-06-03" }),
    ];

    const candidates = selectDispatchCandidates(rows, 4);

    expect(candidates.map((candidate) => [candidate.id, candidate.kind])).toEqual([
      ["ready-high", "dispatch"],
      ["ready-med", "dispatch"],
      ["plan", "plan_gate"],
      ["review", "review"],
    ]);
    expect(candidates.some((candidate) => candidate.id === "blocked")).toBe(false);
  });
});

describe("buildProjectLanes", () => {
  it("aggregates project risk, readiness and active workers", () => {
    const rows = [
      item({ id: "doing", status: "doing", priority: "high", root: "/home/piet/.hermes/hermes-agent" }),
      item({ id: "ready", status: "todo", priority: "high", root: "/home/piet/projects/family-organizer" }),
      // todo + owner "" => ruhige Queue, NICHT unowned (vereinheitlichtes Claim-Modell).
      item({ id: "blocked", status: "todo", priority: "medium", dependsOn: ["ghost"], owner: "", root: "/home/piet/projects/family-organizer" }),
      // review + owner "" => aktiv ohne Owner => zaehlt als unowned.
      item({ id: "fo-active", status: "review", owner: "", root: "/home/piet/projects/family-organizer" }),
    ];

    const lanes = buildProjectLanes(rows, [worker()], NOW);
    const dashboard = lanes.find((lane) => lane.project === "Dashboard");
    const fo = lanes.find((lane) => lane.project === "Family Organizer");

    expect(dashboard?.activeWorkers).toBe(1);
    expect(dashboard?.highRisk).toBe(1);
    // unowned zaehlt nur das review-Item, nicht das owner-lose todo.
    expect(fo).toMatchObject({ blocked: 1, unowned: 1 });
  });
});

describe("buildAgentOpsSnapshot", () => {
  it("combines workers, backlog, metrics and proposal pressure", () => {
    const proposals: Proposal[] = [
      { id: "p1", target: "skill", section: null, rationale_plain: "", diff_before_after: "", mode: "skill", status: "proposed" },
      { id: "p2", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "applied" },
      { id: "p3", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "applied", gate: { phase: "passed" } },
      { id: "p4", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "testing", gate: { phase: "running" } },
    ];
    const results = [
      { run_id: "r", task_id: "t", task_title: "T", verification: ["vitest"] } as KanbanResult,
      { run_id: "r2", task_id: "t2", task_title: "T2", verification: [] } as unknown as KanbanResult,
    ];
    const rows = [
      item({ id: "ready", priority: "high" }),
      item({ id: "blocked", dependsOn: ["ghost"] }),
      item({ id: "plan", planGate: true }),
      item({ id: "unowned", owner: "", lastProof: "2026-01-01" }), // todo + owner "" => ruhige Queue, kein Owner-Gap
    ];

    const snapshot = buildAgentOpsSnapshot({
      workers: [worker()],
      results,
      proposals,
      orchestrationItems: rows,
      contractHealth,
      systemHealth: healthy,
      metrics,
      nowSec: NOW,
    });

    expect(snapshot.activeWorkers).toBe(1);
    expect(snapshot.healthyWorkers).toBe(1);
    expect(snapshot.parallelSlotsFree).toBe(3);
    expect(snapshot.recommendedLaunches).toBe(2);
    expect(snapshot.completedRuns).toBe(2);
    expect(snapshot.verifiedResults).toBe(1);
    expect(snapshot.dispatchReady).toBe(2);
    expect(snapshot.planGates).toBe(1);
    expect(snapshot.blockedItems).toBe(1);
    expect(snapshot.reviewItems).toBe(0);
    expect(snapshot.openProposals).toBe(1);
    expect(snapshot.testingProposals).toBe(1);
    // p3 ist applied+gate:passed → kein OFFENES Proposal mehr → nicht mehr in gatePassed.
    expect(snapshot.gatePassed).toBe(0);
    expect(snapshot.gateRunning).toBe(1);
    expect(snapshot.gatePassRate).toBe(1);
    expect(snapshot.highRiskItems).toBe(1);
    expect(snapshot.staleProofItems).toBeGreaterThanOrEqual(1);
    expect(snapshot.unownedItems).toBe(0); // todo-ohne-Owner ist die ruhige Queue, kein Gap
    expect(snapshot.contractDrift).toBe(2);
    expect(snapshot.operatorDecision.kind).toBe("launch");
    expect(snapshot.readinessGaps.map((item) => item.id)).toContain("contract-drift");
    expect(snapshot.interventions.map((item) => item.id)).toContain("contract-drift");
  });

  it("counts gate phases only on open proposals (applied/skipped gate-zombies excluded)", () => {
    const proposals: Proposal[] = [
      { id: "z1", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "applied", gate: { phase: "failed" } },
      { id: "z2", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "skipped", gate: { phase: "crashed" } },
      { id: "open-f", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "proposed", gate: { phase: "failed" } },
      { id: "open-r", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "testing", gate: { phase: "running" } },
      { id: "open-p", target: "code", section: null, rationale_plain: "", diff_before_after: "", mode: "code", status: "testing", gate: { phase: "passed" } },
    ];
    const snapshot = buildAgentOpsSnapshot({
      workers: [],
      results: [],
      proposals,
      orchestrationItems: [],
      contractHealth,
      systemHealth: healthy,
      metrics,
      nowSec: NOW,
    });
    // Nur die offenen (proposed/testing) Gates zaehlen — die applied/skipped-Zombies nicht.
    expect(snapshot.gateFailed).toBe(1);
    expect(snapshot.gateRunning).toBe(1);
    expect(snapshot.gatePassed).toBe(1);
  });

  it("builds safe copy text without requiring loaded detail", () => {
    const prompt = buildAgentOpsDispatchPrompt(item({ id: "f-x", title: "Ship the tab" }));

    expect(prompt).toContain("Ship the tab");
    expect(prompt).toContain("~/orchestration/backlog/f-x.md");
    expect(prompt).toContain("Isoliert arbeiten");
    expect(prompt).toContain("git status --short");
    // Claim als erste Handlung (vor "Isoliert arbeiten").
    expect(prompt).toContain("CLAIM zuerst");
    expect(prompt).toContain("status: doing");
    expect(prompt.indexOf("CLAIM zuerst")).toBeLessThan(prompt.indexOf("Isoliert arbeiten"));
  });

  it("flags owner-gap only for actively worked items (doing/review), never the quiet queue", () => {
    const base = {
      workers: [],
      results: [],
      proposals: [] as Proposal[],
      contractHealth,
      systemHealth: healthy,
      metrics,
      nowSec: NOW,
    };
    // Positiv: doing + review ohne Owner zaehlen; doing MIT Owner und todo ohne Owner nicht.
    const active = buildAgentOpsSnapshot({
      ...base,
      orchestrationItems: [
        item({ id: "d-noowner", status: "doing", owner: "" }),
        item({ id: "r-noowner", status: "review", owner: "" }),
        item({ id: "d-owned", status: "doing", owner: "piet" }),
        item({ id: "t-noowner", status: "todo", owner: "" }),
      ],
    });
    expect(active.unownedItems).toBe(2);
    expect(active.readinessGaps.map((gap) => gap.id)).toContain("unowned-items");

    // Negativ: nur ruhige Queue ohne Owner => kein Gap.
    const quiet = buildAgentOpsSnapshot({
      ...base,
      orchestrationItems: [
        item({ id: "t1", status: "todo", owner: "" }),
        item({ id: "b1", status: "backlog", owner: "" }),
      ],
    });
    expect(quiet.unownedItems).toBe(0);
    expect(quiet.readinessGaps.map((gap) => gap.id)).not.toContain("unowned-items");
  });

  it("produces a concise morning brief", () => {
    const snapshot = buildAgentOpsSnapshot({
      workers: [],
      results: [],
      proposals: [],
      orchestrationItems: [item({ id: "ready", title: "Ready Work", priority: "high" })],
      contractHealth: null,
      systemHealth: healthy,
      metrics,
      nowSec: NOW,
    });

    const brief = buildMorningBrief(snapshot);

    expect(brief).toContain("Hermes Arbeitsstroeme Brief");
    expect(brief).toContain("Entscheidung:");
    expect(brief).toContain("Ready Work");
    expect(brief).toContain("Slots frei");
    expect(brief).toContain("Proof:");
    expect(brief).toContain("Interventionen:");
  });

  it("builds a four-agent launch brief with candidates and gaps", () => {
    const snapshot = buildAgentOpsSnapshot({
      workers: [worker(), worker({ run_id: "run-2", task_id: "other" })],
      results: [],
      proposals: [],
      orchestrationItems: [
        item({ id: "ready-a", title: "Ready A", priority: "high" }),
        item({ id: "ready-b", title: "Ready B", priority: "medium" }),
        item({ id: "blocked", dependsOn: ["ghost"] }),
      ],
      contractHealth,
      systemHealth: healthy,
      metrics,
      nowSec: NOW,
    });

    const brief = buildFourAgentLaunchBrief(snapshot);

    expect(brief).toContain("Hermes 4-Agenten Startbrief");
    expect(brief).toContain("Kapazitaet: 2/4 aktiv");
    expect(brief).toContain("ready-a");
    expect(brief).toContain("Readiness-Luecken:");
  });
});
