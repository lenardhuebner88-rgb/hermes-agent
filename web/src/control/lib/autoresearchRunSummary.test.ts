import { describe, expect, it } from "vitest";
import {
  getAutoresearchLastRunBrief,
  getAutoresearchRunCard,
  getAutoresearchRunSummary,
} from "./autoresearchRunSummary";
import type { AutoresearchRun } from "./types";

function run(overrides: Partial<AutoresearchRun> = {}): AutoresearchRun {
  return {
    at: "2026-06-06T10:00:00Z",
    lane: "skill",
    request_id: "req-1",
    tokens: 1000,
    proposed: 0,
    errors: 0,
    scanned: 5,
    ...overrides,
  };
}

describe("getAutoresearchRunSummary", () => {
  it("reports 'no run yet' when the runs list is empty", () => {
    const s = getAutoresearchRunSummary({ runs: [], acceptanceRate: null, tokensPerApplied: null });
    expect(s.label).toBe("Noch kein Lauf");
    expect(s.tone).toBe("zinc");
  });

  it("surfaces failure (red) when the latest run reported errors", () => {
    const s = getAutoresearchRunSummary({ runs: [run({ errors: 2, proposed: 3 })], acceptanceRate: null, tokensPerApplied: null });
    expect(s.tone).toBe("red");
    expect(s.label).toBe("Fehler im letzten Lauf");
  });

  it("reports success (emerald) when the latest run produced proposals and no errors", () => {
    const s = getAutoresearchRunSummary({ runs: [run({ proposed: 4 })], acceptanceRate: 0.5, tokensPerApplied: 1000 });
    expect(s.tone).toBe("emerald");
    expect(s.label).toBe("Hat geliefert");
    expect(s.title).toContain("4");
  });

  it("warns (amber) on an expensive run with no proposals", () => {
    const s = getAutoresearchRunSummary({ runs: [run({ proposed: 0, tokens: 200_000 })], acceptanceRate: null, tokensPerApplied: null });
    expect(s.tone).toBe("amber");
    expect(s.label).toBe("Teuer ohne Treffer");
  });

  it("reports a quiet no-hit run (cyan) when cheap and empty", () => {
    const s = getAutoresearchRunSummary({ runs: [run({ proposed: 0, tokens: 1000 })], acceptanceRate: null, tokensPerApplied: null });
    expect(s.tone).toBe("cyan");
    expect(s.label).toBe("Kein Treffer");
  });

  it("prioritizes latest-run errors over its proposals", () => {
    // latest run has both proposals and errors -> error path wins
    const s = getAutoresearchRunSummary({ runs: [run({ proposed: 5, errors: 1 })], acceptanceRate: null, tokensPerApplied: null });
    expect(s.label).toBe("Fehler im letzten Lauf");
  });
});

describe("getAutoresearchRunCard", () => {
  it("renders a failure card (red) for a run with errors", () => {
    const c = getAutoresearchRunCard(run({ errors: 3 }));
    expect(c.tone).toBe("red");
    expect(c.label).toBe("Fehler");
  });

  it("renders a delivered card (emerald) for a run with proposals", () => {
    const c = getAutoresearchRunCard(run({ proposed: 2 }));
    expect(c.tone).toBe("emerald");
    expect(c.label).toBe("Geliefert");
  });

  it("renders an expensive-quiet card (amber) for high-token zero-proposal runs", () => {
    const c = getAutoresearchRunCard(run({ proposed: 0, tokens: 250_000 }));
    expect(c.tone).toBe("amber");
    expect(c.label).toBe("Teuer ruhig");
  });

  it("renders a quiet card (cyan) for a cheap empty run and exposes a veto fact", () => {
    const c = getAutoresearchRunCard(run({ proposed: 0, tokens: 100, vetoed: 2 }));
    expect(c.tone).toBe("cyan");
    expect(c.label).toBe("Ruhig");
    const veto = c.facts.find((f) => f.label === "Veto");
    expect(veto?.value).toBe("2");
  });
});

describe("getAutoresearchLastRunBrief", () => {
  it("reports 'no run yet' when nothing is available", () => {
    const b = getAutoresearchLastRunBrief({ lastRun: null, latestRun: null });
    expect(b.label).toBe("Noch kein Lauf");
    expect(b.tone).toBe("zinc");
  });

  it("surfaces errors (red) from a structured lastRun object", () => {
    const b = getAutoresearchLastRunBrief({ lastRun: { research_errors: 2, proposed: 1 }, latestRun: null });
    expect(b.tone).toBe("red");
    expect(b.label).toBe("Fehler prüfen");
  });

  it("reports refusal (amber) when the backend refused the run", () => {
    const b = getAutoresearchLastRunBrief({ lastRun: { refused: "route not configured" }, latestRun: null });
    expect(b.tone).toBe("amber");
    expect(b.label).toBe("Abgelehnt");
    expect(b.detail).toBe("route not configured");
  });

  it("reports a deliberate stop (cyan)", () => {
    const b = getAutoresearchLastRunBrief({ lastRun: { stopped: true }, latestRun: null });
    expect(b.tone).toBe("cyan");
    expect(b.label).toBe("Gestoppt");
  });

  it("reports delivered proposals (emerald) with kept/reverted backend detail", () => {
    const b = getAutoresearchLastRunBrief({ lastRun: { proposed: 3, kept: 2, reverted: 1 }, latestRun: null });
    expect(b.tone).toBe("emerald");
    expect(b.label).toBe("Hat geliefert");
    expect(b.detail).toContain("2");
    expect(b.detail).toContain("1");
  });

  it("falls back to the latestRun object when lastRun is not a structured object", () => {
    const b = getAutoresearchLastRunBrief({
      lastRun: null,
      latestRun: run({ proposed: 5, scanned: 9 }),
    });
    expect(b.label).toBe("Hat geliefert");
    const proposedFact = b.facts.find((f) => f.label === "Vorschläge");
    expect(proposedFact?.value).toBe("5");
  });
});
