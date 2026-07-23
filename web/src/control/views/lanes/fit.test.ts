import { describe, expect, it } from "vitest";
import {
  COMPASS_ROLES,
  ROLE_REQUIREMENTS,
  codingAffinity,
  contextScore,
  costScore,
  probeKey,
  rankModelsForRole,
  reasoningMatch,
  scoreModelForRole,
  speedScore,
} from "./fit";
import type { LaneModelOption, ModelProbeResult } from "./api";

function mkModel(overrides: Partial<LaneModelOption> & { id: string }): LaneModelOption {
  return {
    label: overrides.id,
    runtime: "hermes",
    group: "Test",
    provider: "openai-codex",
    ...overrides,
  };
}

// A cheap, fast, reasoning-capable coder.
const FAST_CODER = mkModel({
  id: "gpt-5.6-sol",
  provider: "openai-codex",
  authenticated: true,
  sinnvoll: true,
  reasoning_support: ["minimal", "low", "medium", "high"],
  price_in_per_mtok_usd: 1.0,
  price_out_per_mtok_usd: 4.0,
  context_window: 200_000,
  probe: { provider: "openai-codex", model: "gpt-5.6-sol", status: "ok", duration_ms: 400 },
});

// An expensive premium reasoner.
const PREMIUM_REASONER = mkModel({
  id: "claude-opus-4-8",
  runtime: "claude-cli",
  provider: null,
  group: "Claude (Max-Abo)",
  authenticated: true,
  sinnvoll: true,
  reasoning_support: ["low", "medium", "high"],
  price_in_per_mtok_usd: 15,
  price_out_per_mtok_usd: 75,
  context_window: 200_000,
  probe: { provider: "", model: "claude-opus-4-8", status: "ok", duration_ms: 1500 },
});

// No Reasoning-Knopf (honest: alibaba/qwen has no transport branch).
const NO_REASONING = mkModel({
  id: "qwen3.6-plus",
  provider: "alibaba-token-plan",
  authenticated: true,
  sinnvoll: true,
  reasoning_support: [],
  price_in_per_mtok_usd: 0.4,
  price_out_per_mtok_usd: 1.2,
  context_window: 128_000,
});

const SEED = [FAST_CODER, PREMIUM_REASONER, NO_REASONING];

describe("fit signal helpers (pure, deterministic)", () => {
  it("reasoningMatch grades support against the role's wanted level", () => {
    expect(reasoningMatch(["low", "medium", "high"], "high")).toBe(1);
    expect(reasoningMatch(["minimal", "low", "medium", "high"], "medium")).toBe(1);
    expect(reasoningMatch(["low", "medium"], "high")).toBe(0.5);
    expect(reasoningMatch(["minimal"], "high")).toBe(0.25);
    expect(reasoningMatch([], "high")).toBe(0);
    expect(reasoningMatch(undefined, "high")).toBe(0);
    expect(reasoningMatch([], "none")).toBe(1);
  });

  it("speed/cost/context degrade to neutral (0.5) when data is absent", () => {
    expect(speedScore(undefined)).toBe(0.5);
    expect(speedScore(null)).toBe(0.5);
    expect(costScore(undefined, undefined)).toBe(0.5);
    expect(contextScore(undefined, 128_000)).toBe(0.5);
    // and rise with evidence
    expect(speedScore(400)).toBeGreaterThan(speedScore(4000));
    expect(costScore(1, 2)).toBeGreaterThan(costScore(20, 40));
    expect(contextScore(200_000, 128_000)).toBeGreaterThan(contextScore(32_000, 128_000));
  });

  it("codingAffinity spots coders and rejects image models", () => {
    expect(codingAffinity(mkModel({ id: "kimi-k2.7-code" }))).toBe(1);
    expect(codingAffinity(FAST_CODER)).toBeGreaterThan(0.8);
    expect(codingAffinity(mkModel({ id: "qwen-image-2.0" }))).toBeLessThan(0.1);
  });

  it("probeKey is stable across provider-less models", () => {
    expect(probeKey("openai-codex", "gpt-5.6-sol")).toBe("openai-codex::gpt-5.6-sol");
    expect(probeKey(null, "claude-opus-4-8")).toBe("::claude-opus-4-8");
  });
});

