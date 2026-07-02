import { describe, expect, it } from "vitest";
import {
  formatSignedDelta,
  humanizeMetricKey,
  isStrategistAuthored,
  metricSnapshotRows,
  outcomeDeltaValue,
  outcomeStatusLabel,
  outcomeVerdictLabel,
  outcomeVerdictToneClass,
  partitionProposals,
  proposalSource,
  runSummaryText,
  sourceLabel,
  type StrategistProposal,
} from "./strategist";
import type { LeverOutcome } from "./schemas";

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

describe("runSummaryText", () => {
  it("formats a harvest run", () => {
    expect(runSummaryText("harvest", { ts: 1, receipts: 5, candidates: 2 })).toBe("5 Receipts → 2 Vorschläge");
  });
  it("formats a propose run", () => {
    expect(runSummaryText("propose", { ts: 1, candidates: 4, ingested: 1 })).toBe("1 Vorschlag");
  });
  it("handles null", () => {
    expect(runSummaryText("harvest", null)).toBe("noch nicht gelaufen");
  });
});

describe("isStrategistAuthored", () => {
  it("recognises the strategist control-plane authors", () => {
    expect(isStrategistAuthored("strategist-cron")).toBe(true);
    expect(isStrategistAuthored("green-gate-autoheal")).toBe(true);
  });

  it("tolerates surrounding whitespace", () => {
    expect(isStrategistAuthored("  strategist-cron  ")).toBe(true);
  });

  it("treats a hand-ingested operator author as NOT strategist", () => {
    expect(isStrategistAuthored("Hermes Orchestrator / Piet GO ingest only")).toBe(false);
    expect(isStrategistAuthored("operator-disposition")).toBe(false);
  });

  it("treats null/empty as NOT strategist", () => {
    expect(isStrategistAuthored(null)).toBe(false);
    expect(isStrategistAuthored(undefined)).toBe(false);
    expect(isStrategistAuthored("")).toBe(false);
  });
});

describe("partitionProposals", () => {
  const mk = (id: string, created_by: string | null): StrategistProposal => ({
    id,
    title: `PlanSpec ${id}: x`,
    created_by,
    created_at: 1,
    subtask_count: 0,
    target_metric: null,
    roi: null,
    counter_metric: null,
    grounding: null,
  });

  it("splits strategist-authored from hand-ingested proposals", () => {
    const { strategist, manual } = partitionProposals([
      mk("a", "strategist-cron"),
      mk("b", "Hermes Orchestrator / Piet GO ingest only"),
      mk("c", "green-gate-autoheal"),
      mk("d", null),
    ]);
    expect(strategist.map((p) => p.id)).toEqual(["a", "c"]);
    expect(manual.map((p) => p.id)).toEqual(["b", "d"]);
  });

  it("preserves input order within each group", () => {
    const { manual } = partitionProposals([
      mk("first", "operator-x"),
      mk("second", "operator-y"),
    ]);
    expect(manual.map((p) => p.id)).toEqual(["first", "second"]);
  });

  it("returns two empty groups for an empty input", () => {
    expect(partitionProposals([])).toEqual({ strategist: [], manual: [] });
  });
});

describe("outcomeStatusLabel", () => {
  it("maps the three lifecycle statuses", () => {
    expect(outcomeStatusLabel("proposed")).toBe("Vorgeschlagen");
    expect(outcomeStatusLabel("shipped")).toBe("Geshippt");
    expect(outcomeStatusLabel("measured")).toBe("Gemessen");
  });

  it("falls back to the raw value for unknown statuses, and null → Unbekannt", () => {
    expect(outcomeStatusLabel("weird-status")).toBe("weird-status");
    expect(outcomeStatusLabel(null)).toBe("Unbekannt");
    expect(outcomeStatusLabel(undefined)).toBe("Unbekannt");
  });
});

describe("outcomeVerdictLabel / outcomeVerdictToneClass", () => {
  it("labels all four verdicts", () => {
    expect(outcomeVerdictLabel("improved")).toBe("verbessert");
    expect(outcomeVerdictLabel("worsened")).toBe("verschlechtert");
    expect(outcomeVerdictLabel("unchanged")).toBe("unverändert");
    expect(outcomeVerdictLabel("unknown")).toBe("unbekannt");
  });

  it("treats null/unrecognised verdicts as unknown", () => {
    expect(outcomeVerdictLabel(null)).toBe("unbekannt");
    expect(outcomeVerdictLabel(undefined)).toBe("unbekannt");
    expect(outcomeVerdictLabel("weird")).toBe("unbekannt");
  });

  it("tones improved green, worsened red, unchanged zinc — distinct from each other", () => {
    const improved = outcomeVerdictToneClass("improved");
    const worsened = outcomeVerdictToneClass("worsened");
    const unchanged = outcomeVerdictToneClass("unchanged");
    expect(improved).toContain("emerald");
    expect(worsened).toContain("red");
    expect(unchanged).toContain("zinc");
    expect(new Set([improved, worsened, unchanged]).size).toBe(3);
  });

  it("dims null/unknown verdicts without tinting emerald/red/zinc", () => {
    const unknown = outcomeVerdictToneClass("unknown");
    const nullish = outcomeVerdictToneClass(null);
    expect(unknown).toContain("hc-dim");
    expect(unknown).not.toMatch(/emerald|red|zinc/);
    expect(nullish).toBe(unknown);
  });
});

describe("outcomeDeltaValue / formatSignedDelta", () => {
  const mk = (overrides: Partial<LeverOutcome> = {}): LeverOutcome => ({
    lever_key: "lever-x",
    root_task_id: "t_1",
    proposed_at: 1000,
    baseline: { autonomy_pct: 62 },
    metric_key: "autonomy_pct",
    shipped_at: 1100,
    measured_at: 1200,
    current: { autonomy_pct: 68 },
    delta: { autonomy_pct: 6 },
    verdict: "improved",
    status: "measured",
    ...overrides,
  });

  it("extracts the metric_key's own delta value", () => {
    expect(outcomeDeltaValue(mk())).toBe(6);
  });

  it("returns null when metric_key is unset", () => {
    expect(outcomeDeltaValue(mk({ metric_key: null }))).toBeNull();
  });

  it("returns null when delta is missing or the key isn't present", () => {
    expect(outcomeDeltaValue(mk({ delta: null }))).toBeNull();
    expect(outcomeDeltaValue(mk({ delta: {} }))).toBeNull();
  });

  it("formats a positive/negative/zero delta with an explicit sign", () => {
    expect(formatSignedDelta(6)).toBe("+6");
    expect(formatSignedDelta(-3.2)).toBe("-3.2");
    expect(formatSignedDelta(0)).toBe("0");
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
