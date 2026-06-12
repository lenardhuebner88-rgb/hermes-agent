import { useSyncExternalStore } from "react";

// Sekunden-Uhr als externer Store: `Date.now()` direkt im Render verletzt die
// react-hooks-Purity-Regel (Komponenten müssen idempotent rendern). Der Store
// tickt alle 10s und weckt nur Komponenten, die ihn abonniert haben — "vor X"-
// Labels bleiben damit auch auf einem idle Board lebendig, ohne dass jede
// Komponente ihren eigenen Interval-Tick verwaltet.
const CLOCK_TICK_MS = 10_000;
let clockNowSeconds = Math.floor(Date.now() / 1000);
const clockListeners = new Set<() => void>();
let clockTimer: number | null = null;

export function subscribeClock(listener: () => void): () => void {
  clockListeners.add(listener);
  if (clockTimer == null) {
    clockNowSeconds = Math.floor(Date.now() / 1000);
    clockTimer = window.setInterval(() => {
      clockNowSeconds = Math.floor(Date.now() / 1000);
      for (const l of clockListeners) l();
    }, CLOCK_TICK_MS);
  }
  return () => {
    clockListeners.delete(listener);
    if (clockListeners.size === 0 && clockTimer != null) {
      window.clearInterval(clockTimer);
      clockTimer = null;
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
