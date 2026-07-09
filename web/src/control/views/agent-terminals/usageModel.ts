import { classifyWindow } from "../../lib/accountUsage";
import type { AccountUsageProvider } from "../../lib/types";

export const TERMINAL_USAGE_PROVIDER_ORDER = ["openai-codex", "anthropic", "kimi"] as const;

export function sortTerminalUsageProviders(providers: AccountUsageProvider[]): AccountUsageProvider[] {
  const byId = new Map(providers.map((provider) => [provider.provider, provider]));
  return TERMINAL_USAGE_PROVIDER_ORDER.map((id) => byId.get(id)).filter((item): item is AccountUsageProvider => Boolean(item));
}

export function providerUsageSummary(provider: AccountUsageProvider): { sessionPercent: number | null; weeklyPercent: number | null } {
  const session = provider.windows.find((window) => classifyWindow(window) === "session");
  const weekly = provider.windows.find((window) => classifyWindow(window) === "weekly");
  const valid = (value: number | null | undefined) =>
    typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
  return { sessionPercent: valid(session?.used_percent), weeklyPercent: valid(weekly?.used_percent) };
}
