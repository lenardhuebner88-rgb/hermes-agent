import { sortUsageProviders, sortedUsageWindows, usageProviderLabel, windowLabelDe } from "../../lib/accountUsage";
import { DEFAULT_STATS_CONFIG, type StatsFieldConfig } from "../../lib/statsFields";
import type { AccountUsageProvider } from "../../lib/types";

export interface ProviderUsageMeter {
  key: string;
  label: string;
  percent: number | null;
  detail: string | null;
}

export function sortTerminalUsageProviders(
  providers: AccountUsageProvider[],
  config: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): AccountUsageProvider[] {
  return sortUsageProviders(providers, config, "subscription");
}

export function providerUsageMeters(
  provider: AccountUsageProvider,
  config: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): ProviderUsageMeter[] {
  const valid = (value: number | null | undefined) =>
    typeof value === "number" && Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : null;
  return sortedUsageWindows(provider, config).map((window, index) => ({
    key: `${window.window_key ?? window.label}-${window.detail ?? ""}-${index}`,
    label: windowLabelDe(window, config),
    percent: valid(window.used_percent),
    detail: window.detail,
  }));
}

export { usageProviderLabel };
