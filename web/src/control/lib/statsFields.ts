/**
 * Config-driven field definitions for the /control Stats tab (Statistik + Abo-Limits-Cockpit).
 *
 * The provider labels, usage-window labels/kinds, and subscription lanes used to be
 * hardcoded across StatistikView / AccountUsageTile / accountUsage. They now live in
 * `config/stats_fields.yaml` (repo root), are served by `GET /api/stats-config`, and
 * are fetched via `useStatsConfig()`. This module is the typed contract + the built-in
 * fallback used before the fetch resolves (or if it fails) — graceful degradation,
 * never a crash. View files render purely from a resolved `StatsFieldConfig`; no field
 * names are hardcoded in them.
 */
import { z } from "zod";

export const StatsProviderFieldSchema = z.object({
  id: z.string().catch(""),
  label: z.string().catch(""),
  lane: z.string().nullable().catch(null),
  visible: z.boolean().catch(true),
});

export const StatsWindowFieldSchema = z.object({
  key: z.string().catch(""),
  label: z.string().catch(""),
  kind: z.enum(["session", "weekly", "other"]).catch("other"),
});

export const StatsLaneFieldSchema = z.object({
  key: z.string().catch(""),
  label: z.string().catch(""),
  visible: z.boolean().catch(true),
});

export const StatsFieldConfigSchema = z.object({
  version: z.coerce.number().catch(1),
  providers: z.array(StatsProviderFieldSchema).catch([]),
  windows: z.array(StatsWindowFieldSchema).catch([]),
  subscription_lanes: z.array(StatsLaneFieldSchema).catch([]),
});

export type StatsProviderField = z.infer<typeof StatsProviderFieldSchema>;
export type StatsWindowField = z.infer<typeof StatsWindowFieldSchema>;
export type StatsLaneField = z.infer<typeof StatsLaneFieldSchema>;
export type StatsFieldConfig = z.infer<typeof StatsFieldConfigSchema>;

/**
 * Built-in fallback — kept in lockstep with config/stats_fields.yaml and the backend
 * DEFAULT_STATS_CONFIG. Used until `/api/stats-config` resolves, or if it fails.
 */
export const DEFAULT_STATS_CONFIG: StatsFieldConfig = {
  version: 1,
  providers: [
    { id: "anthropic", label: "Claude", lane: "claude", visible: true },
    { id: "openai-codex", label: "ChatGPT / Codex", lane: "chatgpt", visible: true },
    { id: "kimi", label: "Kimi", lane: "kimi", visible: true },
    { id: "xai", label: "Grok", lane: null, visible: true },
    { id: "openrouter", label: "OpenRouter", lane: null, visible: true },
  ],
  windows: [
    { key: "session", label: "5-Std-Fenster", kind: "session" },
    { key: "weekly", label: "Diese Woche", kind: "weekly" },
    { key: "opus_week", label: "Opus-Woche", kind: "other" },
    { key: "sonnet_week", label: "Sonnet-Woche", kind: "other" },
    { key: "scoped_week", label: "Modell-Limit", kind: "other" },
  ],
  subscription_lanes: [
    { key: "chatgpt", label: "ChatGPT/Codex Abo", visible: true },
    { key: "claude", label: "Claude Max Abo", visible: true },
    { key: "kimi", label: "Kimi Abo", visible: true },
  ],
};

/** The provider field for `id`, or undefined if not declared in the config. */
export function providerField(cfg: StatsFieldConfig, id: string): StatsProviderField | undefined {
  return cfg.providers.find((p) => p.id === id);
}

/** German display label for a provider id; falls back to the raw id when undeclared. */
export function providerLabel(cfg: StatsFieldConfig, id: string): string {
  return providerField(cfg, id)?.label || id;
}

/** Worker-run subscription lane for a provider id; null = API-billed / undeclared. */
export function laneForProvider(cfg: StatsFieldConfig, id: string): string | null {
  const field = providerField(cfg, id);
  return field ? field.lane : null;
}

/** True unless the provider is explicitly hidden in config (undeclared = visible). */
export function isProviderVisible(cfg: StatsFieldConfig, id: string): boolean {
  const field = providerField(cfg, id);
  return field ? field.visible : true;
}

/** Config declaration order for a provider id; undeclared providers sort last. */
export function providerOrder(cfg: StatsFieldConfig, id: string): number {
  const idx = cfg.providers.findIndex((p) => p.id === id);
  return idx < 0 ? Number.MAX_SAFE_INTEGER : idx;
}

/** The window field for `key`, or undefined when the key is null/undeclared. */
export function windowField(cfg: StatsFieldConfig, key: string | null): StatsWindowField | undefined {
  if (!key) return undefined;
  return cfg.windows.find((w) => w.key === key);
}

/** First declared window of a given kind (used for heuristic-classified windows). */
export function windowLabelForKind(cfg: StatsFieldConfig, kind: StatsWindowField["kind"]): string | undefined {
  return cfg.windows.find((w) => w.kind === kind)?.label;
}

/** Visible subscription lanes, in config order. */
export function visibleSubscriptionLanes(cfg: StatsFieldConfig): StatsLaneField[] {
  return cfg.subscription_lanes.filter((lane) => lane.visible);
}
