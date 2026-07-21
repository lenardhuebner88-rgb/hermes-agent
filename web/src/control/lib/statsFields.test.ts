import { describe, expect, it } from "vitest";
import {
  DEFAULT_STATS_CONFIG,
  StatsFieldConfigSchema,
  isProviderVisible,
  laneForProvider,
  providerField,
  providerLabel,
  providerOrder,
  usageRoleForProvider,
  visibleSubscriptionLanes,
  windowField,
  windowLabelForKind,
  type StatsFieldConfig,
} from "./statsFields";

describe("config lookups (default config)", () => {
  const cfg = DEFAULT_STATS_CONFIG;

  it("providerLabel resolves declared labels, falls back to the raw id", () => {
    expect(providerLabel(cfg, "anthropic")).toBe("Claude");
    expect(providerLabel(cfg, "openai-codex")).toBe("ChatGPT / Codex");
    expect(providerLabel(cfg, "mystery")).toBe("mystery");
  });

  it("laneForProvider maps to the worker-run lane, null for API-billed/undeclared", () => {
    expect(laneForProvider(cfg, "anthropic")).toBe("claude");
    expect(laneForProvider(cfg, "openrouter")).toBeNull();
    expect(laneForProvider(cfg, "mystery")).toBeNull();
  });

  it("distinguishes four subscription providers from the spend account", () => {
    expect(cfg.providers.filter((p) => usageRoleForProvider(cfg, p.id) === "subscription").map((p) => p.id)).toEqual([
      "anthropic", "openai-codex", "kimi", "xai",
    ]);
    expect(usageRoleForProvider(cfg, "openrouter")).toBe("spend");
    expect(usageRoleForProvider(cfg, "new-live-provider")).toBe("subscription");
  });

  it("windowField + windowLabelForKind resolve from config", () => {
    expect(windowField(cfg, "session")?.kind).toBe("session");
    expect(windowField(cfg, null)).toBeUndefined();
    expect(windowField(cfg, "nope")).toBeUndefined();
    expect(windowLabelForKind(cfg, "weekly")).toBe("Diese Woche");
  });

  it("visibleSubscriptionLanes returns visible lanes in config order", () => {
    expect(visibleSubscriptionLanes(cfg).map((l) => l.key)).toEqual(["chatgpt", "claude", "kimi"]);
  });
});

describe("visibility + order (declutter knobs)", () => {
  const cfg: StatsFieldConfig = {
    version: 1,
    providers: [
      { id: "anthropic", label: "Claude", lane: "claude", visible: false },
      { id: "openai-codex", label: "ChatGPT / Codex", lane: "chatgpt", visible: true },
    ],
    windows: [],
    subscription_lanes: [
      { key: "chatgpt", label: "ChatGPT/Codex Abo", visible: true },
      { key: "claude", label: "Claude Max Abo", visible: false },
    ],
  };

  it("isProviderVisible respects the flag; undeclared providers default visible", () => {
    expect(isProviderVisible(cfg, "anthropic")).toBe(false);
    expect(isProviderVisible(cfg, "openai-codex")).toBe(true);
    expect(isProviderVisible(cfg, "undeclared")).toBe(true);
  });

  it("providerOrder follows config declaration; undeclared sorts last", () => {
    expect(providerOrder(cfg, "anthropic")).toBe(0);
    expect(providerOrder(cfg, "openai-codex")).toBe(1);
    expect(providerOrder(cfg, "undeclared")).toBe(Number.MAX_SAFE_INTEGER);
  });

  it("hidden subscription lanes drop out of visibleSubscriptionLanes", () => {
    expect(visibleSubscriptionLanes(cfg).map((l) => l.key)).toEqual(["chatgpt"]);
  });

  it("providerField returns the declared field or undefined", () => {
    expect(providerField(cfg, "anthropic")?.label).toBe("Claude");
    expect(providerField(cfg, "ghost")).toBeUndefined();
  });
});

describe("StatsFieldConfigSchema (fail-soft parsing)", () => {
  it("parses a well-formed payload", () => {
    const parsed = StatsFieldConfigSchema.parse(DEFAULT_STATS_CONFIG);
    expect(parsed.providers).toHaveLength(5);
  });

  it("coerces missing/garbage fields via .catch instead of throwing", () => {
    const parsed = StatsFieldConfigSchema.parse({
      version: "7",
      providers: [{ id: "x" }], // label/lane/visible missing → caught defaults
      windows: [{ key: "w", kind: "bogus" }], // bad kind → "other"
      subscription_lanes: [{ key: "l" }],
    });
    expect(parsed.version).toBe(7);
    expect(parsed.providers[0]).toEqual({ id: "x", label: "", lane: null, visible: true });
    expect(parsed.windows[0].kind).toBe("other");
    expect(parsed.subscription_lanes[0].visible).toBe(true);
  });
});
