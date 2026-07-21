import { describe, expect, it } from "vitest";

import type { AccountUsageProvider } from "../../lib/types";
import { providerUsageMeters, sortTerminalUsageProviders } from "./usageModel";

function provider(id: string, windows: AccountUsageProvider["windows"] = []): AccountUsageProvider {
  return {
    provider: id, available: true, source: "hidden-source", fetched_at: null, title: id, plan: "Max",
    windows, details: ["must not render"], unavailable_reason: null, cached: false,
  };
}

describe("terminal usage dock", () => {
  it("uses config order for Claude, ChatGPT, Kimi and Grok and excludes spend providers", () => {
    const sorted = sortTerminalUsageProviders([provider("kimi"), provider("openrouter"), provider("xai"), provider("anthropic"), provider("openai-codex")]);
    expect(sorted.map((item) => item.provider)).toEqual(["anthropic", "openai-codex", "kimi", "xai"]);
  });

  it("maps every supplied window including Fable without inventing missing limits", () => {
    const meters = providerUsageMeters(provider("anthropic", [
      { label: "5h", window_key: "five_hour", used_percent: 37, reset_at: null, detail: null },
      { label: "Weekly", window_key: "weekly", used_percent: 62, reset_at: null, detail: null },
      { label: "Model", window_key: "scoped_week", used_percent: 99, reset_at: null, detail: "Fable" },
    ]));
    expect(meters.map((meter) => [meter.label, meter.percent, meter.detail])).toEqual([
      ["5-Std-Fenster", 37, null],
      ["Diese Woche", 62, null],
      ["Modell-Limit", 99, "Fable"],
    ]);
    expect(providerUsageMeters(provider("kimi"))).toEqual([]);
  });
});
