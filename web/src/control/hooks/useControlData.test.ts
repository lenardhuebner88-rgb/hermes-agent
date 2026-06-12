import { describe, expect, it } from "vitest";
import {
  countLibraryUnread,
  HERMES_RECENT_RESULTS_URL,
  HERMES_REVIEW_VERDICTS_URL,
  testFoundryStatusPollIntervalMs,
  type TestFoundryStatus,
} from "./useControlData";

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

describe("Hermes Flow enrichment queries", () => {
  it("requests the server-capped 50 review verdicts and recent results", () => {
    expect(HERMES_REVIEW_VERDICTS_URL).toBe("/api/plugins/kanban/tasks/review-verdicts?limit=50");
    expect(HERMES_RECENT_RESULTS_URL).toBe("/api/plugins/kanban/runs/recent-results?limit=50&since_hours=48&outcome=completed");
  });
});

describe("countLibraryUnread (Bibliothek-Badge)", () => {
  const items = [
    { ts: 100, category: "briefings" },
    { ts: 200, category: "wartung" },
    { ts: 300, category: "news" },
    { ts: 400 }, // Kategorie fehlt → zählt (fail-soft)
  ];

  it("zählt nur Einträge neuer als der letzte Besuch", () => {
    expect(countLibraryUnread(items, 250)).toBe(2); // news + ohne Kategorie
    expect(countLibraryUnread(items, 500)).toBe(0);
  });

  it("ignoriert wartung-Einträge (Routine-Rauschen bumpt das Badge nicht)", () => {
    expect(countLibraryUnread(items, 50)).toBe(3); // alle außer wartung
    expect(countLibraryUnread([{ ts: 999, category: "wartung" }], 50)).toBe(0);
  });

  it("Erstbesuch ohne Stempel zählt nichts", () => {
    expect(countLibraryUnread(items, 0)).toBe(0);
  });
});
