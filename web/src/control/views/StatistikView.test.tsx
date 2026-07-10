import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  BudgetLedgerSection,
  EffizienzSection,
  ErrorTaxonomySection,
  LedgerWorkerRunners,
  LatencySection,
  MotherLedgerSection,
  ReliabilitySection,
  StatsMasthead,
  StatistikView,
  SubscriptionBurnSection,
  WorkerEfficiencySection,
} from "./StatistikView";
import { ERROR_SERIES } from "../lib/statsBroadsheet";
import type {
  AccountUsageProvider,
  AccountUsageWindow,
  CostProfileRow,
  IssueGroup,
  ReliabilityProfile,
  ReviewValueRow,
  RunsDailyPoint,
  WindowedRollupResponse,
  WindowedRollupRoot,
  WindowedRollupWorker,
} from "../lib/schemas";

type TestRollupState = {
  data: WindowedRollupResponse | null;
  error: string | null;
  errorObj: null;
  loading: boolean;
  lastUpdated: number | null;
  isStale: boolean;
  reload: () => Promise<void>;
  updateData: () => void;
};

type ControlHookState = ReturnType<typeof controlState>;

const windowedRollupMock = vi.hoisted(() => ({
  state: null as TestRollupState | null,
}));

const controlDataMock = vi.hoisted(() => ({
  reliability: null as ControlHookState | null,
  summary: null as ControlHookState | null,
  daily: null as ControlHookState | null,
  issues: null as ControlHookState | null,
  accountUsage: null as ControlHookState | null,
  costs: null as ControlHookState | null,
  costSeries: null as ControlHookState | null,
  subscriptionBurn: null as ControlHookState | null,
  chain: null as ControlHookState | null,
  board: null as ControlHookState | null,
}));

vi.mock("../hooks/useControlData", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../hooks/useControlData")>();
  return {
    ...actual,
    useAccountUsage: () => controlDataMock.accountUsage,
    useBoardStats: () => controlDataMock.board,
    useChainCompletion: () => controlDataMock.chain,
    useHermesReliability: () => controlDataMock.reliability,
    useHermesRunSummary: () => controlDataMock.summary,
    useHermesRunsCosts: () => controlDataMock.costs,
    useHermesRunsCostSeries: () => controlDataMock.costSeries,
    useHermesRunsDaily: () => controlDataMock.daily,
    useHermesRunsIssues: () => controlDataMock.issues,
    useHermesSubscriptionBurn: () => controlDataMock.subscriptionBurn,
    useHermesWindowedRollup: () => windowedRollupMock.state!,
  };
});

function controlState(data: unknown = null, over: Record<string, unknown> = {}) {
  return {
    data,
    error: null,
    errorObj: null,
    loading: false,
    lastUpdated: null,
    isStale: false,
    reload: async () => undefined,
    updateData: () => undefined,
    ...over,
  };
}

function rollupState(over: Partial<TestRollupState> = {}): TestRollupState {
  return {
    data: null,
    error: null,
    errorObj: null,
    loading: false,
    lastUpdated: null,
    isStale: false,
    reload: async () => undefined,
    updateData: () => undefined,
    ...over,
  };
}

beforeEach(() => {
  windowedRollupMock.state = rollupState();
  controlDataMock.reliability = controlState({ now: 1_780_000_000, profiles: [], baseline: [] });
  controlDataMock.summary = controlState({ cycle_time_p50_seconds: null, cycle_time_p90_seconds: null });
  controlDataMock.daily = controlState({ series: [] });
  controlDataMock.issues = controlState({ issues: [] });
  controlDataMock.accountUsage = controlState({ providers: [] });
  controlDataMock.costs = controlState({ profiles: [] });
  controlDataMock.costSeries = controlState({ series: [] });
  controlDataMock.subscriptionBurn = controlState(null);
  controlDataMock.chain = controlState({ chain_completion_rate: null });
  controlDataMock.board = controlState({ queue_wait_p50_seconds: null });
});

