import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  activateLane,
  deleteLane,
  profilesFromRows,
  rowsFromLane,
  type Lane,
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
});

describe("draft helpers", () => {
  const lane: Lane = {
    id: "lane_1",
    name: "max-abo",
    active: true,
    builtin: true,
    created_at: 0,
    updated_at: 0,
    profiles: {
      premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      coder: { worker_runtime: "claude-cli", model: null },
    },
  };

  it("rowsFromLane yields sorted editable rows", () => {
    const rows = rowsFromLane(lane);
    expect(rows.map((r) => r.profile)).toEqual(["coder", "premium"]);
    expect(rows[1]).toEqual({
      profile: "premium",
      runtime: "claude-cli",
      model: "claude-fable-5",
    });
    expect(rows[0].model).toBe("");
  });

  it("profilesFromRows drops blank rows and nulls blank fields", () => {
    const out = profilesFromRows([
      { profile: " premium ", runtime: "claude-cli", model: " claude-fable-5 " },
      { profile: "coder", runtime: "", model: "" },
      { profile: "   ", runtime: "hermes", model: "gpt-5.5" },
    ]);
    expect(out).toEqual({
      premium: { worker_runtime: "claude-cli", model: "claude-fable-5" },
      coder: { worker_runtime: null, model: null },
    });
  });
});
