import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  activateLane,
  applyChoice,
  choiceFromEntry,
  choiceOverrideLabel,
  deleteLane,
  editorRows,
  entryFromChoice,
  importOpenRouterModels,
  laneEntryWarnings,
  laneChoiceWarning,
  laneProfileSpawnHealth,
  runLaneAuthSmoke,
  modelLabel,
  modelsForProvider,
  persistLaneModels,
  profilesFromEditorRows,
  providerOptions,
  smokeCheckLaneConfig,
  type EditorRow,
  type Lane,
  type LaneCatalogProfile,
  type LaneModelOption,
} from "./api";

function jsonResponse(body: unknown): Response {
  return {
    status: 200,
    ok: true,
    clone() {
      return this;
    },
    async json() {
      return body;
    },
    async text() {
      return JSON.stringify(body);
    },
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => jsonResponse({ lane: { id: "lane_x" } }));
  vi.stubGlobal("window", {
    __HERMES_AUTH_REQUIRED__: false,
    __HERMES_SESSION_TOKEN__: "tok-test",
    location: { reload: vi.fn(), assign: vi.fn(), pathname: "/control/lanes", search: "" },
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("lanes api client", () => {
  it("activateLane fires a POST against the activate endpoint", async () => {
    await activateLane("lane_abc");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/lane_abc/activate");
    expect(init.method).toBe("POST");
  });

  it("deleteLane fires a DELETE against the lane resource", async () => {
    await deleteLane("lane_abc");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/lane_abc");
    expect(init.method).toBe("DELETE");
  });

  it("smokeCheckLaneConfig posts the selected profile/runtime/model combo", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      status: "healthy",
      reason: "ok",
      dispatcher_path: "hermes",
      resolved_model: "gpt-5.5",
    }));

    const result = await smokeCheckLaneConfig("coder", { worker_runtime: "hermes", model: "gpt-5.5" });

    expect(result.status).toBe("healthy");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/spawn-check");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      profile: "coder",
      worker_runtime: "hermes",
      provider: null,
      model: "gpt-5.5",
    });
  });

  it("runLaneAuthSmoke posts lane, roles, and bounded timeout to auth-smoke", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      ok: true,
      lane_id: "lane_1",
      source: "lanes-auth-smoke",
      scope: {
        requested_roles: ["reviewer"],
        checked_role_count: 1,
        total_role_count: 1,
        truncated: false,
        role_limit: 12,
      },
      summary: {
        decision: "ready",
        safe_to_activate: true,
        ok_count: 1,
        blocking_roles: [],
        fallback_roles: [],
        skipped_roles: [],
        checked_role_count: 1,
        total_role_count: 1,
        truncated: false,
        recommended_next_action: "Lane kann nach kontrolliertem Dashboard-Respawn erneut produktiv verifiziert werden.",
      },
      results: [],
    }));

    const result = await runLaneAuthSmoke({ laneId: "lane_1", roles: ["reviewer"], timeoutSeconds: 30 });

    expect(result.source).toBe("lanes-auth-smoke");
    expect(result.summary).toBeDefined();
    expect(result.summary?.decision).toBe("ready");
    expect(result.scope).toBeDefined();
    expect(result.scope?.checked_role_count).toBe(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/auth-smoke");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      lane_id: "lane_1",
      roles: ["reviewer"],
      timeout_seconds: 30,
    });
  });


  it("importOpenRouterModels posts pasted ids to the smoke/admit endpoint", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      admitted: ["xiaomi/mimo-v2.5"],
      configured: ["xiaomi/mimo-v2.5"],
      results: [
        { id: "xiaomi/mimo-v2.5", status: "admitted", reason: "Smoke ok; added to config" },
      ],
    }));

    const result = await importOpenRouterModels("xiaomi/mimo-v2.5");

    expect(result.admitted).toEqual(["xiaomi/mimo-v2.5"]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/openrouter-models/import");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ raw_text: "xiaomi/mimo-v2.5" });
  });

  it("persistLaneModels posts profiles to the persist endpoint", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({
      written: ["coder"],
      failed: [],
      lanes: [],
      active_id: "lane_1",
    }));

    const result = await persistLaneModels(
      {
        coder: {
          worker_runtime: "hermes",
          provider: "openai-codex",
          model: "gpt-5.5",
          fallback_providers: [],
        },
      },
      ["research"],
    );

    expect(result.written).toEqual(["coder"]);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/plugins/kanban/lanes/persist");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      profiles: {
        coder: {
          worker_runtime: "hermes",
          provider: "openai-codex",
          model: "gpt-5.5",
          fallback_providers: [],
        },
      },
      removed_profiles: ["research"],
    });
  });
});