function withViewport<T>(width: number, run: () => T): T {
  const previousInnerWidth = Object.getOwnPropertyDescriptor(globalThis, "innerWidth");
  const previousMatchMedia = Object.getOwnPropertyDescriptor(globalThis, "matchMedia");
  Object.defineProperty(globalThis, "innerWidth", { configurable: true, value: width });
  Object.defineProperty(globalThis, "matchMedia", {
    configurable: true,
    value: (query: string) => ({
      matches: /max-width:\s*760px/.test(query) ? width <= 760 : false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }),
  });
  try {
    return run();
  } finally {
    if (previousInnerWidth) Object.defineProperty(globalThis, "innerWidth", previousInnerWidth);
    else delete (globalThis as { innerWidth?: number }).innerWidth;
    if (previousMatchMedia) Object.defineProperty(globalThis, "matchMedia", previousMatchMedia);
    else delete (globalThis as { matchMedia?: unknown }).matchMedia;
  }
}

function renderStatistikAtViewport(width: number) {
  windowedRollupMock.state = rollupState({ data: rollupResponse() });
  return withViewport(width, () => renderToStaticMarkup(<StatistikView />));
}

function costSeriesPoint() {
  return {
    day: "2026-07-05",
    runs: 2,
    cost_usd: 0.12,
    cost_usd_equivalent: 0.42,
    api_equivalent_usd: 0.42,
    actual_cost_usd: 0.12,
    billing_neuralwatt_kwh: null,
    billing_neuralwatt_cost_usd: null,
    input_tokens: 1000,
    output_tokens: 400,
    cached_tokens: 100,
    total_tokens: 1500,
  };
}

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
    actual_cost_usd: null,
    api_equivalent_usd: null,
    billing_neuralwatt_kwh: null,
    billing_neuralwatt_charged_kwh: null,
    billing_neuralwatt_usd_per_kwh: null,
    billing_neuralwatt_cost_usd: null,
    input_tokens: null,
    output_tokens: null,
    cached_tokens: null,
    total_tokens: null,
    ...over,
  };
}

function reviewRow(over: Partial<ReviewValueRow> = {}): ReviewValueRow {
  return {
    profile: "reviewer",
    runs: 0,
    approved: 0,
    request_changes: 0,
    findings_blocking: null,
    findings_observations: null,
    input_tokens: null,
    tokens_per_finding: null,
    read_items: null,
    tokens_per_read_item: null,
    ...over,
  };
}

function rollupWorker(over: Partial<WindowedRollupWorker> = {}): WindowedRollupWorker {
  return {
    profile: "coder",
    input_tokens: 1000,
    output_tokens: 200,
    cost_usd: 0,
    actual_cost_usd: 0,
    cost_usd_equivalent: 0.42,
    api_equivalent_usd: 0.42,
    cost_effective_usd: 0.42,
    billing_neuralwatt_kwh: 0,
    billing_neuralwatt_cost_usd: 0,
    run_count: 1,
    provider: "anthropic",
    model: "claude-opus-4-8",
    ...over,
    provider_model_source: over.provider_model_source ?? "run_metadata",
    unknown_run_count: over.unknown_run_count ?? 0,
  };
}

function rollupRoot(over: Partial<WindowedRollupRoot> = {}): WindowedRollupRoot {
  const worker = rollupWorker();
  return {
    id: "t_mother",
    title: "Mother A",
    status: "done",
    assignee: "orchestrator",
    created_at: 100,
    started_at: 110,
    completed_at: 200,
    ended_at: 200,
    providers: ["anthropic"],
    cost_usd: 0,
    cost_usd_equivalent: 0.42,
    cost_effective_usd: 0.42,
    unknown_run_count: 0,
    billing_mode: "subscription_included",
    neuralwatt: null,
    runtime_seconds: 90,
    workers: [worker],
    runners: [{
      id: 501,
      task_id: "t_worker",
      profile: "coder",
      provider: "anthropic",
      model: "claude-opus-4-8",
      provider_model_source: "run_metadata",
      input_tokens: 1000,
      output_tokens: 200,
      cost_usd: 0,
      cost_usd_equivalent: 0.42,
      cost_effective_usd: 0.42,
      billing_mode: "subscription_included",
      neuralwatt: null,
      started_at: 120,
      ended_at: 180,
      runtime_seconds: 60,
    }],
    ...over,
  };
}

