// @vitest-environment jsdom
/**
 * usePaFeed — S6.4a: KI-LAGE-Panel live (GET /api/pa/feed).
 * Der Hook ist ein dünner pollingStore-Wrapper; getestet wird, dass er den
 * Endpoint aufruft und die Feed-Page durchreicht.
 */
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { _resetPollingStore } from "../hooks/pollingStore";

const getPaFeedMock = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api", () => ({
  api: { getPaFeed: getPaFeedMock },
}));

import { PA_FEED_KEY, usePaFeed } from "./usePaFeed";

const FEED_PAGE = {
  items: [
    { id: 1, ts: 1752000000, kind: "news", severity: "info", title: "OpenAI senkt Preise", ref: null, delivered_push: 0 },
    { id: 2, ts: 1752000060, kind: "news", severity: "info", title: "DeepSeek R4 offen", ref: null, delivered_push: 0 },
  ],
  next_since_id: 2,
  has_more: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  _resetPollingStore();
  getPaFeedMock.mockResolvedValue(FEED_PAGE);
});

afterEach(() => {
  cleanup();
  _resetPollingStore();
  vi.restoreAllMocks();
});

describe("usePaFeed", () => {
  it("ruft GET /api/pa/feed und liefert die Feed-Page", async () => {
    const { result } = renderHook(() => usePaFeed());
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(true);

    await waitFor(() => expect(result.current.data).toEqual(FEED_PAGE));
    expect(getPaFeedMock).toHaveBeenCalledTimes(1);
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("Polling-Key ist pa/feed", () => {
    expect(PA_FEED_KEY).toBe("pa/feed");
  });

  it("Fetch-Fehler landet als error, Daten bleiben null", async () => {
    getPaFeedMock.mockRejectedValue(new Error("netz"));
    const { result } = renderHook(() => usePaFeed());

    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.data).toBeNull();
  });
});
