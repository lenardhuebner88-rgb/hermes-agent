import { describe, expect, it } from "vitest";

import type { PlanSpecRecord } from "../../lib/types";
import { planSpecClosedDispositionLabel, planSpecIsClosed, planSpecKanbanLabel } from "./planSpecKanban";

function record(overrides: Partial<PlanSpecRecord>): PlanSpecRecord {
  return {
    path: "plans/example.md",
    agent: "Hermes",
    filename: "example.md",
    topic: "Example",
    status: "ready",
    open: true,
    valid: true,
    freigabe: "GO",
    live_test_depth: null,
    binding: false,
    subtask_count: 1,
    errors: [],
    closed_reason: null,
    kanban_root_task_id: null,
    kanban_root_status: null,
    kanban_state: "not_ingested",
    kanban_child_total: 0,
    kanban_child_done: 0,
    kanban_child_blocked: 0,
    kanban_child_running: 0,
    kanban_ingested_at: null,
    ingest_disposition: "clean",
    ingest_would_block: false,
    ingest_findings: [],
    action_state: "ready",
    ...overrides,
  };
}

describe("planSpecKanban closed provenance", () => {
  it("keeps open specs visibly open", () => {
    const item = record({ open: true, valid: true });

    expect(planSpecIsClosed(item)).toBe(false);
    expect(planSpecClosedDispositionLabel(item)).toBe("open");
  });

  it.each([
    ["obsolete/not-needed", record({ open: false, status: "obsolete", closed_reason: "not needed anymore" })],
    ["shipped", record({ open: false, status: "shipped", closed_reason: "shipped" })],
    ["kanban-completed", record({ open: false, status: "ready", kanban_state: "completed", kanban_root_status: "completed" })],
    ["kanban-archived", record({ open: false, status: "ready", kanban_state: "archived", kanban_root_status: "archived" })],
  ])("labels %s closed provenance", (label, item) => {
    expect(planSpecIsClosed(item)).toBe(true);
    expect(planSpecClosedDispositionLabel(item)).toBe(label);
  });

  it("does not collapse archived kanban state into not-ingested wording", () => {
    expect(planSpecKanbanLabel(record({ kanban_state: "archived" }))).toBe("archiviert");
  });

  it("never calls a closed plan offen or blocked — ruhige deutsche Endzustands-Label", () => {
    // Echte Vault-Formen (Bestand 2026-07-29): Freitext in closed_reason/status,
    // kanban_state ohne Board-Endzustand. Genau diese Records fielen vorher auf
    // „offen" zurück — direkt neben einem COMPLETE-Badge.
    const shipped = record({
      open: false,
      status: "shipped",
      closed_reason: "SHIPPED 2026-06-19 (89527c494 - lane-scope fix)",
      kanban_state: "not_ingested",
      action_state: "completed",
    });
    expect(planSpecKanbanLabel(shipped)).toBe("ausgeliefert");

    const obsolete = record({
      open: false,
      status: "obsolete - fix shipped+dogfood-proven 2026-06-18",
      closed_reason: null,
      kanban_state: "not_ingested",
      action_state: "completed",
    });
    expect(planSpecKanbanLabel(obsolete)).toBe("obsolet");

    const notNeeded = record({
      open: false,
      status: "closed",
      closed_reason: "not needed anymore",
      kanban_state: "not_ingested",
      action_state: "archived",
    });
    expect(planSpecKanbanLabel(notNeeded)).toBe("obsolet");

    const unknownClosed = record({
      open: false,
      status: "closed",
      closed_reason: "weg damit",
      kanban_state: "not_ingested",
      action_state: "archived",
    });
    expect(planSpecKanbanLabel(unknownClosed)).toBe("geschlossen");
  });

  it("keeps the open fallbacks for genuinely open records", () => {
    expect(planSpecKanbanLabel(record({ open: true, valid: true }))).toBe("offen");
    expect(planSpecKanbanLabel(record({ open: true, valid: false }))).toBe("blocked");
  });
});
