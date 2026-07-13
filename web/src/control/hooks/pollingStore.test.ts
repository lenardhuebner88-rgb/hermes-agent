import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { subscribe, refresh, parseStructuredError, setIntervalScale, _resetPollingStore } from "./pollingStore";

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

  it.each([
    ["HTTP 500", new Error("500: injected failure"), "500"],
    ["malformed JSON", new SyntaxError("Unexpected token < in JSON"), "contract"],
    ["schema-empty object", new Error("contract/workers: bad shape {}"), "contract"],
    ["30-second hang timeout", new Error("network timeout after 30000ms"), "network"],
    ["auth expiry", new Error('401: {"error":"session_expired"}'), "401"],
    ["WS drop", new Error("WebSocket closed before recovery"), "contract"],
  ])("retains last-good, discloses %s, and clears the warning after recovery", async (_name, failure, code) => {
    const loader = vi.fn().mockResolvedValueOnce({ id: "last-good" });
    const cb = vi.fn();
    subscribe(`fault-${code}-${String(failure)}`, loader, 1000, cb);
    await vi.advanceTimersByTimeAsync(0);

    loader.mockRejectedValueOnce(failure);
    await refresh(`fault-${code}-${String(failure)}`);
    expect(cb).toHaveBeenLastCalledWith(expect.objectContaining({
      data: { id: "last-good" },
      isStale: true,
      errorObj: expect.objectContaining({ code }),
    }));

    loader.mockResolvedValueOnce({ id: "recovered" });
    await refresh(`fault-${code}-${String(failure)}`);
    expect(cb).toHaveBeenLastCalledWith(expect.objectContaining({
      data: { id: "recovered" },
      isStale: false,
      error: null,
      errorObj: null,
    }));
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

  it("skips the fetch while the document is hidden and resumes on visible", async () => {
    let onVisibility: () => void = () => undefined;
    let hidden = true;
    const doc = {
      get hidden() {
        return hidden;
      },
      set hidden(v: boolean) {
        hidden = v;
      },
      addEventListener: (type: string, fn: () => void) => {
        if (type === "visibilitychange") onVisibility = fn;
      },
    };
    (globalThis as { document?: unknown }).document = doc;

    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 1000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).not.toHaveBeenCalled(); // hidden → no fetch

    doc.hidden = false;
    onVisibility();
    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).toHaveBeenCalledTimes(2); // immediate refresh + next tick
  });

  it("does not fetch for an extended period while the document stays hidden", async () => {
    (globalThis as { document?: unknown }).document = {
      hidden: true,
      addEventListener: () => {},
    };
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("long-hidden", loader, 1000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    // Even after multiple background reschedule cycles no fetch happens.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(loader).not.toHaveBeenCalled();
  });

  it("refresh() forces an immediate tick", async () => {
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 100000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);
    await refresh("k");
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("replaces a short foreground timer with the slower background cadence when hidden", async () => {
    let onVisibility: () => void = () => undefined;
    let hidden = false;
    const doc = {
      get hidden() {
        return hidden;
      },
      set hidden(v: boolean) {
        hidden = v;
      },
      addEventListener: (type: string, fn: () => void) => {
        if (type === "visibilitychange") onVisibility = fn;
      },
    };
    (globalThis as { document?: unknown }).document = doc;

    const setTimeoutSpy = vi.spyOn(globalThis, "setTimeout");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("recalibrate", loader, 1_000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    expect(loader).toHaveBeenCalledTimes(1);

    setTimeoutSpy.mockClear();
    clearTimeoutSpy.mockClear();
    doc.hidden = true;
    onVisibility();

    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);
    expect(setTimeoutSpy).toHaveBeenCalled();
    expect(setTimeoutSpy.mock.calls.at(-1)?.[1]).toBe(6_000);

    await vi.advanceTimersByTimeAsync(5_999);
    expect(loader).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(loader).toHaveBeenCalledTimes(1);
  });

  it("throttles background polling to a bounded interval", async () => {
    let onVisibility: () => void = () => undefined;
    let hidden = false;
    const doc = {
      get hidden() {
        return hidden;
      },
      set hidden(v: boolean) {
        hidden = v;
      },
      addEventListener: (type: string, fn: () => void) => {
        if (type === "visibilitychange") onVisibility = fn;
      },
    };
    (globalThis as { document?: unknown }).document = doc;

    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 1_000, vi.fn());
    await vi.advanceTimersByTimeAsync(0); // initial visible tick
    expect(loader).toHaveBeenCalledTimes(1);

    // background cadence: timer is throttled to 6s but does NOT fetch while hidden.
    doc.hidden = true;
    onVisibility(); // entering background recalibrates the timer

    for (let i = 0; i < 5; i += 1) {
      await vi.advanceTimersByTimeAsync(1_000);
      expect(loader).toHaveBeenCalledTimes(1);
    }

    // at the 6th second the slow heartbeat fires, sees hidden, reschedules again
    await vi.advanceTimersByTimeAsync(1_000);
    expect(loader).toHaveBeenCalledTimes(1);

    doc.hidden = false;
    onVisibility();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(loader).toHaveBeenCalledTimes(3); // immediate refresh + next normal tick
  });

  it("setIntervalScale stretches the cadence and restores it on reset", async () => {
    const loader = vi.fn().mockResolvedValue(1);
    subscribe("k", loader, 1000, vi.fn());
    await vi.advanceTimersByTimeAsync(0); // first tick
    expect(loader).toHaveBeenCalledTimes(1);

    setIntervalScale("k", 5);
    await vi.advanceTimersByTimeAsync(1000); // next tick still at 1× (scale applies after it)
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(4000); // 5×: nothing at 1s..4s
    expect(loader).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1000); // 5000ms reached
    expect(loader).toHaveBeenCalledTimes(3);

    setIntervalScale("k", 1); // reset reschedules at normal cadence immediately
    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).toHaveBeenCalledTimes(4);
  });

  it("does not notify listeners when a poll returns an identical JSON payload", async () => {
    const loader = vi.fn().mockResolvedValue({ count: 1, items: ["a"] });
    const cb = vi.fn();
    subscribe("k", loader, 1000, cb);
    await vi.advanceTimersByTimeAsync(0);
    expect(cb).toHaveBeenCalledTimes(2); // initial loading snapshot + first data snapshot

    await vi.advanceTimersByTimeAsync(1000);
    expect(loader).toHaveBeenCalledTimes(2);
    expect(cb).toHaveBeenCalledTimes(2);
  });
});

