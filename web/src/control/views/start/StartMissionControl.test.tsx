import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

const hooks = vi.hoisted(() => ({
  useAccountUsage: vi.fn(),
  useHostUsage: vi.fn(),
  useHermesSubscriptionBurn: vi.fn(),
  useDecisionInbox: vi.fn(),
  useHermesRunsDaily: vi.fn(),
  useHermesTodayDigest: vi.fn(),
  useStatsConfig: vi.fn(),
  useSystemHealth: vi.fn(),
  useBoard: vi.fn(),
  useHermesWorkers: vi.fn(),
  useStartIssues: vi.fn(),
  useProjectCommits: vi.fn(),
}));

vi.mock("../../hooks/costsUsage", () => ({
  useAccountUsage: hooks.useAccountUsage,
  useHostUsage: hooks.useHostUsage,
  useHermesSubscriptionBurn: hooks.useHermesSubscriptionBurn,
}));
vi.mock("../../hooks/projekte", () => ({ useProjectCommits: hooks.useProjectCommits }));
vi.mock("../../hooks/decisionInbox", () => ({ useDecisionInbox: hooks.useDecisionInbox }));
vi.mock("../../hooks/runsDigestRollup", () => ({
  useHermesRunsDaily: hooks.useHermesRunsDaily,
  useHermesTodayDigest: hooks.useHermesTodayDigest,
}));
vi.mock("../../hooks/stats", () => ({ useStatsConfig: hooks.useStatsConfig }));
vi.mock("../../hooks/systemReleaseHealth", () => ({ useSystemHealth: hooks.useSystemHealth }));
vi.mock("../../hooks/workersBoard", () => ({
  useBoard: hooks.useBoard,
  useHermesWorkers: hooks.useHermesWorkers,
}));
vi.mock("./useStartIssues", () => ({ useStartIssues: hooks.useStartIssues }));

import { StartMissionControl } from "./StartMissionControl";

const poll = <T,>(data: T) => ({
  data,
  loading: false,
  error: null,
  isStale: false,
  lastUpdated: 1_784_642_400,
});

