import { describe, expect, it } from "vitest";
import {
  statusToStage,
  stageActions,
  stageGuard,
  isActionableStatus,
  roleChip,
  buildPipeline,
  groupByStage,
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
});
