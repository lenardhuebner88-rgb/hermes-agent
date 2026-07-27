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

import type { LaneCatalogProfile, LaneModelOption, LaneProfileEntry, ModelProbeResult } from "./api";
import { filterSinnvoll, probeKey, UNREACHABLE_PROBE_STATUSES } from "./api";

// Re-exported so fit consumers (and tests) can key probes through one surface.
export { probeKey };

export type CompassRole =
  | "coder"
  | "coder-frontend"
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
  "coder-frontend": { label: "Frontend", weights: { coding: 0.40, reasoning: 0.20, speed: 0.20, cost: 0.12, context: 0.08 }, wantsReasoning: "medium", minContext: 128_000 },
  reviewer: { label: "Reviewer", weights: { coding: 0.25, reasoning: 0.35, speed: 0.12, cost: 0.10, context: 0.18 }, wantsReasoning: "high",   minContext: 128_000 },
  critic:   { label: "Critic",   weights: { coding: 0.15, reasoning: 0.40, speed: 0.10, cost: 0.10, context: 0.25 }, wantsReasoning: "high",   minContext: 128_000 },
  verifier: { label: "Verifier", weights: { coding: 0.25, reasoning: 0.20, speed: 0.30, cost: 0.15, context: 0.10 }, wantsReasoning: "medium", minContext: 64_000 },
  research: { label: "Research", weights: { coding: 0.10, reasoning: 0.30, speed: 0.15, cost: 0.15, context: 0.30 }, wantsReasoning: "high",   minContext: 200_000 },
  scout:    { label: "Scout",    weights: { coding: 0.20, reasoning: 0.20, speed: 0.35, cost: 0.15, context: 0.10 }, wantsReasoning: "low",    minContext: 64_000 },
  premium:  { label: "Premium",  weights: { coding: 0.30, reasoning: 0.35, speed: 0.08, cost: 0.02, context: 0.25 }, wantsReasoning: "high",   minContext: 128_000 },
};

export const COMPASS_ROLES: CompassRole[] = [
  "coder",
  "coder-frontend",
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
  // Hard gate: a model KNOWN unreachable is never recommended. claude-cli
  // (MAX-Abo / claude binary) carries no inventory provider-credential, so its
  // `authenticated` flag is NOT a reachability signal — exempt it from that
  // clause (sinnvoll still gates it, so a deliberately-excluded claude model is
  // still zeroed). Absent fields fail soft (older payloads / captured fixture).
  const knownUnreachable =
    model.sinnvoll === false ||
    (model.authenticated === false && model.runtime !== "claude-cli");
  if (knownUnreachable) {
    return { score: 0, reasons: ["nicht erreichbar"] };
  }

  const probe = resolveProbe(model, probes);
  if (probe && UNREACHABLE_PROBE_STATUSES.has(probe.status)) {
    return { score: 0, reasons: ["nicht erreichbar"] };
  }
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

// --- S5 guided lane build: Kompass presets -----------------------------------
//
// A preset turns the compass ranking into a complete lane draft: every catalog
// profile whose name is a compass role gets that role's top-ranked model
// (respecting the preset's price doctrine). Locked profiles are skipped — the
// operator reviews the draft in the matrix and persists via the normal SaveBar.

export type LanePresetId = "guenstig" | "premium";

export interface LanePreset {
  id: LanePresetId;
  label: string;
  blurb: string;
}

export const LANE_PRESETS: LanePreset[] = [
  {
    id: "guenstig",
    label: "Coding-Team günstig",
    blurb: "Spitzenmodell je Rolle unter der Preisgrenze — Kosten zuerst.",
  },
  {
    id: "premium",
    label: "Premium",
    blurb: "Spitzenmodell je Rolle ohne Preisbremse — Qualität zuerst.",
  },
];

/** Combined $/1Mtok price cap for the „günstig" preset. */
const GUENSTIG_PRICE_CAP_USD = 8;

function combinedPrice(model: LaneModelOption): number | null {
  if (model.price_in_per_mtok_usd == null && model.price_out_per_mtok_usd == null) return null;
  return (model.price_in_per_mtok_usd ?? 0) + (model.price_out_per_mtok_usd ?? 0);
}

function presetPick(
  ranked: ModelFit[],
  preset: LanePresetId,
): LaneModelOption | null {
  const viable = ranked.filter((fit) => fit.score > 0);
  if (viable.length === 0) return null;
  if (preset === "premium") return viable[0]!.model;
  // günstig: best score under the cap; if nothing is under it, the cheapest
  // viable model wins (deterministic: rank order, then id).
  const underCap = viable.filter((fit) => {
    const price = combinedPrice(fit.model);
    return price != null && price <= GUENSTIG_PRICE_CAP_USD;
  });
  if (underCap.length > 0) return underCap[0]!.model;
  return (
    [...viable].sort((a, b) => {
      const pa = combinedPrice(a.model) ?? Number.POSITIVE_INFINITY;
      const pb = combinedPrice(b.model) ?? Number.POSITIVE_INFINITY;
      return pa - pb || a.model.id.localeCompare(b.model.id);
    })[0]?.model ?? null
  );
}

/** Build a lane-draft `profiles` payload from a preset: one entry per catalog
 *  profile that maps to a compass role. Unknown/locked profiles stay untouched
 *  (they keep the profile-config default in the new lane). */
export function buildPresetDraft(
  preset: LanePresetId,
  models: LaneModelOption[],
  catalog: LaneCatalogProfile[],
  probes?: Record<string, ModelProbeResult>,
): Record<string, Partial<LaneProfileEntry>> {
  const candidates = filterSinnvoll(models);
  const out: Record<string, Partial<LaneProfileEntry>> = {};
  for (const profile of catalog) {
    if (profile.locked) continue;
    if (!COMPASS_ROLES.includes(profile.name as CompassRole)) continue;
    const ranked = rankModelsForRole(candidates, profile.name as CompassRole, probes);
    const pick = presetPick(ranked, preset);
    if (!pick) continue;
    out[profile.name] = {
      worker_runtime: pick.runtime,
      provider: pick.runtime === "hermes" ? (pick.provider ?? null) : null,
      model: pick.id,
    };
  }
  return out;
}
