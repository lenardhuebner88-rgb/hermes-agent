// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import { boardLoader } from "./workersBoard";

function boardPayload(now: number) {
  return {
    columns: [],
    tenants: [],
    assignees: [],
    latest_event_id: now,
    source_errors: [],
    now,
  };
}

function jsonResponse(data: unknown, etag: string): Response {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json", ETag: etag },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("boardLoader ETag revalidation", () => {
  it("stores the first ETag, revalidates, and returns the same parsed object on 304", async () => {
    const payload = boardPayload(1);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(payload, '"board-v1"'))
      .mockResolvedValueOnce(new Response(null, { status: 304, headers: { ETag: '"board-v1"' } }));
    vi.stubGlobal("fetch", fetchMock);

    const first = await boardLoader("revalidation");
    const second = await boardLoader("revalidation");

    expect(first).toEqual(payload);
    expect(second).toBe(first);
    expect(String(fetchMock.mock.calls[0][0])).toContain("done_limit=30");
    const secondHeaders = fetchMock.mock.calls[1][1].headers as Headers;
    expect(secondHeaders.get("If-None-Match")).toBe('"board-v1"');
  });

  it("replaces cached data and ETag after a changed 200 response", async () => {
    const firstPayload = boardPayload(10);
    const changedPayload = boardPayload(11);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(firstPayload, '"replace-v1"'))
      .mockResolvedValueOnce(jsonResponse(changedPayload, '"replace-v2"'))
      .mockResolvedValueOnce(new Response(null, { status: 304 }));
    vi.stubGlobal("fetch", fetchMock);

    const first = await boardLoader("replacement");
    const changed = await boardLoader("replacement");
    const unchanged = await boardLoader("replacement");

    expect(changed).not.toBe(first);
    expect(changed).toEqual(changedPayload);
    expect(unchanged).toBe(changed);
    const secondHeaders = fetchMock.mock.calls[1][1].headers as Headers;
    const thirdHeaders = fetchMock.mock.calls[2][1].headers as Headers;
    expect(secondHeaders.get("If-None-Match")).toBe('"replace-v1"');
    expect(thirdHeaders.get("If-None-Match")).toBe('"replace-v2"');
  });

  it("keeps named-board and default-board cache entries isolated", async () => {
    const defaultPayload = boardPayload(20);
    const namedPayload = boardPayload(21);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(defaultPayload, '"default-v1"'))
      .mockResolvedValueOnce(jsonResponse(namedPayload, '"a-v1"'))
      .mockResolvedValueOnce(new Response(null, { status: 304 }))
      .mockResolvedValueOnce(new Response(null, { status: 304 }));
    vi.stubGlobal("fetch", fetchMock);

    const defaultFirst = await boardLoader();
    const namedFirst = await boardLoader("a");
    const defaultSecond = await boardLoader();
    const namedSecond = await boardLoader("a");

    expect(defaultSecond).toBe(defaultFirst);
    expect(namedSecond).toBe(namedFirst);
    expect((fetchMock.mock.calls[2][1].headers as Headers).get("If-None-Match")).toBe('"default-v1"');
    expect((fetchMock.mock.calls[3][1].headers as Headers).get("If-None-Match")).toBe('"a-v1"');
  });
});