const MODELS: LaneModelOption[] = [
  { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: true },
  { id: "claude-opus-4-8", label: "Claude Opus 4.8", runtime: "claude-cli", group: "Claude (Max-Abo)", provider: null, locked: false },
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "OpenAI Codex", provider: "openai-codex" },
  { id: "glm-5.2-fast", label: "GLM 5.2 Fast", runtime: "hermes", group: "Neuralwatt", provider: "neuralwatt" },
  { id: "qwen/qwen3.7-max", label: "Qwen 3.7 Max", runtime: "hermes", group: "OpenRouter", provider: "openrouter" },
];

describe("choice encoding", () => {
  it("maps default / claude-auto / explicit entries round-trip", () => {
    expect(choiceFromEntry(undefined)).toBe("");
    expect(choiceFromEntry({ worker_runtime: null, model: null })).toBe("");
    expect(choiceFromEntry({ worker_runtime: "claude-cli", model: null })).toBe("claude-cli|");
    expect(choiceFromEntry({ worker_runtime: "hermes", model: "gpt-5.5" })).toBe("hermes|gpt-5.5");

    expect(entryFromChoice("")).toBeNull();
    expect(entryFromChoice("claude-cli|")).toEqual({ worker_runtime: "claude-cli", model: null });
    expect(entryFromChoice("hermes|gpt-5.5")).toEqual({ worker_runtime: "hermes", model: "gpt-5.5" });
  });

  it("derives the runtime from the model id when the entry has none", () => {
    expect(choiceFromEntry({ worker_runtime: null, model: "claude-fable-5" })).toBe(
      "claude-cli|claude-fable-5",
    );
    expect(choiceFromEntry({ worker_runtime: null, model: "gpt-5.5" })).toBe("hermes|gpt-5.5");
  });

  it("modelLabel prefers the catalog label and falls back to the id", () => {
    expect(modelLabel("gpt-5.5", MODELS)).toBe("GPT-5.5");
    expect(modelLabel("unbekannt-9", MODELS)).toBe("unbekannt-9");
  });

  it("provider helpers expose provider-first dynamic options", () => {
    expect(providerOptions(MODELS)).toEqual([
      { id: "openai-codex", label: "OpenAI Codex" },
      { id: "neuralwatt", label: "Neuralwatt" },
      { id: "openrouter", label: "OpenRouter" },
    ]);
    expect(modelsForProvider("neuralwatt", MODELS).map((m) => m.id)).toEqual(["glm-5.2-fast"]);
    expect(modelsForProvider("openrouter", MODELS).map((m) => m.id)).toEqual(["qwen/qwen3.7-max"]);
  });

  it("warns when a persisted choice pairs a model with the wrong runtime", () => {
    expect(laneChoiceWarning("hermes|claude-fable-5", MODELS)).toContain("claude-cli");
    expect(laneChoiceWarning("claude-cli|gpt-5.5", MODELS)).toContain("hermes");
    expect(laneChoiceWarning("hermes|gpt-5.5", MODELS)).toBeNull();
    expect(laneChoiceWarning("", MODELS)).toBeNull();
  });
});

describe("lane fallback warnings", () => {
  it("warns for identical primary/fallback and missing provider/model", () => {
    const [row] = editorRows(
      {
        id: "lane_1",
        name: "test",
        active: true,
        builtin: false,
        created_at: 0,
        updated_at: 0,
        profiles: {
          coder: {
            worker_runtime: "hermes",
            provider: "openrouter",
            model: "qwen/qwen3.7-max",
            fallback_providers: [
              { provider: "openrouter", model: "qwen/qwen3.7-max" },
              { provider: "", model: "" },
            ],
          },
        },
      },
      [{ name: "coder", worker_runtime: "hermes", default_model: "gpt-5.5", default_provider: "openai-codex", description: "" }],
      MODELS,
    );

    expect(laneEntryWarnings(row)).toContain("Primary and fallback are identical.");
    expect(laneEntryWarnings(row)).toContain("Each fallback requires provider and model.");
  });
});

