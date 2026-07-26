import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ScorecardResponse } from "../lib/schemas";

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

  it("renders the scorecard endpoint aggregation shape", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("75.0 %");
    expect(markup).toContain("coder");
    expect(markup).toContain("2026 · W30");
  });

  it("zeigt die materialisierten Scores mit Wert und Row-Anzahl neben den Review-Verdicts", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        planner_brief_shape: { value: .8, count: 12 },
        worker_completion: { value: .6, count: 20 },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    // Neue Scores sichtbar …
    expect(markup).toContain("Event- &amp; Usage-Scores");
    expect(markup).toContain("planner_brief_shape");
    expect(markup).toContain("80.0 %");
    expect(markup).toContain("n = 12");
    expect(markup).toContain("worker_completion");
    expect(markup).toContain("n = 20");
    // … und die bestehende review_verdict-Darstellung bleibt unverändert.
    expect(markup).toContain("75.0 %");
    expect(markup).toContain("coder");
  });

  it("kennzeichnet einen Score mit dünnem Nenner und stellt ihn nicht als Zielwert dar", () => {
    mockState.data = {
      ...baseData(),
      materialized_scores: {
        planner_brief_shape: { value: .9, count: 3 },
        empty_score: { value: null, count: 0 },
      },
    };
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("90.0 %");
    expect(markup).toContain("n = 3");
    expect(markup).toContain("dünne Datenlage");
    expect(markup).toContain("nicht bestätigt");
    expect(markup).toContain("keine Rows");
    // Kein Erfolgs-Label für dünne Nenner.
    expect(markup).not.toContain("Zielwert erreicht");
  });

  it("zeigt bei leerem Datensatz eine ruhige deutsche Erklärung statt eines Erfolgssignals", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("Keine Event- und Usage-Scores im Fenster.");
    expect(markup).toContain("Situation:");
    expect(markup).toContain("Bewertung:");
    expect(markup).toContain("Nächste Aktion:");
    expect(markup).not.toContain("Zielwert erreicht");
  });
});
