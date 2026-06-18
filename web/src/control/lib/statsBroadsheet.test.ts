import { describe, expect, it } from "vitest";
import {
  acceptance,
  acceptanceDelta,
  autonomy,
  costPerDelivery,
  errorTaxonomy,
  germanDate,
  isRosterProfile,
  leaderboard,
  nutzerwert,
  reliabilityStatus,
  rosterProfiles,
} from "./statsBroadsheet";
import { broadsheet } from "./broadsheetTokens";
import type { IssueGroup, ReliabilityProfile, RunsDailyPoint } from "./schemas";

function profile(over: Partial<ReliabilityProfile> = {}): ReliabilityProfile {
  return {
    profile: "coder",
    runs: 0,
    tasks: 0,
    outcomes: {},
    completed_rate: null,
    failed_rate: null,
    retries: 0,
    retry_rate: null,
    judged: 0,
    approved: 0,
    rejected: 0,
    approve_rate: null,
    low_sample: false,
    ...over,
  };
}

function daily(over: Partial<RunsDailyPoint> = {}): RunsDailyPoint {
  return {
    date: "2026-06-10",
    done_roots: 0,
    done_roots_by_class: { nutzer: 0, haertung: 0, meta: 0 },
    done_tasks: 0,
    cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    runs_completed: 0,
    runs_failed: 0,
    cycle_time_p50_seconds: null,
    ...over,
  };
}

function issue(outcomes: Record<string, number>): IssueGroup {
  return {
    signature: "x",
    profile: "coder",
    count: Object.values(outcomes).reduce((a, b) => a + b, 0),
    first_seen: 0,
    last_seen: 0,
    outcomes,
    example_run_id: 0,
    example_task_id: "",
    example_text: "",
  };
}

describe("phantom filter", () => {
  it("keeps roster profiles and drops the NULL/legacy sentinels", () => {
    expect(isRosterProfile("coder")).toBe(true);
    expect(isRosterProfile("coder-claude")).toBe(true);
    expect(isRosterProfile("reviewer")).toBe(true);
    expect(isRosterProfile("family-ui")).toBe(true);
    expect(isRosterProfile("w")).toBe(false);
    expect(isRosterProfile("unbekannt")).toBe(false);
    expect(isRosterProfile("(ohne profil)")).toBe(false);
  });

  it("rosterProfiles strips phantoms from a list", () => {
    const rows = [profile({ profile: "coder" }), profile({ profile: "w" }), profile({ profile: "unbekannt" })];
    expect(rosterProfiles(rows).map((r) => r.profile)).toEqual(["coder"]);
  });
});

describe("acceptance (masthead headline)", () => {
  it("sums verdicts across profiles into a fleet acceptance rate", () => {
    const a = acceptance([
      profile({ approved: 100, rejected: 10 }),
      profile({ approved: 18, rejected: 2 }),
    ]);
    expect(a.approved).toBe(118);
    expect(a.rejected).toBe(12);
    expect(a.rate).toBeCloseTo(118 / 130, 5);
  });

  it("returns a null rate when there are no verdicts", () => {
    expect(acceptance([profile({ judged: 0 })]).rate).toBeNull();
    expect(acceptance([]).rate).toBeNull();
  });

  it("computes the Δ against the 30-day baseline in percentage points", () => {
    const cur = [profile({ approved: 95, rejected: 5 })]; // 95 %
    const base = [profile({ approved: 91, rejected: 9 })]; // 91 %
    expect(acceptanceDelta(cur, base)).toBe(4);
    expect(acceptanceDelta(cur, [])).toBeNull();
  });
});

describe("autonomy (completed ÷ runs)", () => {
  it("uses the explicit completed outcome when present", () => {
    const a = autonomy([
      profile({ runs: 10, outcomes: { completed: 9 } }),
      profile({ runs: 10, outcomes: { completed: 7 } }),
    ]);
    expect(a).toBeCloseTo(16 / 20, 5);
  });

  it("falls back to completed_rate × runs when no explicit outcome", () => {
    const a = autonomy([profile({ runs: 8, completed_rate: 0.5, outcomes: {} })]);
    expect(a).toBeCloseTo(4 / 8, 5);
  });

  it("is null without runs", () => {
    expect(autonomy([])).toBeNull();
    expect(autonomy([profile({ runs: 0 })])).toBeNull();
  });
});

