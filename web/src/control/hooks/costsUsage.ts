import { useCallback, useEffect, useRef, useState } from "react";
import { fetchJSON } from "@/lib/api";
import {
  AccountUsageResponseSchema,
  RunsCostsResponseSchema,
  RunsCostsSeriesResponseSchema,
  SubscriptionTokenBurnResponseSchema,
  ChainCostsResponseSchema,
  parseOrThrow,
} from "../lib/schemas";
import type { RunsCostsResponse, RunsCostsSeriesResponse, SubscriptionTokenBurnResponse, ChainCostsResponse } from "../lib/schemas";
import type { AccountUsageResponse } from "../lib/types";
import { nowSec } from "../lib/derive";
import { withBoardParam } from "../lib/multiBoard";
import { usePolling, extractDetail } from "./internal";

export function useAccountUsage() {
  return usePolling<AccountUsageResponse>(
    "account-usage",
    async () => parseOrThrow(AccountUsageResponseSchema, await fetchJSON<unknown>("/api/account-usage"), "account-usage"),
    60000,
  );
}


// F4 (Statistik): Kosten heute/7 Tage + Top-Profile — gleiche langsame
// Poll-Kadenz wie die übrigen Aggregate.
export function useHermesRunsCosts() {
  return usePolling<RunsCostsResponse>(
    "runs/costs",
    async () => parseOrThrow(
      RunsCostsResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/costs?days=7"),
      "runs/costs",
    ),
    60000,
  );
}


export function useHermesRunsCostSeries() {
  return usePolling<RunsCostsSeriesResponse>(
    "runs/costs-series",
    async () => parseOrThrow(
      RunsCostsSeriesResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/costs-series?days=7"),
      "runs/costs-series",
    ),
    60000,
  );
}


export function useHermesSubscriptionBurn() {
  return usePolling<SubscriptionTokenBurnResponse>(
    "runs/subscription-burn",
    async () => parseOrThrow(
      SubscriptionTokenBurnResponseSchema,
      await fetchJSON<unknown>("/api/plugins/kanban/runs/subscription-burn?days=7"),
      "runs/subscription-burn",
    ),
    60000,
  );
}


// Kosten-Rollup pro Kette: GET /tasks/{id}/chain-costs.
// Nullable taskId → kein Fetch (kein Selektor aktiv). Interval 30 s — Aggregate
// ändern sich seltener als Heartbeats, häufiger als Tages-Statistiken.
export function useHermesChainCosts(taskId: string | null, board: string | null = null) {
  const [data, setData] = useState<ChainCostsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const aliveRef = useRef(true);
  const inFlightRef = useRef(false);
  const paramsRef = useRef({ taskId, board });
  useEffect(() => () => { aliveRef.current = false; }, []);
  useEffect(() => { paramsRef.current = { taskId, board }; }, [taskId, board]);
  const reload = useCallback(async (): Promise<ChainCostsResponse | null> => {
    if (!taskId) {
      if (aliveRef.current) setData(null);
      return null;
    }
    const startParams = { taskId, board };
    inFlightRef.current = true;
    if (aliveRef.current) setLoading(true);
    try {
      const parsed = parseOrThrow(
        ChainCostsResponseSchema,
        await fetchJSON<unknown>(withBoardParam(`/api/plugins/kanban/tasks/${encodeURIComponent(taskId)}/chain-costs`, board)),
        "chain-costs",
      );
      // Board-Switch-Race: Ergebnis verwerfen, wenn sich die Params geändert haben.
      if (aliveRef.current && paramsRef.current.taskId === startParams.taskId && paramsRef.current.board === startParams.board) {
        setData(parsed);
        setError("");
        setLastUpdated(nowSec());
      }
      return parsed;
    } catch (e) {
      const detail = extractDetail(e);
      if (aliveRef.current && paramsRef.current.taskId === startParams.taskId && paramsRef.current.board === startParams.board) setError(detail);
      return null;
    } finally {
      inFlightRef.current = false;
      if (aliveRef.current && paramsRef.current.taskId === startParams.taskId && paramsRef.current.board === startParams.board) setLoading(false);
    }
  }, [taskId, board]);
  useEffect(() => {
    const initial = window.setTimeout(() => { void reload(); }, 0);
    if (!taskId) return () => window.clearTimeout(initial);
    const interval = window.setInterval(() => {
      if (!document.hidden && !inFlightRef.current) void reload();
    }, 30000);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(interval);
    };
  }, [taskId, reload]);
  return { data, loading, error, lastUpdated, isStale: Boolean(error && data), reload };
}

