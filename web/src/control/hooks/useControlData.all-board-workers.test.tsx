// @vitest-environment jsdom

import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetPollingStore } from "./pollingStore";
import { useAllBoardWorkers } from "./useControlData";

const api = vi.hoisted(() => ({ fetchJSON: vi.fn() }));
vi.mock("@/lib/api", () => api);

function workerPayload(runId: number, taskId: string) {
  return {
    workers: [{ run_id: runId, task_id: taskId }],
    count: 1,
    cap: 3,
    checked_at: 1_783_960_000,
  };
}

describe("useAllBoardWorkers operational visibility", () => {
  beforeEach(() => {
    _resetPollingStore();
    api.fetchJSON.mockReset();
  });

  afterEach(() => {
    cleanup();
    _resetPollingStore();
  });

  it("keeps workers from an active unbound internal board in Fleet", async () => {
    api.fetchJSON.mockImplementation((url: string) => {
      if (url.endsWith("/boards")) {
        return Promise.resolve({
          boards: [
            { slug: "default", name: "Hermes Agent", archived: false, project_bound: true },
            { slug: "health-track", name: "Health Track", archived: false, project_bound: true },
            { slug: "internal-test", name: "Internal Test", archived: false, project_bound: false },
          ],
          current: "default",
        });
      }
      if (url.includes("board=health-track")) return Promise.resolve(workerPayload(2, "health-worker"));
      if (url.includes("board=internal-test")) return Promise.resolve(workerPayload(3, "diagnostic-worker"));
      return Promise.resolve(workerPayload(1, "hermes-worker"));
    });

    const { result } = renderHook(() => useAllBoardWorkers());

    await waitFor(() => expect(result.current.data?.workers).toHaveLength(3));
    expect(result.current.data?.workers.map((worker) => worker.task_id)).toEqual([
      "hermes-worker",
      "health-worker",
      "diagnostic-worker",
    ]);
    expect(result.current.data?.workers.find((worker) => worker.task_id === "diagnostic-worker")?.board_slug).toBe("internal-test");
  });
});
