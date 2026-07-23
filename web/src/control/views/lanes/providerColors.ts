// Stable provider → data-palette identity for the Modell-Plattform.
//
// DESIGN.md W6-4: `--color-data-1..7` mark IDENTITY only (distinct series /
// engines / roles) — never status, never an affordance, and never color-alone
// (a dot always carries the provider/model name beside it). The color is drawn
// in lanes.css (`.lp .pdot.p-N { background: var(--color-data-N) }`) so that
// (a) no raw hex ever lives in a .ts file (token ratchet) and (b) Tailwind never
// has to generate a dynamic `bg-data-N` class — the modifier class is a stable
// literal here, the actual paint is a CSS custom property reference.

export type ProviderDot = "p-1" | "p-2" | "p-3" | "p-4" | "p-5" | "p-6" | "p-7";

// Binding map (Brief S2): openai-codex→data-3, alibaba-token-plan→data-1,
// neuralwatt→data-2, moonshotai/kimi→data-4, claude-cli/anthropic→data-5,
// nous→data-6, openrouter→data-7, default→data-6 (neutral).
const PROVIDER_DOT: Record<string, ProviderDot> = {
  "openai-codex": "p-3",
  "openai": "p-3",
  "alibaba-token-plan": "p-1",
  "neuralwatt": "p-2",
  "moonshotai": "p-4",
  "kimi": "p-4",
  "anthropic": "p-5",
  "claude-cli": "p-5",
  "nous": "p-6",
  "openrouter": "p-7",
};

const DEFAULT_DOT: ProviderDot = "p-6";

/** Identity-dot modifier class for a provider. Provider-less rows (claude-cli
 *  Max models, free-form kimi entries) fall back to the model id so identity
 *  stays stable instead of collapsing everything to neutral. */
export function providerDot(provider: string | null | undefined, modelId?: string | null): ProviderDot {
  const p = (provider ?? "").trim().toLowerCase();
  const known = p ? PROVIDER_DOT[p] : undefined;
  if (known) return known;
  const m = (modelId ?? "").trim().toLowerCase();
  if (p === "claude-cli" || m.startsWith("claude")) return "p-5";
  if (m.includes("kimi") || m.includes("moonshot")) return "p-4";
  return DEFAULT_DOT;
}
