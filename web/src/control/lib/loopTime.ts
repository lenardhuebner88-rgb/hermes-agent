import { useEffect, useState } from "react";

const ISO_INSTANT_WITH_ZONE = /^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/;

/** Parse only an actual wire instant. Timezone-less local timestamps and non-string
 * values are ambiguous at the browser boundary and must never become a plausible age. */
export function parseLoopTimestamp(value: unknown): number | null {
  if (typeof value !== "string" || !ISO_INSTANT_WITH_ZONE.test(value)) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/** Elapsed whole seconds, or null when the wire value is invalid/ambiguous/future.
 * Five seconds of positive clock skew is tolerated across server and browser. */
export function loopElapsedSeconds(value: unknown, nowMs: number): number | null {
  const startedMs = parseLoopTimestamp(value);
  if (startedMs === null || startedMs > nowMs + 5_000) return null;
  return Math.max(0, Math.floor((nowMs - startedMs) / 1_000));
}

/** Render an API ISO instant in the browser/operator timezone. The optional timezone
 * exists for deterministic DST tests; production intentionally uses the browser default. */
export function formatLoopTimestamp(iso: string, timeZone?: string): string {
  const ms = parseLoopTimestamp(iso);
  if (ms === null) return "—";
  return new Intl.DateTimeFormat("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
    ...(timeZone ? { timeZone } : {}),
  }).format(new Date(ms));
}

/** Live clock for elapsed labels. It deliberately does not depend on the 5s API poll:
 * a slow or failed poll must not freeze a phase counter that still looks live. */
export function useLoopNowMs(intervalMs = 1_000): number {
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return nowMs;
}
