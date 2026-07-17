// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SystemHealthResponse } from "../lib/types";
import { de } from "../i18n/de";
import { OfflineStaleBanner, REFOCUS_GRACE_S } from "./OfflineStaleBanner";
import { ATTEMPT_DEADLINE_MS, subscribe, _resetPollingStore } from "../hooks/pollingStore";

const clock = vi.hoisted(() => ({ now: 100, visibleSince: 0 as number | null }));

vi.mock("../lib/clock", () => ({
  useClientNowSeconds: () => clock.now,
  useVisibleSinceSeconds: () => clock.visibleSince,
}));

const baseHealth: SystemHealthResponse = {
  schema: "hermes-health-v1",
  checked_at: 100,
  overall: "healthy",
  subsystems: {
    gateway: { status: "healthy", detail: "ok", error: null, latency_ms: 12 },
    autoresearch: { status: "healthy", detail: "fresh", error: null, heartbeat_age_s: 2 },
    kanban_db: { status: "healthy", detail: "ok", error: null, latency_ms: 4 },
    kanban_dispatcher: { status: "healthy", detail: "ok", error: null, heartbeat_age_s: 5 },
  },
};

/** Real health poll interval (15s → freshness threshold 45s). */
const POLL_MS = 15_000;

/** lastUpdated far enough in the past that freshness().stale is true at clientNow=100. */
const AGE_STALE_LAST_UPDATED = 0;

function ageStaleHealth(overrides: Partial<{
  error: string | null;
  isStale: boolean;
  pollIntervalMs: number;
  lastUpdated: number | null;
}> = {}) {
  return {
    data: baseHealth,
    error: null as string | null,
    isStale: false,
    pollIntervalMs: POLL_MS,
    lastUpdated: AGE_STALE_LAST_UPDATED as number | null,
    ...overrides,
  };
}

beforeEach(() => {
  clock.now = 100;
  clock.visibleSince = 0;
  _resetPollingStore();
  vi.useFakeTimers();
  vi.setSystemTime(new Date(100_000)); // epoch-ms aligned with clock.now=100s
});

afterEach(() => {
  cleanup();
  _resetPollingStore();
  vi.useRealTimers();
});

describe("OfflineStaleBanner refocus grace", () => {
  it("does not render age-stale when visible for less than the refocus grace", () => {
    // visible for 5s < REFOCUS_GRACE_S
    clock.visibleSince = clock.now - 5;
    const { container } = render(<OfflineStaleBanner health={ageStaleHealth()} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText(de.staleBanner.pausedOrStale)).toBeNull();
  });

  it("renders age-stale with pausedOrStale once visible for at least the refocus grace", () => {
    clock.visibleSince = clock.now - REFOCUS_GRACE_S;
    render(<OfflineStaleBanner health={ageStaleHealth()} />);
    expect(screen.getByText(de.staleBanner.pausedOrStale)).toBeTruthy();
  });

  it("renders fetch-error immediately even within the refocus grace", () => {
    clock.visibleSince = clock.now - 1; // well inside grace
    render(
      <OfflineStaleBanner
        health={ageStaleHealth({ error: "network down", lastUpdated: 99 })}
      />,
    );
    expect(screen.getByText(de.staleBanner.fetchError)).toBeTruthy();
    expect(screen.queryByText(de.staleBanner.pausedOrStale)).toBeNull();
  });

  it("renders explicit isStale immediately even within the refocus grace", () => {
    // Realistic epoch so freshness() treats lastUpdated as valid/fresh (not age-stale).
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - 1;
    vi.setSystemTime(new Date(clock.now * 1000));
    render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          isStale: true,
          lastUpdated: clock.now - 2,
          error: null,
        })}
      />,
    );
    expect(screen.getByText(de.staleBanner.stale)).toBeTruthy();
  });

  it("keeps the 65s-hidden / 12s-grace path green (no in-flight attempt)", () => {
    // Simulated mobile return: last success 65s ago, visible for exactly the grace.
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - REFOCUS_GRACE_S;
    vi.setSystemTime(new Date(clock.now * 1000));
    render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          lastUpdated: clock.now - 65,
          pollIntervalMs: POLL_MS,
        })}
      />,
    );
    expect(screen.getByText(de.staleBanner.pausedOrStale)).toBeTruthy();
  });
});

describe("OfflineStaleBanner legal pending refresh", () => {
  it("suppresses age-stale while a legal in-flight health refresh is within the deadline", async () => {
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - REFOCUS_GRACE_S; // grace elapsed
    vi.setSystemTime(new Date(clock.now * 1000));

    // Hang the health-status poll so getAttemptState reports refreshing.
    subscribe(
      "health-status",
      () => new Promise(() => { /* pending */ }),
      POLL_MS,
      vi.fn(),
    );
    await vi.advanceTimersByTimeAsync(0);

    const { container } = render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          lastUpdated: clock.now - 65,
          pollIntervalMs: POLL_MS,
        })}
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText(de.staleBanner.pausedOrStale)).toBeNull();
  });

  it("shows age-stale when grace elapsed and no in-flight attempt", () => {
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - REFOCUS_GRACE_S;
    vi.setSystemTime(new Date(clock.now * 1000));
    // No health-status subscription → no legal refresh.
    render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          lastUpdated: clock.now - 65,
          pollIntervalMs: POLL_MS,
        })}
      />,
    );
    expect(screen.getByText(de.staleBanner.pausedOrStale)).toBeTruthy();
  });

  it("shows fetch-error immediately even during a legal in-flight refresh", async () => {
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - 1;
    vi.setSystemTime(new Date(clock.now * 1000));

    subscribe(
      "health-status",
      () => new Promise(() => { /* pending */ }),
      POLL_MS,
      vi.fn(),
    );
    await vi.advanceTimersByTimeAsync(0);

    render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          error: "network down",
          lastUpdated: clock.now - 2,
        })}
      />,
    );
    expect(screen.getByText(de.staleBanner.fetchError)).toBeTruthy();
  });

  it("shows age-stale again after the attempt deadline with no success", async () => {
    clock.now = 1_700_000_100;
    clock.visibleSince = clock.now - REFOCUS_GRACE_S;
    vi.setSystemTime(new Date(clock.now * 1000));

    subscribe(
      "health-status",
      () => new Promise(() => { /* pending forever */ }),
      POLL_MS,
      vi.fn(),
    );
    await vi.advanceTimersByTimeAsync(0);

    // Still within deadline → suppressed.
    const first = render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          lastUpdated: clock.now - 65,
          pollIntervalMs: POLL_MS,
        })}
      />,
    );
    expect(first.container.firstChild).toBeNull();
    first.unmount();

    // Advance wall clock past ATTEMPT_DEADLINE_MS; banner clock follows.
    const deadlineS = Math.ceil(ATTEMPT_DEADLINE_MS / 1000) + 1;
    clock.now += deadlineS;
    vi.setSystemTime(new Date(clock.now * 1000));
    // Attempt still "in flight" in the store, but past deadline → not legal.
    render(
      <OfflineStaleBanner
        health={ageStaleHealth({
          lastUpdated: clock.now - 65 - deadlineS,
          pollIntervalMs: POLL_MS,
        })}
      />,
    );
    expect(screen.getByText(de.staleBanner.pausedOrStale)).toBeTruthy();
  });
});