function installFixtures() {
  hooks.useAccountUsage.mockReturnValue(poll({
    providers: [{
      provider: "anthropic",
      available: true,
      source: "live",
      fetched_at: "2026-07-21T19:00:00Z",
      signal_at: "2026-07-21T19:00:00Z",
      title: "Claude",
      plan: "Max",
      windows: [{ label: "Week", window_key: "weekly", used_percent: 82, reset_at: "2026-07-24T12:00:00Z", detail: null }],
      details: [],
      unavailable_reason: null,
      cached: false,
    }, {
      provider: "openai-codex",
      available: true,
      source: "usage_api",
      fetched_at: "2026-07-21T19:00:00Z",
      signal_at: "2026-07-21T19:00:00Z",
      title: "ChatGPT / Codex",
      plan: "Pro",
      windows: [{ label: "Weekly", window_key: "weekly", used_percent: 21, reset_at: "2026-07-28T17:08:39Z", detail: null }],
      details: [],
      unavailable_reason: null,
      cached: false,
    }, {
      provider: "kimi",
      available: true,
      source: "usage_api",
      fetched_at: "2026-07-21T19:00:00Z",
      signal_at: "2026-07-21T19:00:00Z",
      title: "Kimi",
      plan: "Advanced",
      windows: [{ label: "Diese Woche", window_key: "weekly", used_percent: 79, reset_at: "2026-07-27T00:00:00Z", detail: "21/100 verbleibend" }],
      details: [],
      unavailable_reason: null,
      cached: false,
    }, {
      provider: "xai",
      available: true,
      source: "billing_api",
      fetched_at: "2026-07-21T19:00:00Z",
      signal_at: "2026-07-21T19:00:00Z",
      title: "Grok",
      plan: null,
      windows: [{ label: "Diese Woche", window_key: "weekly", used_percent: 25, reset_at: "2026-07-26T17:58:33Z", detail: null }],
      details: [],
      unavailable_reason: null,
      cached: false,
    }],
    cache_ttl_seconds: 300,
  }));
  hooks.useHostUsage.mockReturnValue(poll({
    generated_at: 1_784_642_400,
    days: 7,
    dates: ["2026-07-15", "2026-07-16", "2026-07-17", "2026-07-18", "2026-07-19", "2026-07-20", "2026-07-21"],
    total_tokens: 3_000_000,
    total_sessions: 14,
    active_tmux_panes: 7,
    sources: [{ source: "hermes", label: "Hermes", tokens: 2_000_000, sessions: 8 }, { source: "terminal", label: "Terminals", tokens: 1_000_000, sessions: 6 }],
    providers: [
      { provider: "claude", label: "Claude", total_tokens: 2_500_000, sessions: 10, daily: [{ date: "2026-07-21", tokens: 2_500_000, sessions: 10 }] },
      { provider: "kimi", label: "Kimi", total_tokens: 500_000, sessions: 4, daily: [{ date: "2026-07-21", tokens: 500_000, sessions: 4 }] },
    ],
    errors: [],
    accounting_note: "Aktive Ein-/Ausgabe ohne Cache",
    cached: false,
  }));
  hooks.useHermesSubscriptionBurn.mockReturnValue(poll({
    days: 7,
    now: 1_784_642_400,
    window_start: 1_784_037_600,
    totals: { runs: 3, completed_runs: 2, failed_runs: 1, blocked_runs: 0, input_tokens: 2_700_000, output_tokens: 300_000, total_tokens: 3_000_000 },
    by_lane: [{ subscription: "claude", profile: "premium", runs: 3, completed_runs: 2, failed_runs: 1, blocked_runs: 0, input_tokens: 2_700_000, output_tokens: 300_000, total_tokens: 3_000_000 }],
    by_class: [],
    daily: [{ subscription: "claude", date: "2026-07-21", runs: 3, completed_runs: 2, failed_runs: 1, blocked_runs: 0, input_tokens: 2_700_000, output_tokens: 300_000, total_tokens: 3_000_000 }],
    buckets: [],
  }));
  hooks.useDecisionInbox.mockReturnValue({
    items: [{ key: "held", title: "Release prüfen", why: "Verifier fordert Entscheidung", nextAction: "Öffnen", target: "/control/fleet?task=t1" }],
    summary: { total: 1 },
    loading: false,
  });
  hooks.useHermesRunsDaily.mockReturnValue(poll({
    days: 30,
    now: 1_784_642_400,
    series: [{ date: "2026-07-21", done_roots: 4, done_roots_by_class: { nutzer: 2, haertung: 1, meta: 1 }, done_tasks: 7, cost_usd: 0, input_tokens: 2_700_000, output_tokens: 300_000, runs_completed: 8, runs_failed: 2, cycle_time_p50_seconds: 300 }],
  }));
  hooks.useHermesTodayDigest.mockReturnValue(poll({
    count: 1,
    items: [{ run_id: 1, task_id: "t_done", task_title: "Provider-Audit geliefert", deliverable_excerpt: "Live-Signal verifiziert", task_summary: null, verification_state: "approved", verdict_label: "APPROVED" }],
  }));
  hooks.useStatsConfig.mockReturnValue(poll(null));
  hooks.useSystemHealth.mockReturnValue(poll({ overall: "healthy" }));
  hooks.useHermesWorkers.mockReturnValue(poll({ workers: [{ id: "w1" }] }));
  hooks.useBoard.mockReturnValue(poll({
    now: 1_784_642_400,
    columns: [{ name: "blocked", tasks: [{ id: "t1", status: "blocked" }, { id: "t2", status: "done" }] }],
  }));
  hooks.useStartIssues.mockReturnValue(poll({
    days: 7,
    now: 1_784_642_400,
    total_failed_runs: 3,
    group_count: 2,
    truncated: false,
    issues: [
      { signature: "timeout", profile: "premium", cause_key: "budget", cause_label: "Zeit / Iterationen", cause_hint: "Budget erschöpft", count: 2, first_seen: 1, last_seen: 2, outcomes: { timed_out: 2 }, example_run_id: 1, example_task_id: "t1", example_task_title: "Lauf braucht zu lange", example_assignee: "premium", example_block_kind: null, example_text: "Zeitbudget wurde ausgeschöpft" },
      { signature: "block", profile: "reviewer", cause_key: "review", cause_label: "Review-Korrektur", cause_hint: "Nachweis fehlt", count: 1, first_seen: 1, last_seen: 2, outcomes: { blocked: 1 }, example_run_id: 2, example_task_id: "t2", example_task_title: "Prüfnachweis ergänzen", example_assignee: "coder", example_block_kind: "review_revision", example_text: "Reviewer fordert einen Grenzfall-Nachweis" },
    ],
  }));
  hooks.useProjectCommits.mockReturnValue(poll({
    generated_at: 1_784_642_400,
    errors: [],
    commits: [{
      project: "hermes-infra",
      project_name: "Hermes",
      hash: "210b4e51e",
      message: "kanban(t_1a2b3c4d): wire live provider usage",
      author: "Hermes Worker",
      committed_at: 1_784_642_000,
      age_seconds: 400,
      attribution: { kind: "kanban", pack: null, task_id: "t_1a2b3c4d", lane: "coder", model: "gpt", label: "Live Provider-Nutzung vereinheitlichen" },
    }],
  }));
}

describe("StartMissionControl", () => {
  it("renders A4.2 entirely from live response values", () => {
    installFixtures();
    const html = renderToStaticMarkup(
      <MemoryRouter initialEntries={["/control"]}>
        <StartMissionControl density="compact" />
      </MemoryRouter>,
    );

    expect(html).toContain("Provider-Matrix");
    expect(html).toContain("3.0 M");
    expect(html).toContain("4/4 live");
    expect(html).toContain("Claude: 82 Prozent verbraucht");
    expect(html).toContain("ChatGPT / Codex: 21 Prozent verbraucht");
    expect(html).toContain("Kimi: 79 Prozent verbraucht");
    expect(html).toContain("Grok: 25 Prozent verbraucht");
    expect(html).toContain("7 tmux");
    expect(html).toContain("1 offen");
    expect(html).toContain("2 heute");
    expect(html).toContain("4 Vorhaben");
    expect(html).toContain("Release prüfen");
    expect(html).toContain("Provider-Audit geliefert");
    expect(html).toContain("Zeit / Iterationen");
    expect(html).toContain("Prüfnachweis ergänzen");
    expect(html).toContain("Live Provider-Nutzung vereinheitlichen");
    expect(html).toContain("210b4e51e");
    expect(html).not.toContain("kein Prozent-Signal");
    expect(html).not.toContain("Autonomie");
  });
});
