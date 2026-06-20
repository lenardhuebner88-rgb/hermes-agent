import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  BudgetLedgerSection,
  EffizienzSection,
  ErrorTaxonomySection,
  LatencySection,
  ReliabilitySection,
  StatsMasthead,
  SubscriptionBurnSection,
} from "./StatistikView";
import { broadsheet } from "../lib/broadsheetTokens";
import type {
  AccountUsageProvider,
  AccountUsageWindow,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
  RunsDailyPoint,
} from "../lib/schemas";

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

function uwindow(over: Partial<AccountUsageWindow> = {}): AccountUsageWindow {
  return { label: "Limit", window_key: null, used_percent: null, reset_at: null, detail: null, ...over };
}
function provider(over: Partial<AccountUsageProvider> = {}): AccountUsageProvider {
  return {
    provider: "anthropic",
    available: true,
    source: "oauth_usage_api",
    fetched_at: null,
    title: "Account limits",
    plan: null,
    windows: [],
    details: [],
    unavailable_reason: null,
    cached: false,
    ...over,
  };
}
function costRow(over: Partial<CostProfileRow> = {}): CostProfileRow {
  return {
    profile: "coder",
    subscription: null,
    runs: 0,
    cost_usd: null,
    cost_usd_equivalent: null,
    input_tokens: null,
    output_tokens: null,
    ...over,
  };
}

describe("StatsMasthead (ST4)", () => {
  it("leads with the fleet Akzeptanzrate and the three Stütz-KPIs", () => {
    const html = renderToStaticMarkup(
      <StatsMasthead
        now={1781769600} // 2026-06-18T08:00:00Z → "18. Juni"
        profiles={[
          // Phantom (not in the roster) — the masthead must drop it before
          // counting, exactly like the leaderboard. If filtering regressed, its
          // 1000/1000 verdicts + 50 runs would crater the 91 % / 90 % below.
          profile({ profile: "w", runs: 50, outcomes: { completed: 50 }, completed_rate: 1, judged: 2000, approved: 1000, rejected: 1000 }),
          profile({ profile: "coder", runs: 20, outcomes: { completed: 19 }, completed_rate: 0.95, judged: 108, approved: 100, rejected: 8 }),
          profile({ profile: "verifier", runs: 10, outcomes: { completed: 8 }, completed_rate: 0.8, judged: 22, approved: 18, rejected: 4 }),
        ]}
        baseline={[
          profile({ approved: 90, rejected: 10 }),
          profile({ profile: "unbekannt", approved: 500, rejected: 0 }), // phantom baseline → dropped, Δ stays +1 pp
        ]}
        series={[
          daily({ cost_usd: 1.0, done_roots: 2, done_roots_by_class: { nutzer: 3, haertung: 1, meta: 0 } }),
          daily({ cost_usd: 1.08, done_roots: 0, done_roots_by_class: { nutzer: 2, haertung: 0, meta: 1 } }),
        ]}
      />,
    );
    // Acceptance = 118/130 = 91 %.
    expect(html).toContain("Akzeptanzrate");
    expect(html).toContain('class="sb-mast"');
    expect(html).toContain("91");
    expect(html).toContain("118 abgenommen · 12 verworfen");
    // Δ vs the 90 % baseline → +1 pp, ok-status.
    expect(html).toContain("1 pp ggü. 30 Tg");
    expect(html).toContain("sb-d sb-ok");
    // Masthead meta carries the German date + window.
    expect(html).toContain("18. Juni · 7 Tage");
    // Supporting KPIs: Autonomie (accent) 27/30 = 90 %, $1.04/Lieferung, 5 Nutzer.
    expect(html).toContain("sb-n sb-accent");
    expect(html).toContain("Autonomie");
    expect(html).toContain("90");
    expect(html).toContain("Kosten je Lieferung");
    expect(html).toContain("$ 1.04");
    expect(html).toContain("Nutzerwert");
  });

  it("stays calm with em-dashes when there are no verdicts/cost", () => {
    const html = renderToStaticMarkup(
      <StatsMasthead now={1781769600} profiles={[profile({ runs: 0 })]} baseline={[]} series={[]} stale />,
    );
    expect(html).toContain("Noch keine Verifier-Urteile im Fenster");
    expect(html).toContain("keine 30-Tage-Baseline");
    expect(html).toContain("veraltet");
  });
});