describe("foreground refresh stagger", () => {
  it("staggers per-key refreshes on visibilitychange instead of firing all at once", async () => {
    let onVisibility: (() => void) | null = null;
    (globalThis as { document?: unknown }).document = {
      hidden: false,
      addEventListener: (type: string, fn: () => void) => {
        if (type === "visibilitychange") onVisibility = fn;
      },
    };
    const loaders = [
      vi.fn().mockResolvedValue(1),
      vi.fn().mockResolvedValue(2),
      vi.fn().mockResolvedValue(3),
    ];
    subscribe("k0", loaders[0], 60_000, vi.fn());
    subscribe("k1", loaders[1], 60_000, vi.fn());
    subscribe("k2", loaders[2], 60_000, vi.fn());
    await vi.advanceTimersByTimeAsync(0); // initial ticks
    loaders.forEach((l) => l.mockClear());
    expect(onVisibility).not.toBeNull();

    onVisibility!();
    await vi.advanceTimersByTimeAsync(0);
    expect(loaders[0]).toHaveBeenCalledTimes(1); // first key fires immediately
    expect(loaders[1]).not.toHaveBeenCalled(); // the rest are staggered
    expect(loaders[2]).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(150);
    expect(loaders[1]).toHaveBeenCalledTimes(1);
    expect(loaders[2]).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(150);
    expect(loaders[2]).toHaveBeenCalledTimes(1);
  });

  it("clears pending background timers before delayed foreground refreshes", async () => {
    let onVisibility: (() => void) | null = null;
    let hidden = true;
    const doc = {
      get hidden() {
        return hidden;
      },
      addEventListener: (type: string, fn: () => void) => {
        if (type === "visibilitychange") onVisibility = fn;
      },
    };
    (globalThis as { document?: unknown }).document = doc;

    const loaders = [
      vi.fn().mockResolvedValue(1),
      vi.fn().mockResolvedValue(2),
    ];
    subscribe("k0", loaders[0], 1_000, vi.fn());
    subscribe("k1", loaders[1], 1_000, vi.fn());
    await vi.advanceTimersByTimeAsync(0);
    loaders.forEach((loader) => loader.mockClear());

    // Hidden heartbeat for both keys is now scheduled at 6s. Returning to the
    // foreground after 5.9s must cancel those heartbeats, otherwise k1 would
    // fetch once at 6.0s and again at its 150ms stagger.
    await vi.advanceTimersByTimeAsync(5_900);
    hidden = false;
    onVisibility!();

    await vi.advanceTimersByTimeAsync(100);
    expect(loaders[0]).toHaveBeenCalledTimes(1);
    expect(loaders[1]).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(50);
    expect(loaders[1]).toHaveBeenCalledTimes(1);
  });
});
