import type { LaneAuthSmokeResult } from "./api";

// Pure auth-smoke presentation helpers, kept out of LanesView.tsx so that file
// stays a component-only module (react-refresh/only-export-components). The
// button labels live here too — they are part of these helpers' contract and
// are unit-tested directly. No other LanesView code references them.
const authSmokeLabels = {
  authCheck: "Auth prüfen",
  authCheckRunning: "Auth prüft...",
  authCheckSavedLane: "Gespeicherte Lane prüfen",
};

export function laneAuthSmokeTone(status: LaneAuthSmokeResult["status"]): "emerald" | "amber" | "red" | "zinc" {
  if (status === "ok") return "emerald";
  if (["auth_error", "quota_or_rate_limit", "timeout", "config_error", "error"].includes(status)) return "red";
  if (status === "skipped") return "zinc";
  return "amber";
}

export function authSmokeButtonLabel(dirty: boolean, running: boolean): string {
  if (running) return authSmokeLabels.authCheckRunning;
  if (dirty) return authSmokeLabels.authCheckSavedLane;
  return authSmokeLabels.authCheck;
}

export function authSmokeDisabled(input: {
  busy: boolean;
  running: boolean;
  hasLaneId: boolean;
  dirty: boolean;
}): boolean {
  return input.busy || input.running || !input.hasLaneId || input.dirty;
}

export function authSmokeRenderableResults(
  results: LaneAuthSmokeResult[],
  state: { running: boolean; error: string | null },
): LaneAuthSmokeResult[] {
  if (state.running || state.error) return [];
  return results;
}
