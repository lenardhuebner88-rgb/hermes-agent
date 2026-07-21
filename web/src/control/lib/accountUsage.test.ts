import { describe, expect, it } from "vitest";
import type { AccountUsageProvider, AccountUsageWindow } from "./types";
import {
  classifyWindow,
  formatReset,
  pickBottleneck,
  providerToLane,
  sortUsageProviders,
  sortedUsageWindows,
  staleUsageSignalLabel,
  usageProviderLabel,
  windowLabelDe,
} from "./accountUsage";
import { DEFAULT_STATS_CONFIG, type StatsFieldConfig } from "./statsFields";

// Default-config window labels, by key — the helpers fall back to DEFAULT_STATS_CONFIG.
const WINDOW_DE = Object.fromEntries(DEFAULT_STATS_CONFIG.windows.map((w) => [w.key, w.label]));

function win(partial: Partial<AccountUsageWindow>): AccountUsageWindow {
  return {
    label: "",
    window_key: null,
    used_percent: null,
    reset_at: null,
    detail: null,
    ...partial,
  };
}

function provider(partial: Partial<AccountUsageProvider>): AccountUsageProvider {
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
    ...partial,
  };
}

describe("classifyWindow", () => {
  it("classifies primarily by window_key (all real keys)", () => {
    expect(classifyWindow(win({ window_key: "session" }))).toBe("session");
    expect(classifyWindow(win({ window_key: "weekly" }))).toBe("weekly");
    expect(classifyWindow(win({ window_key: "opus_week" }))).toBe("other");
    expect(classifyWindow(win({ window_key: "sonnet_week" }))).toBe("other");
    expect(classifyWindow(win({ window_key: "other" }))).toBe("other");
  });

  it("falls back to label heuristic when window_key is missing", () => {
    expect(classifyWindow(win({ label: "Current session" }))).toBe("session");
    expect(classifyWindow(win({ label: "5-Std-Fenster" }))).toBe("session");
    expect(classifyWindow(win({ label: "Kimi 5h" }))).toBe("session");
    expect(classifyWindow(win({ label: "Current week" }))).toBe("weekly");
    expect(classifyWindow(win({ label: "Weekly" }))).toBe("weekly");
    expect(classifyWindow(win({ label: "Kimi 7d" }))).toBe("weekly");
    expect(classifyWindow(win({ label: "Mystery window" }))).toBe("other");
  });
});

describe("windowLabelDe", () => {
  it("maps real window_keys to German labels", () => {
    expect(windowLabelDe(win({ window_key: "session" }))).toBe(WINDOW_DE.session);
    expect(windowLabelDe(win({ window_key: "weekly" }))).toBe(WINDOW_DE.weekly);
    expect(windowLabelDe(win({ window_key: "opus_week" }))).toBe(WINDOW_DE.opus_week);
    expect(windowLabelDe(win({ window_key: "sonnet_week" }))).toBe(WINDOW_DE.sonnet_week);
  });

  it("derives German label from heuristic when key missing, else raw label", () => {
    expect(windowLabelDe(win({ label: "Current session" }))).toBe(WINDOW_DE.session);
    expect(windowLabelDe(win({ label: "Mystery window" }))).toBe("Mystery window");
    expect(windowLabelDe(win({ label: "" }))).toBe("Limit");
  });
});

describe("pickBottleneck", () => {
  it("picks the highest session/weekly window across available providers", () => {
    const providers = [
      provider({
        provider: "anthropic",
        windows: [
          win({ window_key: "session", used_percent: 24 }),
          win({ window_key: "weekly", used_percent: 62 }),
        ],
      }),
      provider({
        provider: "openai-codex",
        windows: [
          win({ window_key: "session", used_percent: 0 }),
          win({ window_key: "weekly", used_percent: 96 }),
        ],
      }),
    ];
    const bn = pickBottleneck(providers);
    expect(bn).not.toBeNull();
    expect(bn?.providerId).toBe("openai-codex");
    expect(bn?.kind).toBe("weekly");
    expect(bn?.usedPercent).toBe(96);
  });

  it("ignores non session/weekly windows even if higher", () => {
    const providers = [
      provider({
        provider: "anthropic",
        windows: [
          win({ window_key: "weekly", used_percent: 40 }),
          win({ window_key: "opus_week", used_percent: 99 }),
        ],
      }),
    ];
    expect(pickBottleneck(providers)?.usedPercent).toBe(40);
  });

  it("ignores unavailable providers", () => {
    const providers = [
      provider({ provider: "openai-codex", available: false, windows: [win({ window_key: "weekly", used_percent: 99 })] }),
      provider({ provider: "anthropic", windows: [win({ window_key: "weekly", used_percent: 30 })] }),
    ];
    expect(pickBottleneck(providers)?.usedPercent).toBe(30);
  });

  it("returns null when no session/weekly windows exist", () => {
    const providers = [
      provider({ provider: "kimi", windows: [] }),
      provider({ provider: "openrouter", windows: [win({ window_key: "other", used_percent: 50 })] }),
    ];
    expect(pickBottleneck(providers)).toBeNull();
  });
});

