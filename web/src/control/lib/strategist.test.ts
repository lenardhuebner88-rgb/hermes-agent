import { describe, expect, it } from "vitest";
import {
  humanizeMetricKey,
  metricSnapshotRows,
  proposalSource,
  sourceLabel,
} from "./strategist";

// Realistic nested snapshot shape matching the live vision-metrics.json envelope.
const NESTED_SNAPSHOT = {
  schema_version: 2,
  generated_at: "2026-06-21T06:00:00Z",
  generated_epoch: 1781290800,
  window_days: 7,
  metrics: {
    autonomy: { autonomy_pct: 97.1, autonomous_done: 68, total_done: 70, counter: null },
    green_gate_streak: { streak: 12 },
    escalation_rate: { escalations_per_week: 2 },
    cost_per_task: { recent_avg_cost_per_task: 0.12, trend: "stable" },
    classification_coverage: { coverage_pct: 88.5 },
  },
};

describe("metricSnapshotRows", () => {
  it("returns [] for null/undefined", () => {
    expect(metricSnapshotRows(null)).toEqual([]);
    expect(metricSnapshotRows(undefined)).toEqual([]);
  });

  it("returns [] for an empty object", () => {
    expect(metricSnapshotRows({})).toEqual([]);
  });

  it("produces exactly the 5 curated rows from a nested snapshot in correct order", () => {
    const rows = metricSnapshotRows(NESTED_SNAPSHOT);
    expect(rows).toHaveLength(5);
    expect(rows[0]).toEqual({ key: "autonomy.autonomy_pct",                          label: "Autonomie",              value: "97.1%" });
    expect(rows[1]).toEqual({ key: "green_gate_streak.streak",                       label: "Green-Gate-Streak",      value: "12" });
    expect(rows[2]).toEqual({ key: "escalation_rate.escalations_per_week",           label: "Eskalationen/Woche",     value: "2" });
    expect(rows[3]).toEqual({ key: "cost_per_task.recent_avg_cost_per_task",         label: "Kosten/Aufgabe",         value: "$0.12" });
    expect(rows[4]).toEqual({ key: "classification_coverage.coverage_pct",           label: "Klassifik.-Abdeckung",   value: "88.5%" });
  });

  it("skips rows whose nested path is missing — no throw", () => {
    const partial = {
      metrics: {
        autonomy: { autonomy_pct: 62 },
        // green_gate_streak missing entirely
        escalation_rate: { escalations_per_week: 5 },
        cost_per_task: { recent_avg_cost_per_task: 0.08 },
        classification_coverage: { coverage_pct: 75 },
      },
    };
    const rows = metricSnapshotRows(partial);
    expect(rows).toHaveLength(4);
    expect(rows.find((r) => r.label === "Green-Gate-Streak")).toBeUndefined();
  });

  it("does not emit envelope noise rows (schema_version, generated_at, etc.)", () => {
    const rows = metricSnapshotRows(NESTED_SNAPSHOT);
    const keys = rows.map((r) => r.key);
    expect(keys).not.toContain("schema_version");
    expect(keys).not.toContain("generated_at");
    expect(keys).not.toContain("window_days");
    expect(keys).not.toContain("generated_epoch");
  });

  it("does not emit a JSON-stringified metrics blob as a row value", () => {
    const rows = metricSnapshotRows(NESTED_SNAPSHOT);
    for (const row of rows) {
      expect(row.value).not.toContain("{");
    }
  });

  it("falls back gracefully when the input has no inner metrics key (flat shape)", () => {
    // Safety net for old flat snapshots.
    const flat = {
      autonomy: { autonomy_pct: 50 },
      green_gate_streak: { streak: 1 },
      escalation_rate: { escalations_per_week: 3 },
      cost_per_task: { recent_avg_cost_per_task: 0.05 },
      classification_coverage: { coverage_pct: 60 },
    };
    const rows = metricSnapshotRows(flat);
    expect(rows).toHaveLength(5);
  });
});

describe("humanizeMetricKey", () => {
  it("title-cases snake and kebab keys", () => {
    expect(humanizeMetricKey("cost_per_task")).toBe("Cost Per Task");
    expect(humanizeMetricKey("a-b-c")).toBe("A B C");
  });
});

describe("proposalSource (legacy)", () => {
  it("labels the strategist cron and falls back to raw/created-by", () => {
    expect(proposalSource("strategist-cron")).toBe("Stratege");
    expect(proposalSource(null)).toBe("Stratege");
    expect(proposalSource("someone-else")).toBe("someone-else");
  });
});

describe("sourceLabel", () => {
  it("maps receipt → Aus eurer Arbeit", () => {
    expect(sourceLabel("receipt")).toBe("Aus eurer Arbeit");
  });

  it("maps gate → Gate-Heilung", () => {
    expect(sourceLabel("gate")).toBe("Gate-Heilung");
  });

  it("maps metric → Aus Kennzahl", () => {
    expect(sourceLabel("metric")).toBe("Aus Kennzahl");
  });

  it("maps other/null/undefined → Stratege", () => {
    expect(sourceLabel("other")).toBe("Stratege");
    expect(sourceLabel(null)).toBe("Stratege");
    expect(sourceLabel(undefined)).toBe("Stratege");
    expect(sourceLabel("unknown-type")).toBe("Stratege");
  });
});
