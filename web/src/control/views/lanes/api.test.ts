import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  activateLane,
  choiceFromEntry,
  choiceOverrideLabel,
  deleteLane,
  editorRows,
  entryFromChoice,
  laneChoiceWarning,
  laneProfileSpawnHealth,
  modelLabel,
  profilesFromEditorRows,
  smokeCheckLaneConfig,
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
      model: "gpt-5.5",
    });
  });
});

const MODELS: LaneModelOption[] = [
  { id: "claude-fable-5", label: "Claude Fable 5", runtime: "claude-cli", group: "Claude (Max-Abo)" },
  { id: "gpt-5.5", label: "GPT-5.5", runtime: "hermes", group: "API-Modelle" },
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

  it("warns when a persisted choice pairs a model with the wrong runtime", () => {
    expect(laneChoiceWarning("hermes|claude-fable-5", MODELS)).toContain("claude-cli");
    expect(laneChoiceWarning("claude-cli|gpt-5.5", MODELS)).toContain("hermes");
    expect(laneChoiceWarning("hermes|gpt-5.5", MODELS)).toBeNull();
    expect(laneChoiceWarning("", MODELS)).toBeNull();
  });
});

describe("readiness & override state", () => {
  const catalog: LaneCatalogProfile[] = [
    {
      name: "coder",
      worker_runtime: "hermes",
      default_model: "gpt-5.5",
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
    const rows = editorRows(lane, catalog, MODELS);
    expect(profilesFromEditorRows(rows)).toEqual({
      premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      altprofil: { worker_runtime: "hermes", model: "gpt-5.5" },
    });
  });
});
