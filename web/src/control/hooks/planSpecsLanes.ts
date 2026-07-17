import { useCallback } from "react";
import { fetchJSON } from "@/lib/api";
import { PlanSpecsResponseSchema, PlanSpecDetailResponseSchema, LanesCatalogResponseSchema, parseOrThrow } from "../lib/schemas";
import type { PlanSpecsResponse, PlanSpecDetailResponse, LanesCatalogResponse } from "../lib/schemas";
import { usePolling } from "./internal";

export interface PlanSpecQueryOptions {
  limit?: number;
  scope?: "open" | "all";
  valid?: boolean | null;
  search?: string;
}


function planSpecsUrl(options: PlanSpecQueryOptions = {}, board: string | null = null) {
  const params = new URLSearchParams({ scope: options.scope ?? "open" });
  if (options.limit && options.limit > 0) params.set("limit", String(options.limit));
  if (options.valid != null) params.set("valid", String(options.valid));
  const query = options.search?.trim();
  if (query) params.set("q", query);
  if (board) params.set("board", board);
  return `/api/plugins/kanban/planspecs?${params.toString()}`;
}


export function usePlanSpecs(options: PlanSpecQueryOptions = {}, board: string | null = null) {
  const key = `kanban/planspecs:${board ?? "current"}:${options.scope ?? "open"}:${options.limit ?? "all"}:${options.valid ?? "any"}:${options.search?.trim() ?? ""}`;
  return usePolling<PlanSpecsResponse>(
    key,
    async () => parseOrThrow(PlanSpecsResponseSchema, await fetchJSON<unknown>(planSpecsUrl(options, board)), board ? `kanban/planspecs:${board}` : "kanban/planspecs"),
    15000,
  );
}


// Lane-Preset-Katalog: Modell-Optionen und Profile-Defaults für das Plan-Cockpit.
// 60s — Lane-Konfiguration ändert sich selten.
export function useLanesCatalog() {
  return usePolling<LanesCatalogResponse>(
    "kanban/lanes",
    async () => parseOrThrow(LanesCatalogResponseSchema, await fetchJSON<unknown>("/api/plugins/kanban/lanes"), "kanban/lanes"),
    60000,
  );
}


// On-demand PlanSpec detail. The path is part of the polling key so a switch
// can never flash another PlanSpec's retained payload. For the same path we do
// retain the last good detail across transient failures and disclose staleness.
export function usePlanSpecDetail(path: string | null) {
  const loader = useCallback(async (): Promise<PlanSpecDetailResponse | null> => {
    if (!path) return null;
    return parseOrThrow(
      PlanSpecDetailResponseSchema,
      await fetchJSON<unknown>(`/api/plugins/kanban/planspecs/detail?path=${encodeURIComponent(path)}`),
      "planspecs/detail",
    );
  }, [path]);
  const detail = usePolling<PlanSpecDetailResponse | null>(
    `planspec/detail:${path ?? "__idle__"}`,
    loader,
    path ? 60_000 : 600_000,
  );

  if (path) return detail;
  return {
    ...detail,
    data: null,
    error: null,
    errorObj: null,
    loading: false,
    lastUpdated: null,
    isStale: false,
  };
}