describe("readiness & override state", () => {
  const catalog: LaneCatalogProfile[] = [
    {
      name: "coder",
      worker_runtime: "hermes",
      default_model: "gpt-5.5",
      default_provider: "openai-codex",
      description: "",
      kanban_spawn_health: "healthy",
    },
    { name: "premium", worker_runtime: "claude-cli", default_model: null, description: "" },
  ];
  const baseLane: Lane = {
    id: "lane_1",
    name: "test",
    active: true,
    builtin: false,
    created_at: 0,
    updated_at: 0,
    profiles: {},
  };

  it("laneProfileSpawnHealth: Lane-Eintrag gewinnt über Katalog, String-Form wird normalisiert", () => {
    expect(laneProfileSpawnHealth("coder", baseLane, catalog)).toEqual({ status: "healthy" });
    const laneWithEvidence: Lane = {
      ...baseLane,
      profiles: {
        coder: {
          worker_runtime: "hermes",
          model: null,
          kanban_spawn_health: { status: "unhealthy", reason: "Probe rot" },
        },
      },
    };
    expect(laneProfileSpawnHealth("coder", laneWithEvidence, catalog)).toEqual({
      status: "unhealthy",
      reason: "Probe rot",
    });
    // keine Evidenz auf beiden Ebenen → null (Anzeige: ungeprüft)
    expect(laneProfileSpawnHealth("premium", baseLane, catalog)).toBeNull();
    expect(laneProfileSpawnHealth("fehlt", baseLane, catalog)).toBeNull();
  });

  it("choiceOverrideLabel: Standard → null, sonst sprechendes Modell-Label", () => {
    expect(choiceOverrideLabel("", MODELS)).toBeNull();
    expect(choiceOverrideLabel("claude-cli|", MODELS)).toBe("Claude (automatisch)");
    expect(choiceOverrideLabel("hermes|gpt-5.5", MODELS)).toBe("GPT-5.5");
    // unbekanntes Modell bleibt als Roh-Id sichtbar statt zu verschwinden
    expect(choiceOverrideLabel("hermes|fremd-1", MODELS)).toBe("fremd-1");
  });
});

