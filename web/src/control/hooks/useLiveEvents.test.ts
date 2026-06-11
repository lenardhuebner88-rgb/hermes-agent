import { afterEach, describe, expect, it, vi } from "vitest";
import { refreshKeysForLiveEvent, setLivePollingMode } from "./useLiveEvents";
import { subscribe, _resetPollingStore } from "./pollingStore";

afterEach(() => {
  vi.useRealTimers();
  _resetPollingStore();
});

describe("refreshKeysForLiveEvent", () => {
  it("refreshes board and decision keys for task events", () => {
    expect(refreshKeysForLiveEvent({ id: 1, task_id: "task-1", kind: "status" })).toEqual([
      "kanban/board",
      "workers/active",
      "kanban/decision-queue",
    ]);
  });

  it("adds run-derived queues for completion and verifier events", () => {
    expect(refreshKeysForLiveEvent({ id: 2, task_id: "task-1", run_id: "run-1", kind: "verifier_request_changes" })).toEqual([
      "kanban/board",
      "workers/active",
      "kanban/decision-queue",
      "tasks/review-verdicts",
      "runs/blocked-completions",
      "runs/recent-results",
    ]);
  });

  it("refreshes epics for epic membership events", () => {
    expect(refreshKeysForLiveEvent({ id: 3, task_id: "task-1", kind: "epic_changed" })).toContain("kanban/epics");
  });
});

describe("setLivePollingMode", () => {
  it("stretches event-driven keys to 5× while live and restores 1× after", async () => {
    vi.useFakeTimers();
    _resetPollingStore();
    const loader = vi.fn().mockResolvedValue({ ok: true });
    subscribe("kanban/board", loader, 8000, vi.fn());
    await vi.advanceTimersByTimeAsync(0); // first tick
    expect(loader).toHaveBeenCalledTimes(1);

    setLivePollingMode(true);
    await vi.advanceTimersByTimeAsync(8000); // this tick was already scheduled at 1×
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(8000); // 5×: nothing yet at +8s
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(32000); // +40s reached
    expect(loader).toHaveBeenCalledTimes(3);

    setLivePollingMode(false); // disconnect → normal cadence again
    await vi.advanceTimersByTimeAsync(8000);
    expect(loader).toHaveBeenCalledTimes(4);
  });
});

describe("setLivePollingMode — status polls", () => {
  it("also stretches the always-on status polls (health/metrics/proposals)", async () => {
    vi.useFakeTimers();
    _resetPollingStore();
    const loader = vi.fn().mockResolvedValue({ ok: true });
    subscribe("health-status", loader, 5000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);

    setLivePollingMode(true);
    await vi.advanceTimersByTimeAsync(5000); // already scheduled at 1×
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(5000); // 5×: nothing yet at +5s
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(20000); // +25s reached
    expect(loader).toHaveBeenCalledTimes(3);

    setLivePollingMode(false);
    await vi.advanceTimersByTimeAsync(5000);
    expect(loader).toHaveBeenCalledTimes(4);
  });
});
