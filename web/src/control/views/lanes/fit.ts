// Lane-Kompass — pure, deterministic model→role fit scoring (S2 feature d).
//
// No I/O, no clocks, no randomness: the same model + role + probes always yield
// the same {score, reasons}. Capability data is sparse in the catalog (no real
// benchmark numbers), so each signal degrades to a documented NEUTRAL (0.5) when
// its data is absent rather than guessing — the score only moves on evidence.
//
// Hard gate: a model KNOWN to be unreachable (authenticated === false) or KNOWN
// to be outside the curated working set (sinnvoll === false) scores 0 — the
// compass must never recommend a model it can verify is broken. Older payloads
// (and the captured live fixture) omit both fields; absent ≠ false, so those
// models are still scored (fail-soft) — just without the auth/sinnvoll bonus.

import type { LaneModelOption, ModelProbeResult } from "./api";
import { probeKey } from "./api";

// Re-exported so fit consumers (and tests) can key probes through one surface.
export { probeKey };

export type CompassRole =
  | "coder"
  | "reviewer"
  | "critic"
  | "verifier"
  | "research"
  | "scout"
  | "premium";

/** Role requirement profile — weights sum to 1.0 per role. */
export interface RoleRequirement {
  label: string;
  weights: { coding: number; reasoning: number; speed: number; cost: number; context: number };
  /** Reasoning level the role benefits from (matched against reasoning_support). */
  wantsReasoning: "high" | "medium" | "low" | "none";
  /** Rough useful-minimum context window (tokens); the context signal scales to it. */
  minContext: number;
}

export const ROLE_REQUIREMENTS: Record<CompassRole, RoleRequirement> = {
  coder:    { label: "Coder",    weights: { coding: 0.35, reasoning: 0.25, speed: 0.20, cost: 0.12, context: 0.08 }, wantsReasoning: "medium", minContext: 128_000 },
  reviewer: { label: "Reviewer", weights: { coding: 0.25, reasoning: 0.35, speed: 0.12, cost: 0.10, context: 0.18 }, wantsReasoning: "high",   minContext: 128_000 },
  critic:   { label: "Critic",   weights: { coding: 0.15, reasoning: 0.40, speed: 0.10, cost: 0.10, context: 0.25 }, wantsReasoning: "high",   minContext: 128_000 },
  verifier: { label: "Verifier", weights: { coding: 0.25, reasoning: 0.20, speed: 0.30, cost: 0.15, context: 0.10 }, wantsReasoning: "medium", minContext: 64_000 },
  research: { label: "Research", weights: { coding: 0.10, reasoning: 0.30, speed: 0.15, cost: 0.15, context: 0.30 }, wantsReasoning: "high",   minContext: 200_000 },
  scout:    { label: "Scout",    weights: { coding: 0.20, reasoning: 0.20, speed: 0.35, cost: 0.15, context: 0.10 }, wantsReasoning: "low",    minContext: 64_000 },
  premium:  { label: "Premium",  weights: { coding: 0.30, reasoning: 0.35, speed: 0.08, cost: 0.02, context: 0.25 }, wantsReasoning: "high",   minContext: 128_000 },
};

export const COMPASS_ROLES: CompassRole[] = [
  "coder",
  "reviewer",
  "critic",
  "verifier",
  "research",
  "scout",
  "premium",
];

const NEUTRAL = 0.5;
const clamp01 = (value: number): number => Math.max(0, Math.min(1, value));

const REASONING_RANK: Record<string, number> = { minimal: 1, low: 2, medium: 3, high: 4 };

/** How well a model's reasoning transport matches what the role wants.
 *  Empty support → 0 (honest: grok/qwen/alibaba have no Reasoning-Knopf). */
export function reasoningMatch(
  support: string[] | undefined,
  wanted: RoleRequirement["wantsReasoning"],
): number {
  if (wanted === "none") return 1;
  const levels = (support ?? []).map((s) => REASONING_RANK[s] ?? 0).filter((n) => n > 0);
  if (levels.length === 0) return 0;
  const best = Math.max(...levels);
  const target = REASONING_RANK[wanted];
  if (best >= target) return 1;
  if (best === target - 1) return 0.5;
  return 0.25;
}

/** Keyword coding-affinity heuristic (no real benchmark data exists in the
 *  catalog; transparent + deterministic, documented as an estimate). */