function rollupResponse(root: WindowedRollupRoot = rollupRoot()): WindowedRollupResponse {
  return {
    schema: "kanban-windowed-rollup-v1",
    since_hours: 168,
    now: 200,
    completed_roots: 1,
    roots: [root],
  };
}

describe("WorkerEfficiencySection (B3)", () => {
  it("renders worker-efficiency metrics from rollup roots and attributed review verdicts", () => {
    const root = rollupRoot({
      id: "t_b3",
      workers: [
        rollupWorker({
          profile: "coder",
          input_tokens: 9000,
          output_tokens: 1000,
          run_count: 2,
          cost_usd_equivalent: 1.5,
          api_equivalent_usd: 1.5,
          cost_effective_usd: 1.5,
        }),
        rollupWorker({
          profile: "premium",
          input_tokens: 4000,
          output_tokens: 1000,
          run_count: 1,
          cost_usd_equivalent: 2,
          api_equivalent_usd: 2,
          cost_effective_usd: 2,
        }),
      ],
      runners: [
        {
          id: 501,
          task_id: "t_worker",
          profile: "coder",
          provider: "anthropic",
          model: "claude-opus-4-8",
          provider_model_source: "run_metadata",
          input_tokens: 9000,
          output_tokens: 1000,
          cost_usd: 0,
          cost_usd_equivalent: 1.5,
          cost_effective_usd: 1.5,
          billing_mode: "subscription_included",
          neuralwatt: null,
          started_at: 120,
          ended_at: 180,
          runtime_seconds: 60,
        },
        {
          id: 502,
          task_id: "t_premium",
          profile: "premium",
          provider: "openai",
          model: "gpt-5.5",
          provider_model_source: "run_metadata",
          input_tokens: 4000,
          output_tokens: 1000,
          cost_usd: 0,
          cost_usd_equivalent: 2,
          cost_effective_usd: 2,
          billing_mode: "subscription_included",
          neuralwatt: null,
          started_at: 130,
          ended_at: 190,
          runtime_seconds: 60,
        },
      ],
    });

    const html = renderToStaticMarkup(
      <WorkerEfficiencySection
        roots={[root]}
        profiles={[
          profile({ profile: "coder", judged: 10, approved: 6, rejected: 4 }),
          profile({ profile: "premium", judged: 10, approved: 9, rejected: 1 }),
        ]}
      />,
    );

    expect(html).toContain('data-testid="worker-efficiency"');
    expect(html).toContain("Worker vergleichen");
    expect(html).toContain("Token/Task");
    expect(html).toContain("Token/min");
    expect(html).toContain("$/Task");
    expect(html).toContain("Review zurück");
    expect(html).toContain("Coder-Review-Schleifen senken");
    expect(html).toContain("Premium für riskante Tasks");
    expect(html).not.toContain("Nutzerwert");
    expect(html).not.toContain("Meta Burn");
    expect(html).not.toContain("API-Äquivalent");
    expect(html).not.toContain("gesch.");
  });

  it("keeps review-return honest when a worker has no attributed review outputs", () => {
    const html = renderToStaticMarkup(
      <WorkerEfficiencySection
        roots={[rollupRoot()]}
        profiles={[profile({ profile: "coder", judged: 0, approved: 0, rejected: 0 })]}
      />,
    );

    expect(html).toContain("Keine Review-Daten im Fenster.");
    expect(html).toContain("Die Rücklaufquote ist noch nicht bewertbar.");
    expect(html).toContain("Token/Task");
    expect(html).toContain("—");
  });

  it("places B3 before the legacy Statistik masthead", () => {
    windowedRollupMock.state = rollupState({ data: rollupResponse() });
    controlDataMock.reliability = controlState({
      now: 1_780_000_000,
      profiles: [profile({ profile: "coder", judged: 3, approved: 2, rejected: 1 })],
      baseline: [],
    });

    const html = renderToStaticMarkup(<StatistikView />);

    const b3 = html.indexOf('data-testid="worker-efficiency"');
    const masthead = html.indexOf('data-testid="stats-masthead-figure"');
    expect(b3).toBeGreaterThanOrEqual(0);
    expect(masthead).toBeGreaterThan(b3);
  });
});

