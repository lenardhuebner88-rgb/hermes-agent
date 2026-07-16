// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { refreshKeysForLiveEvent, setLivePollingMode, useLiveEvents } from "./useLiveEvents";
import { subscribe, _resetPollingStore } from "./pollingStore";

vi.mock("@/lib/api", () => ({ buildWsUrl: vi.fn().mockResolvedValue("ws://example.test/kanban-events") }));
vi.mock("./useControlData", () => ({ boardLoader: vi.fn().mockResolvedValue({ latest_event_id: 1 }) }));

let sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  close = vi.fn();

  constructor() {
    sockets.push(this);
  }
}

beforeEach(() => {
  sockets = [];
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
  global.WebSocket = FakeWebSocket as unknown as typeof WebSocket;
});

afterEach(() => {
  cleanup();
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

describe("useLiveEvents connection state", () => {
  it("transitions from reconnecting to connected and back after a socket error", async () => {
    const { result } = renderHook(() => useLiveEvents());

    expect(result.current).toBe("reconnecting");
    await waitFor(() => expect(sockets).toHaveLength(1));

    act(() => sockets[0].onopen?.(new Event("open")));
    expect(result.current).toBe("connected");

    act(() => sockets[0].onerror?.(new Event("error")));
    expect(result.current).toBe("reconnecting");
  });
});
