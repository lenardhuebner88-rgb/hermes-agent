import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import {
  applyChoice,
  editorRows,
  filterSinnvoll,
  isModelReachable,
  persistPayloadFromEditorRows,
  profilesFromEditorRows,
  UNREACHABLE_PROBE_STATUSES,
  type EditorRow,
  type Lane,
  type LaneCatalogProfile,
  type LaneModelOption,
  type LanesResponse,
} from "./api";

// The REAL captured live payload (3 lanes, 10 profiles, 55 models, 7 groups).
// It predates the S1 fields (no sinnvoll/probe/reasoning/authenticated), so it
// is the canonical fail-soft fixture: every new field must be optional and the
// helpers must degrade gracefully when they are absent.
const fixture = JSON.parse(
  readFileSync(new URL("./__fixtures__/lanes-live.json", import.meta.url), "utf8"),
) as LanesResponse;

describe("filterSinnvoll — curated working set", () => {
  it("reduces the 55-model live fixture via the curated heuristic (< 55)", () => {
    const models = fixture.models ?? [];
    expect(models.length).toBe(55);
    // No `sinnvoll` field anywhere in the captured payload → fail-soft heuristic.
    expect(models.some((m) => m.sinnvoll !== undefined)).toBe(false);

    const curated = filterSinnvoll(models);
    expect(curated.length).toBeLessThan(models.length);
    expect(curated.length).toBe(5); // exactly the claude-cli Max-Abo models
    for (const model of curated) {
      expect(model.runtime === "claude-cli" || model.source === "claude-cli").toBe(true);
    }
  });

  it("uses the backend `sinnvoll` flag when present (no heuristic)", () => {
    const models: LaneModelOption[] = [
      { id: "a", label: "a", runtime: "hermes", group: "g", provider: "nous", sinnvoll: true },
      // source claude-cli but explicitly NOT sinnvoll → the flag wins over heuristic
      { id: "b", label: "b", runtime: "claude-cli", group: "g", source: "claude-cli", sinnvoll: false },
      { id: "c", label: "c", runtime: "hermes", group: "g", provider: "openai-codex", sinnvoll: true },
    ];
    expect(filterSinnvoll(models).map((m) => m.id)).toEqual(["a", "c"]);
  });
});

describe("isModelReachable — ModelSelect default filter", () => {
  const base: LaneModelOption = { id: "m", label: "m", runtime: "hermes", group: "g", provider: "openai-codex" };

  it("treats unknown models as reachable (fail-soft on missing fields)", () => {
    expect(isModelReachable(base)).toBe(true);
  });

  it("hides models flagged not-sinnvoll or with a blocking probe", () => {
    expect(isModelReachable({ ...base, sinnvoll: false })).toBe(false);
    for (const status of UNREACHABLE_PROBE_STATUSES) {
      expect(isModelReachable({ ...base, probe: { provider: "openai-codex", model: "m", status } })).toBe(false);
    }
  });

  it("keeps transient quota/rate-limit and healthy probes visible", () => {
    expect(isModelReachable({ ...base, probe: { provider: "p", model: "m", status: "quota_or_rate_limit" } })).toBe(true);
    expect(isModelReachable({ ...base, probe: { provider: "p", model: "m", status: "ok" } })).toBe(true);
    expect(isModelReachable({ ...base, probe: { provider: "p", model: "m", status: "fallback" } })).toBe(true);
  });
});

// --- reasoning roundtrip -----------------------------------------------------

const REASONING_MODELS: LaneModelOption[] = [
  {
    id: "gpt-5.6-sol",
    label: "gpt-5.6-sol",
    runtime: "hermes",
    group: "OpenAI Codex",
    provider: "openai-codex",
    reasoning_support: ["minimal", "low", "medium", "high"],
  },
  {
    id: "qwen3.6-plus",
    label: "qwen3.6-plus",
    runtime: "hermes",
    group: "alibaba-token-plan",
    provider: "alibaba-token-plan",
    reasoning_support: [], // ehrlich: kein Reasoning-Knopf
  },
];

