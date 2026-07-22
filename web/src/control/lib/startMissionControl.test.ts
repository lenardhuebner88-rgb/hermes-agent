import { describe, expect, it } from "vitest";
import type { AccountUsageProvider } from "./types";
import type { RunsIssuesResponse, SubscriptionTokenBurnResponse } from "./schemas";
import { DEFAULT_STATS_CONFIG } from "./statsFields";
import {
  aggregateStartIssueCauses,
  buildStartCapacityCards,
  buildStartProviderRows,
  classifyCommitTheme,
  readableCommitTopic,
  startDateAxis,
  startFlowFromToday,
  visibleAccountProviders,
} from "./startMissionControl";

const providers: AccountUsageProvider[] = [
  {
    provider: "anthropic",
    available: true,
    source: "live",
    fetched_at: "2026-07-21T19:00:00Z",
    title: "Claude",
    plan: "Max",
    windows: [
      { label: "Week", window_key: "weekly", used_percent: 89, reset_at: "2026-07-24T12:00:00Z", detail: null },
      { label: "Session", window_key: "session", used_percent: 9, reset_at: null, detail: null },
    ],
    details: [],
    unavailable_reason: null,
    cached: false,
  },
  {
    provider: "openai-codex",
    available: true,
    source: "live",
    fetched_at: "2026-07-21T19:00:00Z",
    title: "Codex",
    plan: "Pro",
    windows: [{ label: "Week", window_key: "weekly", used_percent: 3, reset_at: null, detail: null }],
    details: [],
    unavailable_reason: null,
    cached: false,
  },
  {
    provider: "kimi",
    available: true,
    source: "usage_api",
    fetched_at: "2026-07-21T19:00:00Z",
    title: "Kimi",
    plan: "Advanced",
    windows: [
      { label: "Week", window_key: "weekly", used_percent: 79, reset_at: null, detail: "21/100 verbleibend" },
      { label: "Session", window_key: "session", used_percent: 16, reset_at: null, detail: "84/100 verbleibend" },
    ],
    details: [],
    unavailable_reason: null,
    cached: false,
  },
  {
    provider: "xai",
    available: true,
    source: "billing_api",
    fetched_at: "2026-07-21T19:00:00Z",
    title: "Grok",
    plan: "SuperGrok",
    windows: [{ label: "Week", window_key: "weekly", used_percent: 25, reset_at: null, detail: null }],
    details: [],
    unavailable_reason: null,
    cached: false,
  },
  {
    provider: "openrouter",
    available: true,
    source: "credits_api",
    fetched_at: "2026-07-21T19:00:00Z",
    title: "OpenRouter",
    plan: null,
    windows: [],
    details: ["Guthaben: 8.50 USD"],
    unavailable_reason: null,
    cached: false,
  },
];

const burn = {
  days: 2,
  now: 1_784_642_400,
  window_start: 1,
  totals: { runs: 5, completed_runs: 4, failed_runs: 1, blocked_runs: 0, input_tokens: 4_050_000, output_tokens: 450_000, total_tokens: 4_500_000 },
  by_lane: [
    { subscription: "claude", profile: "premium", runs: 2, input_tokens: 1_800_000, output_tokens: 200_000, total_tokens: 2_000_000, completed_runs: 1, failed_runs: 1, blocked_runs: 0 },
    { subscription: "chatgpt", profile: "coder", runs: 2, input_tokens: 1_800_000, output_tokens: 200_000, total_tokens: 2_000_000, completed_runs: 2, failed_runs: 0, blocked_runs: 0 },
    { subscription: "grok", profile: "research", runs: 1, input_tokens: 450_000, output_tokens: 50_000, total_tokens: 500_000, completed_runs: 1, failed_runs: 0, blocked_runs: 0 },
  ],
  by_class: [],
  daily: [
    { subscription: "claude", date: "2026-07-20", runs: 1, input_tokens: 900_000, output_tokens: 100_000, total_tokens: 1_000_000, completed_runs: 1, failed_runs: 0, blocked_runs: 0 },
    { subscription: "claude", date: "2026-07-21", runs: 1, input_tokens: 900_000, output_tokens: 100_000, total_tokens: 1_000_000, completed_runs: 0, failed_runs: 1, blocked_runs: 0 },
    { subscription: "chatgpt", date: "2026-07-21", runs: 2, input_tokens: 1_800_000, output_tokens: 200_000, total_tokens: 2_000_000, completed_runs: 2, failed_runs: 0, blocked_runs: 0 },
    { subscription: "grok", date: "2026-07-21", runs: 1, input_tokens: 450_000, output_tokens: 50_000, total_tokens: 500_000, completed_runs: 1, failed_runs: 0, blocked_runs: 0 },
  ],
  buckets: [],
} as SubscriptionTokenBurnResponse & {
  by_lane: Array<SubscriptionTokenBurnResponse["by_lane"][number] & { completed_runs: number; failed_runs: number; blocked_runs: number }>;
  daily: Array<SubscriptionTokenBurnResponse["daily"][number] & { completed_runs: number; failed_runs: number; blocked_runs: number }>;
};

