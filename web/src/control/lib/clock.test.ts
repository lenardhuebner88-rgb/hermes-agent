// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetModules();
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => "visible",
  });
});

describe("client clock", () => {
  it("refreshes immediately on refocus after hidden-tab timer throttling", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-13T12:00:00Z"));
    const clock = await import("./clock");
    const unsubscribe = clock.subscribeClock(() => undefined);
    const before = clock.getClockNowSeconds();

    vi.setSystemTime(new Date("2026-07-13T12:05:00Z"));
    document.dispatchEvent(new Event("visibilitychange"));

    expect(clock.getClockNowSeconds() - before).toBe(300);
    unsubscribe();
  });

  it("useVisibleSinceSeconds tracks last visible transition", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-13T12:00:00Z"));
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    });
    const clock = await import("./clock");
    const { result } = renderHook(() => clock.useVisibleSinceSeconds());
    const mountedAt = Math.floor(new Date("2026-07-13T12:00:00Z").getTime() / 1000);
    expect(result.current).toBe(mountedAt);

    // Become hidden, then visible again later — stamp resets to refocus time.
    let state: DocumentVisibilityState = "hidden";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => state,
    });
    vi.setSystemTime(new Date("2026-07-13T12:01:00Z"));
    state = "visible";
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(result.current).toBe(Math.floor(new Date("2026-07-13T12:01:00Z").getTime() / 1000));
  });
});
