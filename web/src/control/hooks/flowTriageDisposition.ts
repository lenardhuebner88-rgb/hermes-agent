import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import { DispositionListResponseSchema, FunnelDraftListResponseSchema, parseOrThrow } from "../lib/schemas";
import type { DispositionListResponse, FunnelDraftListResponse } from "../lib/schemas";
import { extractDetail, usePolling } from "./internal";

export interface FlowTriageFailuresResponse {
  failures: { run_id: number; task_id: string }[];
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
  return usePolling<FunnelDraftListResponse>(
    "kanban/flow-funnel-drafts",
    async () => parseOrThrow(
      FunnelDraftListResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/funnel/drafts?days=30"),
      "funnel-drafts",
    ),
    10000,
  );
}


/**
 * Funnel-Draft-Aktionen (Freigabe-Gate). Jede Aktion meldet `true` bei
 * Server-Erfolg (inkl. abgeschlossenem Reload — kein optimistisches
 * Ausblenden) und `false` bei Fehlschlag; der lesbare Grund steht dann in
 * `error` (Detail aus der Backend-Antwort, nicht der rohe fetchJSON-String).
 */
export function useFunnelDraftActions(reload: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await reload();
      return true;
    } catch (e) {
      if (aliveRef.current) setError(extractDetail(e));
      return false;
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [reload]);

  const approveDraft = useCallback(
    (id: string) => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(id)}/approve`,
      { method: "POST" },
    )),
    [run],
  );

  const reviseDraft = useCallback(
    (id: string, draftText: string, operatorNote = "") => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(id)}/revise`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ draft_text: draftText, operator_note: operatorNote }),
      },
    )),
    [run],
  );

  const dismissDraft = useCallback(
    (id: string) => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/funnel/drafts/${encodeURIComponent(id)}/dismiss`,
      { method: "POST" },
    )),
    [run],
  );

  return { busy, error, approveDraft, reviseDraft, dismissDraft };
}


// ── Disposition-Items (FRD Phase 3b) ────────────────────────────────────────
export function useDispositionItems() {
  return usePolling<DispositionListResponse>(
    "kanban/disposition-items",
    async () => parseOrThrow(DispositionListResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/disposition-items?status=open"), "disposition-items"),
    30000,
  );
}


/**
 * Disposition-Aktionen. Gleicher Vertrag wie useFunnelDraftActions: Rückgabe
 * `true` erst nach Server-Erfolg + Reload, `false` + lesbarer `error` sonst.
 */
export function useDispositionActions(reload: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await reload();
      return true;
    } catch (e) {
      if (aliveRef.current) setError(extractDetail(e));
      return false;
    } finally {
      if (aliveRef.current) setBusy(false);
    }
  }, [reload]);

  const acceptDisposition = useCallback(
    (id: string) => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/accept`,
      { method: "POST" },
    )),
    [run],
  );

  const dismissDisposition = useCallback(
    (id: string, reason: string) => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/dismiss`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) },
    )),
    [run],
  );

  const createFixTaskFromDisposition = useCallback(
    (id: string) => run(() => fetchJSON<unknown>(
      `/api/plugins/kanban/disposition-items/${encodeURIComponent(id)}/create-fix-task`,
      { method: "POST" },
    )),
    [run],
  );

  return { busy, error, acceptDisposition, dismissDisposition, createFixTaskFromDisposition };
}
