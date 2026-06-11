import { describe, expect, it } from "vitest";
import { refreshKeysForLiveEvent } from "./useLiveEvents";

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