describe("scoreModelForRole", () => {
  it("is deterministic for the same input", () => {
    const a = scoreModelForRole(FAST_CODER, "coder");
    const b = scoreModelForRole(FAST_CODER, "coder");
    expect(a).toEqual(b);
    expect(a.score).toBeGreaterThanOrEqual(0);
    expect(a.score).toBeLessThanOrEqual(100);
  });

  it("never recommends a model KNOWN to be unreachable or un-curated (score 0)", () => {
    const unauthenticated = mkModel({ id: "x-1", authenticated: false });
    const uncurated = mkModel({ id: "x-2", authenticated: true, sinnvoll: false });
    expect(scoreModelForRole(unauthenticated, "coder").score).toBe(0);
    expect(scoreModelForRole(uncurated, "coder").score).toBe(0);
  });

  it("fails soft when authenticated/sinnvoll are absent (older payload)", () => {
    const legacy = mkModel({ id: "gpt-5.5", reasoning_support: ["low", "medium", "high"] });
    expect(legacy.authenticated).toBeUndefined();
    expect(legacy.sinnvoll).toBeUndefined();
    expect(scoreModelForRole(legacy, "coder").score).toBeGreaterThan(0);
  });

  it("carries latency + price reason tokens only when the data is present", () => {
    const withData = scoreModelForRole(FAST_CODER, "coder");
    expect(withData.reasons).toContain("400 ms");
    expect(withData.reasons).toContain("$5.00/1M");

    const noProbe = scoreModelForRole(NO_REASONING, "coder");
    expect(noProbe.reasons.some((r) => r.endsWith(" ms"))).toBe(false);
    // qwen has price data → price token present, reasoning absent → "Reason −"
    expect(noProbe.reasons).toContain("$1.60/1M");
    expect(noProbe.reasons).toContain("Reason −");
  });
});

describe("rankModelsForRole", () => {
  it("coder ranking prefers the cheap, fast coder over the expensive reasoner", () => {
    const ranked = rankModelsForRole(SEED, "coder");
    expect(ranked[0].model.id).toBe("gpt-5.6-sol");
    const coder = Object.fromEntries(ranked.map((r) => [r.model.id, r.score]));
    expect(coder["gpt-5.6-sol"]).toBeGreaterThan(coder["claude-opus-4-8"]);
  });

  it("is deterministic (stable tie-break on model id)", () => {
    const a = rankModelsForRole(SEED, "reviewer").map((r) => `${r.model.id}:${r.score}`);
    const b = rankModelsForRole(SEED, "reviewer").map((r) => `${r.model.id}:${r.score}`);
    expect(a).toEqual(b);
  });

  it("honors every configured role and keeps weights normalized", () => {
    for (const role of COMPASS_ROLES) {
      const w = ROLE_REQUIREMENTS[role].weights;
      const sum = w.coding + w.reasoning + w.speed + w.cost + w.context;
      expect(sum).toBeCloseTo(1, 5);
      const ranked = rankModelsForRole(SEED, role);
      expect(ranked).toHaveLength(SEED.length);
    }
  });

  it("uses fresh probes passed in over the cached model.probe", () => {
    const fresh: Record<string, ModelProbeResult> = {
      [probeKey(FAST_CODER.provider, FAST_CODER.id)]: {
        provider: "openai-codex",
        model: "gpt-5.6-sol",
        status: "ok",
        duration_ms: 7_500, // much slower than the cached 400ms
      },
    };
    const cached = scoreModelForRole(FAST_CODER, "coder");
    const probed = scoreModelForRole(FAST_CODER, "coder", fresh);
    expect(probed.reasons).toContain("7500 ms");
    expect(probed.score).toBeLessThan(cached.score);
  });
});
