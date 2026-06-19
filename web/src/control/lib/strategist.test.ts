import { describe, expect, it } from "vitest";
import {
  humanizeMetricKey,
  metricSnapshotRows,
  proposalSource,
} from "./strategist";

describe("metricSnapshotRows", () => {
  it("returns [] for null/empty/invalid snapshots", () => {
    expect(metricSnapshotRows(null)).toEqual([]);
    expect(metricSnapshotRows(undefined)).toEqual([]);
    expect(metricSnapshotRows({})).toEqual([]);
  });

  it("maps known keys to friendly labels and percent-suffixes rate keys", () => {
    const rows = metricSnapshotRows({ autonomy_pct: 73, green_gate_streak: 4 });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
    expect(byKey.autonomy_pct.label).toBe("Autonomie-%");
    expect(byKey.autonomy_pct.value).toBe("73%");
    expect(byKey.green_gate_streak.label).toBe("Green-Gate-Streak");
    expect(byKey.green_gate_streak.value).toBe("4");
  });

  it("humanizes unknown keys instead of hiding them", () => {
    const rows = metricSnapshotRows({ some_new_metric: 5 });
    expect(rows[0].label).toBe("Some New Metric");
    expect(rows[0].value).toBe("5");
  });

  it("renders strings, booleans and nested values", () => {
    const rows = metricSnapshotRows({
      window: "7d",
      paused: false,
      counter: { a: 1 },
    });
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r.value]));
    expect(byKey.window).toBe("7d");
    expect(byKey.paused).toBe("nein");
    expect(byKey.counter).toBe('{"a":1}');
  });
});

describe("humanizeMetricKey", () => {
  it("title-cases snake and kebab keys", () => {
    expect(humanizeMetricKey("cost_per_task")).toBe("Cost Per Task");
    expect(humanizeMetricKey("a-b-c")).toBe("A B C");
  });
});

describe("proposalSource", () => {
  it("labels the strategist cron and falls back to raw/created-by", () => {
    expect(proposalSource("strategist-cron")).toBe("Stratege");
    expect(proposalSource(null)).toBe("Stratege");
    expect(proposalSource("someone-else")).toBe("someone-else");
  });
});
