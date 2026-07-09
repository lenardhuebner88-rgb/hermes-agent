import { describe, expect, it } from "vitest";

import type { AccountUsageProvider } from "../../lib/types";
import { providerUsageSummary, sortTerminalUsageProviders } from "./usageModel";

function provider(id: string, windows: AccountUsageProvider["windows"] = []): AccountUsageProvider {
  return {
    provider: id, available: true, source: "hidden-source", fetched_at: null, title: id, plan: "Max",
    windows, details: ["must not render"], unavailable_reason: null, cached: false,
  };
}

describe("terminal usage dock", () => {
  it("uses ChatGPT, Claude, Kimi ordering and excludes unrelated providers", () => {
    const sorted = sortTerminalUsageProviders([provider("kimi"), provider("openrouter"), provider("anthropic"), provider("openai-codex")]);
    expect(sorted.map((item) => item.provider)).toEqual(["openai-codex", "anthropic", "kimi"]);
  });

  it("maps session and weekly percentages without inventing missing limits", () => {
    const summary = providerUsageSummary(provider("openai-codex", [
      { label: "5h", window_key: "five_hour", used_percent: 37, reset_at: null, detail: null },
      { label: "Weekly", window_key: "weekly", used_percent: 62, reset_at: null, detail: null },
    ]));
    expect(summary).toEqual({ sessionPercent: 37, weeklyPercent: 62 });
    expect(providerUsageSummary(provider("kimi"))).toEqual({ sessionPercent: null, weeklyPercent: null });
  });
});
