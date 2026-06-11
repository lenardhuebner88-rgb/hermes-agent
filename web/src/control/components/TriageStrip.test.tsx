import { describe, expect, it } from "vitest";
import {
  ESCALATION_MODEL,
  ESCALATION_PROFILE,
  effectiveRuntime,
  escalationPatchSequence,
  escalationPlan,
  type LanesRuntimeInfo,
} from "../views/lanes/api";

// „Nochmal stärker" muss auf jedem Profil wirklich stärker sein: auf
// Nicht-claude-cli-Runtimes (ohne Anthropic-Key fiele der Worker still aufs
// Provider-Fallback, live belegt 2026-06-11: research → gpt-5.4) wird der
// Task aufs premium-Profil umgehängt; claude-cli-Profile bleiben beim
// reinen model_override.

const LANES: LanesRuntimeInfo = {
  active_id: "lane_api",
  lanes: [
    {
      id: "lane_api",
      active: true,
      profiles: {
        research: { worker_runtime: "hermes", kanban_spawn_health: { status: "healthy" } },
        premium: { worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
      },
    },
    {
      id: "lane_max",
      active: false,
      profiles: { research: { worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } } },
    },
  ],
  profiles: [
    { name: "research", worker_runtime: "hermes", kanban_spawn_health: { status: "healthy" } },
    { name: "coder-claude", worker_runtime: "claude-cli", kanban_spawn_health: { status: "healthy" } },
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

describe("escalationPlan", () => {
  it("claude-cli-Runtime → nur model_override, KEIN assignee im PATCH-Body", () => {
    const plan = escalationPlan("premium", LANES);
    expect(plan.reassigns).toBe(false);
    expect(plan.warns).toBe(false);
    expect(plan.patch).toEqual({ model_override: ESCALATION_MODEL });
    expect(plan.hint).toContain(ESCALATION_MODEL);
  });

  it("hermes-Runtime → hängt auf premium um (assignee im PATCH-Body) und nennt den Tool-Verlust", () => {
    const plan = escalationPlan("research", LANES);
    expect(plan.reassigns).toBe(true);
    expect(plan.warns).toBe(true);
    expect(plan.patch).toEqual({ assignee: ESCALATION_PROFILE, model_override: ESCALATION_MODEL });
    expect(plan.hint).toContain("premium");
    expect(plan.hint).toContain("Spezialwerkzeuge");
    expect(plan.hint).toContain("research");
  });

  it("ohne Katalog (fetch fehlgeschlagen) → fail-soft: kein Umhängen, neutraler Hint", () => {
    const plan = escalationPlan("research", null);
    expect(plan.disabled).toBe(false);
    expect(plan.reassigns).toBe(false);
    expect(plan.warns).toBe(false);
    expect(plan.patch).toEqual({ model_override: ESCALATION_MODEL });
  });

  it("unhealthy premium-Ziellane → blockiert Nochmal stärker mit sicherem Alternativ-Hinweis", () => {
    const lanes: LanesRuntimeInfo = {
      ...LANES,
      lanes: LANES.lanes?.map((lane) => lane.active ? {
        ...lane,
        profiles: {
          ...lane.profiles,
          premium: {
            worker_runtime: "claude-cli",
            kanban_spawn_health: {
              status: "unhealthy",
              reason: "Unknown skill(s): kanban-worker",
            },
          },
        },
      } : lane),
    };

    const plan = escalationPlan("research", lanes);

    expect(plan.disabled).toBe(true);
    expect(plan.reassigns).toBe(false);
    expect(plan.warns).toBe(true);
    expect(plan.patch).toBeNull();
    expect(plan.hint).toContain("premium");
    expect(plan.hint).toContain("Unknown skill(s): kanban-worker");
    expect(plan.hint).toContain("Nochmal");
    expect(plan.hint).toContain("Operator");
  });

  it("disabled escalation path liefert keine PATCH-Bodies", () => {
    const plan = escalationPlan("research", {
      ...LANES,
      lanes: LANES.lanes?.map((lane) => lane.active ? {
        ...lane,
        profiles: {
          ...lane.profiles,
          premium: { worker_runtime: "claude-cli", kanban_spawn_health: { status: "unhealthy" } },
        },
      } : lane),
    });

    expect(escalationPatchSequence(plan)).toEqual([]);
  });
});