const hostUsage = {
  generated_at: burn.now,
  days: 2,
  dates: ["2026-07-20", "2026-07-21"],
  total_tokens: 4_750_000,
  total_sessions: 11,
  active_tmux_panes: 7,
  sources: [
    { source: "hermes", label: "Hermes", tokens: 3_000_000, sessions: 5 },
    { source: "terminal", label: "Terminals", tokens: 1_750_000, sessions: 6 },
  ],
  providers: [
    { provider: "claude", label: "Claude", total_tokens: 2_000_000, sessions: 3, daily: [{ date: "2026-07-20", tokens: 1_000_000, sessions: 1 }, { date: "2026-07-21", tokens: 1_000_000, sessions: 2 }] },
    { provider: "codex", label: "Codex", total_tokens: 2_000_000, sessions: 4, daily: [{ date: "2026-07-20", tokens: 0, sessions: 0 }, { date: "2026-07-21", tokens: 2_000_000, sessions: 4 }] },
    { provider: "kimi", label: "Kimi", total_tokens: 250_000, sessions: 2, daily: [{ date: "2026-07-20", tokens: 50_000, sessions: 1 }, { date: "2026-07-21", tokens: 200_000, sessions: 1 }] },
    { provider: "grok", label: "Grok", total_tokens: 500_000, sessions: 2, daily: [{ date: "2026-07-20", tokens: 0, sessions: 0 }, { date: "2026-07-21", tokens: 500_000, sessions: 2 }] },
  ],
  errors: [],
  accounting_note: "Aktive Ein-/Ausgabe ohne Cache",
  cached: false,
};

