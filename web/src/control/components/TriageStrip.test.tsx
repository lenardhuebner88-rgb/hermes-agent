import { describe, expect, it } from "vitest";
import {
  ESCALATION_MODEL,
  effectiveRuntime,
  escalateHintFor,
  type LanesRuntimeInfo,
} from "./TriageStrip";

// Härtung (d): „Nochmal stärker" verspricht claude-fable-5 — auf hermes-
// Runtime-Profilen ohne Anthropic-Key fällt der Worker aber still aufs
// Provider-Fallback zurück (live belegt 2026-06-11: research → gpt-5.4).
// Der Hint muss das runtime-bewusst benennen.

const LANES: LanesRuntimeInfo = {
  active_id: "lane_api",
  lanes: [
    {
      id: "lane_api",
      active: true,
      profiles: {
        research: { worker_runtime: "hermes" },
        premium: { worker_runtime: "claude-cli" },
      },
    },
    {
      id: "lane_max",
      active: false,
      profiles: { research: { worker_runtime: "claude-cli" } },
    },
  ],
  profiles: [
    { name: "research", worker_runtime: "hermes" },
    { name: "coder-claude", worker_runtime: "claude-cli" },
  ],
};

describe("effectiveRuntime", () => {
  it("liest die Runtime aus der AKTIVEN Lane (nicht aus einer inaktiven)", () => {
    expect(effectiveRuntime("research", LANES)).toBe("hermes");
    expect(effectiveRuntime("premium", LANES)).toBe("claude-cli");
  });

  it("fällt auf den Profil-Default zurück, wenn die Lane das Profil nicht kennt", () => {
    expect(effectiveRuntime("coder-claude", LANES)).toBe("claude-cli");
  });

  it("bleibt null ohne Katalog/Profil (fail-soft → neutraler Hint)", () => {
    expect(effectiveRuntime(null, LANES)).toBeNull();
    expect(effectiveRuntime("research", null)).toBeNull();
    expect(effectiveRuntime("unbekannt", LANES)).toBeNull();
  });
});

describe("escalateHintFor", () => {
  it("claude-cli-Runtime → neutraler Standard-Hint mit Modellnamen", () => {
    const { hint, warns } = escalateHintFor("premium", LANES);
    expect(warns).toBe(false);
    expect(hint).toContain(ESCALATION_MODEL);
    expect(hint).not.toContain("Achtung");
  });

  it("hermes-Runtime → warnt ehrlich vor dem stillen Provider-Fallback", () => {
    const { hint, warns } = escalateHintFor("research", LANES);
    expect(warns).toBe(true);
    expect(hint).toContain("API-Runtime");
    expect(hint).toContain("Fallback");
  });

  it("ohne Katalog (fetch fehlgeschlagen) → neutraler Hint, keine falsche Warnung", () => {
    const { warns } = escalateHintFor("research", null);
    expect(warns).toBe(false);
  });
});
