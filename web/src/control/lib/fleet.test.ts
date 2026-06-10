import { describe, expect, it } from "vitest";
import {
  statusToStage,
  stageActions,
  stageGuard,
  isActionableStatus,
  roleChip,
  buildPipeline,
  groupByStage,
  flowCounts,
  captureRequest,
  flowCaptureRequest,
  usesFlowCaptureEndpoint,
  STAGE_META,
  type BoardTaskLite,
} from "./fleet";
import type { TaskStatus } from "./types";

describe("statusToStage", () => {
  it("maps every kanban status onto an operator stage", () => {
    expect(statusToStage("triage")).toBe("capture");
    expect(statusToStage("todo")).toBe("plan");
    expect(statusToStage("scheduled")).toBe("plan");
    expect(statusToStage("ready")).toBe("plan"); // ready = planned & queued, not yet executing
    expect(statusToStage("running")).toBe("execute");
    expect(statusToStage("review")).toBe("verify");
    expect(statusToStage("done")).toBe("ship");
  });
  it("treats blocked as execute (a fallen-out run) and archived as no stage", () => {
    expect(statusToStage("blocked")).toBe("execute");
    expect(statusToStage("archived")).toBeNull();
  });
  it("keeps STAGE_META status lists consistent with statusToStage", () => {
    for (const stage of Object.values(STAGE_META)) {
      for (const status of stage.statuses) {
        expect(statusToStage(status)).toBe(stage.id);
      }
    }
  });
});

describe("stageActions", () => {
  it("offers Plan from triage (→ todo)", () => {
    const [a] = stageActions("triage");
    expect(a.key).toBe("plan");
    expect(a.target).toBe("todo");
  });
  it("offers Dispatch from todo/scheduled (→ ready)", () => {
    expect(stageActions("todo")[0]).toMatchObject({ key: "dispatch", target: "ready" });
    expect(stageActions("scheduled")[0]).toMatchObject({ key: "dispatch", target: "ready" });
  });
  it("offers Ship + Rework from review", () => {
    const keys = stageActions("review").map((a) => a.key);
    expect(keys).toEqual(["ship", "rework"]);
    expect(stageActions("review").find((a) => a.key === "ship")?.target).toBe("done");
    expect(stageActions("review").find((a) => a.key === "rework")?.target).toBe("blocked");
  });
  it("offers Reopen from blocked (→ ready)", () => {
    expect(stageActions("blocked")[0]).toMatchObject({ key: "reopen", target: "ready" });
  });
  it("offers no manual action for worker/gate-driven statuses", () => {
    expect(stageActions("ready")).toEqual([]);
    expect(stageActions("running")).toEqual([]);
    expect(stageActions("done")).toEqual([]);
  });
});

describe("stageGuard", () => {
  it("explains why ready/running/done have no manual button", () => {
    expect(stageGuard("ready")).toBeTruthy();
    expect(stageGuard("running")).toBeTruthy();
    expect(stageGuard("done")).toBeTruthy();
  });
  it("returns null for operator-actionable statuses", () => {
    expect(stageGuard("triage")).toBeNull();
    expect(stageGuard("todo")).toBeNull();
    expect(stageGuard("review")).toBeNull();
    expect(stageGuard("blocked")).toBeNull();
  });
});

describe("isActionableStatus", () => {
  it("is true exactly for operator-decidable + blocked", () => {
    const yes: TaskStatus[] = ["triage", "todo", "scheduled", "blocked", "review"];
    const no: TaskStatus[] = ["ready", "running", "done", "archived"];
    yes.forEach((s) => expect(isActionableStatus(s)).toBe(true));
    no.forEach((s) => expect(isActionableStatus(s)).toBe(false));
  });
});

describe("roleChip", () => {
  it("colours known profiles by role", () => {
    expect(roleChip("verifier")).toMatchObject({ label: "Verifier", tone: "sky" });
    expect(roleChip("coder")).toMatchObject({ label: "Coder", tone: "amber" });
    expect(roleChip("research")).toMatchObject({ label: "Researcher", tone: "emerald" });
  });
  it("overrides to Verifier when the run is a verification run", () => {
    expect(roleChip("coder", "verification")).toMatchObject({ label: "Verifier" });
  });
  it("falls back gracefully for unknown profiles and null", () => {
    expect(roleChip(null)).toMatchObject({ label: "Worker" });
    expect(roleChip("mystery")).toMatchObject({ label: "mystery", short: "M", tone: "zinc" });
  });
});