describe("LatencySection (ST4)", () => {
  it("renders p50/p90 as a two-figure card", () => {
    const html = renderToStaticMarkup(<LatencySection p50={240} p90={1020} />);
    expect(html).toContain("sb-twin");
    expect(html).toContain("Median · p50");
    expect(html).toContain("4m");
    expect(html).toContain("p90");
    expect(html).toContain("17m");
  });

  it("shows em-dashes when latency is unknown", () => {
    const html = renderToStaticMarkup(<LatencySection p50={null} p90={null} />);
    expect(html).toContain("—");
  });
});

describe("ReliabilitySection (ST4)", () => {
  it("phantom-filters and ranks the roster by completion rate", () => {
    const html = renderToStaticMarkup(
      <ReliabilitySection
        profiles={[
          profile({ profile: "w", runs: 99, completed_rate: 1 }), // phantom
          profile({ profile: "coder", runs: 20, completed_rate: 0.95 }),
          profile({ profile: "premium", runs: 2, completed_rate: 1, low_sample: true }),
        ]}
      />,
    );
    expect(html).toContain("Verlässlichkeit");
    // Phantom "w" (99 runs) is dropped.
    expect(html).not.toContain("99 Läufe");
    // Coder (well-sampled) ranks above low-sample Premium.
    expect(html.indexOf("Coder")).toBeLessThan(html.indexOf("Premium"));
    expect(html).toContain("sb-sc sb-ok"); // coder 95 % → ok ink
    expect(html).toContain("20 Läufe");
  });

  it("renders a calm empty state with no roster runs", () => {
    const html = renderToStaticMarkup(<ReliabilitySection profiles={[profile({ profile: "w" })]} />);
    expect(html).toContain("Noch keine Profil-Läufe im Fenster.");
  });
});

describe("ErrorTaxonomySection (ST4)", () => {
  it("buckets the failures and renders the harness-lifecycle verdict", () => {
    const html = renderToStaticMarkup(
      <ErrorTaxonomySection
        issues={[issue({ crashed: 4, spawn_failed: 1 }), issue({ timed_out: 3 }), issue({ gave_up: 2 })]}
      />,
    );
    expect(html).toContain("Fehler-Taxonomie");
    expect(html).toContain("sb-estack");
    expect(html).toContain(`background:${broadsheet.errorSeries[0]}`);
    expect(html).toContain("Prozess tot");
    expect(html).toContain("Zeitüberschreitung");
    expect(html).toContain("Budget erschöpft");
    expect(html).toContain("<b>5</b>"); // dead bucket count
    // The Befund line bolds "Harness-Lifecycle".
    expect(html).toContain("<b>Harness-Lifecycle</b>");
    expect(html).toContain("Issues — wiederkehrende Fehler");
  });

  it("shows a clean-window verdict and no bar when there are no issues", () => {
    const html = renderToStaticMarkup(<ErrorTaxonomySection issues={[]} />);
    expect(html).toContain("sauberes Fenster");
    expect(html).not.toContain("sb-estack");
  });
});

describe("BudgetLedgerSection (ST5)", () => {
  it("orders providers Engpass-first, leads with the bottleneck, tags Kimi estimated", () => {
    const html = renderToStaticMarkup(
      <BudgetLedgerSection
        providers={[
          provider({ provider: "kimi", source: "kanban_subscription_tokens", title: "Kimi subscription tokens", windows: [] }),
          provider({
            provider: "anthropic",
            source: "oauth_usage_api",
            windows: [
              uwindow({ window_key: "session", used_percent: 30 }),
              uwindow({ window_key: "weekly", used_percent: 92, reset_at: "2026-06-19T00:00:00Z" }),
            ],
          }),
          provider({ provider: "openai-codex", source: "usage_api", windows: [uwindow({ window_key: "weekly", used_percent: 40 })] }),
        ]}
      />,
    );
    expect(html).toContain("Budget-Ledger");
    expect(html).toContain("sb-led-row");
    // Engpass lead names the tightest window (Claude · Woche · 92 %).
    expect(html).toContain("sb-lead");
    expect(html).toContain("Claude Woche bei 92 %");
    // Claude (92 %) sorts before ChatGPT (40 %); both render.
    expect(html.indexOf("ChatGPT")).toBeGreaterThan(0);
    expect(html).toContain("sb-led-fig sb-crit"); // 92 % → crit ink
    // Kimi is flagged estimated and has no provider limit.
    expect(html).toContain("sb-tagm");
    expect(html).toContain("geschätzt");
    expect(html).toContain("kein Provider-Limit");
  });

  it("renders a calm empty state when no provider limits are available", () => {
    const html = renderToStaticMarkup(<BudgetLedgerSection providers={[]} />);
    expect(html).toContain("Keine Limit-Daten verfügbar.");
  });

  it("carries an unavailable provider through without a meter", () => {
    const html = renderToStaticMarkup(
      <BudgetLedgerSection
        providers={[provider({ provider: "anthropic", available: false, unavailable_reason: "no oauth token", windows: [] })]}
      />,
    );
    expect(html).toContain("no oauth token");
    expect(html).toContain("—");
  });
});

