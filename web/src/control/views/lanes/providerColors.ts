// Stable provider → data-palette identity for the Modell-Plattform.
//
// DESIGN.md W6-4: `--color-data-1..7` mark IDENTITY only (distinct series /
// engines / roles) — never status, never an affordance, and never color-alone
// (a dot always carries the provider/model name beside it). The color is drawn
// in lanes.css (`.lp .pdot.p-N { background: var(--color-data-N) }`) so that
// (a) no raw hex ever lives in a .ts file (token ratchet) and (b) Tailwind never
// has to generate a dynamic `bg-data-N` class — the modifier class is a stable
// literal here, the actual paint is a CSS custom property reference.

// The modifier MUST NOT be named `p-N`: Tailwind generates `p-N` padding
// utilities for the same class names, which inflated the 8px dot into a
// provider-dependent 16–48px circle (found 2026-07-24 in the real-build render).
export type ProviderDot = "pd-1" | "pd-2" | "pd-3" | "pd-4" | "pd-5" | "pd-6" | "pd-7";

// Binding map (Brief S2): openai-codex→data-3, alibaba-token-plan→data-1,
// neuralwatt→data-2, moonshotai/kimi→data-4, claude-cli/anthropic→data-5,
// nous→data-6, openrouter→data-7, default→data-6 (neutral).
const PROVIDER_DOT: Record<string, ProviderDot> = {
  "openai-codex": "pd-3",
  "openai": "pd-3",
  "alibaba-token-plan": "pd-1",
  "neuralwatt": "pd-2",
  "moonshotai": "pd-4",
  "kimi": "pd-4",
  "anthropic": "pd-5",
  "claude-cli": "pd-5",
  "nous": "pd-6",
  "openrouter": "pd-7",
};

const DEFAULT_DOT: ProviderDot = "pd-6";

/** Identity-dot modifier class for a provider. Provider-less rows (claude-cli
 *  Max models, free-form kimi entries) fall back to the model id so identity
 *  stays stable instead of collapsing everything to neutral. */
export function providerDot(provider: string | null | undefined, modelId?: string | null): ProviderDot {
  const p = (provider ?? "").trim().toLowerCase();
  const known = p ? PROVIDER_DOT[p] : undefined;
  if (known) return known;
  const m = (modelId ?? "").trim().toLowerCase();
  if (p === "claude-cli" || m.startsWith("claude")) return "pd-5";
  if (m.includes("kimi") || m.includes("moonshot")) return "pd-4";
  return DEFAULT_DOT;
}
