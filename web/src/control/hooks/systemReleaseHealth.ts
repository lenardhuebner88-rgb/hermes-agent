import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  DictateStatusResponseSchema,
  SystemHealthResponseSchema,
  VaultProvenanceResponseSchema,
  MetricsLiteResponseSchema,
  PressureStatusResponseSchema,
  ReleaseStatusResponseSchema,
  ReleaseModeResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { DictateStatusResponse, ReleaseStatusResponse, ReleaseModeResponse } from "../lib/schemas";
import type { MetricsLiteResponse, PressureStatusResponse, SystemHealthResponse, VaultProvenanceResponse } from "../lib/types";
import { usePolling, extractDetail } from "./internal";

/** Health chrome poll cadence. Keep in sync with OfflineStaleBanner freshness. */
export const HEALTH_POLL_INTERVAL_MS = 15_000;

export function useDictateStatus() {
  return usePolling<DictateStatusResponse>(
    "dictate-status",
    async () => parseOrThrow(
      DictateStatusResponseSchema,
      await fetchJSON<unknown>("/api/dictate/status"),
      "dictate-status",
    ),
    15_000,
  );
}


export function useSystemHealth() {
  return usePolling<SystemHealthResponse>(
    "health-status",
    async () => parseOrThrow(SystemHealthResponseSchema, await fetchJSON<unknown>("/api/health-status"), "health-status"),
    // chrome-badge cadence, 15s staleness accepted (perf plan 2026-07-17)
    HEALTH_POLL_INTERVAL_MS,
  );
}


export function useVaultProvenance() {
  return usePolling<VaultProvenanceResponse>(
    "vault-provenance",
    async () => parseOrThrow(VaultProvenanceResponseSchema, await fetchJSON<unknown>("/api/vault/provenance"), "vault-provenance"),
    20000,
  );
}


// In-process self-metrics (per route-group latency/error rates). 5s like health.
export function useMetricsLite() {
  return usePolling<MetricsLiteResponse>(
    "metrics-lite",
    async () => parseOrThrow(MetricsLiteResponseSchema, await fetchJSON<unknown>("/api/metrics-lite"), "metrics-lite"),
    5000,
  );
}


export function usePressureStatus() {
  return usePolling<PressureStatusResponse>(
    "pressure-status",
    async () => parseOrThrow(PressureStatusResponseSchema, await fetchJSON<unknown>("/api/pressure-status"), "pressure-status"),
    5000,
  );
}


// Auto-release timeline (recent/anchors) — feeds the Risiko-Tab Aktivität rail.
// (The Hero cockpit itself reads autonomous/max_tier_autonomous/red_streak/
// max_in_progress from the WRITE-backed useReleaseMode() below instead.) Same
// GET /api/plugins/kanban/release-status AutoReleaseTile polls inline; kept as
// its own hook (not a shared subscription with AutoReleaseTile) since the two
// live in mutually-exclusive Fleet subtabs (Plan vs. Risiko) — no double-poll.
export function useReleaseStatus() {
  return usePolling<ReleaseStatusResponse>(
    "release-status",
    async () => parseOrThrow(
      ReleaseStatusResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/release-status"),
      "release-status",
    ),
    15000,
  );
}


// Read-side of the Risiko-Tab Hero cockpit: autonomous, max_tier_autonomous,
// pause_on_red_streak, red_streak (current streak, the "x" in "Streak x/N")
// and max_in_progress (kanban.max_in_progress). The two POST twins below
// write it back; callers reload() this after a successful write so the UI
// reflects the persisted config rather than staying purely optimistic.
export function useReleaseMode() {
  return usePolling<ReleaseModeResponse>(
    "release-mode",
    async () => parseOrThrow(
      ReleaseModeResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/release-mode"),
      "release-mode",
    ),
    15000,
  );
}


// POST /release-mode — flips release.autonomous and/or max_tier_autonomous.
// Both fields optional so a caller can send just the one knob it changed.
export function useReleaseModeWrite() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (next: { autonomous?: boolean; max_tier_autonomous?: string }) => {
    setBusy(true);
    if (aliveRef.current) setError(null);
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
        "/api/plugins/kanban/release-mode",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) },
      );
      if (res?.ok === false) {
        const detail = res.detail || "Änderung fehlgeschlagen.";
        if (aliveRef.current) setError(detail);
        return { ok: false as const, detail };
      }
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, []);
  return { busy, error, run };
}


// POST /release-concurrency — writes any of kanban.max_in_progress,
// kanban.max_in_progress_per_profile, kanban.max_concurrent_per_repo. Fields
// are optional; only the ones present in the call are sent — the Risiko-Tab's
// coupled "Parallele Worker pro Profil" stepper calls
// run({ max_in_progress_per_profile: N, max_concurrent_per_repo: N }) in one
// request, while the "Max. Worker gesamt" stepper calls
// run({ max_in_progress: N }) alone.
export function useReleaseConcurrencyWrite() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const run = useCallback(async (next: {
    max_in_progress?: number;
    max_in_progress_per_profile?: number;
    max_concurrent_per_repo?: number;
  }) => {
    setBusy(true);
    if (aliveRef.current) setError(null);
    try {
      const res = await fetchJSON<{ ok?: boolean; detail?: string }>(
        "/api/plugins/kanban/release-concurrency",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(next) },
      );
      if (res?.ok === false) {
        const detail = res.detail || "Änderung fehlgeschlagen.";
        if (aliveRef.current) setError(detail);
        return { ok: false as const, detail };
      }
      return { ok: true as const };
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current) setError(detail);
      return { ok: false as const, detail };
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, []);
  return { busy, error, run };
}