describe("buildPipeline", () => {
  const t = (id: string, status: TaskStatus, priority = 0): BoardTaskLite => ({ id, title: id, status, priority });

  it("counts tasks into the five stage buckets and ignores archived", () => {
    const tasks = [t("a", "triage"), t("b", "todo"), t("c", "ready"), t("d", "running"), t("e", "review"), t("f", "done"), t("g", "archived")];
    const p = buildPipeline(tasks);
    const counts = Object.fromEntries(p.buckets.map((b) => [b.stage, b.count]));
    expect(counts).toEqual({ capture: 1, plan: 2, execute: 1, verify: 1, ship: 1 }); // ready in plan, running in execute
    expect(p.total).toBe(6); // archived excluded
  });

  it("counts blocked separately and keeps it actionable", () => {
    const p = buildPipeline([t("x", "blocked"), t("y", "blocked"), t("z", "done")]);
    expect(p.blockedCount).toBe(2);
    expect(p.actionable.map((a) => a.id).sort()).toEqual(["x", "y"]);
  });

  it("orders actionable review → blocked → triage → todo, then by priority", () => {
    const tasks = [t("todo1", "todo", 1), t("review1", "review"), t("blocked1", "blocked"), t("triage1", "triage"), t("todo2", "todo", 5)];
    const order = buildPipeline(tasks).actionable.map((a) => a.id);
    expect(order[0]).toBe("review1");
    expect(order[1]).toBe("blocked1");
    expect(order[2]).toBe("triage1");
    // todo2 (priority 5) before todo1 (priority 1)
    expect(order.indexOf("todo2")).toBeLessThan(order.indexOf("todo1"));
  });
});

describe("groupByStage", () => {
  const t = (id: string, status: TaskStatus, priority = 0): BoardTaskLite => ({ id, title: id, status, priority });

  it("places each task in its Flow column and drops archived", () => {
    const cols = groupByStage([
      t("a", "triage"), t("b", "todo"), t("c", "scheduled"), t("d", "ready"),
      t("e", "running"), t("f", "review"), t("g", "done"), t("h", "archived"),
    ]);
    expect(cols.capture.map((x) => x.id)).toEqual(["a"]);
    expect(cols.plan.map((x) => x.id).sort()).toEqual(["b", "c", "d"]);
    expect(cols.execute.map((x) => x.id)).toEqual(["e"]);
    expect(cols.verify.map((x) => x.id)).toEqual(["f"]);
    expect(cols.ship.map((x) => x.id)).toEqual(["g"]);
    expect(Object.values(cols).flat().some((x) => x.id === "h")).toBe(false);
  });

  it("puts blocked tasks in Execute (rework guard) and sorts by priority desc", () => {
    const cols = groupByStage([t("lo", "todo", 1), t("hi", "todo", 5), t("blk", "blocked")]);
    expect(cols.execute.map((x) => x.id)).toEqual(["blk"]);
    expect(cols.plan.map((x) => x.id)).toEqual(["hi", "lo"]);
  });

  it("sorts Ship by completion recency (newest first), NOT priority — a fresh ship is always on top", () => {
    const done = (id: string, completed_at: number, created_age: number, priority = 0): BoardTaskLite => ({
      id, title: id, status: "done", priority, completed_at, age: { created_age_seconds: created_age },
    });
    const cols = groupByStage([
      done("old", 1000, 99999, 9), // ancient + highest priority — must NOT win
      done("newest", 5000, 10, 0),
      done("mid", 3000, 500, 5),
    ]);
    expect(cols.ship.map((x) => x.id)).toEqual(["newest", "mid", "old"]);
  });

  it("Ship falls back to creation recency when completed_at is missing", () => {
    const cols = groupByStage([
      { id: "older", title: "older", status: "done", age: { created_age_seconds: 900 } },
      { id: "newer", title: "newer", status: "done", age: { created_age_seconds: 100 } },
    ]);
    expect(cols.ship.map((x) => x.id)).toEqual(["newer", "older"]);
  });
});

describe("flowCounts", () => {
  const t = (status: TaskStatus): BoardTaskLite => ({ id: status, title: status, status });
  it("counts running/plan/review/blocked + wip, excluding done/archived", () => {
    const c = flowCounts([
      t("triage"), t("todo"), t("scheduled"), t("ready"),
      t("running"), t("review"), t("blocked"), t("done"), t("archived"),
    ]);
    expect(c).toEqual({ running: 1, plan: 3, review: 1, blocked: 1, wip: 7 });
  });
  it("is all-zero for an empty / fully-terminal board", () => {
    expect(flowCounts([t("done"), t("archived")])).toEqual({ running: 0, plan: 0, review: 0, blocked: 0, wip: 0 });
  });
});

