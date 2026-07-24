import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../hooks/scorecard", () => ({
  useScorecard: () => ({ loading: false, error: null, data: {
    overall: { runs: 12, approved: 9, approval_rate: .75 },
    verdicts: { approved: 9, rejected: 3 },
    profiles: [{ name: "coder", runs: 10, approved: 8, approval_rate: .8 }],
    models: [{ name: "gpt-test", runs: 10, approved: 8, approval_rate: .8 }],
    weeks: [{ year: 2026, week: 30, runs: 10, approved: 8, approval_rate: .8 }],
    checked_at: 1,
  }}),
}));

import { ScorecardView } from "./ScorecardView";

describe("ScorecardView", () => {
  it("renders the scorecard endpoint aggregation shape", () => {
    const markup = renderToStaticMarkup(<ScorecardView />);
    expect(markup).toContain("75.0 %");
    expect(markup).toContain("coder");
    expect(markup).toContain("2026 · W30");
  });
});