describe("formatReset", () => {
  const now = 1_700_000_000_000;

  it("returns empty string for null/invalid", () => {
    expect(formatReset(null, now)).toBe("");
    expect(formatReset("not-a-date", now)).toBe("");
  });

  it("renders sub-hour as minutes", () => {
    expect(formatReset(new Date(now + 18 * 60 * 1000).toISOString(), now)).toBe("in 18 Min");
  });

  it("renders sub-day with hours and minutes", () => {
    expect(formatReset(new Date(now + (3 * 3600 + 18 * 60) * 1000).toISOString(), now)).toBe(
      "in 3 Std 18 Min",
    );
  });

  it("drops the minutes part when zero", () => {
    expect(formatReset(new Date(now + 5 * 3600 * 1000).toISOString(), now)).toBe("in 5 Std");
  });

  it("renders exactly 24h as an absolute weekday + time", () => {
    const out = formatReset(new Date(now + 24 * 3600 * 1000).toISOString(), now);
    expect(out).toMatch(/^(Mo|Di|Mi|Do|Fr|Sa|So) \d{2}:\d{2}$/);
  });

  it("treats a past reset as 'jetzt'", () => {
    expect(formatReset(new Date(now - 1000).toISOString(), now)).toBe("jetzt");
  });
});

describe("providerToLane", () => {
  it("maps providers to subscription-token lanes (default config)", () => {
    expect(providerToLane("anthropic")).toBe("claude");
    expect(providerToLane("openai-codex")).toBe("chatgpt");
    expect(providerToLane("kimi")).toBe("kimi");
    expect(providerToLane("openrouter")).toBeNull();
    expect(providerToLane("whatever")).toBeNull();
  });
});

describe("shared provider presentation", () => {
  it("uses canonical config labels instead of generic backend titles", () => {
    expect(usageProviderLabel(provider({ provider: "anthropic", title: "Account limits" }))).toBe("Claude");
    expect(usageProviderLabel(provider({ provider: "xai", title: "Grok" }))).toBe("Grok");
  });

  it("sorts the four subscriptions and excludes the spend-only provider", () => {
    const rows = sortUsageProviders([
      provider({ provider: "openrouter" }),
      provider({ provider: "xai" }),
      provider({ provider: "kimi" }),
      provider({ provider: "anthropic" }),
      provider({ provider: "openai-codex" }),
    ], DEFAULT_STATS_CONFIG, "subscription");
    expect(rows.map((row) => row.provider)).toEqual(["anthropic", "openai-codex", "kimi", "xai"]);
  });

  it("keeps every supplied window and orders session before weekly caps", () => {
    const rows = sortedUsageWindows(provider({ windows: [
      win({ window_key: "weekly", used_percent: 40 }),
      win({ window_key: "scoped_week", used_percent: 99, detail: "Fable" }),
      win({ window_key: "session", used_percent: 5 }),
    ] }));
    expect(rows.map((row) => row.window_key)).toEqual(["session", "weekly", "scoped_week"]);
  });

  it("reports only genuinely stale signals", () => {
    const now = Date.parse("2026-07-21T20:00:00Z");
    expect(staleUsageSignalLabel(provider({ fetched_at: "2026-07-21T19:30:00Z" }), now)).toBeNull();
    expect(staleUsageSignalLabel(provider({ signal_at: "2026-07-20T18:00:00Z" }), now)).toBe("Stand 1d");
  });
});

// AC-2 at the unit level: the helpers are config-driven — a different config yields
// different labels/lanes/kinds with no code change.
describe("config-driven overrides", () => {
  const cfg: StatsFieldConfig = {
    version: 1,
    providers: [
      { id: "anthropic", label: "Claude Custom", lane: "max", visible: true },
      { id: "newprov", label: "Brand New", lane: null, visible: true },
    ],
    windows: [
      { key: "session", label: "Sitzung (5h)", kind: "session" },
      { key: "rolling", label: "Rollierend", kind: "weekly" },
    ],
    subscription_lanes: [{ key: "max", label: "Max Abo", visible: true }],
  };

  it("windowLabelDe resolves labels from the supplied config", () => {
    expect(windowLabelDe(win({ window_key: "session" }), cfg)).toBe("Sitzung (5h)");
    expect(windowLabelDe(win({ window_key: "rolling" }), cfg)).toBe("Rollierend");
    // window_key absent from config → heuristic kind → first window of that kind.
    expect(windowLabelDe(win({ window_key: "weekly", label: "Current week" }), cfg)).toBe("Rollierend");
  });

  it("classifyWindow uses the config's kind mapping", () => {
    expect(classifyWindow(win({ window_key: "rolling" }), cfg)).toBe("weekly");
    expect(classifyWindow(win({ window_key: "session" }), cfg)).toBe("session");
  });

  it("providerToLane follows the config's provider→lane mapping", () => {
    expect(providerToLane("anthropic", cfg)).toBe("max");
    expect(providerToLane("newprov", cfg)).toBeNull();
    // openai-codex is not in this config at all → null
    expect(providerToLane("openai-codex", cfg)).toBeNull();
  });
});
