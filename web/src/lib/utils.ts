import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// Tailwind's bare `text-` prefix serves both font-size (`text-lg`) and
// text-color (`text-red-500`) utilities; tailwind-merge only resolves that
// ambiguity for names it recognises (its own default scale, or an explicitly
// registered theme scale) — an unrecognised custom name falls into the same
// ambiguous bucket as an unrecognised color, so combining them silently drops
// one. `web/src/control/theme.css` defines a custom `--text-*` scale (micro/
// sec/body/emph/h1/h2/hero) alongside a color token) that tailwind-merge's
// default config has no way to know about; without this, e.g.
// `cn("text-micro font-semibold text-ink-3")` used to collapse to just
// `"text-ink-3"` (verified via `twMerge("text-micro text-ink-3") ===
// "text-ink-3"` before this fix — found building the /control type-scale
// rollout, 2026-07-10). Registering the scale under `font-size` disambiguates
// it from any text-color class without touching anything else in the
// default config.
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      "font-size": [{ text: ["micro", "sec", "body", "emph", "h1", "h2", "hero"] }],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Mondwest font only — use on layout shells; do not force normal-case here or `text-display` chrome (Segmented, badges) stops uppercasing. */
export const themedFont = "font-mondwest";

/** Mondwest body copy — sentence-case themed text (not uppercase chrome). */
export const themedBody = "font-mondwest normal-case";

/** Mondwest brand chrome — uppercase section headers and nav labels. */
export const themedChrome = "font-mondwest text-display";

/** Relative time from a Unix epoch timestamp (seconds). */
export function timeAgo(ts: number): string {
  const delta = Date.now() / 1000 - ts;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  if (delta < 172800) return "yesterday";
  return `${Math.floor(delta / 86400)}d ago`;
}

/** Relative time from an ISO-8601 timestamp string. */
export function isoTimeAgo(iso: string): string {
  const delta = (Date.now() - new Date(iso).getTime()) / 1000;
  if (delta < 0 || Number.isNaN(delta)) return "unknown";
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}