describe("captureRequest", () => {
  it("park method → triage+park, trimmed title, no home ping, no hardcoded assignee", () => {
    expect(captureRequest("  Tisch decken  ", "park")).toEqual({
      title: "Tisch decken", assignee: null, priority: 0, tenant: "flow-capture",
      triage: true, park: true, notify_home: false,
    });
  });
  it("lean method → triage only (no park), pings home, leaves assignee unset for the decomposer", () => {
    const r = captureRequest("Baue X", "lean");
    expect(r.triage).toBe(true);
    expect(r.park).toBe(false);
    expect(r.notify_home).toBe(true);
    // Critical: assignee must NOT be hardcoded — the in-gateway decomposer routes
    // the triage task; a pre-set "coder" would short-circuit it straight to coder.
    expect(r.assignee).toBeNull();
  });
});

describe("flowCaptureRequest", () => {
  it("document method → method=document, carries the gate flag, trims title", () => {
    expect(flowCaptureRequest("  Baue X in 3 Teilen  ", "document", true)).toEqual({
      title: "Baue X in 3 Teilen", method: "document", gate: true,
      tenant: "flow-capture", priority: 0, notify_home: true,
    });
  });
  it("lean method maps to method=lean (only ever routed here for the gate combo)", () => {
    const r = flowCaptureRequest("Baue X", "lean", true);
    expect(r.method).toBe("lean");
    expect(r.gate).toBe(true);
  });
  it("park is never a flow-capture method → coerces to lean shape", () => {
    expect(flowCaptureRequest("X", "park", false).method).toBe("lean");
  });
});

describe("usesFlowCaptureEndpoint", () => {
  it("park and lean+AUTO use the plain POST /tasks (Stufe-A, backward-compat)", () => {
    expect(usesFlowCaptureEndpoint("park", false)).toBe(false);
    expect(usesFlowCaptureEndpoint("park", true)).toBe(false);
    expect(usesFlowCaptureEndpoint("lean", false)).toBe(false);
  });
  it("document (either gate) and lean+GATE use the backend-driven endpoint", () => {
    expect(usesFlowCaptureEndpoint("lean", true)).toBe(true);
    expect(usesFlowCaptureEndpoint("document", false)).toBe(true);
    expect(usesFlowCaptureEndpoint("document", true)).toBe(true);
  });
});

// ── Ketten-Modell + Projekt-Achse (Phase 2, Operator-Vertrag 2026-06-10) ────
import { buildChains, groupChainsByEpic, projectKey, projectLabel, projectOptions, UNSORTED_PROJECT, type ChainTaskLite } from "./fleet";

function task(over: Partial<ChainTaskLite> & { id: string; status: TaskStatus }): ChainTaskLite {
  return { title: over.id, priority: 0, ...over };
}