describe("SubscriptionBurnSection (S3)", () => {
  it("renders real breakdown, top-N burners and anti-pattern flags", () => {
    const html = renderToStaticMarkup(
      <SubscriptionBurnSection
        burn={{
          days: 7,
          now: 100,
          window_start: 0,
          totals: { runs: 6, input_tokens: 900, output_tokens: 100, total_tokens: 1000 },
          by_lane: [
            { subscription: "claude-max", profile: "verifier", runs: 2, input_tokens: 150, output_tokens: 50, total_tokens: 200 },
            { subscription: "codex", profile: "coder", runs: 4, input_tokens: 750, output_tokens: 50, total_tokens: 800 },
          ],
          by_class: [
            { subscription: "codex", value_class: "meta", runs: 3, input_tokens: 650, output_tokens: 50, total_tokens: 700 },
            { subscription: "claude-max", value_class: "nutzer", runs: 3, input_tokens: 250, output_tokens: 50, total_tokens: 300 },
          ],
          daily: [],
          buckets: [],
        }}
      />,
    );

    expect(html).toContain("Abo-Burn-Breakdown");
    expect(html).toContain("data-testid=\"subscription-burn-breakdown\"");
    expect(html).toContain("Top-N-Burner");
    expect(html).toContain("Anti-Muster");
    expect(html).toContain("coder · codex");
    expect(html).toContain("meta · codex");
    expect(html).toContain("1 k");
    expect(html).toContain("sb-subburn-grid");
  });
});

describe("EffizienzSection (ST5)", () => {
  it("shows the three efficiency KPIs and the per-lane token burn", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection
        profiles={[profile({ runs: 80, rejected: 8 }), profile({ runs: 20, rejected: 2 })]}
        costs={[
          costRow({ profile: "coder", input_tokens: 1_200_000, output_tokens: 0, runs: 12 }),
          costRow({ profile: "w", input_tokens: 9_000_000, output_tokens: 0, runs: 1 }), // phantom → dropped
        ]}
        chainRate={0.75}
        queueWaitSeconds={240}
      />,
    );
    expect(html).toContain("Flotten-Effizienz");
    // Chain-Completion 75 % (accent), Queue-Wait p50 = 4m, Gate 10/100 = 10 %.
    expect(html).toContain("Ketten-Abschluss");
    expect(html).toContain("sb-n sb-accent");
    expect(html).toContain("75");
    expect(html).toContain("Queue-Wartezeit");
    expect(html).toContain("4m");
    expect(html).toContain("Gate-Quote");
    // Token-Burn leaderboard: coder 1.2 M, phantom dropped.
    expect(html).toContain("Token-Burn je Lane");
    expect(html).toContain("sb-lr");
    expect(html).toContain("1.2 M");
    expect(html).toContain("Coder");
    expect(html).toContain("12 Läufe");
    expect(html).not.toContain("9.0 M");
  });

  it("stays calm with em-dashes and an empty-burn note when nothing ran", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection profiles={[]} costs={[]} chainRate={null} queueWaitSeconds={null} />,
    );
    expect(html).toContain("Noch kein Token-Burn im Fenster.");
    expect(html).toContain("—");
  });
});
