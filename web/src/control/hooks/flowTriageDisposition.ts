import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { DispositionListResponseSchema, parseOrThrow } from "../lib/schemas";
import type { DispositionListResponse } from "../lib/schemas";
import { usePolling } from "./internal";

export interface FlowTriageFailuresResponse {
  failures: { run_id: number; task_id: string }[];
}


export interface FlowFunnelDraftsResponse {
  drafts: { id: string }[];
}


export function useFlowTriageFailures() {
  return usePolling<FlowTriageFailuresResponse>(
    "kanban/flow-triage-failures",
    async () => {
      const raw = await fetchJSON<FlowTriageFailuresResponse>(
        "/api/plugins/kanban/runs/failures?hours=48&limit=20",
      );
      return raw;
    },
    10000,
  );
}


export function useFunnelDrafts() {
  return usePolling<FlowFunnelDraftsResponse>(
    "kanban/flow-funnel-drafts",
    async () => {
      const raw = await fetchJSON<FlowFunnelDraftsResponse>(
        "/api/plugins/kanban/funnel/drafts?days=30",
      );
      return raw;
    },
    10000,
  );
}


// ── Disposition-Items (FRD Phase 3b) ────────────────────────────────────────
export function useDispositionItems() {
  return usePolling<DispositionListResponse>(
    "kanban/disposition-items",
    async () => parseOrThrow(DispositionListResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/disposition-items?status=open"), "disposition-items"),
    30000,
  );
}


export function useDispositionActions(reload: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const acceptDisposition = useCallback(async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      await fetchJSON<unknown>(`/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/accept`, { method: "POST" });
      await reload();
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [reload]);

  const dismissDisposition = useCallback(async (id: string, reason: string) => {
    setBusy(true);
    setError(null);
    try {
      await fetchJSON<unknown>(
        `/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/dismiss`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) },
      );
      await reload();
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [reload]);

  const createFixTaskFromDisposition = useCallback(async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      await fetchJSON<unknown>(`/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/create-fix-task`, { method: "POST" });
      await reload();
    } catch (e) {
      if (aliveRef.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [reload]);

  return { busy, error, acceptDisposition, dismissDisposition, createFixTaskFromDisposition };
}
