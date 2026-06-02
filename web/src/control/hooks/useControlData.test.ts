import { describe, expect, it } from "vitest";
import { testFoundryStatusPollIntervalMs, type TestFoundryStatus } from "./useControlData";

function status(state: TestFoundryStatus["state"]): TestFoundryStatus {
  return {
    schema: "test-foundry-status-v1",
    state,
    pid: state === "running" ? 123 : null,
    target: "hermes_cli/kanban_db.py",
    started_at: state === "running" ? "2026-06-02T23:00:00Z" : null,
    last_run: null,
  };
}

describe("useTestFoundry status polling", () => {
  it("polls every 5s only while the backend reports running", () => {
    expect(testFoundryStatusPollIntervalMs(null)).toBeNull();
    expect(testFoundryStatusPollIntervalMs(status("idle"))).toBeNull();
    expect(testFoundryStatusPollIntervalMs(status("error"))).toBeNull();
    expect(testFoundryStatusPollIntervalMs(status("running"))).toBe(5000);
  });
});
