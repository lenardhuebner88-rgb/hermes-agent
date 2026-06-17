/**
 * Reine Helfer für das Abo-Limits-Cockpit (CommandHome + StatistikView).
 *
 * Klassifiziert Provider-Fenster sprach-unabhängig über das Backend-`window_key`
 * (Fallback: Label-Heuristik), liefert deutsche Labels, das knappste Fenster über
 * alle Abos (Engpass) und die Lane-Zuordnung für den Worker-Run-Abgleich.
 *
 * Bewusst seiteneffektfrei: `formatReset` bekommt `now` injiziert (kein
 * `Date.now()`), damit die Funktionen deterministisch testbar sind.
 */
import type { AccountUsageProvider, AccountUsageWindow } from "./types";

export type WindowKind = "session" | "weekly" | "other";

/** window_key → deutsches Label. Fallback (unbekannter Key): roher `label`. */
export const WINDOW_DE: Record<string, string> = {
  session: "5-Std-Fenster",
  weekly: "Diese Woche",
  opus_week: "Opus-Woche",
  sonnet_week: "Sonnet-Woche",
};

/** Subscription-Lanes des Worker-Run-Abgleichs (StatistikView SUBSCRIPTION_BUCKETS). */
export type SubscriptionLane = "claude" | "chatgpt" | "kimi";

export interface Bottleneck {
  providerId: string;
  kind: "session" | "weekly";
  windowLabel: string;
  usedPercent: number;
  resetAt: string | null;
}

/**
 * Klassifiziert ein Fenster robust: primär über das stabile Backend-`window_key`,
 * erst als Fallback über eine Label-Heuristik (falls ein Provider künftig ohne
 * Key liefert oder umbenennt).
 */
export function classifyWindow(w: AccountUsageWindow): WindowKind {
  const key = w.window_key;
  if (key === "session") return "session";
  if (key === "weekly") return "weekly";
  if (key === "opus_week" || key === "sonnet_week" || key === "other") return "other";

  const label = (w.label ?? "").toLowerCase();
  if (/session|sitzung|5\s?h|5-std/.test(label)) return "session";
  if (/week|woche|7\s?d/.test(label)) return "weekly";
  return "other";
}

/** Deutsches Label für ein Fenster — über window_key, dann Heuristik, dann roher Label. */
export function windowLabelDe(w: AccountUsageWindow): string {
  if (w.window_key && WINDOW_DE[w.window_key]) return WINDOW_DE[w.window_key];
  const kind = classifyWindow(w);
  if (kind === "session") return WINDOW_DE.session;
  if (kind === "weekly") return WINDOW_DE.weekly;
  return w.label || "Limit";
}

/**
 * Das knappste session/weekly-Fenster über alle *verfügbaren* Provider —
 * „other"-Fenster (Opus/Sonnet/Extra) zählen nicht. Liefert immer das höchste
 * (der Aufrufer entscheidet den Ton ab 75/90 %); `null` nur, wenn es gar kein
 * session/weekly-Fenster mit Prozentwert gibt.
 */
export function pickBottleneck(providers: AccountUsageProvider[]): Bottleneck | null {
  let best: Bottleneck | null = null;
  for (const provider of providers) {
    if (!provider.available) continue;
    for (const w of provider.windows) {
      const kind = classifyWindow(w);
      if (kind !== "session" && kind !== "weekly") continue;
      const used =
        typeof w.used_percent === "number" && Number.isFinite(w.used_percent) ? w.used_percent : null;
      if (used == null) continue;
      if (best == null || used > best.usedPercent) {
        best = {
          providerId: provider.provider,
          kind,
          windowLabel: windowLabelDe(w),
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
 * Worker-Run-Verbrauch). Provider ohne Lane (OpenRouter = $-Guthaben) → null.
 */
export function providerToLane(provider: string): SubscriptionLane | null {
  if (provider === "anthropic") return "claude";
  if (provider === "openai-codex") return "chatgpt";
  if (provider === "kimi") return "kimi";
  return null;
}
