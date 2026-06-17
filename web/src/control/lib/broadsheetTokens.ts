/**
 * Broadsheet design tokens for JS access (inline meter widths, stacked-bar
 * segment fills, future chart hues). The canonical source stays
 * styles/stats-broadsheet.css (the CSS variables under [data-stats-broadsheet]);
 * this object mirrors them for the few cases where a Tailwind/CSS class can't
 * reach — e.g. an inline `width:%` on a ledger meter, or per-segment fills on
 * the error stack. Fork-local to /control/statistik (PlanSpec 2026-06-17,
 * Richtung B · Broadsheet); see lib/tokens.ts for the equivalent control-wide
 * mirror this follows.
 */
export const broadsheet = {
  // Newsprint paper + ink ladder.
  paper: "#f1f2ec",
  ink: "#15171b",
  ink2: "#4d525c",
  ink3: "#8a8f99",
  rule: "#d8d8cf",
  rule2: "#c2c2b6",
  // The single ink accent — identity/interaction only, never a status.
  navy: "#15355f",
  // Status inks (read as text on paper, fill a meter/dot).
  status: {
    emerald: "#0a7d52",
    amber: "#a6620a",
    red: "#bf2f3f",
    neutral: "#9aa0ac",
  },
  /**
   * Stacked error-bar palette, in severity order. The navy accent doubles as
   * the third bucket fill (a structural, non-status segment); neutral closes
   * out the residual bucket. Mirrors the mockup's estack fills so ST4 doesn't
   * hardcode them.
   */
  errorSeries: ["#bf2f3f", "#a6620a", "#15355f", "#9aa0ac"],
  font: {
    display: '"Bricolage Grotesque", system-ui, sans-serif',
    body: '"Hanken Grotesk", system-ui, sans-serif',
    mono: '"Spline Sans Mono", ui-monospace, monospace',
  },
} as const;

/** Semantic status of a figure/meter — maps to the sb status classes/inks. */
export type BroadsheetStatus = "ok" | "warn" | "crit" | "neutral";