describe("editor rows", () => {
  const catalog: LaneCatalogProfile[] = [
    { name: "coder", worker_runtime: "hermes", default_model: "gpt-5.5", description: "schreibt" },
    { name: "premium", worker_runtime: "claude-cli", default_model: "claude-fable-5", description: "" },
  ];
  const lane: Lane = {
    id: "lane_1",
    name: "max-abo",
    active: true,
    builtin: true,
    created_at: 0,
    updated_at: 0,
    profiles: {
      premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      altprofil: { worker_runtime: "hermes", model: "gpt-5.5" },
    },
  };

  it("yields one row per catalog profile plus lane-only extras", () => {
    const rows = editorRows(lane, catalog, MODELS);
    expect(rows.map((r) => r.profile)).toEqual(["coder", "premium", "altprofil"]);
    expect(rows[0]).toMatchObject({ choice: "", defaultLabel: "GPT-5.5" });
    expect(rows[1].choice).toBe("claude-cli|claude-fable-5");
    expect(rows[2].choice).toBe("hermes|gpt-5.5");
  });

  it("profilesFromEditorRows drops default rows and keeps explicit ones", () => {
    const rows = editorRows(lane, catalog, MODELS).map((row) => ({ ...row, touched: true }));
    expect(profilesFromEditorRows(rows)).toEqual({
      premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      altprofil: {
        worker_runtime: "hermes",
        provider: null,
        model: "gpt-5.5",
        fallback_providers: [],
      },
    });
  });

  it("the quick-switch full-map keep-set preserves a sibling override", () => {
    const freshRows = editorRows(lane, catalog, MODELS);
    const coder = freshRows.find((row) => row.profile === "coder")!;
    const updatedCoder = {
      ...applyChoice(coder, "hermes|neuralwatt|glm-5.2-fast", MODELS),
      touched: true,
    };
    const profiles = profilesFromEditorRows(
      freshRows.map((row) =>
        row.profile === updatedCoder.profile
          ? updatedCoder
          : { ...row, touched: (row.initialChoice ?? "") !== "" },
      ),
    );
    expect(profiles.coder).toMatchObject({
      provider: "neuralwatt",
      model: "glm-5.2-fast",
    });
    expect(profiles.premium).toEqual({
      worker_runtime: "claude-cli",
      model: "claude-fable-5",
    });
    expect(profiles.altprofil).toMatchObject({
      worker_runtime: "hermes",
      model: "gpt-5.5",
    });
  });

  it("quick-switch keep-set excludes a Standard row that only inherits config fallbacks", () => {
    // F3-3 regression guard: the first round-2 rule (mark every sibling touched)
    // serialized Standard rows that only inherit config fallbacks into a
    // spurious model-null lane override that froze the config chain. The keep-set
    // must include only rows that actually carry a lane override (initialChoice).
    const inherited: EditorRow = {
      touched: false,
      initialChoice: "",
      profile: "research",
      description: "",
      defaultLabel: "automatisch",
      defaultRuntime: "hermes",
      defaultProvider: "openai-codex",
      defaultModel: "gpt-5.5",
      defaultFallbackProviders: [{ provider: "openrouter", model: "fb-model" }],
      worker_runtime: "hermes",
      provider: null,
      model: null,
      fallbackProviders: [{ provider: "openrouter", model: "fb-model" }],
      locked: false,
      lockedReason: null,
      choice: "",
      reasoningSupport: [],
      defaultReasoningSupport: [],
      reasoning: null,
      defaultReasoning: null,
    };
    const keepSet = profilesFromEditorRows([
      { ...inherited, touched: (inherited.initialChoice ?? "") !== "" },
    ]);
    expect(keepSet.research).toBeUndefined();
    // The old over-inclusive rule would have created the spurious override:
    const overInclusive = profilesFromEditorRows([{ ...inherited, touched: true }]);
    expect(overInclusive.research).toBeDefined();
  });

  // F3-1: the locked branch of profilesFromEditorRows conflated "locked" with
  // "claude-cli" and dropped fallback_providers for BOTH. A locked HERMES row
  // (catalog-locked custom-lane entry) carries a fallback chain that must
  // survive the serializer; only claude-cli legitimately drops it.
  function lockedHermesRow(): EditorRow {
    return {
      touched: true,
      initialChoice: "hermes|openai-codex|gpt-5.5",
      profile: "research",
      description: "",
      defaultLabel: "GPT-5.5",
      defaultRuntime: "hermes",
      defaultProvider: "openai-codex",
      defaultModel: "gpt-5.5",
      defaultFallbackProviders: [{ provider: "openrouter", model: "fb-model" }],
      worker_runtime: "hermes",
      provider: "openai-codex",
      model: "gpt-5.5",
      fallbackProviders: [{ provider: "openrouter", model: "fb-model" }],
      locked: true,
      lockedReason: "catalog-locked",
      choice: "hermes|openai-codex|gpt-5.5",
      reasoningSupport: ["minimal", "low", "medium", "high"],
      defaultReasoningSupport: ["minimal", "low", "medium", "high"],
      reasoning: null,
      defaultReasoning: null,
    };
  }

  it("F3-1: a locked HERMES row keeps its fallback chain through the serializer", () => {
    const profiles = profilesFromEditorRows([lockedHermesRow()]);
    expect(profiles.research).toEqual({
      worker_runtime: "hermes",
      provider: "openai-codex",
      model: "gpt-5.5",
      fallback_providers: [{ provider: "openrouter", model: "fb-model" }],
    });
  });

  it("F3-1: a locked HERMES row still serializes a reasoning change alongside fallbacks", () => {
    const row = { ...lockedHermesRow(), reasoning: "high" };
    const profiles = profilesFromEditorRows([row]);
    expect(profiles.research).toEqual({
      worker_runtime: "hermes",
      provider: "openai-codex",
      model: "gpt-5.5",
      fallback_providers: [{ provider: "openrouter", model: "fb-model" }],
      reasoning_effort: "high",
    });
  });

  it("F3-1 control: a claude-cli row still drops fallbacks (no fallback transport)", () => {
    const cliRow: EditorRow = {
      ...lockedHermesRow(),
      profile: "premium",
      worker_runtime: "claude-cli",
      provider: null,
      model: "claude-fable-5",
      choice: "claude-cli|claude-fable-5",
      fallbackProviders: [{ provider: "openrouter", model: "fb-model" }],
    };
    expect(profilesFromEditorRows([cliRow]).premium).toEqual({
      worker_runtime: "claude-cli",
      model: "claude-fable-5",
    });
  });

  it("keeps ordinary profiles on claude-cli when a Claude Max model is selected", () => {
    const cloudMaxLane: Lane = {
      ...lane,
      profiles: {
        coder: { worker_runtime: "claude-cli", provider: null, model: "claude-opus-4-8", fallback_providers: [] },
        altprofil: { worker_runtime: "hermes", provider: "neuralwatt", model: "glm-5.2-fast", fallback_providers: [] },
      },
    };

    const rows = editorRows(cloudMaxLane, catalog, MODELS).map((row) => ({ ...row, touched: true }));
    const coder = rows.find((row) => row.profile === "coder");

    expect(coder).toMatchObject({
      profile: "coder",
      worker_runtime: "claude-cli",
      provider: null,
      model: "claude-opus-4-8",
      choice: "claude-cli|claude-opus-4-8",
    });
    expect(laneEntryWarnings(coder!)).toEqual([]);
    expect(profilesFromEditorRows(rows).coder).toEqual({ worker_runtime: "claude-cli", model: "claude-opus-4-8" });
    expect(profilesFromEditorRows(rows).altprofil).toMatchObject({ worker_runtime: "hermes", provider: "neuralwatt", model: "glm-5.2-fast" });
  });

  // Bug 1 (live, 2026-07-06): the Fleet quick-switch produces provider-aware
  // 3-part choices ("runtime|provider|model"); the claude-cli branch of
  // profilesFromEditorRows used to parse them with the legacy 2-part
  // `entryFromChoice`, which sliced at the FIRST pipe and persisted
  // model: "|claude-haiku-4-5" (leading pipe) into the lane config.
  function claudeCliRow(choice: string, model: string | null): EditorRow {
    return {
      touched: true,
      profile: "admin",
      description: "",
      defaultLabel: "automatisch",
      defaultRuntime: "claude-cli",
      defaultProvider: null,
      defaultFallbackProviders: [],
      worker_runtime: "claude-cli",
      provider: null,
      model,
      fallbackProviders: [],
      locked: false,
      lockedReason: null,
      choice,
    };
  }

  it("profilesFromEditorRows parses provider-aware 3-part claude-cli choices without a stray leading pipe", () => {
    const row = claudeCliRow("claude-cli||claude-haiku-4-5", "claude-haiku-4-5");
    const profiles = profilesFromEditorRows([row]);
    expect(profiles.admin?.model).toBe("claude-haiku-4-5");
    expect(profiles.admin?.worker_runtime).toBe("claude-cli");
  });

  it("profilesFromEditorRows still parses legacy 2-part claude-cli choices", () => {
    const row = claudeCliRow("claude-cli|claude-opus-4-8", "claude-opus-4-8");
    const profiles = profilesFromEditorRows([row]);
    expect(profiles.admin?.model).toBe("claude-opus-4-8");
    expect(profiles.admin?.worker_runtime).toBe("claude-cli");
  });

  // Bug 2 (live, 2026-07-06): lane `api-standard` profile `admin` started as
  // runtime hermes + no model override. After switching to "Claude Haiku 4.5"
  // (runtime flips to claude-cli) and then reverting to "Standard" (choice ""),
  // applyChoice used to KEEP the flipped worker_runtime, which made the
  // spawn-check reject the profile's own default model forever.
  it("applyChoice reverts worker_runtime to the profile's catalog default when choice is empty", () => {
    const claudeHaiku: LaneModelOption = {
      id: "claude-haiku-4-5",
      label: "Claude Haiku 4.5",
      runtime: "claude-cli",
      group: "Claude (Max-Abo)",
      provider: null,
      locked: false,
      source: "claude-cli",
    };
    const switchedRow = claudeCliRow("claude-cli||claude-haiku-4-5", "claude-haiku-4-5");
    // simulate the row having been switched away from its catalog default (hermes)
    switchedRow.defaultRuntime = "hermes";
    switchedRow.worker_runtime = "claude-cli";

    const reverted = applyChoice(switchedRow, "", [claudeHaiku]);

    expect(reverted.worker_runtime).toBe("hermes");
    expect(reverted.model).toBeNull();
    expect(reverted.provider).toBeNull();
    expect(reverted.choice).toBe("");
  });
});
