import type { AutoresearchStatus, ToneName } from "./types";

export function clampLoopIterations(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.min(5, Math.round(value)));
}

export function describeLoopStatus(status: AutoresearchStatus | null) {
  const running = status?.state === "running" || status?.state === "stopping";
  const iteration = status?.iteration ?? 0;
  const max = status?.max ?? 0;
  const progressPercent = running && max > 0 ? Math.max(0, Math.min(100, (iteration / max) * 100)) : 0;
  const routeStatus = status?.route_status || "unbekannt";
  const routeOk = routeStatus === "configured";

  return {
    running,
    iterationLabel: running && max > 0 ? `${iteration} / ${max}` : "kein Lauf aktiv",
    progressPercent,
    stepLabel: status?.last_step || "-",
    evalLabel: status?.last_eval || "-",
    heartbeatLabel: status?.heartbeat_age_s == null ? "-" : `${status.heartbeat_age_s}s ${status.heartbeat_fresh ? "frisch" : "stale"}`,
    routeTone: (routeOk ? "emerald" : "amber") as ToneName,
    routeHint: routeOk ? null : "Modell-Route nicht bestätigt",
  };
}
