import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, buildWsAuthParam, downloadAuthedArtifact, fetchJSON, openAuthedApiFile } from "./api";

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

describe("authenticated file opening", () => {
  it("opens a placeholder tab, fetches API deliverables with the session-token header, then navigates to a blob URL", async () => {
    const blob = new Blob(["# receipt"], { type: "text/markdown" });
    const opened = {
      opener: {} as unknown,
      location: { href: "about:blank" },
      document: { write: vi.fn() },
      close: vi.fn(),
    };
    const open = vi.fn(() => opened);
    const createObjectURL = vi.fn(() => "blob:receipt");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => ({
      status: 200,
      ok: true,
      async blob() {
        return blob;
      },
      async text() {
        return "";
      },
    } as unknown as Response)));
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    (window as unknown as { open: typeof open }).open = open;

    await openAuthedApiFile("/api/plugins/kanban/tasks/t_408/deliverables/RESULT.md");

    expect(fetch).toHaveBeenCalledWith(
      "/api/plugins/kanban/tasks/t_408/deliverables/RESULT.md",
      expect.objectContaining({
        credentials: "include",
        headers: expect.any(Headers),
      }),
    );
    const headers = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1].headers as Headers;
    expect(headers.get("X-Hermes-Session-Token")).toBe("tok-123");
    expect(createObjectURL).toHaveBeenCalledWith(blob);
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(opened.opener).toBeNull();
    expect(opened.document.write).toHaveBeenCalledWith("<p>Hermes-Deliverable wird geladen…</p>");
    expect(opened.location.href).toBe("blob:receipt");
    expect(opened.close).not.toHaveBeenCalled();
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });
});

describe("downloadAuthedArtifact", () => {
  function stubDocument() {
    const anchor = {
      href: "",
      download: "",
      rel: "",
      click: vi.fn(),
      remove: vi.fn(),
    };
    const createElement = vi.fn(() => anchor);
    const appendChild = vi.fn();
    vi.stubGlobal("document", { createElement, body: { appendChild } });
    return { anchor, createElement, appendChild };
  }

  it("hands the download manager a query-token URL with the real filename (no blob, no tab)", () => {
    const open = vi.fn();
    (window as unknown as { open: typeof open }).open = open;
    const { anchor, createElement } = stubDocument();

    downloadAuthedArtifact(
      "/api/artifacts/hermes-dictate-1.4-6a9ec48d3.apk",
      "hermes-dictate-1.4-6a9ec48d3.apk",
    );

    expect(createElement).toHaveBeenCalledWith("a");
    // Real filename via the download attribute — mobile Chrome's blob path
    // otherwise names the file after the blob UUID.
    expect(anchor.download).toBe("hermes-dictate-1.4-6a9ec48d3.apk");
    expect(anchor.href).toBe(
      "/api/artifacts/hermes-dictate-1.4-6a9ec48d3.apk?token=tok-123",
    );
    expect(anchor.click).toHaveBeenCalledTimes(1);
    expect(anchor.remove).toHaveBeenCalledTimes(1);
    // Never opens an about:blank tab (the old blob path did — it hung on mobile).
    expect(open).not.toHaveBeenCalled();
  });

  it("omits the token param when no session token is present (gated mode → cookie auth on the request)", () => {
    (window as unknown as { __HERMES_SESSION_TOKEN__: string | undefined }).__HERMES_SESSION_TOKEN__ =
      undefined;
    const { anchor } = stubDocument();

    downloadAuthedArtifact("/api/artifacts/x.apk", "x.apk");

    expect(anchor.href).toBe("/api/artifacts/x.apk");
  });
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

describe("fetchJSON GET timeout", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("aborts a hung GET after 20s with a network-classified timeout error", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const pending = fetchJSON("/api/slow");
    const assertion = expect(pending).rejects.toThrow(/network timeout after 20000ms/);
    await vi.advanceTimersByTimeAsync(20_000);
    await assertion;
  });

  it("applies NO default timeout to mutations (long-running POST actions)", async () => {
    vi.useFakeTimers();
    let resolveFetch: (r: Response) => void = () => {};
    const fetchMock = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const pending = fetchJSON("/api/action", { method: "POST" });
    await vi.advanceTimersByTimeAsync(120_000);
    resolveFetch(mockResponse(200, { jsonBody: { ok: true } }));
    await expect(pending).resolves.toEqual({ ok: true });
  });

  it("honours an explicit timeoutMs override", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn(
      (_url: string, init?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new DOMException("Aborted", "AbortError")),
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const pending = fetchJSON("/api/slow", undefined, { timeoutMs: 1_000 });
    const assertion = expect(pending).rejects.toThrow(/network timeout after 1000ms/);
    await vi.advanceTimersByTimeAsync(1_000);
    await assertion;
  });
});


describe("buildWsAuthParam", () => {
  // Regression coverage for the SW-poisoning incident 2026-07-03: a stale
  // service worker (vite-plugin-pwa's Workbox precache) answered navigations
  // with the static build `index.html` instead of letting the server render
  // it, which strips both `__HERMES_AUTH_REQUIRED__` and
  // `__HERMES_SESSION_TOKEN__`. Before the fix, the loopback branch sent
  // `["token", ""]` unconditionally, which the server's
  // `_ws_auth_reason` rejects as `no_credential` on every WebSocket connect
  // (Terminal-Attach, Kanban-Live-Events) while REST kept working via the
  // cookie — a confusing partial outage.

  it("gated mode mints a fresh ticket", async () => {
    vi.stubGlobal("window", {
      __HERMES_AUTH_REQUIRED__: true,
      __HERMES_SESSION_TOKEN__: undefined,
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => mockResponse(200, { jsonBody: { ticket: "tix-1", ttl_seconds: 30 } })),
    );

    await expect(buildWsAuthParam()).resolves.toEqual(["ticket", "tix-1"]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/ws-ticket",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });

  it("loopback mode with an injected token returns it unchanged", async () => {
    vi.stubGlobal("window", {
      __HERMES_AUTH_REQUIRED__: false,
      __HERMES_SESSION_TOKEN__: "tok-123",
    });
    vi.stubGlobal("fetch", vi.fn());

    await expect(buildWsAuthParam()).resolves.toEqual(["token", "tok-123"]);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("regression: both auth flags missing (SW-poisoned client) falls back to a minted ticket instead of an empty token", async () => {
    vi.stubGlobal("window", {});
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => mockResponse(200, { jsonBody: { ticket: "tix-2", ttl_seconds: 30 } })),
    );

    await expect(buildWsAuthParam()).resolves.toEqual(["ticket", "tix-2"]);
    expect(fetch).toHaveBeenCalledWith(
      "/api/auth/ws-ticket",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });
});

describe("agent terminal worker contract", () => {
  it("posts JSON bodies with an application/json content type", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse(200, { jsonBody: { window: { session: "work", window: "codex" } } })));

    await api.ensureAgentTerminalWindow("codex");

    expect(fetch).toHaveBeenCalledWith(
      "/api/agent-terminals/ensure",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ kind: "codex" }),
        headers: expect.any(Headers),
      }),
    );
    const headers = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0][1].headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
  });
});