describe("start mission-control derivations", () => {
  it("builds a continuous provider matrix from account limits and attributed burn", () => {
    const rows = buildStartProviderRows({ providers, burn, hostUsage, config: DEFAULT_STATS_CONFIG, nowMs: burn.now * 1000 });

    expect(rows.map((row) => row.key)).toEqual(["claude", "chatgpt", "kimi", "grok"]);
    expect(rows[0]).toMatchObject({ label: "Claude", totalTokens: 2_000_000, todayTokens: 1_000_000, weeklyPercent: 89, completedRuns: 1 });
    expect(rows[0].days).toHaveLength(2);
    expect(rows[1].successPerMillion).toBe(1);
    expect(rows[2]).toMatchObject({ label: "Kimi", tokenTelemetry: true, totalTokens: 250_000, capacityPercent: 79, totalSessions: 2 });
    expect(rows[3]).toMatchObject({ label: "Grok", plan: "SuperGrok", tokenTelemetry: true, totalTokens: 500_000, capacityPercent: 25 });
    expect(rows[1].days[1].intensity).toBe(1);
  });

  it("uses the tightest live window and excludes spend-only providers", () => {
    const withScopedLimit: AccountUsageProvider[] = providers.map((provider) => provider.provider === "anthropic"
      ? { ...provider, windows: [...provider.windows, { label: "Scoped", window_key: "scoped_week", used_percent: 99, reset_at: null, detail: "Fable" }] }
      : provider);
    const visible = visibleAccountProviders(withScopedLimit, DEFAULT_STATS_CONFIG);
    expect(visible.map((provider) => provider.provider)).toEqual(["anthropic", "openai-codex", "kimi", "xai"]);
    const rows = buildStartProviderRows({ providers: visible, burn, hostUsage, config: DEFAULT_STATS_CONFIG, nowMs: burn.now * 1000 });

    expect(rows[0]).toMatchObject({ capacityPercent: 99, capacityLabel: "Modell-Limit · Fable" });
    expect(rows.some((row) => row.providerId === "openrouter")).toBe(false);

    const cards = buildStartCapacityCards(visible, DEFAULT_STATS_CONFIG, burn.now * 1000);
    expect(cards.map((card) => card.providerId)).toEqual(["anthropic", "openai-codex", "kimi", "xai"]);
    expect(cards[0]).toMatchObject({
      label: "Claude",
      percent: 99,
      windowLabel: "Modell-Limit · Fable",
      state: "live",
      secondary: [
        { label: "Diese Woche", percent: 89 },
        { label: "5-Std-Fenster", percent: 9 },
      ],
    });
    expect(cards[2]).toMatchObject({ percent: 79, windowLabel: "Diese Woche", secondary: [{ label: "5-Std-Fenster", percent: 16 }] });
  });

  it("labels a recent verified provider snapshot as fallback without changing its percent", () => {
    const fallbackProviders = providers.map((provider) => provider.provider === "kimi"
      ? { ...provider, fallback: true, cached: true }
      : provider);
    const cards = buildStartCapacityCards(fallbackProviders, DEFAULT_STATS_CONFIG, burn.now * 1000);

    expect(cards.find((card) => card.providerId === "kimi")).toMatchObject({ percent: 79, state: "fallback" });
  });

  it("keeps the local date axis continuous across the requested window", () => {
    expect(startDateAxis(3, 1_784_642_400)).toEqual(["2026-07-19", "2026-07-20", "2026-07-21"]);
  });

  it("derives a non-overlapping daily flow from completed and failed runs", () => {
    expect(startFlowFromToday({
      date: "2026-07-21",
      done_roots: 5,
      done_roots_by_class: { nutzer: 1, haertung: 1, meta: 3 },
      done_tasks: 8,
      cost_usd: 0,
      input_tokens: 1,
      output_tokens: 1,
      runs_completed: 21,
      runs_failed: 12,
      cycle_time_p50_seconds: 300,
    })).toEqual({ ended: 33, successful: 21, failed: 12, friction: 12, delivered: 5, deliveredTasks: 8 });
  });

  it("groups raw issue outcomes into operator-readable causes", () => {
    const issues: RunsIssuesResponse = {
      days: 7,
      now: 1,
      total_failed_runs: 9,
      group_count: 3,
      truncated: false,
      issues: [
        { signature: "a", profile: "coder", cause_key: "budget", cause_label: "Zeit / Iterationen", cause_hint: "Budget erschöpft", count: 4, first_seen: 1, last_seen: 2, outcomes: { timed_out: 4 }, example_run_id: 1, example_task_id: "t1", example_task_title: "Budgetlauf", example_assignee: "coder", example_block_kind: null, example_text: "a" },
        { signature: "review", profile: "reviewer", cause_key: "review", cause_label: "Review-Korrektur", cause_hint: "Änderung nötig", count: 2, first_seen: 1, last_seen: 2, outcomes: { blocked: 2 }, example_run_id: 2, example_task_id: "t2", example_task_title: "Prüfung", example_assignee: "coder", example_block_kind: "review_revision", example_text: "review" },
        { signature: "b", profile: "premium", cause_key: "runtime", cause_label: "Start / Laufzeit", cause_hint: "Start fehlgeschlagen", count: 3, first_seen: 1, last_seen: 2, outcomes: { spawn_failed: 3 }, example_run_id: 3, example_task_id: "t3", example_task_title: "Worker", example_assignee: "premium", example_block_kind: null, example_text: "b" },
      ],
    };

    expect(aggregateStartIssueCauses(issues)).toEqual([
      { key: "budget", label: "Zeit / Iterationen", count: 4 },
      { key: "runtime", label: "Start / Laufzeit", count: 3 },
      { key: "review", label: "Review-Korrektur", count: 2 },
    ]);
  });

  it("turns commit evidence into a non-coder topic and theme", () => {
    const commit = {
      project: "hermes-infra",
      project_name: "Hermes",
      hash: "210b4e51e",
      message: "kanban(t_1a2b3c4d): wire live provider usage",
      author: "Hermes Worker",
      committed_at: 1,
      age_seconds: 2,
      attribution: { kind: "kanban", pack: null, task_id: "t_1a2b3c4d", lane: "coder", model: "gpt", label: "Live Provider-Nutzung vereinheitlichen" },
    } as const;

    expect(readableCommitTopic(commit)).toBe("Live Provider-Nutzung vereinheitlichen");
    expect(classifyCommitTheme(commit)).toBe("Provider-Nutzung");
  });
});
