import { describe, expect, it } from "vitest";
import {
  FLEET_BOARD_STORAGE_KEY,
  mergeBoardWorkers,
  persistFleetBoard,
  readFleetBoard,
  validateFleetBoard,
  type BoardsResponse,
} from "./multiBoard";
import type { Worker, WorkersResponse } from "./types";

function worker(runId: string, taskId: string): Worker {
  return {
    run_id: runId,
    task_id: taskId,
    task_title: taskId,
    task_status: "running",
    task_assignee: "hermes",
    profile: "coder",
    worker_pid: 1,
    started_at: 1,
    claim_lock: "lock",
    claim_expires: 2,
    last_heartbeat_at: 2,
    max_runtime_seconds: 60,
    run_status: "running",
    run_outcome: null,
  };
}

function response(workers: Worker[], cap: number | null, checkedAt: number): WorkersResponse {
  return { workers, count: workers.length, cap, checked_at: checkedAt };
}

describe("multi-board worker aggregation", () => {
  it("merges workers, tags their board, and keeps the current-board cap", () => {
    const merged = mergeBoardWorkers([
      { board: "default", response: response([worker("1", "t_default")], 3, 10) },
      { board: "health-track", response: response([worker("1", "t_health")], 7, 12) },
    ], "default");

    expect(merged.workers.map((item) => [item.task_id, item.board_slug])).toEqual([
      ["t_default", "default"],
      ["t_health", "health-track"],
    ]);
    expect(merged.count).toBe(2);
    expect(merged.cap).toBe(3);
    expect(merged.checked_at).toBe(12);
  });
});

describe("Fleet board selector state", () => {
  const catalog: BoardsResponse = {
    current: "default",
    boards: [
      { slug: "default", name: "Hermes Agent", archived: false, is_current: true, project_bound: true },
      { slug: "health-track", name: "Health Track", archived: false, is_current: false, project_bound: true },
      { slug: "internal-test", name: "Internal Test", archived: false, is_current: false, project_bound: false },
      { slug: "old", name: "Alt", archived: true, is_current: false, project_bound: true },
    ],
  };

  it("persists an explicit foreign board and clears back to today's default behavior", () => {
    const values = new Map<string, string>();
    const storage = {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => { values.set(key, value); },
      removeItem: (key: string) => { values.delete(key); },
    };
    persistFleetBoard(storage, "health-track");
    expect(readFleetBoard(storage)).toBe("health-track");
    expect(values.get(FLEET_BOARD_STORAGE_KEY)).toBe("health-track");
    persistFleetBoard(storage, null);
    expect(readFleetBoard(storage)).toBeNull();
  });

  it("rejects current, internal, archived, and missing stored boards", () => {
    expect(validateFleetBoard("health-track", catalog)).toBe("health-track");
    expect(validateFleetBoard("default", catalog)).toBeNull();
    expect(validateFleetBoard("internal-test", catalog)).toBeNull();
    expect(validateFleetBoard("old", catalog)).toBeNull();
    expect(validateFleetBoard("missing", catalog)).toBeNull();
  });

  it("falls back to a project board when the global current board is internal", () => {
    const internalCurrent: BoardsResponse = {
      ...catalog,
      current: "internal-test",
    };

    expect(validateFleetBoard(null, internalCurrent)).toBe("default");
    expect(validateFleetBoard("internal-test", internalCurrent)).toBe("default");
  });

  it("keeps active boards selectable when project metadata is unavailable", () => {
    const missingProjects: BoardsResponse = {
      ...catalog,
      boards: catalog.boards.map((board) => ({ ...board, project_bound: false })),
    };

    expect(validateFleetBoard(null, missingProjects)).toBeNull();
    expect(validateFleetBoard("default", missingProjects)).toBeNull();
    expect(validateFleetBoard("health-track", missingProjects)).toBe("health-track");
    expect(validateFleetBoard("internal-test", missingProjects)).toBe("internal-test");
    expect(validateFleetBoard("old", missingProjects)).toBeNull();
  });
});
