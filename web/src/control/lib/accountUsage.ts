/**
 * Reine Helfer für das Abo-Limits-Cockpit (CommandHome + StatistikView).
 *
 * Klassifiziert Provider-Fenster sprach-unabhängig über das Backend-`window_key`
 * (Fallback: Label-Heuristik), liefert deutsche Labels, das knappste Fenster über
 * alle Abos (Engpass) und die Lane-Zuordnung für den Worker-Run-Abgleich.
 *
 * Die Feld-Definitionen (Provider-Labels, Fenster-Labels/-Kinds, Lanes) sind
 * config-getrieben: sie kommen aus `StatsFieldConfig` (`/api/stats-config`,
 * Default = `DEFAULT_STATS_CONFIG`) statt aus hartcodierten Konstanten.
 *
 * Bewusst seiteneffektfrei: `formatReset` bekommt `now` injiziert (kein
 * `Date.now()`), damit die Funktionen deterministisch testbar sind.
 */
import type { AccountUsageProvider, AccountUsageWindow } from "./types";
import {
  DEFAULT_STATS_CONFIG,
  isProviderVisible,
  laneForProvider,
  providerLabel,
  providerOrder,
  usageRoleForProvider,
  windowField,
  windowLabelForKind,
  type StatsFieldConfig,
} from "./statsFields";

export type WindowKind = "session" | "weekly" | "other";

/**
 * Subscription-Lane des Worker-Run-Abgleichs. Die konkreten Lanes sind config-
 * getrieben (`subscription_lanes` in der StatsFieldConfig), daher ein offener
 * String statt einer festen Union.
 */
export type SubscriptionLane = string;

export type UsageProviderRole = "subscription" | "spend";

export interface Bottleneck {
  providerId: string;
  kind: "session" | "weekly";
  windowLabel: string;
  usedPercent: number;
  resetAt: string | null;
}

/**
 * Klassifiziert ein Fenster robust: primär über das config-deklarierte `window_key`,
 * erst als Fallback über eine Label-Heuristik (falls ein Provider künftig ohne
 * Key liefert oder umbenennt).
 */
export function classifyWindow(w: AccountUsageWindow, cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG): WindowKind {
  const field = windowField(cfg, w.window_key);
  if (field) return field.kind;

  const label = (w.label ?? "").toLowerCase();
  if (/session|sitzung|5\s?h|5-std/.test(label)) return "session";
  if (/week|woche|7\s?d/.test(label)) return "weekly";
  return "other";
}

/** Deutsches Label für ein Fenster — über config-`window_key`, dann Heuristik, dann roher Label. */
export function windowLabelDe(w: AccountUsageWindow, cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG): string {
  const field = windowField(cfg, w.window_key);
  if (field) return field.label;
  const kind = classifyWindow(w, cfg);
  if (kind === "session" || kind === "weekly") {
    const byKind = windowLabelForKind(cfg, kind);
    if (byKind) return byKind;
  }
  return w.label || "Limit";
}

/**
 * Das knappste session/weekly-Fenster über alle *verfügbaren* Provider —
 * „other"-Fenster (Opus/Sonnet/Extra) zählen nicht. Liefert immer das höchste
 * (der Aufrufer entscheidet den Ton ab 75/90 %); `null` nur, wenn es gar kein
 * session/weekly-Fenster mit Prozentwert gibt.
 */
export function pickBottleneck(
  providers: AccountUsageProvider[],
  cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): Bottleneck | null {
  let best: Bottleneck | null = null;
  for (const provider of providers) {
    if (!provider.available) continue;
    for (const w of provider.windows) {
      const kind = classifyWindow(w, cfg);
      if (kind !== "session" && kind !== "weekly") continue;
      const used =
        typeof w.used_percent === "number" && Number.isFinite(w.used_percent) ? w.used_percent : null;
      if (used == null) continue;
      if (best == null || used > best.usedPercent) {
        best = {
          providerId: provider.provider,
          kind,
          windowLabel: windowLabelDe(w, cfg),
          usedPercent: used,
          resetAt: w.reset_at,
        };
      }
    }
  }
  return best;
}

const WEEKDAYS_DE = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * Reset als relativer Countdown, wenn < 24 h („in 3 Std 18 Min"), sonst als
 * kurzer Wochentag + Uhrzeit („Fr 06:00") — wie die Provider-Apps. `nowMs` wird
 * injiziert, damit die Funktion rein/testbar bleibt.
 */
export function formatReset(resetAt: string | null, nowMs: number): string {
  if (!resetAt) return "";
  const t = Date.parse(resetAt);
  if (Number.isNaN(t)) return "";
  const diff = t - nowMs;
  if (diff <= 0) return "jetzt";
  if (diff < 24 * 3_600_000) {
    const hours = Math.floor(diff / 3_600_000);
    const minutes = Math.floor((diff % 3_600_000) / 60_000);
    if (hours <= 0) return `in ${minutes} Min`;
    return minutes > 0 ? `in ${hours} Std ${minutes} Min` : `in ${hours} Std`;
  }
  const d = new Date(t);
  return `${WEEKDAYS_DE[d.getDay()]} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/**
 * Provider → Worker-Run-Subscription-Lane (für den Abgleich Provider-% ↔
 * Worker-Run-Verbrauch), config-getrieben. Provider ohne Lane (OpenRouter =
 * $-Guthaben) → null.
 */
export function providerToLane(
  provider: string,
  cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): SubscriptionLane | null {
  return laneForProvider(cfg, provider);
}

/** Canonical provider name; never trust generic backend titles such as "Account limits". */
export function usageProviderLabel(
  provider: AccountUsageProvider,
  cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): string {
  const configured = providerLabel(cfg, provider.provider);
  if (configured !== provider.provider) return configured;
  return provider.title && provider.title !== "Account limits" ? provider.title : provider.provider;
}

/** Visible providers in declarative config order, optionally restricted by account role. */
export function sortUsageProviders(
  providers: AccountUsageProvider[],
  cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG,
  role?: UsageProviderRole,
): AccountUsageProvider[] {
  return providers
    .filter((provider) => isProviderVisible(cfg, provider.provider))
    .filter((provider) => role == null || usageRoleForProvider(cfg, provider.provider) === role)
    .sort((a, b) => providerOrder(cfg, a.provider) - providerOrder(cfg, b.provider));
}

/** All provider-supplied windows, ordered session → weekly → other without dropping duplicates. */
export function sortedUsageWindows(
  provider: AccountUsageProvider,
  cfg: StatsFieldConfig = DEFAULT_STATS_CONFIG,
): AccountUsageWindow[] {
  const rank = (window: AccountUsageWindow) => {
    const kind = classifyWindow(window, cfg);
    return kind === "session" ? 0 : kind === "weekly" ? 1 : 2;
  };
  return provider.windows
    .map((window, index) => ({ window, index }))
    .sort((a, b) => rank(a.window) - rank(b.window) || a.index - b.index)
    .map(({ window }) => window);
}

/** Human-readable age only when a provider signal is older than the live threshold. */
export function staleUsageSignalLabel(
  provider: AccountUsageProvider,
  nowMs: number,
  maxAgeMs = 60 * 60 * 1000,
): string | null {
  const signalMs = Date.parse(provider.signal_at ?? provider.fetched_at ?? "");
  if (!Number.isFinite(signalMs) || nowMs - signalMs <= maxAgeMs) return null;
  const ageHours = Math.max(1, Math.floor((nowMs - signalMs) / (60 * 60 * 1000)));
  return ageHours < 24 ? `Stand ${ageHours}h` : `Stand ${Math.floor(ageHours / 24)}d`;
}
