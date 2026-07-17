import { fetchJSON } from "@/lib/api";
import {
  StrategistCountSchema,
  StrategistLastRunsSchema,
  StrategistOutcomesResponseSchema,
  type StrategistOutcomesResponse,
  parseOrThrow,
} from "../lib/schemas";
import type { StrategistLastRuns } from "../lib/schemas";
import { usePolling } from "./internal";

export function useStrategistCount() {
  return usePolling(
    "strategist/count",
    async () => parseOrThrow(StrategistCountSchema, await fetchJSON<unknown>("/api/plugins/kanban/strategist/proposals"), "strategist-count"),
    5000,
  );
}


export function useStrategistLastRuns() {
  return usePolling<StrategistLastRuns>(
    "strategist/last-runs",
    async () => parseOrThrow(StrategistLastRunsSchema, await fetchJSON<unknown>("/api/plugins/kanban/strategist/last-runs"), "strategist-last-runs"),
    15000,
  );
}


// Ziel-4: Wirkungs-Historie geshippter Lever (lever-outcomes.json read-through).
// 30s-Poll — nichts, was schneller als der propose-/reflect-Takt ändert.
export function useStrategistOutcomes() {
  return usePolling<StrategistOutcomesResponse>(
    "strategist/outcomes",
    async () => parseOrThrow(StrategistOutcomesResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/strategist/outcomes"), "strategist-outcomes"),
    30000,
  );
}

// ── Flow attention counts ────────────────────────────────────────────────────
// These two hooks feed the FlowView hero attention band with real counts.
// They are lightweight (count only, ~10s interval) and read-only; the
// The detailed triage surface continues to poll the same endpoint for its own
// rendering. Funnel drafts remain count-only after the parked approval surface
// was removed.
