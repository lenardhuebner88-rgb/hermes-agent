import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribe, refresh, parseStructuredError, _resetPollingStore } from "./pollingStore";

beforeEach(() => {
  _resetPollingStore();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  _resetPollingStore();
  delete (globalThis as { document?: unknown }).document;
});

describe("parseStructuredError", () => {
  it("extracts the HTTP status code from a fetchJSON error", () => {
    expect(parseStructuredError(new Error("500: boom")).code).toBe("500");
    expect(parseStructuredError(new Error("404: nope")).code).toBe("404");
  });
  it("classifies non-HTTP errors as network or contract", () => {
    expect(parseStructuredError(new Error("Failed to fetch")).code).toBe("network");
    expect(parseStructuredError(new Error("contract/foo: bad shape")).code).toBe("contract");
  });
});

describe("pollingStore", () => {
  it("dedupes: two subscribers on one key share a single loader call per tick", async () => {
    const loader = vi.fn().mockResolvedValue(1);
    const a = vi.fn();
    const b = vi.fn();
    subscribe("k", loader, 1000, a);
    subscribe("k", loader, 1000, b);
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);
    // both listeners received the resolved data
    expect(a).toHaveBeenLastCalledWith(expect.objectContaining({ data: 1, loading: false }));
    expect(b).toHaveBeenLastCalledWith(expect.objectContaining({ data: 1 }));
  });

  it("backs off on 5xx and keeps the last good value (stale-while-error)", async () => {
    const loader = vi
      .fn()
      .mockResolvedValueOnce(1)
      .mockRejectedValue(new Error("500: down"));
    const cb = vi.fn();
    subscribe("k", loader, 1000, cb);
    await vi.advanceTimersByTimeAsync(0); // first tick → data=1
    expect(cb).toHaveBeenLastCalledWith(expect.objectContaining({ data: 1, isStale: false }));

    await vi.advanceTimersByTimeAsync(1000); // next tick → 500
    const afterError = cb.mock.calls.at(-1)![0];
    expect(afterError.data).toBe(1); // stale value retained
    expect(afterError.isStale).toBe(true);
    expect(afterError.errorObj.code).toBe("500");

    // Backoff: the NEXT tick should be scheduled at 2000ms, not 1000ms.
    loader.mockClear();
    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).not.toHaveBeenCalled(); // still backing off
    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("resets backoff after a success", async () => {
    const loader = vi
      .fn()
      .mockRejectedValueOnce(new Error("500: x"))
      .mockResolvedValue(7);
    const cb = vi.fn();
    subscribe("k", loader, 1000, cb);
    await vi.advanceTimersByTimeAsync(0); // fail → backoff 2000
    await vi.advanceTimersByTimeAsync(2000); // success → resets to 1000
    expect(cb).toHaveBeenLastCalledWith(expect.objectContaining({ data: 7, isStale: false, error: null }));
    loader.mockClear();
    await vi.advanceTimersByTimeAsync(1000); // normal cadence restored
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("stops the timer when the last subscriber unsubscribes (no leak)", async () => {
    const loader = vi.fn().mockResolvedValue(1);
    const un = subscribe("k", loader, 1000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);
    un();
    loader.mockClear();
    await vi.advanceTimersByTimeAsync(5000);
    expect(loader).not.toHaveBeenCalled();
  });

  it("skips the fetch while the document is hidden", async () => {
    (globalThis as { document?: unknown }).document = { hidden: true, addEventListener: () => {} };
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 1000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).not.toHaveBeenCalled(); // hidden → no fetch
    (globalThis as { document: { hidden: boolean } }).document.hidden = false;
    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).toHaveBeenCalledTimes(1); // resumes when visible
  });

  it("refresh() forces an immediate tick", async () => {
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 100000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);
    await refresh("k");
    expect(loader).toHaveBeenCalledTimes(2);
  });
});
