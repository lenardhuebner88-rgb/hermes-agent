import { describe, expect, it } from "vitest";

import type { LoopModelsResponse, LoopPackSummary } from "../../lib/types";
import {
  autolandArmed,
  buildPhaseOverrides,
  effortLevelsFor,
  routeAfterEngineChange,
  type PhaseRoute,
} from "./routeOverrides";

const models = {
  engines: {
    claude: { label: "Claude", models: ["claude-opus-5"], effort_levels: ["low", "medium", "high", "xhigh", "max"] },
    codex: { label: "Codex", models: ["gpt-5.6-sol"], effort_levels: ["none", "low", "medium", "high", "xhigh"] },
    xai: { label: "xAI", models: ["grok-4.5"], effort_levels: ["low", "medium", "high"] },
    kimi: { label: "Kimi", models: ["k3"], effort_levels: [] },
  },
} as unknown as LoopModelsResponse;

function pack(overrides: Partial<LoopPackSummary> = {}): LoopPackSummary {
  return {
    name: "fliessband",
    type: "pipeline",
    autoland_capable: true,
    phases: {
      plan: { engine: "claude", model: "claude-opus-5", timeout: 60 },
      build: { engine: "codex", model: "gpt-5.6-sol", timeout: 60 },
    },
    ...overrides,
  } as unknown as LoopPackSummary;
}

describe("effortLevelsFor", () => {
  it("liefert die Stufen der Engine aus dem Backend-Katalog", () => {
    expect(effortLevelsFor(models, "claude")).toEqual(["low", "medium", "high", "xhigh", "max"]);
    // Der Endpoint lehnt 'minimal' ab — es darf hier gar nicht erst auftauchen.
    expect(effortLevelsFor(models, "codex")).not.toContain("minimal");
  });

  it("liefert [] für eine Engine ohne Effort-Transport (= kein Control)", () => {
    expect(effortLevelsFor(models, "kimi")).toEqual([]);
  });

  it("liefert [] statt zu werfen, wenn der Katalog noch nicht geladen ist", () => {
    expect(effortLevelsFor(null, "claude")).toEqual([]);
    expect(effortLevelsFor(models, "gibtsnicht")).toEqual([]);
  });
});

describe("routeAfterEngineChange", () => {
  it("behält den Effort, solange die Engine dieselbe bleibt", () => {
    const before: PhaseRoute = { engine: "claude", model: "claude-opus-5", effort: "max" };
    expect(routeAfterEngineChange(before, { engine: "claude", model: "claude-sonnet-5" }).effort).toBe("max");
  });

  it("verwirft den Effort beim Engine-Wechsel", () => {
    // `max` gibt es auf xai nicht — mitgewandert würde der Lauf fail-closed sterben.
    const before: PhaseRoute = { engine: "claude", model: "claude-opus-5", effort: "max" };
    const after = routeAfterEngineChange(before, { engine: "xai", model: "grok-4.5" });
    expect(after.effort).toBeNull();
    expect(after.engine).toBe("xai");
  });

  it("kommt mit einer noch unbekannten Phase klar", () => {
    expect(routeAfterEngineChange(undefined, { engine: "codex", model: "gpt-5.6-sol" }).effort).toBeNull();
  });
});

describe("buildPhaseOverrides", () => {
  it("schickt Engine, Modell und den gesetzten Effort", () => {
    const out = buildPhaseOverrides(pack(), {
      plan: { engine: "claude", model: "claude-opus-5", effort: "high" },
      build: { engine: "codex", model: "gpt-5.6-sol", effort: "medium" },
    });
    expect(out.PHASE_PLAN_EFFORT).toBe("high");
    expect(out.PHASE_BUILD_EFFORT).toBe("medium");
    expect(out.PHASE_PLAN_MODEL).toBe("claude-opus-5");
  });

  it("lässt den Effort-Override bei 'Standard' ganz weg", () => {
    // Kein leerer Wert: der CLI-Default soll gelten, nicht ein leeres Flag.
    const out = buildPhaseOverrides(pack(), {
      plan: { engine: "claude", model: "claude-opus-5", effort: null },
      build: { engine: "codex", model: "gpt-5.6-sol", effort: null },
    });
    expect(out).not.toHaveProperty("PHASE_PLAN_EFFORT");
    expect(out).not.toHaveProperty("PHASE_BUILD_EFFORT");
    expect(out.PHASE_PLAN_ENGINE).toBe("claude");
  });
});

describe("autolandArmed — der Zweitschlüssel", () => {
  it("ist scharf, wenn der Pack-Name exakt wiederholt wird", () => {
    expect(autolandArmed(pack(), true, "fliessband")).toBe(true);
    expect(autolandArmed(pack(), true, "  fliessband  ")).toBe(true);
  });

  it("bleibt aus, wenn der Name nicht stimmt", () => {
    // Ein kopiertes Override-File eines anderen Packs darf nicht landen.
    expect(autolandArmed(pack(), true, "anderes-pack")).toBe(false);
    expect(autolandArmed(pack(), true, "")).toBe(false);
    expect(autolandArmed(pack(), true, "Fliessband")).toBe(false); // case-sensitiv
  });

  it("bleibt aus, solange der Schalter nicht gesetzt ist", () => {
    expect(autolandArmed(pack(), false, "fliessband")).toBe(false);
  });

  it("greift nicht bei Vertrags-Autoland und nicht bei sweep-Packs", () => {
    expect(autolandArmed(pack({ autoland: true }), true, "fliessband")).toBe(false);
    expect(autolandArmed(pack({ autoland_capable: false }), true, "fliessband")).toBe(false);
  });
});
