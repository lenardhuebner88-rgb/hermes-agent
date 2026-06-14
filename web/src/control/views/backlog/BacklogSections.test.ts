import { describe, it, expect } from "vitest";
import { partitionReadinessZones } from "./readinessZones";
import type { BacklogItem } from "../../lib/schemas";

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

  it("an item with readiness=blocked lands in ideas (not ready, not grooming)", () => {
    const { ready, grooming, ideas } = partitionReadinessZones([
      item({ id: "0001", status: "blocked" }),
    ]);
    expect(ideas).toHaveLength(1);
    expect(ready).toHaveLength(0);
    expect(grooming).toHaveLength(0);
  });

  it("every item lands in exactly ONE zone (disjoint + complete)", () => {
    const items = [
      item({ id: "0001", readiness: "ready" }),
      item({ id: "0002", readiness: "needs_grooming" }),
      item({ id: "0003", status: "blocked" }),               // v1 → "blocked" → ideas
      item({ id: "0004", status: "readyish" as BacklogItem["status"] }), // drift → grooming
      item({ id: "0005", readiness: "blocked" }),            // explicit → ideas
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
      item({ id: "i1", status: "blocked" }),                 // v1 fallback → "blocked" → ideas
      item({ id: "i2", readiness: "blocked" }),              // explicit → ideas
    ];
    const { ready, grooming, ideas } = partitionReadinessZones(items);
    expect(ready.map((it) => it.id).sort()).toEqual(["r1", "r2"].sort());
    expect(grooming.map((it) => it.id).sort()).toEqual(["g1", "g2"].sort());
    expect(ideas.map((it) => it.id).sort()).toEqual(["i1", "i2"].sort());
  });

  it("preserves original item references (no copies)", () => {
    const src = item({ id: "0001", readiness: "ready" });
    const { ready } = partitionReadinessZones([src]);
    expect(ready[0]).toBe(src);
  });

  // Regression: v1-fallback bug — readinessForFoItem returns "ready" for a clean
  // `later` item (no quality issues, no server readiness). Without the status guard,
  // such items would appear in Bereit instead of Ideenspeicher.
  it("a later item whose v1-readiness would be 'ready' lands in ideas, never in ready", () => {
    // No readiness field → v1 fallback fires. No quality issues → fallback returns "ready".
    // Status guard must intercept before readinessForFoItem is used for zone placement.
    const laterClean = item({ id: "idea1", status: "later", owner: "claude" });
    const { ready, grooming, ideas } = partitionReadinessZones([laterClean]);
    expect(ideas).toHaveLength(1);
    expect(ready).toHaveLength(0);
    expect(grooming).toHaveLength(0);
    expect(ideas[0].id).toBe("idea1");
  });

  it("excluded-status items (later, done, in_progress, blocked) all go to ideas", () => {
    const inputs = [
      item({ id: "lt", status: "later" }),
      item({ id: "dn", status: "done" }),
      item({ id: "ip", status: "in_progress" }),
      item({ id: "bl", status: "blocked" }),
    ];
    const { ready, grooming, ideas } = partitionReadinessZones(inputs);
    expect(ideas).toHaveLength(4);
    expect(ready).toHaveLength(0);
    expect(grooming).toHaveLength(0);
  });
});