describe("MotherLedgerSection", () => {
  it("zeigt den harten Fehlerzustand nur wenn noch keine Rollup-Daten vorliegen", () => {
    windowedRollupMock.state = rollupState({ data: null, error: "timeout", loading: false });

    const html = renderToStaticMarkup(<MotherLedgerSection />);

    expect(html).toContain("Kosten konnten nicht geladen werden");
    expect(html).not.toContain("Mother A");
    expect(html).not.toContain("Letzte Daten angezeigt");
  });

  it("rendert letzte gute Daten weiter und markiert sie bei transientem Fehler", () => {
    windowedRollupMock.state = rollupState({
      data: rollupResponse(),
      error: "timeout",
      isStale: true,
      lastUpdated: 1782070000,
    });

    const html = renderToStaticMarkup(<MotherLedgerSection />);

    expect(html).toContain("Mother A");
    expect(html).toContain("Kettenkosten — pro Worker");
    expect(html).toContain("Abo-Wert verbraucht · 7T");
    expect(html).toContain("Echt ausgegeben · 7T");
    expect(html).toContain("$0.42");
    expect(html).toContain("gesch.");
    expect(html).toContain("echt —");
    expect(html).toContain("Cache/Backend gerade nicht frisch; zeige letzten erfolgreichen Stand.");
    expect(html).not.toContain("Kosten konnten nicht geladen werden");
  });

  it("zeigt in Läufer-Zeilen Abo-Wert und echte USD getrennt", () => {
    const worker = rollupWorker();
    const root = rollupRoot({ workers: [worker] });

    const html = renderToStaticMarkup(<LedgerWorkerRunners root={root} worker={worker} />);

    expect(html).toContain("#501");
    expect(html).toContain("$0.42");
    expect(html).toContain("Abo-Wert (gesch.): $0.42 gesch.");
    expect(html).toContain("Echt $: —");
    expect(html).toContain("st-ledger-abo");
    expect(html).toContain("st-ledger-real");
    expect(html).toContain("<small>gesch.</small>");
  });

  it("marks the unified MotherLedger responsive layout as mobile at the 390px Statistik viewport", () => {
    const html = renderStatistikAtViewport(390);

    expect(html).toContain('data-ledger-viewport="mobile"');
    expect(html).toContain('class="st-ledger" data-ledger-viewport="mobile"');
    expect(html).toContain("Abo-Wert verbraucht · 7T");
    expect(html).toContain("Echt ausgegeben · 7T");
    expect(html).toContain("st-ledger-meter");
    expect(html).toContain("Mother A");
    expect(html).toContain("echt —");
  });

  it("marks the unified MotherLedger responsive layout as desktop at the 1440px Statistik viewport", () => {
    const html = renderStatistikAtViewport(1440);

    expect(html).toContain('data-ledger-viewport="desktop"');
    expect(html).toContain('class="st-ledger" data-ledger-viewport="desktop"');
    expect(html).toContain("Abo-Wert verbraucht · 7T");
    expect(html).toContain("Echt ausgegeben · 7T");
    expect(html).toContain("st-ledger-meter");
    expect(html).toContain("Mother A");
    expect(html).toContain("echt —");
  });
});

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
    expect(html).toContain('class="st-mast"');
    expect(html).toContain("91");
    expect(html).toContain("118 abgenommen · 12 verworfen");
    // Δ vs the 90 % baseline → +1 pp, ok-status.
    expect(html).toContain("1 pp ggü. 30 Tg");
    expect(html).toContain("st-mast-delta text-status-ok");
    // Masthead meta carries the German date + window.
    expect(html).toContain("18. Juni · 7 Tage");
    // Supporting KPIs: Autonomie 27/30 = 90 %, $1.04/Lieferung, 5 Nutzer.
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
    expect(html).toContain("Latenz");
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
    expect(html).toContain("st-lr-score text-status-ok"); // coder 95 % → ok ink
    expect(html).toContain("20 Läufe");
  });

  it("renders a calm empty state with no roster runs", () => {
    const html = renderToStaticMarkup(<ReliabilitySection profiles={[profile({ profile: "w" })]} />);
    expect(html).toContain("Noch keine Profil-Läufe im Fenster.");
    expect(html).toContain("Die Verlässlichkeit ist noch nicht bewertbar.");
    expect((html.match(/Noch keine Profil-Läufe im Fenster\./g) ?? []).length).toBe(1);
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
    expect(html).toContain("st-estack");
    expect(html).toContain(`background:${ERROR_SERIES[0]}`);
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
    expect(html).toContain("Das Fehlerfenster ist unauffällig.");
    expect(html).not.toContain("st-estack");
  });

  it("marks the unified MotherLedger responsive layout as desktop at the 1440px Statistik viewport", () => {
    const html = renderStatistikAtViewport(1440);

    expect(html).toContain('data-ledger-viewport="desktop"');
    expect(html).not.toContain("<table");
  });

  it("renders the /runs/costs-series trend from live token and api-equivalent fields", () => {
    controlDataMock.costSeries = controlState({
      days: 7,
      now: 1_800_000_000,
      series: [costSeriesPoint()],
      field_sources: {
        tokens: "task_runs.input_tokens/output_tokens/cached_tokens plus metadata fallbacks",
        api_equivalent_usd: "task_runs.cost_usd_equivalent plus metadata cost fallbacks",
      },
    });

    const html = renderToStaticMarkup(<StatistikView />);

    expect(html).toContain('data-testid="runs-costs-series-trend"');
    expect(html).toContain("07-05");
    expect(html).toContain("2 k");
    expect(html).toContain("$ 0.42");
    expect(html).toContain("task_runs");
  });
});

