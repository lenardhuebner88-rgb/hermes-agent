// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(() => {
  vi.useRealTimers();
  vi.resetModules();
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
});