describe("costPerDelivery", () => {
  it("divides measured $ by delivered roots over the window", () => {
    const v = costPerDelivery([
      daily({ cost_usd: 1.0, done_roots: 2 }),
      daily({ cost_usd: 1.08, done_roots: 0 }),
    ]);
    expect(v).toBeCloseTo(2.08 / 2, 5);
  });

  it("is null when nothing was measured or no roots were delivered", () => {
    expect(costPerDelivery([daily({ cost_usd: null, done_roots: 3 })])).toBeNull();
    expect(costPerDelivery([daily({ cost_usd: 2, done_roots: 0 })])).toBeNull();
  });
});

describe("nutzerwert", () => {
  it("sums user-feature roots across the window", () => {
    expect(nutzerwert([
      daily({ done_roots_by_class: { nutzer: 2, haertung: 1, meta: 4 } }),
      daily({ done_roots_by_class: { nutzer: 3, haertung: 0, meta: 1 } }),
    ])).toBe(5);
  });
});

describe("reliabilityStatus", () => {
  it("maps the rate to ok/warn/crit and neutralises low samples", () => {
    expect(reliabilityStatus(0.9, false)).toBe("ok");
    expect(reliabilityStatus(0.7, false)).toBe("warn");
    expect(reliabilityStatus(0.4, false)).toBe("crit");
    expect(reliabilityStatus(0.99, true)).toBe("neutral");
    expect(reliabilityStatus(null, false)).toBe("neutral");
  });
});

describe("leaderboard", () => {
  it("phantom-filters, labels, and sorts well-sampled by rate desc, low-sample last", () => {
    const rows = leaderboard([
      profile({ profile: "verifier", runs: 12, completed_rate: 0.7, low_sample: false }),
      profile({ profile: "w", runs: 99, completed_rate: 1, low_sample: false }), // phantom
      profile({ profile: "coder", runs: 20, completed_rate: 0.95, low_sample: false }),
      profile({ profile: "premium", runs: 2, completed_rate: 1, low_sample: true }),
    ]);
    expect(rows.map((r) => r.profile)).toEqual(["coder", "verifier", "premium"]);
    expect(rows[0].label).toBe("Coder");
    expect(rows[0].status).toBe("ok");
    expect(rows[1].status).toBe("warn");
    expect(rows[2].status).toBe("neutral"); // low sample
  });
});

describe("errorTaxonomy", () => {
  it("buckets lifecycle outcomes by severity with the broadsheet palette + widths", () => {
    const tax = errorTaxonomy([
      issue({ crashed: 4, spawn_failed: 1 }), // dead = 5
      issue({ timed_out: 3 }), // timeout = 3
      issue({ gave_up: 1, iteration_budget_exhausted: 1 }), // budget = 2
      issue({ blocked: 0 }), // dropped (0)
    ]);
    expect(tax.total).toBe(10);
    expect(tax.allLifecycle).toBe(true);
    const dead = tax.buckets.find((b) => b.key === "dead");
    expect(dead?.count).toBe(5);
    expect(dead?.color).toBe(broadsheet.errorSeries[0]);
    expect(dead?.pct).toBeCloseTo(50, 5);
    expect(tax.buckets.find((b) => b.key === "timeout")?.count).toBe(3);
    expect(tax.buckets.find((b) => b.key === "budget")?.count).toBe(2);
    // empty bucket is filtered out
    expect(tax.buckets.find((b) => b.key === "other")).toBeUndefined();
  });

  it("folds residual/unknown outcomes into the neutral 'other' bucket and flags non-lifecycle", () => {
    const tax = errorTaxonomy([issue({ blocked: 2, weird_state: 1 })]);
    const other = tax.buckets.find((b) => b.key === "other");
    expect(other?.count).toBe(3);
    expect(other?.color).toBe(broadsheet.errorSeries[3]);
    expect(tax.allLifecycle).toBe(false); // weird_state is not a known endstate
  });

  it("is empty and lifecycle-clean for no issues", () => {
    const tax = errorTaxonomy([]);
    expect(tax.buckets).toEqual([]);
    expect(tax.total).toBe(0);
    expect(tax.allLifecycle).toBe(true);
  });
});

describe("germanDate", () => {
  it("formats a UTC epoch as a German day · month", () => {
    // 2026-06-18T08:00:00Z
    expect(germanDate(1781769600)).toBe("18. Juni");
  });
});