describe("BudgetLedgerSection", () => {
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
    expect(html).toContain("st-led-row");
    // Engpass lead names the tightest window (Claude · Woche · 92 %).
    expect(html).toContain("st-lead");
    expect(html).toContain("Claude Woche bei 92 %");
    // Claude (92 %) sorts before ChatGPT (40 %); both render.
    expect(html.indexOf("ChatGPT")).toBeGreaterThan(0);
    expect(html).toContain("st-fig text-status-alert"); // 92 % → crit ink
    // Kimi is flagged estimated and has no provider limit.
    expect(html).toContain("st-tag");
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
    expect(html).toContain("st-subburn-grid");
    // No trend block when daily is empty.
    expect(html).not.toContain("data-testid=\"subscription-burn-trend\"");
  });

  it("renders the Zeit-Trend block when daily data is present", () => {
    const html = renderToStaticMarkup(
      <SubscriptionBurnSection
        burn={{
          days: 7,
          now: 100,
          window_start: 0,
          totals: { runs: 8, input_tokens: 900, output_tokens: 100, total_tokens: 1000 },
          by_lane: [
            { subscription: "codex", profile: "coder", runs: 8, input_tokens: 900, output_tokens: 100, total_tokens: 1000 },
          ],
          by_class: [],
          daily: [
            { subscription: "codex", date: "2026-06-17", runs: 3, input_tokens: 300, output_tokens: 100, total_tokens: 400 },
            { subscription: "codex", date: "2026-06-18", runs: 5, input_tokens: 500, output_tokens: 100, total_tokens: 600 },
          ],
          buckets: [],
        }}
      />,
    );

    expect(html).toContain("data-testid=\"subscription-burn-trend\"");
    // Both dates must appear in the trend block.
    expect(html).toContain("2026-06-17");
    expect(html).toContain("2026-06-18");
    // Token values formatted (400 → "400", 600 → "600").
    expect(html).toContain("400");
    expect(html).toContain("600");
    // i18n kicker.
    expect(html).toContain("Zeit-Trend");
  });
});

