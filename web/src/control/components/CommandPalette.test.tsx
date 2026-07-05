import { describe, expect, it, vi } from "vitest";
import { buildCommandPaletteItems, filterCommandPaletteItems, type CommandPaletteSnapshots } from "./commandPaletteItems";
import type { Worker } from "../lib/types";

const noop = vi.fn();

const snapshots: CommandPaletteSnapshots = {
  board: {
    columns: [
      {
        name: "running",
        tasks: [
          {
            id: "task-42",
            title: "Command Center live machen",
            status: "running",
            assignee: "codex",
            priority: 1,
            created_at: 1,
            started_at: 2,
            completed_at: null,
            branch_name: null,
            latest_summary: null,
            link_counts: { parents: 0, children: 0 },
            comment_count: 0,
            progress: null,
            age: null,
            tenant: "hermes",
            root_id: "task-42",
            epic_id: "epic-1",
          },
        ],
      },
    ],
    tenants: [],
    assignees: [],
    latest_event_id: 7,
    source_errors: [],
    now: 10,
  },
  crons: {
    schema: "cron-observability-v1",
    checked_at: 1,
    gateway: { running: true, pids: [] },
    jobs: [
      {
        id: "nightly-sweep",
        name: "Nightly Sweep",
        enabled: true,
        state: "ready",
        paused_at: null,
        paused_reason: null,
        schedule_display: "0 4 * * *",
        repeat: null,
        next_run_at: null,
        last_run_at: null,
        last_status: "ok",
        last_error: null,
        last_delivery_error: null,
        deliver: null,
        skill: null,
        model: null,
        profile: "default",
        is_default_profile: true,
        has_script: false,
        has_prompt: true,
        latest_output: null,
      },
    ],
  },
  backlog: {
    schema: "fo-backlog-v1",
    checked_at: 1,
    items: [{ id: "0063", title: "Einkaufsliste stabilisieren", status: "now", owner: "piet", risk: "med", area: "shopping", updated: "", lane: null, result: null, stale: false }],
    counts: { now: 1, next: 0, in_progress: 0, blocked: 0, later: 0, done: 0 },
    source: { dir: "", ref: "", count: 1 },
    error: null,
  },
  orchestration: {
    schema: "orchestration-backlog-v1",
    checked_at: 1,
    items: [{ id: "orch-7", title: "Lane koordinieren", status: "doing", priority: "high", dependsOn: [], planGate: false, created: "" }],
    counts: { backlog: 0, todo: 0, doing: 1, review: 0, done: 0 },
    contract_health: { source_count: 1, counted_sum: 1, unknown_statuses: [], invalid_priority_count: 0, missing_dep_count: 0 },
    source: { dir: "", ref: "", count: 1 },
    error: null,
  },
  epics: {
    epics: [{ id: "epic-1", title: "Control Center", body: null, status: "open", created_at: 1, closed_at: null, task_count: 3, open_tasks: 2, done_tasks: 1, cost_usd: null, input_tokens: null, output_tokens: null }],
    count: 1,
  },
};

describe("CommandPalette search index", () => {
  it("indexes tasks, crons, backlog items, epics and navigation", () => {
    const navigate = vi.fn();
    const items = buildCommandPaletteItems({ workers: [], snapshots, onNavigate: navigate, onGenerate: noop, onApplyAll: noop });

    expect(filterCommandPaletteItems(items, "task-42")[0]?.group).toBe("Tasks");
    expect(filterCommandPaletteItems(items, "nightly")[0]?.group).toBe("Crons");
    expect(filterCommandPaletteItems(items, "0063")[0]?.hint).toContain("now");
    expect(filterCommandPaletteItems(items, "Control Center")[0]?.group).toBe("Epics");
    expect(filterCommandPaletteItems(items, "g f").some((item) => item.label === "Fleet")).toBe(true);

    filterCommandPaletteItems(items, "task-42")[0]?.action();
    expect(navigate).toHaveBeenCalledWith("/control/fleet?task=task-42");
  });

  it("caps each group at eight matches", () => {
    const workers: Worker[] = Array.from({ length: 12 }, (_, index) => ({
      run_id: `run-${index}`,
      task_id: `task-${index}`,
      task_title: `worker match ${index}`,
      task_status: "running",
      task_assignee: "codex",
      profile: "coder",
      worker_pid: null,
      started_at: 1,
      claim_lock: "",
      claim_expires: 2,
      last_heartbeat_at: 1,
      max_runtime_seconds: 100,
      run_status: "running",
      run_outcome: null,
    }));
    const items = buildCommandPaletteItems({ workers, snapshots: { ...snapshots, board: null, crons: null, backlog: null, orchestration: null, epics: null }, onNavigate: noop, onGenerate: noop, onApplyAll: noop });

    expect(filterCommandPaletteItems(items, "worker match").filter((item) => item.group === "Worker")).toHaveLength(8);
  });
});
