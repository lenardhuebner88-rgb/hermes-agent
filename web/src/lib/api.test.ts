import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, fetchJSON } from "./api";

// Regression coverage for the loopback stale-token auto-reload (commit
// fe5c8ec4a). The bug: /api/auth/me answers 401 on every call in non-gated
// mode, which the reload logic mistook for a stale token and reloaded the page
// — on every mount — producing an infinite SPA reload-loop that also pinned the
// dashboard process. getAuthMe must now opt out of the reload so its 401
// bubbles up to AuthWidget instead.

type SessionStoreMock = Storage & { _map: Map<string, string> };

function makeSessionStorage(): SessionStoreMock {
  const map = new Map<string, string>();
  return {
    _map: map,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
    key: (i: number) => Array.from(map.keys())[i] ?? null,
    get length() {
      return map.size;
    },
  } as SessionStoreMock;
}

function mockResponse(status: number, opts: { jsonBody?: unknown; text?: string } = {}) {
  const ok = status >= 200 && status < 300;
  return {
    status,
    ok,
    clone() {
      return this;
    },
    async json() {
      if (!("jsonBody" in opts)) throw new Error("not json");
      return opts.jsonBody;
    },
    async text() {
      return opts.text ?? "";
    },
  } as unknown as Response;
}

let reload: ReturnType<typeof vi.fn>;
let session: SessionStoreMock;

beforeEach(() => {
  reload = vi.fn();
  session = makeSessionStorage();
  vi.stubGlobal("window", {
    __HERMES_AUTH_REQUIRED__: false,
    __HERMES_SESSION_TOKEN__: "tok-123",
    location: { reload, assign: vi.fn(), pathname: "/control/autoresearch", search: "" },
  });
  vi.stubGlobal("sessionStorage", session);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("fetchJSON loopback stale-token reload", () => {
  it("does NOT reload on the expected /api/auth/me 401 (no reload-loop)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse(401, { text: "unauthorized" })));

    await expect(api.getAuthMe()).rejects.toThrow(/^401:/);
    expect(reload).not.toHaveBeenCalled();
    // The guard must not have been engaged either — otherwise a later genuine
    // stale-token 401 would be wrongly suppressed.
    expect(session.getItem("hermes.tokenReloadAttempted")).toBeNull();
  });

  it("reloads exactly once for a genuine stale-token 401 on a normal endpoint", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse(401, { text: "stale" })));

    // The reload path returns a never-resolving promise, so don't await it.
    void fetchJSON("/api/status");
    await Promise.resolve();
    await Promise.resolve();

    expect(reload).toHaveBeenCalledTimes(1);
    expect(session.getItem("hermes.tokenReloadAttempted")).toBe("1");
  });

  it("does not reload a second time once the guard is set", async () => {
    session.setItem("hermes.tokenReloadAttempted", "1");
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse(401, { text: "still stale" })));

    await expect(fetchJSON("/api/status")).rejects.toThrow(/^401:/);
    expect(reload).not.toHaveBeenCalled();
  });

  it("clears the reload guard after a successful 2xx", async () => {
    session.setItem("hermes.tokenReloadAttempted", "1");
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse(200, { jsonBody: { ok: true } })));

    await expect(fetchJSON("/api/status")).resolves.toEqual({ ok: true });
    expect(session.getItem("hermes.tokenReloadAttempted")).toBeNull();
  });
});