describe("A3: MotherLedger Summen-Ehrlichkeit", () => {
  it("summiert Abo-Wert und zeigt genuinely unknown Roots als unbekannt statt $0.00", () => {
    const knownRoot = rollupRoot({ cost_effective_usd: 1.5, cost_usd_equivalent: 1.5, cost_usd: 0 });
    const unknownRoot = rollupRoot({
      id: "t_unknown",
      title: "Unknown A",
      cost_effective_usd: null,
      cost_usd_equivalent: null,
      cost_usd: null,
    });
    windowedRollupMock.state = rollupState({
      data: { ...rollupResponse(knownRoot), roots: [knownRoot, unknownRoot], completed_roots: 2 },
    });

    const html = renderToStaticMarkup(<MotherLedgerSection />);

    // Known Abo sum $1.50; the unknown root keeps null-cost honesty in its own header.
    expect(html).toContain("$1.50");
    expect(html).toContain("Unknown A");
    expect(html).toContain("Abo-Wert verbraucht · 7T");
    expect(html).toContain("Abo-Wert (gesch.): — gesch.");
    expect(html).toContain("<b class=\"st-mono\">—</b><small>gesch.</small>");
  });

  it("zeigt keine unbekannt-Meldung wenn alle Roots Kostenwerte haben", () => {
    windowedRollupMock.state = rollupState({ data: rollupResponse() });

    const html = renderToStaticMarkup(<MotherLedgerSection />);

    expect(html).not.toContain("unbekannt");
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
        reviewValue={[]}
        chainRate={0.75}
        queueWaitSeconds={240}
      />,
    );
    expect(html).toContain("Flotten-Effizienz");
    // Chain-Completion 75 %, Queue-Wait p50 = 4m, Gate 10/100 = 10 %.
    expect(html).toContain("Ketten-Abschluss");
    expect(html).toContain("75");
    expect(html).toContain("Queue-Wartezeit");
    expect(html).toContain("4m");
    expect(html).toContain("Gate-Quote");
    // Token-Burn leaderboard: coder 1.2 M, phantom dropped.
    expect(html).toContain("Token-Burn je Lane");
    expect(html).toContain("st-lr");
    expect(html).toContain("1.2 M");
    expect(html).toContain("Coder");
    expect(html).toContain("12 Läufe");
    expect(html).not.toContain("9.0 M");
  });

  it("bindet Token-Burn an die echten /runs/costs-Felder statt an Tages-0-Dollar", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection
        profiles={[]}
        costs={[
          costRow({
            profile: "coder",
            runs: 7,
            cost_usd: 0,
            actual_cost_usd: 0,
            api_equivalent_usd: 0.91,
            input_tokens: 1_234_567,
            output_tokens: 765_433,
          }),
        ]}
        reviewValue={[]}
        chainRate={null}
        queueWaitSeconds={null}
      />,
    );

    expect(html).toContain("Token-Burn je Lane");
    expect(html).toContain("Coder");
    expect(html).toContain("2.0 M");
    expect(html).toContain("7 Läufe");
    expect(html).not.toContain("$0.00");
  });

  it("stays calm with em-dashes and an empty-burn note when nothing ran", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection profiles={[]} costs={[]} reviewValue={[]} chainRate={null} queueWaitSeconds={null} />,
    );
    expect(html).toContain("Noch kein Token-Burn im Fenster.");
    expect(html).toContain("Noch keine Review-Läufe im Fenster.");
    expect(html).toContain("—");
  });

  it("shows a review-value row per active stage with quote, findings and tokens/finding", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection
        profiles={[]}
        costs={[]}
        reviewValue={[
          // verifier: field present, 1 blocker + 2 obs = 3 findings, 300 K in →
          // 100 K / finding; quote 3/4 = 75 %.
          reviewRow({
            profile: "verifier",
            runs: 4,
            approved: 3,
            request_changes: 1,
            findings_blocking: 1,
            findings_observations: 2,
            input_tokens: 300_000,
            tokens_per_finding: 100_000,
          }),
          // reviewer: Altbestand — findings/tokens NULL despite runs; quote —.
          reviewRow({ profile: "reviewer", runs: 5, approved: 0, request_changes: 0 }),
          // critic: 0 runs → filtered out entirely.
          reviewRow({ profile: "critic", runs: 0 }),
        ]}
        chainRate={null}
        queueWaitSeconds={null}
      />,
    );
    expect(html).toContain("Review-Wert je Stufe");
    // verifier row: 3 findings (score), 75 % quote, 100 K je Fund, 4 Läufe.
    expect(html).toContain("Verifier");
    expect(html).toContain("75 %");
    expect(html).toContain("100 k je Fund");
    expect(html).toContain("4 Läufe");
    // reviewer NULL row: quote/findings/tokens all em-dash, still 5 Läufe.
    expect(html).toContain("Reviewer");
    expect(html).toContain("5 Läufe");
    // critic (0 runs) is not rendered.
    expect(html).not.toContain("Critic");
  });

  it("renders the scout stage with read-evidence value, not findings/quote", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection
        profiles={[]}
        costs={[]}
        reviewValue={[
          // scout: read-only Recon — 40 gelesene Items (score), 120 K in →
          // 3 K je Item; kein Verdikt, keine Funde, keine Quote.
          reviewRow({
            profile: "scout",
            runs: 6,
            input_tokens: 120_000,
            read_items: 40,
            tokens_per_read_item: 3_000,
          }),
        ]}
        chainRate={null}
        queueWaitSeconds={null}
      />,
    );
    expect(html).toContain("Scout");
    // read-evidence: 40 items as score, 3 K je Item, 6 Läufe.
    expect(html).toContain("40");
    expect(html).toContain("3 k je Item");
    expect(html).toContain("6 Läufe");
    // scout has no verdict path: no per-finding cost line (the "Gate-Quote" KPI
    // above is unrelated, so we key on the unique "je Fund" verdict marker).
    expect(html).not.toContain("je Fund");
  });

  it("shows an em-dash for a scout stage without read-metadata (Altbestand)", () => {
    const html = renderToStaticMarkup(
      <EffizienzSection
        profiles={[]}
        costs={[]}
        reviewValue={[reviewRow({ profile: "scout", runs: 3 })]}
        chainRate={null}
        queueWaitSeconds={null}
      />,
    );
    expect(html).toContain("Scout");
    expect(html).toContain("3 Läufe");
    expect(html).toContain("—");
    expect(html).not.toContain("je Item");
  });
});

