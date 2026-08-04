import { describe, expect, it } from "vitest";

import type { PlanSpecRecord } from "../../lib/types";
import { planSpecActionState, planSpecChainStateLabel, planSpecClosedDispositionLabel, planSpecFindingIsStatusEcho, planSpecIsClosed, planSpecKanbanLabel, planSpecStateChipLabel, planSpecVisibleFindingCount } from "./planSpecKanban";

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

    // Live-Record 2026-08-04 (Abo-Limits-Cockpit): Hub projiziert
    // closed_reason „closed status: done" bei kanban_state „not_ingested".
    // Liste („Fertig" aus action_state) und Detail müssen denselben
    // Endzustand zeigen.
    const statusEchoDone = record({
      open: false,
      status: "done",
      closed_reason: "closed status: done",
      kanban_state: "not_ingested",
      action_state: "completed",
    });
    expect(planSpecKanbanLabel(statusEchoDone)).toBe("erledigt");

    const statusEchoArchived = record({
      open: false,
      status: "archived",
      closed_reason: "closed status: archived",
      kanban_state: "not_ingested",
      action_state: "archived",
    });
    expect(planSpecKanbanLabel(statusEchoArchived)).toBe("archiviert");
  });

  it("keeps the open fallbacks for genuinely open records", () => {
    expect(planSpecKanbanLabel(record({ open: true, valid: true }))).toBe("offen");
    expect(planSpecKanbanLabel(record({ open: true, valid: false }))).toBe("blocked");
  });
});

describe("planSpec findings — Status-Echo ist kein Befund", () => {
  it("erkennt das Abschluss-Echo in allen Groß-/Kleinschreibungen", () => {
    expect(planSpecFindingIsStatusEcho("closed status: done")).toBe(true);
    expect(planSpecFindingIsStatusEcho("  Closed Status: archived")).toBe(true);
    expect(planSpecFindingIsStatusEcho("scope-less code subtask: ER-S2")).toBe(false);
    expect(planSpecFindingIsStatusEcho("lane coder nicht auflösbar")).toBe(false);
  });

  it("zählt das Echo nicht als sichtbaren Befund — Live-Form finding_count=1", () => {
    // Live-Befund 2026-08-04: 399 abgeschlossene PlanSpecs trugen
    // finding_count=1 mit genau diesem Echo in ingest_findings.
    const item = record({
      open: false,
      closed_reason: "closed status: done",
      action_state: "completed",
      ingest_findings: ["closed status: done"],
      finding_count: 1,
    });
    expect(planSpecVisibleFindingCount(item)).toBe(0);

    const mitEchtemBefund = record({
      open: false,
      closed_reason: "closed status: done",
      action_state: "completed",
      ingest_findings: ["closed status: done", "scope-less code subtask: ER-S2"],
      finding_count: 2,
    });
    expect(planSpecVisibleFindingCount(mitEchtemBefund)).toBe(1);

    expect(planSpecVisibleFindingCount(record({ finding_count: 0 }))).toBe(0);
    expect(planSpecVisibleFindingCount(record({ finding_count: undefined }))).toBe(0);
  });
});

describe("planSpecActionState — action_state ist die Wahrheit, kein Fallback (V1)", () => {
  it("leitet nichts aus Kanban-Feldern ab: fehlendes action_state wird „unknown“", () => {
    // Genau diese Fälle malte der entfernte Fallback falsch — die
    // Backend-Reparaturen vom 2026-07-29/30 (geschlossene Specs als
    // „Blockiert"/„Im Board") wären im Frontend wiederauferstanden.
    expect(planSpecActionState(record({
      action_state: undefined,
      open: false,
      closed_reason: "closed status: done",
      kanban_state: "not_ingested",
    }))).toBe("unknown");
    expect(planSpecActionState(record({
      action_state: undefined,
      kanban_root_task_id: "t_1",
      kanban_state: "queued",
      kanban_root_status: "scheduled",
    }))).toBe("unknown");
    expect(planSpecActionState(record({ action_state: undefined, valid: false }))).toBe("unknown");
    expect(planSpecActionState(record({ action_state: "ready" }))).toBe("ready");
  });

  it("zeigt ohne action_state kein abgeleitetes Chip-Label", () => {
    expect(planSpecStateChipLabel(record({
      action_state: undefined,
      open: false,
      closed_reason: "closed status: done",
      kanban_state: "not_ingested",
    }))).toBe("ohne Zustand");
    expect(planSpecStateChipLabel(record({
      action_state: "completed",
      open: false,
      closed_reason: "closed status: done",
      kanban_state: "not_ingested",
    }))).toBe("erledigt");
  });
});

describe("planSpecChainStateLabel — Ausführungsachse getrennt (V2)", () => {
  it("beschriftet den Kettenlauf nur bei vorhandener Wurzel", () => {
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: null, kanban_state: "running" }))).toBeNull();
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "running" }))).toBe("läuft");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "blocked" }))).toBe("blockiert");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "queued", kanban_root_status: "scheduled" }))).toBe("gehalten");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "queued", kanban_root_status: null }))).toBe("geplant");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "completed" }))).toBe("fertig");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "archived" }))).toBe("archiviert");
    expect(planSpecChainStateLabel(record({ kanban_root_task_id: "t_1", kanban_state: "not_ingested" }))).toBeNull();
  });
});
