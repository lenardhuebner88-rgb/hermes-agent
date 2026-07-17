import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  OperatorInventoryResponseSchema,
  BacklogResponseSchema,
  OrchestrationBacklogResponseSchema,
  OrchestrationDetailSchema,
  BacklogDetailSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { BacklogDetail, BacklogResponse, OrchestrationDetail, OrchestrationBacklogResponse } from "../lib/schemas";
import type { OperatorInventoryResponse } from "../lib/types";
import { usePolling } from "./internal";

export function useOperatorInventory() {
  return usePolling<OperatorInventoryResponse>(
    "operator-inventory",
    async () => parseOrThrow(OperatorInventoryResponseSchema, await fetchJSON<unknown>("/api/operator-inventory"), "operator-inventory"),
    30000,
  );
}


// Read-only family-organizer backlog board. Polled slowly — the backlog changes
// rarely (a handful of git commits a day), so 30s keeps it fresh without churn.
export function useBacklog() {
  return usePolling<BacklogResponse>(
    "family-organizer/backlog",
    async () => parseOrThrow(BacklogResponseSchema, await fetchJSON<unknown>("/api/family-organizer/backlog"), "family-organizer/backlog"),
    30000,
  );
}


// Read-only Orchestrator backlog board (~/orchestration/backlog working tree).
// Polled slowly — planning scratch changes a handful of times a day; 30s keeps it
// fresh without churn.
export function useOrchestrationBacklog() {
  return usePolling<OrchestrationBacklogResponse>(
    "orchestration/backlog",
    async () => parseOrThrow(OrchestrationBacklogResponseSchema, await fetchJSON<unknown>("/api/orchestration/backlog"), "orchestration/backlog"),
    30000,
  );
}


export function useOrchestrationBacklogDetail() {
  const [detailById, setDetailById] = useState<Record<string, OrchestrationDetail>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const fetch = useCallback(async (id: string) => {
    setLoadingId(id);
    try {
      const raw = await fetchJSON<unknown>(`/api/orchestration/backlog/${encodeURIComponent(id)}`);
      const data = parseOrThrow(OrchestrationDetailSchema, raw, "orchestration/backlog/detail");
      if (!aliveRef.current) return;
      setDetailById((prev) => ({ ...prev, [id]: data }));
      setErrorById((prev) => ({ ...prev, [id]: "" }));
    } catch (err) {
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { detailById, errorById, loadingId, fetch };
}


export function useBacklogDetail() {
  const [detailById, setDetailById] = useState<Record<string, BacklogDetail>>({});
  const [errorById, setErrorById] = useState<Record<string, string>>({});
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const aliveRef = useRef(true);
  useEffect(() => () => { aliveRef.current = false; }, []);
  const fetch = useCallback(async (id: string) => {
    setLoadingId(id);
    try {
      const raw = await fetchJSON<unknown>(`/api/family-organizer/backlog/${encodeURIComponent(id)}`);
      const data = parseOrThrow(BacklogDetailSchema, raw, "family-organizer/backlog/detail");
      if (!aliveRef.current) return;
      setDetailById((prev) => ({ ...prev, [id]: data }));
      setErrorById((prev) => ({ ...prev, [id]: "" }));
    } catch (err) {
      if (aliveRef.current) setErrorById((prev) => ({ ...prev, [id]: err instanceof Error ? err.message : String(err) }));
    } finally {
      if (aliveRef.current) setLoadingId(null);
    }
  }, []);
  return { detailById, errorById, loadingId, fetch };
}

