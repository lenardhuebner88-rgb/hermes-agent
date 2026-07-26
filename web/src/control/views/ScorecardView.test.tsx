import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScorecardResponseSchema, type ScorecardResponse } from "../lib/schemas";

const mockState = vi.hoisted(() => ({ data: null as ScorecardResponse | null }));

vi.mock("../hooks/scorecard", () => ({
  useScorecard: () => ({ loading: false, error: null, data: mockState.data }),
}));

import { ScorecardView } from "./ScorecardView";

const baseData = (): ScorecardResponse => ({
  overall: { runs: 12, approved: 9, approval_rate: .75 },
  verdicts: { approved: 9, rejected: 3 },
  profiles: [{ name: "coder", runs: 10, approved: 8, approval_rate: .8 }],
  models: [{ name: "gpt-test", runs: 10, approved: 8, approval_rate: .8 }],
  weeks: [{ year: 2026, week: 30, runs: 10, approved: 8, approval_rate: .8 }],
  materialized_scores: {},
  checked_at: 1,
});

describe("ScorecardView", () => {
  beforeEach(() => { mockState.data = baseData(); });

  it("preserves nullable numeric distribution statistics from the scorecard response", () => {
    const scores = ScorecardResponseSchema.parse({
      ...baseData(),
      materialized_scores: {
        run_cost_usd: { value: 1.5, min: 0.5, max: 2.5, sum: 3, count: 2 },
        run_outcome_kind: { value: { completed: 2 }, count: 2 },
      },
    }).materialized_scores;

    expect(scores.run_cost_usd).toMatchObject({ value: 1.5, min: 0.5, max: 2.5, sum: 3, count: 2 });
    expect(scores.run_outcome_kind).toMatchObject({ value: { completed: 2 }, count: 2 });
  });

  it("renders the scorecard endpoint aggregation shape", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("75.0 %");
    expect(markup).toContain("coder");
    expect(markup).toContain("2026 · W30");
  });

  it("zeigt numerische Serien einheitengerecht statt als Prozentwert (SC-S5)", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        run_duration_seconds: { value: 167.45, min: 0, max: 85699, sum: 1374284, count: 8207 },
        run_cost_usd: { value: 0.0027357, min: 0, max: 6.22306474, sum: 11.7472338, count: 4294 },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    // AC-1: Dauer traegt eine Zeiteinheit und erscheint NICHT als Prozentwert.
    expect(markup).toContain("2,8 min");
    expect(markup).not.toContain("16745");
    // AC-2: Kosten erscheinen als Geldbetrag.
    expect(markup).toContain("$");
    expect(markup).toContain("0,0027");
    // AC-5: Maximum und Anzahl je numerischer Serie sichtbar.
    expect(markup).toContain("max 23,8 h");
    expect(markup).toContain("n = 8.207");
    expect(markup).toContain("n = 4.294");
  });

  it("zeigt unbekannte Score-Namen als schlichte Zahl neben den Review-Verdicts (SC-S5)", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        planner_brief_shape: { value: .8, count: 12 },
        worker_completion: { value: .6, count: 20 },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    // AC-7: Unbekannte Namen verschwinden nicht und werden nicht als Prozent verkauft.
    expect(markup).toContain("Event- &amp; Usage-Scores");
    expect(markup).toContain("planner_brief_shape");
    expect(markup).toContain("0,8");
    expect(markup).not.toContain("80.0 % · n = 12");
    expect(markup).toContain("n = 12");
    expect(markup).toContain("worker_completion");
    expect(markup).toContain("n = 20");
    // AC-8: Die bestehende review_verdict-Darstellung bleibt unverändert.
    expect(markup).toContain("75.0 %");
    expect(markup).toContain("coder");
  });

  it("schlüsselt die kategoriale Serie als Häufigkeitskarte auf (SC-S5)", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        run_outcome_kind: {
          value: { blocked: 109, completed: 171, "unknown_outcome_code:0.0": 12 },
          count: 292,
        },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    // AC-3: Labels mit Anzahlen, absteigend sortiert; kein Platzhalterwort.
    expect(markup).toContain("completed (171)");
    expect(markup).toContain("blocked (109)");
    expect(markup.indexOf("completed (171)")).toBeLessThan(markup.indexOf("blocked (109)"));
    expect(markup).toContain("n = 292");
    expect(markup).not.toContain("kategorial");
    // AC-4: unknown_outcome_code-Eintraege bleiben sichtbar.
    expect(markup).toContain("unknown_outcome_code:0.0 (12)");
  });

  it("kennzeichnet dünne und leere Nenner ausdrücklich statt als Zielwert (SC-S5)", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        planner_brief_shape: { value: .9, count: 3 },
        empty_score: { value: null, count: 0 },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("0,9");
    expect(markup).toContain("n = 3");
    expect(markup).toContain("dünne Datenlage");
    expect(markup).toContain("nicht bestätigt");
    // AC-6: Leere Serie bleibt ausdruecklich leer, ohne erfundenen Nullwert.
    expect(markup).toContain("keine Rows · dünne Datenlage");
    expect(markup).not.toContain("Zielwert erreicht");
    // Genau ein Mittelwert im Markup: der der belegten Serie, keiner fuer die leere.
    expect(markup.match(/Ø/g)).toHaveLength(1);
  });

  it("zeigt bei leerem Datensatz eine ruhige deutsche Erklärung ohne falsches Zeitfenster (SC-S5)", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("Keine materialisierten Event- und Usage-Scores.");
    expect(markup).toContain("Situation:");
    expect(markup).toContain("Bewertung:");
    expect(markup).toContain("Nächste Aktion:");
    expect(markup).not.toContain("28 Tage");
    expect(markup).not.toContain("Zielwert erreicht");
  });
});