const REASONING_CATALOG: LaneCatalogProfile[] = [
  {
    name: "coder",
    worker_runtime: "hermes",
    default_model: "gpt-5.6-sol",
    default_provider: "openai-codex",
    description: "",
    reasoning_effort: "medium",
    reasoning_support: ["minimal", "low", "medium", "high"],
  },
];

const EMPTY_LANE: Lane = {
  id: "lane_1",
  name: "test",
  active: true,
  builtin: false,
  created_at: 0,
  updated_at: 0,
  profiles: {},
};

function coderRow(): EditorRow {
  return editorRows(EMPTY_LANE, REASONING_CATALOG, REASONING_MODELS).find(
    (row) => row.profile === "coder",
  )!;
}

describe("reasoning stage roundtrip", () => {
  it("editorRows seeds the stage from the profile's current reasoning_effort", () => {
    const row = coderRow();
    expect(row.touched).toBe(false);
    expect(row.defaultReasoning).toBe("medium");
    expect(row.reasoning).toBe("medium"); // valid for the effective model's support
    expect(row.reasoningSupport).toContain("medium");
    expect(row.defaultReasoningSupport).toContain("high");
  });

  it("profilesFromEditorRows sends reasoning_effort only for a non-default value", () => {
    const high = { ...coderRow(), touched: true, reasoning: "high" };
    expect(profilesFromEditorRows([high]).coder?.reasoning_effort).toBe("high");
  });

  it("omits an untouched row even when reasoning is seeded", () => {
    const row = { ...coderRow(), touched: false, reasoning: "medium" };
    expect(profilesFromEditorRows([row]).coder).toBeUndefined();
  });

  it("sends and persists an empty reasoning_effort when a touched row clears to Standard", () => {
    const row = { ...coderRow(), touched: true, reasoning: null };
    expect(profilesFromEditorRows([row]).coder?.reasoning_effort).toBe("");
    expect(persistPayloadFromEditorRows([row]).coder?.reasoning_effort).toBe("");
  });

  it("a reasoning-only change is persisted, falling back to the profile-default model", () => {
    const row = { ...coderRow(), touched: true, reasoning: "high" }; // no model/provider/choice override
    const payload = persistPayloadFromEditorRows([row]);
    expect(payload.coder).toBeDefined();
    expect(payload.coder.model).toBe("gpt-5.6-sol"); // defaultModel, not ""
    expect(payload.coder.reasoning_effort).toBe("high");
    expect(payload.coder.worker_runtime).toBe("hermes");
  });

  it("drops a row with neither a model nor a reasoning override", () => {
    const row = { ...coderRow(), touched: true, reasoning: "medium" };
    expect(persistPayloadFromEditorRows([row]).coder).toBeUndefined();
  });

  it("switching to a model without Reasoning drops the staged value (persist stays valid)", () => {
    const row = { ...coderRow(), reasoning: "high" };
    const qwenChoice = `hermes|alibaba-token-plan|qwen3.6-plus`;
    const switched = applyChoice(row, qwenChoice, REASONING_MODELS);
    expect(switched.model).toBe("qwen3.6-plus");
    expect(switched.reasoningSupport).toEqual([]);
    expect(switched.reasoning).toBeNull();
  });

  it("keeps a still-valid staged value when the model supports it", () => {
    const row = { ...coderRow(), reasoning: "high" };
    const same = applyChoice(row, "hermes|openai-codex|gpt-5.6-sol", REASONING_MODELS);
    expect(same.reasoning).toBe("high");
    expect(same.reasoningSupport).toContain("high");
  });

  it("seeds config fallbacks when the lane has no profile entry", () => {
    const fallbackProviders = [
      { provider: "openrouter", model: "backup/model" },
      { provider: "neuralwatt", model: "backup-fast" },
    ];
    const catalog = [{ ...REASONING_CATALOG[0], fallback_providers: fallbackProviders }];
    const row = editorRows(EMPTY_LANE, catalog, REASONING_MODELS).find(
      (candidate) => candidate.profile === "coder",
    );
    expect(row?.fallbackProviders).toEqual(fallbackProviders);
  });
});
