import { describe, expect, it } from "vitest";
import { AccountUsageResponseSchema, HostUsageResponseSchema, SubscriptionTokenBurnResponseSchema } from "./costsUsage";

describe("AccountUsageResponseSchema", () => {
  it("marks live values and recent verified fallbacks explicitly", () => {
    const parsed = AccountUsageResponseSchema.parse({
      cache_ttl_seconds: 60,
      providers: [{
        provider: "kimi",
        available: true,
        source: "usage_api",
        fetched_at: "2026-07-21T22:00:00Z",
        signal_at: "2026-07-21T22:00:00Z",
        title: "Kimi",
        plan: "Advanced",
        windows: [{ label: "Diese Woche", window_key: "weekly", used_percent: 79, reset_at: null, detail: null }],
        details: [],
        unavailable_reason: null,
        cached: true,
        fallback: true,
      }],
    });

    expect(parsed.providers[0]).toMatchObject({ provider: "kimi", fallback: true });
    expect(parsed.providers[0].windows[0].used_percent).toBe(79);

    const legacy = AccountUsageResponseSchema.parse({
      cache_ttl_seconds: 60,
      providers: [{
        provider: "kimi",
        available: false,
        source: null,
        fetched_at: null,
        signal_at: null,
        title: "Kimi",
        plan: null,
        windows: [],
        details: [],
        unavailable_reason: "offline",
        cached: false,
      }],
    });
    expect(legacy.providers[0].fallback).toBe(false);
  });
});

describe("SubscriptionTokenBurnResponseSchema", () => {
  it("preserves provider outcome counts and defaults older payloads safely", () => {
    const parsed = SubscriptionTokenBurnResponseSchema.parse({
      days: 7,
      now: 1_784_642_400,
      window_start: 1_784_037_600,
      totals: {
        runs: 3,
        completed_runs: 1,
        failed_runs: 1,
        blocked_runs: 1,
        input_tokens: 300,
        output_tokens: 30,
        total_tokens: 330,
      },
      by_lane: [{
        subscription: "claude",
        profile: "premium",
        runs: 3,
        completed_runs: 1,
        failed_runs: 1,
        blocked_runs: 1,
        input_tokens: 300,
        output_tokens: 30,
        total_tokens: 330,
      }],
      by_class: [],
      daily: [],
      buckets: [],
    });

    expect(parsed.by_lane[0]).toMatchObject({
      completed_runs: 1,
      failed_runs: 1,
      blocked_runs: 1,
    });

    const legacy = SubscriptionTokenBurnResponseSchema.parse({
      ...parsed,
      totals: { runs: 1, input_tokens: 10, output_tokens: 5, total_tokens: 15 },
      by_lane: [],
    });
    expect(legacy.totals.completed_runs).toBeUndefined();
    expect(legacy.totals.failed_runs).toBeUndefined();
    expect(legacy.totals.blocked_runs).toBeUndefined();
  });
});

describe("HostUsageResponseSchema", () => {
  it("keeps host sources, sessions and provider days grounded", () => {
    const parsed = HostUsageResponseSchema.parse({
      generated_at: 1_784_642_400,
      days: 7,
      dates: ["2026-07-21"],
      total_tokens: 350,
      total_sessions: 6,
      active_tmux_panes: 7,
      sources: [{ source: "terminal", label: "Terminals", tokens: 320, sessions: 5 }],
      providers: [{ provider: "qwen", label: "Qwen", total_tokens: 100, sessions: 1, daily: [{ date: "2026-07-21", tokens: 100, sessions: 1 }] }],
      errors: [],
      accounting_note: "Aktive Ein-/Ausgabe ohne Cache",
      cached: false,
    });

    expect(parsed.active_tmux_panes).toBe(7);
    expect(parsed.providers[0]).toMatchObject({ provider: "qwen", total_tokens: 100, sessions: 1 });
    expect(parsed.sources[0].label).toBe("Terminals");
  });
});
