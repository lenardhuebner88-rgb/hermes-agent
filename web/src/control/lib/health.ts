import type { HealthStatus } from "./types";

/**
 * Shared Gateway/Dashboard health → LED-class + label mapping. Extracted from
 * ControlShell.tsx (S1-style dedupe) so PulsLeiste can render the same
 * GATEWAY instrument as the Rail's GatewayLed / the old Masthead's StatusDots
 * without a circular import between the two components.
 */
export function healthLed(status: HealthStatus | "unknown", stale: boolean): string {
  if (stale) return "hc-led-warn";
  if (status === "healthy") return "hc-led-live";
  if (status === "degraded") return "hc-led-warn";
  if (status === "offline") return "hc-led-error";
  return "hc-led-idle";
}

export function healthLabel(status: HealthStatus | "unknown", stale: boolean): string {
  if (stale) return "stale";
  if (status === "healthy") return "gesund";
  if (status === "degraded") return "degraded";
  if (status === "offline") return "offline";
  return "unbekannt";
}