export function codingAffinity(model: LaneModelOption): number {
  const id = model.id.toLowerCase();
  if (id.includes("image") || id.includes("wan2") || id.includes("vision")) return 0.05;
  if (id.includes("code") || id.includes("codex") || id.includes("coder")) return 1;
  if (id.startsWith("gpt-5")) return 0.9;
  if (id.startsWith("claude")) return 0.85;
  if (id.includes("kimi")) return 0.7;
  if (id.includes("qwen")) return 0.6;
  if (id.includes("glm")) return 0.55;
  if (id.includes("deepseek")) return 0.6;
  if (id.includes("minimax")) return 0.55;
  return NEUTRAL;
}

/** Latency → 0..1 (≤500ms ≈ 1.0, ≥8000ms ≈ 0.05). No probe → neutral. */
export function speedScore(durationMs: number | null | undefined): number {
  if (durationMs == null || durationMs <= 0) return NEUTRAL;
  return clamp01(1 - (durationMs - 500) / (8000 - 500)) * 0.95 + 0.05;
}

/** Price → 0..1 (≤$1/1M combined ≈ 1.0, ≥$30 ≈ 0.05). No price → neutral. */
export function costScore(priceIn: number | null | undefined, priceOut: number | null | undefined): number {
  if (priceIn == null && priceOut == null) return NEUTRAL;
  const combined = (priceIn ?? 0) + (priceOut ?? 0);
  return clamp01(1 - (combined - 1) / (30 - 1)) * 0.95 + 0.05;
}

/** Context window → 0..1 relative to the role's useful minimum. Absent → neutral. */
export function contextScore(contextWindow: number | null | undefined, minContext: number): number {
  if (contextWindow == null || contextWindow <= 0 || minContext <= 0) return NEUTRAL;
  return clamp01(contextWindow / minContext) * 0.9 + 0.1;
}

function fmtPrice(priceIn: number | null | undefined, priceOut: number | null | undefined): string | null {
  if (priceIn == null && priceOut == null) return null;
  const combined = (priceIn ?? 0) + (priceOut ?? 0);
  return `$${combined.toFixed(2)}/1M`;
}

function resolveProbe(
  model: LaneModelOption,
  probes?: Record<string, ModelProbeResult>,
): ModelProbeResult | null {
  if (probes) {
    const fresh = probes[probeKey(model.provider, model.id)];
    if (fresh) return fresh;
  }
  return model.probe ?? null;
}

export interface FitScore {
  score: number;
  reasons: string[];
}

/** Score one model for one role (0–100) with the evidence tokens that drove it. */
export function scoreModelForRole(
  model: LaneModelOption,
  role: CompassRole,
  probes?: Record<string, ModelProbeResult>,
): FitScore {
  const req = ROLE_REQUIREMENTS[role];
  // Hard gate: KNOWN-bad models are never recommended. Absent fields fail soft.
  if (model.authenticated === false || model.sinnvoll === false) {
    return { score: 0, reasons: ["nicht erreichbar"] };
  }

  const probe = resolveProbe(model, probes);
  const reasons: string[] = [];

  const coding = codingAffinity(model);
  if (coding >= 0.85) reasons.push("Code-Profil");

  const reasoning = reasoningMatch(model.reasoning_support, req.wantsReasoning);
  if (req.wantsReasoning !== "none") {
    reasons.push(reasoning >= 1 ? "Reason ✓" : reasoning > 0 ? "Reason ~" : "Reason −");
  }

  const speed = speedScore(probe?.duration_ms);
  if (probe?.duration_ms != null && probe.duration_ms > 0) reasons.push(`${probe.duration_ms} ms`);

  const cost = costScore(model.price_in_per_mtok_usd, model.price_out_per_mtok_usd);
  const priceToken = fmtPrice(model.price_in_per_mtok_usd, model.price_out_per_mtok_usd);
  if (priceToken) reasons.push(priceToken);

  const context = contextScore(model.context_window, req.minContext);
  if (model.context_window != null && model.context_window > 0) {
    reasons.push(`${Math.round(model.context_window / 1000)}k ctx`);
  }

  const w = req.weights;
  const weighted =
    w.coding * coding +
    w.reasoning * reasoning +
    w.speed * speed +
    w.cost * cost +
    w.context * context;

  return { score: Math.round(weighted * 100), reasons };
}

export interface ModelFit extends FitScore {
  model: LaneModelOption;
}

/** Rank models for a role, best first; deterministic tie-break on model id. */
export function rankModelsForRole(
  models: LaneModelOption[],
  role: CompassRole,
  probes?: Record<string, ModelProbeResult>,
): ModelFit[] {
  return models
    .map((model) => ({ model, ...scoreModelForRole(model, role, probes) }))
    .sort((a, b) => b.score - a.score || a.model.id.localeCompare(b.model.id));
}