describe("buildChains", () => {
  it("groups members by root_id, keeps the sink as root and counts stages", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "root", status: "todo", root_id: "root", tenant: "family-organizer" }),
      task({ id: "a", status: "running", root_id: "root" }),
      task({ id: "b", status: "done", root_id: "root", completed_at: 100 }),
      task({ id: "solo", status: "review", root_id: "solo" }),
    ];
    const board = buildChains(tasks);
    expect(board.active).toHaveLength(1);
    const chain = board.active[0];
    expect(chain.rootId).toBe("root");
    expect(chain.root?.id).toBe("root");
    expect(chain.total).toBe(3);
    expect(chain.doneCount).toBe(1);
    expect(chain.runningCount).toBe(1);
    expect(chain.stageCounts.execute).toBe(1);
    expect(chain.stageCounts.ship).toBe(1);
    expect(chain.tenant).toBe("family-organizer");
    expect(board.singles.map((t) => t.id)).toEqual(["solo"]);
  });

  it("splits done chains/singles from active ones and sorts done newest-first", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "r1", status: "done", root_id: "r1", completed_at: 50 }),
      task({ id: "m1", status: "done", root_id: "r1", completed_at: 60 }),
      task({ id: "r2", status: "done", root_id: "r2", completed_at: 200 }),
      task({ id: "m2", status: "done", root_id: "r2", completed_at: 190 }),
      task({ id: "old", status: "done", root_id: "old", completed_at: 10 }),
      task({ id: "new", status: "done", root_id: "new", completed_at: 999 }),
    ];
    const board = buildChains(tasks);
    expect(board.active).toHaveLength(0);
    expect(board.done.map((c) => c.rootId)).toEqual(["r2", "r1"]);
    expect(board.done[0].latestCompletedAt).toBe(200);
    expect(board.doneSingles.map((t) => t.id)).toEqual(["new", "old"]);
  });

  it("ranks active chains by urgency: running > review > blocked > plan", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "p", status: "todo", root_id: "p" }), task({ id: "p1", status: "todo", root_id: "p" }),
      task({ id: "b", status: "todo", root_id: "b" }), task({ id: "b1", status: "blocked", root_id: "b" }),
      task({ id: "v", status: "todo", root_id: "v" }), task({ id: "v1", status: "review", root_id: "v" }),
      task({ id: "x", status: "todo", root_id: "x" }), task({ id: "x1", status: "running", root_id: "x" }),
    ];
    expect(buildChains(tasks).active.map((c) => c.rootId)).toEqual(["x", "v", "b", "p"]);
  });

  it("drops archived tasks and treats a missing root_id as standalone", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "gone", status: "archived" }),
      task({ id: "legacy", status: "ready" }), // kein root_id-Feld (alter Server)
    ];
    const board = buildChains(tasks);
    expect(board.active).toHaveLength(0);
    expect(board.singles.map((t) => t.id)).toEqual(["legacy"]);
  });

  it("sorts chain members in stage order (execute first, ship last)", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "root", status: "done", root_id: "root", completed_at: 5 }),
      task({ id: "a", status: "review", root_id: "root" }),
      task({ id: "c", status: "running", root_id: "root" }),
      task({ id: "d", status: "todo", root_id: "root" }),
    ];
    const chain = buildChains(tasks).active[0];
    expect(chain.members.map((m) => m.id)).toEqual(["c", "a", "d", "root"]);
  });
});

describe("groupChainsByEpic", () => {
  it("groups by epicId in chain order and puts Ohne Epic last", () => {
    const tasks: ChainTaskLite[] = [
      // none-Kette ist die dringendste (running) — "Ohne Epic" muss trotzdem ans Ende.
      task({ id: "n", status: "todo", root_id: "n" }), task({ id: "n1", status: "running", root_id: "n" }),
      task({ id: "a", status: "todo", root_id: "a", epic_id: "e_1" }), task({ id: "a1", status: "review", root_id: "a", epic_id: "e_1" }),
      task({ id: "b", status: "todo", root_id: "b", epic_id: "e_2" }), task({ id: "b1", status: "blocked", root_id: "b", epic_id: "e_2" }),
      task({ id: "c", status: "todo", root_id: "c", epic_id: "e_1" }), task({ id: "c1", status: "todo", root_id: "c", epic_id: "e_1" }),
    ];
    const groups = groupChainsByEpic(buildChains(tasks).active);
    expect(groups.map((g) => g.epicId)).toEqual(["e_1", "e_2", null]);
    expect(groups[0].chains.map((c) => c.rootId)).toEqual(["a", "c"]);
    expect(groups[2].chains.map((c) => c.rootId)).toEqual(["n"]);
  });

  it("returns no Ohne-Epic group when every chain has an epic", () => {
    const tasks: ChainTaskLite[] = [
      task({ id: "a", status: "todo", root_id: "a", epic_id: "e_1" }),
      task({ id: "a1", status: "todo", root_id: "a", epic_id: "e_1" }),
    ];
    const groups = groupChainsByEpic(buildChains(tasks).active);
    expect(groups).toHaveLength(1);
    expect(groups[0].epicId).toBe("e_1");
  });
});

describe("projectOptions / projectLabel", () => {
  it("counts per tenant, labels known projects and puts Unsortiert last", () => {
    const tasks = [
      task({ id: "1", status: "todo", tenant: "family-organizer" }),
      task({ id: "2", status: "todo", tenant: "family-organizer" }),
      task({ id: "3", status: "review", tenant: "orchestrator" }),
      task({ id: "4", status: "done" }),
      task({ id: "5", status: "archived", tenant: "family-organizer" }), // zählt nicht
    ];
    const opts = projectOptions(tasks);
    expect(opts.map((o) => [o.key, o.label, o.count])).toEqual([
      ["family-organizer", "Family Organizer", 2],
      ["orchestrator", "Orchestrierung", 1],
      [UNSORTED_PROJECT, "Unsortiert", 1],
    ]);
  });
  it("projectKey/Label fall back honestly for unknown tenants", () => {
    expect(projectKey(null)).toBe(UNSORTED_PROJECT);
    expect(projectLabel("acme")).toBe("acme");
    expect(projectLabel(null)).toBe("Unsortiert");
  });
});