describe("StatistikView (W3-3 masthead removal)", () => {
  it("no longer renders its own masthead — the shell Puls-Leiste carries the route label since W3-3", () => {
    windowedRollupMock.state = rollupState({ data: rollupResponse() });
    controlDataMock.reliability = controlState({
      now: 1_780_000_000,
      profiles: [profile({ profile: "coder", judged: 3, approved: 2, rejected: 1 })],
      baseline: [],
    });

    const html = renderToStaticMarkup(<StatistikView />);

    expect(html).not.toContain("st-masthead");
    expect(html).not.toContain("st-brand");
    expect(html).not.toContain("st-live-dot");
    // The Akzeptanzrate hero (StatsMasthead) is real content, not chrome —
    // it stays, unlike the removed brand/LIVE-dot band.
    expect(html).toContain("Akzeptanzrate");
  });

  it("gives the MotherLedger window/sort chipset a >=44px hit-area (W3-3 touch target, 5 controls baseline-flagged <24px)", () => {
    windowedRollupMock.state = rollupState({ data: rollupResponse() });

    const html = renderToStaticMarkup(<MotherLedgerSection />);

    // h-12/min-h-12 = 3rem = 45px am 15px-Root (die visual-verify-Heuristik
    // flaggt nur <24px und kann die 44px-AC daher nicht beweisen — das
    // müssen die Klassen selbst tun).
    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>7T/);
    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>24Std/);
    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Abo-Wert/);
    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Tokens/);
    expect(html).toMatch(/class="[^"]*\bmin-h-12\b[^"]*"[^>]*>Runs/);
  });
});
