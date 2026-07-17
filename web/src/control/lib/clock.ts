import { useEffect, useState, useSyncExternalStore } from "react";

// Sekunden-Uhr als externer Store: `Date.now()` direkt im Render verletzt die
// react-hooks-Purity-Regel (Komponenten müssen idempotent rendern). Der Store
// tickt alle 10s und weckt nur Komponenten, die ihn abonniert haben — "vor X"-
// Labels bleiben damit auch auf einem idle Board lebendig, ohne dass jede
// Komponente ihren eigenen Interval-Tick verwaltet.
const CLOCK_TICK_MS = 10_000;
let clockNowSeconds = Math.floor(Date.now() / 1000);
const clockListeners = new Set<() => void>();
let clockTimer: number | null = null;

function refreshClock(): void {
  clockNowSeconds = Math.floor(Date.now() / 1000);
  for (const listener of clockListeners) listener();
}

export function subscribeClock(listener: () => void): () => void {
  clockListeners.add(listener);
  if (clockTimer == null) {
    clockNowSeconds = Math.floor(Date.now() / 1000);
    clockTimer = window.setInterval(refreshClock, CLOCK_TICK_MS);
    // Hidden tabs may throttle the interval for minutes. Refresh synchronously
    // when the document returns so the first visible frame cannot show a
    // frozen heartbeat or worker age as current.
    document.addEventListener("visibilitychange", refreshClock);
  }
  return () => {
    clockListeners.delete(listener);
    if (clockListeners.size === 0 && clockTimer != null) {
      window.clearInterval(clockTimer);
      clockTimer = null;
      document.removeEventListener("visibilitychange", refreshClock);
    }
  };
}

export function getClockNowSeconds(): number {
  return clockNowSeconds;
}

/** Aktuelle Client-Zeit in Epoch-Sekunden, render-pure (10s-Auflösung). */
export function useClientNowSeconds(): number {
  return useSyncExternalStore(subscribeClock, getClockNowSeconds);
}

/**
 * Epoch seconds when the document last became visible.
 * null when hidden / SSR / no document. Used by OfflineStaleBanner for a short
 * post-refocus grace so mobile app-switch + timer thaw does not flash age-stale.
 */
function initialVisibleSinceSeconds(): number | null {
  if (typeof document === "undefined") return null;
  if (document.visibilityState === "visible") {
    return Math.floor(Date.now() / 1000);
  }
  return null;
}

export function useVisibleSinceSeconds(): number | null {
  const [visibleSince, setVisibleSince] = useState<number | null>(initialVisibleSinceSeconds);

  useEffect(() => {
    if (typeof document === "undefined") return;
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        setVisibleSince(Math.floor(Date.now() / 1000));
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  return visibleSince;
}
