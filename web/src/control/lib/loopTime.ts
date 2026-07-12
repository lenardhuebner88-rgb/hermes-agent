import { useEffect, useState } from "react";

/** Render an API ISO instant in the browser/operator timezone. The optional timezone
 * exists for deterministic DST tests; production intentionally uses the browser default. */
export function formatLoopTimestamp(iso: string, timeZone?: string): string {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return "—";
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
