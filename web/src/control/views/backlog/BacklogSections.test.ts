import { describe, it, expect } from "vitest";
import { partitionReadinessZones } from "./readinessZones";
import type { BacklogItem } from "../../lib/schemas";
import { STATUS_TONE } from "./shared";

function item(overrides: Partial<BacklogItem> & { id: string }): BacklogItem {
  return {
    title: `Task ${overrides.id}`,
    status: "next",
    owner: "claude",
    risk: "low",
    area: "lists",
    updated: "2026-06-01",
    lane: null,
    result: null,
    stale: false,
    excerpt: undefined,
    ...overrides,
  };
}

describe("partitionReadinessZones", () => {
  it("maps non-judging archive lifecycle states to neutral", () => {
    expect(STATUS_TONE.deferred).toBe("zinc");
    expect(STATUS_TONE.superseded).toBe("zinc");
    expect(STATUS_TONE.archived).toBe("zinc");
  });

  it("empty input returns three empty zones", () => {
    const { ready, grooming, ideas } = partitionReadinessZones([]);
    expect(ready).toHaveLength(0);
    expect(grooming).toHaveLength(0);
    expect(ideas).toHaveLength(0);
  });

  it("a clean item (no quality issues, clean status) lands in ready", () => {
    const { ready, grooming, ideas } = partitionReadinessZones([
      item({ id: "0001", owner: "claude", status: "next" }),
    ]);
    expect(ready).toHaveLength(1);
    expect(grooming).toHaveLength(0);
    expect(ideas).toHaveLength(0);
    expect(ready[0].id).toBe("0001");
  });

  it("an item with server readiness=ready lands in ready", () => {
    const { ready, grooming } = partitionReadinessZones([
      item({ id: "0001", readiness: "ready" }),
    ]);
    expect(ready).toHaveLength(1);
    expect(grooming).toHaveLength(0);
  });

  it("an item with server readiness=needs_grooming lands in grooming", () => {
    const { ready, grooming, ideas } = partitionReadinessZones([
      item({ id: "0001", readiness: "needs_grooming" }),
    ]);
    expect(grooming).toHaveLength(1);
    expect(ready).toHaveLength(0);
    expect(ideas).toHaveLength(0);
  });

  it("an item with readiness=drift lands in grooming (not ideas)", () => {
    // drift = unknown status → grooming zone so operator can fix contract issues
    const { grooming, ideas } = partitionReadinessZones([
      item({ id: "0001", status: "readyish" as BacklogItem["status"] }),
    ]);
    expect(grooming).toHaveLength(1);
    expect(ideas).toHaveLength(0);
  });

  // Regression (new behavior): blocked = needs external unblocking = Schleifen,
  // not Ideenspeicher. The operator can act on it.
  it("an item with readiness=blocked lands in grooming (Schleifen), not ideas", () => {
    const { ready, grooming, ideas } = partitionReadinessZones([
      item({ id: "0001", status: "blocked" }),
    ]);
    expect(grooming).toHaveLength(1);
    expect(ready).toHaveLength(0);
    expect(ideas).toHaveLength(0);
  });

  it("every item lands in exactly ONE zone (disjoint + complete)", () => {
    const items = [
      item({ id: "0001", readiness: "ready" }),
      item({ id: "0002", readiness: "needs_grooming" }),
      item({ id: "0003", status: "blocked" }),               // readiness="blocked" → grooming
      item({ id: "0004", status: "readyish" as BacklogItem["status"] }), // drift → grooming
      item({ id: "0005", readiness: "blocked" }),            // explicit → grooming
    ];
    const { ready, grooming, ideas } = partitionReadinessZones(items);
    // Completeness: all items appear somewhere
    expect(ready.length + grooming.length + ideas.length).toBe(items.length);
    // Disjoint: no id appears in two zones
    const allIds = [...ready, ...grooming, ...ideas].map((it) => it.id);
    expect(new Set(allIds).size).toBe(items.length);
  });

  it("multiple items of each kind are all placed correctly", () => {
    // Use explicit server readiness fields to make placements deterministic and
    // independent of v1 client-heuristic edge cases.
    const items = [
      item({ id: "r1", readiness: "ready" }),
      item({ id: "r2", owner: "claude", status: "next" }),   // v1 fallback, no issues → ready
      item({ id: "g1", readiness: "needs_grooming" }),
      item({ id: "g2", status: "readyish" as BacklogItem["status"] }), // drift → grooming
      item({ id: "g3", status: "blocked" }),                 // readiness="blocked" → grooming
      item({ id: "g4", readiness: "blocked" }),              // explicit → grooming
      item({ id: "i1", status: "done" }),                    // done → ideas
      item({ id: "i2", status: "in_progress", owner: "claude", readiness: "ready" }), // in_progress is never Bereit
    ];
    const { ready, grooming, ideas } = partitionReadinessZones(items);
    expect(ready.map((it) => it.id).sort()).toEqual(["r1", "r2"].sort());
    expect(grooming.map((it) => it.id).sort()).toEqual(["g1", "g2", "g3", "g4"].sort());
    expect(ideas.map((it) => it.id).sort()).toEqual(["i1", "i2"].sort());
  });

  it("preserves original item references (no copies)", () => {
    const src = item({ id: "0001", readiness: "ready" });
    const { ready } = partitionReadinessZones([src]);
    expect(ready[0]).toBe(src);
  });

  // Regression (new behavior): a `later` item with server readiness==="ready" is a
  // parked-but-done task that the backend promoted. It now belongs in Bereit, not
  // Ideenspeicher. Readiness drives the zone, not status.
  it("a later item with readiness=ready lands in Bereit (not ideas)", () => {
    const laterReady = item({ id: "park1", status: "later", readiness: "ready" });
    const { ready, grooming, ideas } = partitionReadinessZones([laterReady]);
    expect(ready).toHaveLength(1);
    expect(grooming).toHaveLength(0);
    expect(ideas).toHaveLength(0);
    expect(ready[0].id).toBe("park1");
  });

  // Regression: a `later` item whose v1-readiness would be "ready" (no quality
  // issues, no server readiness field) also lands in Bereit now.
  it("a later item whose v1-readiness is 'ready' (no server field) lands in Bereit", () => {
    const laterClean = item({ id: "idea1", status: "later", owner: "claude" });
    const { ready, grooming, ideas } = partitionReadinessZones([laterClean]);
    expect(ready).toHaveLength(1);
    expect(grooming).toHaveLength(0);
    expect(ideas).toHaveLength(0);
    expect(ready[0].id).toBe("idea1");
  });

  // Regression: a `later` item with readiness=needs_grooming lands in Schleifen.
  it("a later item with readiness=needs_grooming lands in grooming (Schleifen)", () => {
    const laterGroom = item({ id: "park2", status: "later", readiness: "needs_grooming" });
    const { ready, grooming, ideas } = partitionReadinessZones([laterGroom]);
    expect(grooming).toHaveLength(1);
    expect(ready).toHaveLength(0);
    expect(ideas).toHaveLength(0);
  });

  it("done and in_progress items always go to ideas regardless of readiness", () => {
    const inputs = [
      item({ id: "dn", status: "done" }),
      item({ id: "ip", status: "in_progress", readiness: "ready" }), // even if ready
    ];
    const { ready, grooming, ideas } = partitionReadinessZones(inputs);
    expect(ideas).toHaveLength(2);
    expect(ready).toHaveLength(0);
    expect(grooming).toHaveLength(0);
  });
});
