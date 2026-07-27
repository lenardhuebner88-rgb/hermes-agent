// S5 Werkbank helpers: lane diff rows, spawn-health rollup, Kompass presets.
// Pure-function tests only (no snapshots) — same discipline as
// lanes.helpers.test.ts.
import { describe, expect, it } from "vitest";
import {
  laneDiffRows,
  laneHealthSummary,
  type Lane,
  type LaneCatalogProfile,
  type LaneModelOption,
} from "./api";
import { buildPresetDraft, LANE_PRESETS } from "./fit";

const catalog: LaneCatalogProfile[] = [
  { name: "coder", worker_runtime: "hermes", default_model: "gpt-5.5", default_provider: "openai-codex", description: "" },
  { name: "reviewer", worker_runtime: "hermes", default_model: null, default_provider: null, description: "" },
  { name: "premium", worker_runtime: "claude-cli", default_model: "claude-opus-4-8", description: "", locked: true },
];

const models: LaneModelOption[] = [
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "OpenAI", provider: "openai-codex", price_in_per_mtok_usd: 2, price_out_per_mtok_usd: 8 },
  { id: "cheap-1", label: "Cheap One", runtime: "hermes", group: "Nous", provider: "nous", price_in_per_mtok_usd: 0.2, price_out_per_mtok_usd: 0.4, sinnvoll: true },
  // "coder" im Namen → hohe Coding-Affinität: Premium-Top trotz Preis.
  { id: "pricey-coder", label: "Pricey Coder", runtime: "hermes", group: "Nous", provider: "nous", price_in_per_mtok_usd: 15, price_out_per_mtok_usd: 45, sinnvoll: true },
];

function lane(id: string, profiles: Lane["profiles"]): Lane {
  return { id, name: id, profiles, active: false, builtin: false, created_at: null, updated_at: null };
}

describe("laneDiffRows", () => {
  it("marks rows differing in model wiring, keeps identical rows clean", () => {
    const a = lane("a", { coder: { worker_runtime: "hermes", provider: "openai-codex", model: "gpt-5.5" } });
    const b = lane("b", { coder: { worker_runtime: "hermes", provider: "nous", model: "cheap-1" } });
    const rows = laneDiffRows(a, b, catalog, models);
    const coder = rows.find((r) => r.profile === "coder")!;
    expect(coder.differs).toBe(true);
    expect(coder.aLabel).toContain("GPT-5.5");
    expect(coder.bLabel).toContain("Cheap One");
    // Both lanes leave reviewer unwired → same "Standard" label, no diff.
    const reviewer = rows.find((r) => r.profile === "reviewer")!;
    expect(reviewer.differs).toBe(false);
    expect(reviewer.aLabel).toBe(reviewer.bLabel);
    expect(reviewer.aLabel).toContain("Standard");
  });

  it("counts a provider-only flip as a deviation", () => {
    const a = lane("a", { coder: { worker_runtime: "hermes", provider: "nous", model: "cheap-1" } });
    const b = lane("b", { coder: { worker_runtime: "hermes", provider: "openai-codex", model: "cheap-1" } });
    expect(laneDiffRows(a, b, catalog, models).find((r) => r.profile === "coder")!.differs).toBe(true);
  });

  it("appends lane-only extras after the catalog rows (sorted)", () => {
    const a = lane("a", { zeta: { worker_runtime: "hermes", model: "cheap-1" } });
    const b = lane("b", {});
    const rows = laneDiffRows(a, b, catalog, models);
    expect(rows[rows.length - 1]!.profile).toBe("zeta");
    expect(rows[rows.length - 1]!.differs).toBe(true);
  });

  it("carries per-cell probe evidence (cached models[].probe)", () => {
    const withProbe: LaneModelOption[] = models.map((m) =>
      m.id === "gpt-5.5" ? { ...m, probe: { provider: "openai-codex", model: "gpt-5.5", status: "ok" as const, duration_ms: 320 } } : m,
    );
    const a = lane("a", {});
    const b = lane("b", {});
    const coder = laneDiffRows(a, b, catalog, withProbe).find((r) => r.profile === "coder")!;
    expect(coder.aProbe?.duration_ms).toBe(320);
  });
});

describe("laneHealthSummary", () => {
  it("rolls up healthy/unhealthy/unknown across the catalog", () => {
    const l = lane("x", {
      coder: { worker_runtime: "hermes", model: "gpt-5.5", kanban_spawn_health: { status: "healthy" } },
      reviewer: { worker_runtime: "hermes", model: "cheap-1", kanban_spawn_health: { status: "unhealthy", reason: "boom" } },
    });
    const summary = laneHealthSummary(l, catalog);
    expect(summary.healthy).toBe(1);
    expect(summary.unhealthy).toBe(1);
    expect(summary.unknown).toBe(1); // premium: no evidence
    expect(summary.rows.find((r) => r.profile === "reviewer")!.health?.reason).toBe("boom");
  });
});

describe("buildPresetDraft", () => {
  it("offers the two documented presets", () => {
    expect(LANE_PRESETS.map((p) => p.id)).toEqual(["guenstig", "premium"]);
  });

  it("premium picks the pricey top model, günstig stays under the price cap", () => {
    const openCatalog = catalog.filter((p) => !p.locked);
    const premium = buildPresetDraft("premium", models, openCatalog);
    const guenstig = buildPresetDraft("guenstig", models, openCatalog);
    // both drafts cover the same open compass roles
    expect(Object.keys(premium).sort()).toEqual(Object.keys(guenstig).sort());
    expect(Object.keys(premium)).toContain("coder");
    const priceOf = (id: string | null | undefined) => {
      const m = models.find((x) => x.id === id)!;
      return (m.price_in_per_mtok_usd ?? 0) + (m.price_out_per_mtok_usd ?? 0);
    };
    for (const entry of Object.values(guenstig)) {
      expect(priceOf(entry.model)).toBeLessThanOrEqual(8);
    }
    // premium has no price brake — at least one role may go pricey
    expect(Object.values(premium).some((e) => priceOf(e.model) > 8)).toBe(true);
  });

  it("skips locked profiles and non-compass catalog entries", () => {
    const draft = buildPresetDraft("premium", models, catalog);
    expect(draft.premium).toBeUndefined();
    const withAlien: LaneCatalogProfile[] = [
      ...catalog,
      { name: "alien-role", worker_runtime: "hermes", default_model: null, description: "" },
    ];
    expect(buildPresetDraft("premium", models, withAlien)["alien-role"]).toBeUndefined();
  });
});
